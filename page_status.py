"""Errores de página y comprobación del código HTTP, compartidos por los scrapers."""

import json

STOP_STATUSES = {403, 429, 503}


class BlockedError(Exception):
    """El sitio ha denegado el acceso o no se puede continuar con seguridad."""


class PageError(Exception):
    """Ha fallado una página concreta, pero se puede seguir con las demás."""


def enable_status_logging(options):
    """Activa el registro de Chrome del que `document_status` lee las respuestas."""
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})


def status_from_log(entries, current_url):
    """Devuelve el último código HTTP registrado para el documento actual."""
    current = current_url.split("#")[0]
    status = None
    for entry in entries:
        message = json.loads(entry["message"])["message"]
        if message["method"] != "Network.responseReceived":
            continue
        params = message["params"]
        response = params["response"]
        if params.get("type") == "Document" and response["url"].split("#")[0] == current:
            status = response["status"]
    return status


def document_status(driver):
    return status_from_log(driver.get_log("performance"), driver.current_url)


def check_status(status, url):
    """Detiene todo ante 403, 429 o 503, y descarta la página ante otros errores."""
    if status in STOP_STATUSES:
        raise BlockedError(f"HTTP {status} en {url}")
    if status is not None and status >= 400:
        raise PageError(f"HTTP {status}")
