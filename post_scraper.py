#!/usr/bin/env python3
"""Extractor de posts para phpBB con manejo de tiempos muertos y carga 'eager'.

Ejemplo:
    python post_scraper.py --max-topics 30 --min-delay 6 --max-delay 12 --simulate-human --headed
"""

import argparse
import json
import logging
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import undetected_chromedriver as uc

from data_paths import POSTS_FILE, TOPICS_FILE, require_parent_dir
from page_status import (
    BlockedError,
    PageError,
    check_status,
    document_status,
    enable_status_logging,
)

log = logging.getLogger("post_scraper")

CHALLENGE_TITLES = ("just a moment", "un momento", "checking your browser")
CHALLENGE_SELECTOR = "#challenge-form, #challenge-running, #cf-challenge-running"
BLOCK_SELECTOR = "#cf-error-details, .cf-error-details"
USERNAME_SELECTOR = ".username, .username-coloured"


def canonicalize(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, True) if k != "sid"]
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def find_browser_binary():
    candidates = [
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--topics-file",
        type=Path,
        default=TOPICS_FILE,
        help="archivo .jsonl con el índice de temas",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=POSTS_FILE,
        help="archivo de destino para los posts",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path("chrome_profile"),
        help="carpeta del perfil de Chrome",
    )
    parser.add_argument(
        "--max-topics",
        type=int,
        default=None,
        help="cantidad máxima de temas a procesar en esta ejecución",
    )
    parser.add_argument("--min-delay", type=float, default=5.0, help="pausa mínima (s)")
    parser.add_argument("--max-delay", type=float, default=10.0, help="pausa máxima (s)")
    parser.add_argument(
        "--challenge-timeout",
        type=int,
        default=300,
        help="segundos para resolver un desafío manual",
    )
    parser.add_argument("--page-timeout", type=int, default=45)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="abre la ventana visible de Chrome",
    )
    parser.add_argument(
        "--simulate-human",
        action="store_true",
        help="aplica scroll suave y pausas aleatorias de lectura",
    )
    args = parser.parse_args()
    require_parent_dir(parser, args.topics_file, args.output)
    return args


def build_driver(profile_dir, page_timeout, headless=True):
    browser_path = find_browser_binary()
    if not browser_path:
        raise FileNotFoundError("No se encontró el ejecutable de Google Chrome.")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument("--window-size=1440,900")
    options.add_argument("--lang=en-US,en")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # 'eager' no espera a que carguen imágenes rotas o scripts externos
    options.page_load_strategy = "eager"
    enable_status_logging(options)

    driver = uc.Chrome(
        options=options,
        browser_executable_path=browser_path,
        headless=headless,
    )
    driver.set_page_load_timeout(page_timeout)
    return driver


def is_challenge(driver):
    title = driver.title.strip().lower()
    if any(text in title for text in CHALLENGE_TITLES):
        return True
    elements = driver.find_elements(By.CSS_SELECTOR, CHALLENGE_SELECTOR)
    return any(el.is_displayed() for el in elements)


TEMPORARY_ORIGIN_ERRORS = ("520:", "521:", "522:", "523:", "524:", "502:", "504:")


def is_origin_server_error(driver):
    """Comprueba si es un fallo temporal del servidor web (no un bloqueo de bot)."""
    title = driver.title.strip().lower()
    return any(err in title for err in TEMPORARY_ORIGIN_ERRORS)


def is_blocked(driver):
    """Detecta bloqueos reales de Cloudflare (WAF, ban de IP, 1020), ignorando caídas 52x."""
    if is_origin_server_error(driver):
        return False
    elements = driver.find_elements(By.CSS_SELECTOR, BLOCK_SELECTOR)
    return any(el.is_displayed() for el in elements)


def simulate_human_behavior(driver):
    try:
        total_height = driver.execute_script("return document.body.scrollHeight") or 1000
    except WebDriverException:
        return

    current_pos = 0
    while current_pos < total_height:
        step = random.randint(300, 650)
        current_pos += step
        try:
            driver.execute_script(f"window.scrollTo({{top: {current_pos}, behavior: 'smooth'}});")
        except WebDriverException:
            break
        time.sleep(random.uniform(0.3, 0.6))

        if random.random() < 0.20:
            time.sleep(random.uniform(0.8, 1.8))

        try:
            total_height = driver.execute_script("return document.body.scrollHeight") or total_height
        except WebDriverException:
            break


