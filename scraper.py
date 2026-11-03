#!/usr/bin/env python3
"""Scraper con undetected-chromedriver para foros phpBB detrás de Cloudflare."""

import argparse
import json
import logging
import random
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import shutil
import undetected_chromedriver as uc

log = logging.getLogger("scraper")

CHALLENGE_TITLES = ("just a moment", "un momento", "checking your browser")
CHALLENGE_SELECTOR = "#challenge-form, #challenge-running, #cf-challenge-running"
BLOCK_SELECTOR = "#cf-error-details, .cf-error-details"
PHPBB_TOPIC_SELECTOR = "a.topictitle"
USERNAME_SELECTOR = ".username, .username-coloured"
ROBOTS_DIRECTIVE = re.compile(
    r"^\s*(user-agent|allow|disallow|sitemap|crawl-delay)\s*:", re.IGNORECASE
)


class BlockedError(Exception):
    """El sitio ha denegado el acceso o no se puede continuar con seguridad."""


class PageError(Exception):
    """Ha fallado una página concreta, pero se puede seguir con las demás."""


def canonicalize(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, True) if k != "sid"]
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("urls", nargs="*", help="URLs que se van a visitar")
    parser.add_argument("--urls-file", type=Path, help="archivo con una URL por línea")
    parser.add_argument("--selector", help="selector CSS que se extrae (por defecto, h1)")
    parser.add_argument(
        "--phpbb",
        action="store_true",
        help="extrae los temas de un listado de foro phpBB",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="páginas que se recorren por cada foro con --phpbb",
    )
    parser.add_argument("--output", type=Path, default=Path("resultados.jsonl"))
    parser.add_argument("--profile-dir", type=Path, default=Path("chrome_profile"))
    parser.add_argument("--min-delay", type=float, default=5.0, help="pausa mínima (s)")
    parser.add_argument("--max-delay", type=float, default=12.0, help="pausa máxima (s)")
    parser.add_argument(
        "--challenge-timeout",
        type=int,
        default=300,
        help="segundos para resolver un desafío de forma manual",
    )
    parser.add_argument("--page-timeout", type=int, default=60)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="abre la ventana visible (necesario para resolver Cloudflare)",
    )
    parser.add_argument(
        "--selector-timeout",
        type=int,
        default=20,
        help="segundos de espera hasta que aparezca el selector",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=5,
        help="errores seguidos tras los que se detiene el proceso",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="vuelve a visitar las URLs que ya estén en el archivo de salida",
    )
    parser.add_argument(
        "--simulate-human",
        action="store_true",
        help="activa desplazamientos suaves y lecturas parciales en cada carga",
    )
    args = parser.parse_args()

    if args.urls_file:
        lines = args.urls_file.read_text(encoding="utf-8").splitlines()
        args.urls += [line.strip() for line in lines if line.strip()]
    if not args.urls:
        parser.error("indica al menos una URL o usa --urls-file")

    args.urls = list(dict.fromkeys(canonicalize(url) for url in args.urls))
    args.selector = PHPBB_TOPIC_SELECTOR if args.phpbb else (args.selector or "h1")
    return args

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

def build_driver(profile_dir, page_timeout, headless=False):
    browser_path = find_browser_binary()
    if not browser_path:
        raise FileNotFoundError("No se encontró Google Chrome en WSL.")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument("--window-size=1440,900")
    options.add_argument("--lang=en-US,en")
    
    # Flags necesarias para estabilidad en WSL2 / contenedores
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

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

    # Comprueba visibilidad real en lugar de solo existencia en el DOM
    elements = driver.find_elements(By.CSS_SELECTOR, CHALLENGE_SELECTOR)
    return any(el.is_displayed() for el in elements)


def is_blocked(driver):
    elements = driver.find_elements(By.CSS_SELECTOR, BLOCK_SELECTOR)
    return any(el.is_displayed() for el in elements)


def simulate_human_behavior(driver):
    try:
        total_height = driver.execute_script("return document.body.scrollHeight") or 1000
    except WebDriverException:
        return

    current_pos = 0
    while current_pos < total_height:
        step = random.randint(250, 600)
        current_pos += step
        driver.execute_script(f"window.scrollTo({{top: {current_pos}, behavior: 'smooth'}});")
        time.sleep(random.uniform(0.4, 0.9))

        if random.random() < 0.25:
            time.sleep(random.uniform(1.2, 2.5))

        total_height = driver.execute_script("return document.body.scrollHeight") or total_height

    if random.random() < 0.40:
        scroll_back = max(0, current_pos - random.randint(300, 700))
        driver.execute_script(f"window.scrollTo({{top: {scroll_back}, behavior: 'smooth'}});")
        time.sleep(random.uniform(0.6, 1.2))


