"""Ubicación compartida de los datos extraídos.

La carpeta predeterminada es D:\\Dropbox\\SNES Development\\cf_scraper, vista desde
WSL. Se puede cambiar con la variable de entorno CF_SCRAPER_DATA_DIR.
"""

import os
from pathlib import Path

DATA_DIR = Path(
    os.environ.get(
        "CF_SCRAPER_DATA_DIR",
        "/mnt/d/Dropbox/SNES Development/cf_scraper",
    )
)
TOPICS_FILE = DATA_DIR / "resultados.jsonl"
POSTS_FILE = DATA_DIR / "posts.jsonl"


def require_parent_dir(parser, *paths):
    """Evita crear la carpeta en otro disco si Dropbox o D: no están disponibles."""
    for path in paths:
        if not path.parent.is_dir():
            parser.error(
                f"no existe la carpeta {path.parent}; comprueba que la unidad "
                "esté montada o usa CF_SCRAPER_DATA_DIR"
            )