def load_page(driver, url, challenge_timeout, simulate_human=False, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
        except TimeoutException:
            log.warning("Tiempo agotado al cargar %s. Deteniendo peticiones pendientes...", url)
            try:
                driver.execute_script("window.stop();")
            except WebDriverException:
                pass

        if is_challenge(driver):
            log.warning("Desafío en %s: resuélvelo en la ventana abierta.", url)
            deadline = time.monotonic() + challenge_timeout
            while is_challenge(driver):
                if time.monotonic() > deadline:
                    raise BlockedError(f"Desafío no resuelto tras {challenge_timeout}s en {url}")
                time.sleep(2)
            log.info("Desafío superado.")
            time.sleep(2.0)

        # Si el servidor dio 522 / 524, esperar y reintentar recargar la página
        if is_origin_server_error(driver):
            if attempt < max_retries:
                wait_time = attempt * 20
                log.warning(
                    "Servidor saturado (%s) en intento %d/%d. Reintentando en %ds...",
                    driver.title.strip(),
                    attempt,
                    max_retries,
                    wait_time,
                )
                time.sleep(wait_time)
                continue
            else:
                raise WebDriverException(f"Servidor inaccesible tras {max_retries} intentos: {driver.title}")

        if is_blocked(driver):
            raise BlockedError(f"Acceso denegado en {url}: {driver.title}")

        check_status(document_status(driver), url)

        # Si llegó aquí sin errores de servidor ni bloqueos, salir del bucle de reintentos
        break

    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
    except TimeoutException:
        pass

    if simulate_human:
        simulate_human_behavior(driver)


def parse_topic_posts(html, topic_info):
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for post_div in soup.select("div.post"):
        raw_id = post_div.get("id", "")
        post_id = re.sub(r"\D", "", raw_id) if raw_id else None

        author_el = post_div.select_one(
            f".postprofile {USERNAME_SELECTOR}, .postbody .author {USERNAME_SELECTOR}"
        )
        author = author_el.get_text(" ", strip=True) if author_el else None

        time_el = post_div.select_one(".postbody .author time")
        posted_at = (
            time_el.get("datetime")
            if time_el and time_el.get("datetime")
            else (time_el.get_text(" ", strip=True) if time_el else None)
        )

        subject_el = post_div.select_one(".postbody h3 a")
        subject = subject_el.get_text(" ", strip=True) if subject_el else None

        content_el = post_div.select_one(".postbody .content")
        content_text = content_el.get_text("\n", strip=True) if content_el else ""

        posts.append({
            "type": "post",
            "topic_id": topic_info["id"],
            "topic_title": topic_info["title"],
            "post_id": post_id,
            "subject": subject,
            "author": author,
            "created_at": posted_at,
            "content": content_text,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    next_link = soup.select_one('.pagination li.next a, .pagination a[rel="next"]')
    next_url = None
    if next_link and next_link.get("href"):
        candidate = canonicalize(urljoin(topic_info["url"], next_link["href"]))
        candidate_tid = parse_qs(urlsplit(candidate).query).get("t", [None])[0]
        if str(candidate_tid) == str(topic_info["id"]):
            next_url = candidate

    return posts, next_url


def load_unique_topics(topics_path: Path):
    if not topics_path.exists():
        raise FileNotFoundError(f"No existe el archivo de índice {topics_path}")

    topics = {}
    with topics_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                page_data = json.loads(line)
                for item in page_data.get("items", []):
                    tid = str(item.get("id"))
                    if tid and tid not in topics:
                        topics[tid] = item
            except json.JSONDecodeError:
                continue
    return list(topics.values())


def load_completed_topics(output_path: Path):
    completed = set()
    if not output_path.exists():
        return completed

    with output_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
                if record.get("type") == "topic_completed":
                    completed.add(str(record.get("topic_id")))
            except (json.JSONDecodeError, KeyError):
                continue
    return completed


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = parse_args()

    all_topics = load_unique_topics(args.topics_file)
    completed_topics = load_completed_topics(args.output)
    pending_topics = [t for t in all_topics if str(t["id"]) not in completed_topics]

    log.info(
        "Total de temas: %d | Completados: %d | Pendientes: %d",
        len(all_topics),
        len(completed_topics),
        len(pending_topics),
    )

    if not pending_topics:
        log.info("Todos los temas ya fueron extraídos.")
        return 0

    topics_to_process = (
        pending_topics[: args.max_topics] if args.max_topics else pending_topics
    )
    log.info("Procesando %d temas en esta tanda.", len(topics_to_process))

    driver = build_driver(args.profile_dir, args.page_timeout, headless=not args.headed)

    try:
        with args.output.open("a", encoding="utf-8") as out:
            for index, topic in enumerate(topics_to_process, 1):
                topic_id = str(topic["id"])
                current_url = topic["url"]
                topic_posts_count = 0
                page_num = 1

                log.info(
                    "[%d/%d] Iniciando tema %s: '%s'",
                    index,
                    len(topics_to_process),
                    topic_id,
                    topic["title"][:50],
                )

                try:
                    while current_url:
                        time.sleep(random.uniform(args.min_delay, args.max_delay))
                        load_page(
                            driver,
                            current_url,
                            args.challenge_timeout,
                            simulate_human=args.simulate_human,
                        )

                        posts, next_url = parse_topic_posts(driver.page_source, topic)
                        if not posts:
                            raise PageError(f"sin mensajes en {current_url}")
                        for post in posts:
                            out.write(json.dumps(post, ensure_ascii=False) + "\n")
                        out.flush()

                        topic_posts_count += len(posts)
                        current_url = next_url
                        if current_url:
                            page_num += 1

                    completion_record = {
                        "type": "topic_completed",
                        "topic_id": topic_id,
                        "pages_scraped": page_num,
                        "total_posts": topic_posts_count,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    out.write(json.dumps(completion_record, ensure_ascii=False) + "\n")
                    out.flush()

                    log.info(
                        "Tema %s finalizado (%d posts en %d pág).",
                        topic_id,
                        topic_posts_count,
                        page_num,
                    )
                except (PageError, TimeoutException, WebDriverException) as err:
                    log.error("Fallo temporal en el tema %s (%s). Se omitirá para reintentar más adelante.", topic_id, err)
                    continue

    except BlockedError as exc:
        log.error("Detenido por seguridad: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupción manual recibida. El progreso actual fue guardado.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())