def load_page(driver, url, challenge_timeout, simulate_human=False):
    driver.get(url)

    if is_challenge(driver):
        log.warning("Desafío en %s: resuélvelo en la ventana abierta.", url)
        deadline = time.monotonic() + challenge_timeout
        while is_challenge(driver):
            if time.monotonic() > deadline:
                raise BlockedError(f"desafío sin resolver tras {challenge_timeout}s en {url}")
            time.sleep(1.5)
        log.info("Desafío superado.")
        # Pausa breve para permitir que Cloudflare asiente las cookies de pase
        time.sleep(2.0)

    if is_blocked(driver):
        raise BlockedError(f"acceso denegado en {url}: {driver.title}")

    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    if simulate_human:
        simulate_human_behavior(driver)


class Pacer:
    def __init__(self, min_delay, max_delay):
        self.min_delay = min_delay
        self.max_delay = max(min_delay, max_delay)
        self.started = False

    def update_crawl_delay(self, delay):
        if delay > self.min_delay:
            log.info("Ajustando ritmo: robots.txt pide Crawl-delay de %.1fs", delay)
            self.min_delay = delay
            self.max_delay = max(self.max_delay, delay + 3.0)

    def wait(self):
        if self.started:
            time.sleep(random.uniform(self.min_delay, self.max_delay))
        self.started = True


def parse_robots(text):
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    if not any(ROBOTS_DIRECTIVE.match(line) for line in lines):
        return None, None

    rules = []
    agents = []
    crawl_delay = None
    in_rules = False

    for line in lines:
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()

        if field == "user-agent":
            if in_rules:
                agents = []
                in_rules = False
            agents.append(value)
        elif field in ("allow", "disallow"):
            in_rules = True
            if "*" in agents and value:
                rules.append((field == "allow", value))
        elif field == "crawl-delay":
            in_rules = True
            if "*" in agents and value:
                try:
                    crawl_delay = float(value)
                except ValueError:
                    pass

    return rules, crawl_delay


def robots_allows(rules, url):
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"

    best_length = -1
    allowed = True
    for is_allow, pattern in rules:
        regex = re.escape(pattern).replace(r"\*", ".*")
        if regex.endswith(r"\$"):
            regex = regex[:-2] + "$"
        if re.match(regex, path):
            if len(pattern) > best_length or (len(pattern) == best_length and is_allow):
                best_length = len(pattern)
                allowed = is_allow
    return allowed


class RobotsChecker:
    def __init__(self, driver, challenge_timeout, pacer):
        self.driver = driver
        self.challenge_timeout = challenge_timeout
        self.pacer = pacer
        self.rules = {}

    def allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.rules:
            self.pacer.wait()
            try:
                load_page(
                    self.driver,
                    f"{origin}/robots.txt",
                    self.challenge_timeout,
                    simulate_human=False,
                )
            except PageError as exc:
                raise BlockedError(f"no se pudo cargar robots.txt: {exc}") from exc

            text = self.driver.find_element(By.TAG_NAME, "body").text
            rules, delay = parse_robots(text)
            if rules is None:
                raise BlockedError(f"{origin}/robots.txt no tiene un formato válido")

            if delay is not None:
                self.pacer.update_crawl_delay(delay)

            self.rules[origin] = rules
        return robots_allows(self.rules[origin], url)


def first_int(text):
    match = re.search(r"\d[\d.,]*", text)
    return int(re.sub(r"\D", "", match.group())) if match else None


def element_text(element):
    return element.get_text(" ", strip=True) if element else None


def post_time(element):
    found = element.select_one("time") if element else None
    return found.get("datetime") or element_text(found) if found else None


def next_forum_page(soup, base_url):
    link = soup.select_one('.pagination li.next a, .pagination a[rel="next"]')
    if not link or not link.get("href"):
        return None
    candidate = canonicalize(urljoin(base_url, link["href"]))
    current, target = urlsplit(base_url), urlsplit(candidate)
    same_forum = (
        (current.scheme, current.netloc, current.path)
        == (target.scheme, target.netloc, target.path)
        and parse_qs(current.query).get("f") == parse_qs(target.query).get("f")
    )
    return candidate if same_forum else None


def parse_phpbb_forum(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    topics = {}
    for link in soup.select(PHPBB_TOPIC_SELECTOR):
        row = link.find_parent(class_="row")
        if row is None:
            continue
        classes = row.get("class", [])
        url = canonicalize(urljoin(base_url, link.get("href", "")))
        inner = row.select_one(".topic-poster") or row.select_one(".list-inner")
        last_post = row.select_one("dd.lastpost")
        topics[url] = {
            "id": parse_qs(urlsplit(url).query).get("t", [None])[0],
            "title": element_text(link),
            "url": url,
            "kind": next(
                (k for k in ("global-announce", "announce", "sticky") if k in classes),
                "topic",
            ),
            "author": element_text(inner.select_one(USERNAME_SELECTOR)) if inner else None,
            "created_at": post_time(inner),
            "replies": first_int(element_text(row.select_one("dd.posts")) or ""),
            "views": first_int(element_text(row.select_one("dd.views")) or ""),
            "last_post_author": (
                element_text(last_post.select_one(USERNAME_SELECTOR)) if last_post else None
            ),
            "last_post_at": post_time(last_post),
        }
    return list(topics.values()), next_forum_page(soup, base_url)


def parse_generic(html, selector):
    soup = BeautifulSoup(html, "html.parser")
    texts = (el.get_text(" ", strip=True) for el in soup.select(selector))
    return [text for text in texts if text]


def extract(driver, url, args):
    html = driver.page_source
    final_url = canonicalize(driver.current_url)
    if args.phpbb:
        items, next_url = parse_phpbb_forum(html, final_url)
    else:
        items, next_url = parse_generic(html, args.selector), None

    if not items:
        raise PageError("la página no contiene elementos coincidentes")

    return {
        "url": url,
        "final_url": final_url,
        "title": driver.title,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "next_url": next_url,
        "items": items,
    }


def wait_for_selector(driver, selector, timeout):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, selector)
        )
    except TimeoutException as exc:
        raise PageError(f"tiempo de espera agotado para el selector {selector!r}") from exc


def load_done(path):
    done = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as saved:
        for number, line in enumerate(saved, 1):
            try:
                record = json.loads(line)
                done[canonicalize(record["url"])] = record.get("next_url")
            except (json.JSONDecodeError, KeyError, TypeError):
                log.warning("Línea %d corrupta en %s; ignorada", number, path)
    return done


def scrape(driver, args, output):
    pacer = Pacer(args.min_delay, args.max_delay)
    robots = RobotsChecker(driver, args.challenge_timeout, pacer)
    done = {} if args.no_resume else load_done(args.output)
    queue = deque((url, 1) for url in args.urls)
    seen = set()
    saved = failed = consecutive = 0

    def follow(next_url, page):
        if args.phpbb and next_url and page < args.max_pages:
            queue.append((next_url, page + 1))

    while queue:
        url, page = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        if url in done:
            log.info("Página ya procesada previamente: %s", url)
            follow(done[url], page)
            continue

        try:
            if not robots.allowed(url):
                log.info("Acceso restringido por robots.txt: %s", url)
                continue

            pacer.wait()
            load_page(
                driver,
                url,
                args.challenge_timeout,
                simulate_human=args.simulate_human,
            )
            wait_for_selector(driver, args.selector, args.selector_timeout)
            record = extract(driver, url, args)
        except (PageError, WebDriverException) as exc:
            failed += 1
            consecutive += 1
            log.error("Error al procesar %s: %s", url, getattr(exc, "msg", None) or exc)
            if consecutive >= args.max_consecutive_errors:
                raise BlockedError(f"{consecutive} errores consecutivos") from exc
            continue

        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()
        saved += 1
        consecutive = 0
        log.info("OK (%d temas extraídos): %s", len(record["items"]), url)
        follow(record["next_url"], page)

    log.info("Finalizado: %d guardados, %d con error.", saved, failed)
    return failed


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = parse_args()
    
    # Limpiar perfil previo si quedó en estado inconsistente tras los bucles
    driver = build_driver(
        args.profile_dir,
        args.page_timeout,
        headless=not args.headed,
    )

    try:
        with args.output.open("a", encoding="utf-8") as output:
            failed = scrape(driver, args, output)
    except BlockedError as exc:
        log.error("Finalización por bloqueo: %s", exc)
        return 1
    finally:
        try:
            driver.quit()
        except Exception as exc:
            log.warning("No se pudo liberar Chrome: %s", exc)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())