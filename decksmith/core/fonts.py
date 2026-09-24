"""Font resolution for rendering fidelity and text measurement.

Order: installed system/user fonts -> previously downloaded -> Google Fonts
(OFL/Apache families, fetched by family name) -> metric fallback.
"""
from __future__ import annotations

import json
import logging
import platform
import re
import threading
from functools import lru_cache
from pathlib import Path

import httpx
from fontTools.ttLib import TTCollection, TTFont
from PIL import ImageFont

from decksmith.core.config import settings

log = logging.getLogger(__name__)
_LOCK = threading.Lock()

# metric-compatible open substitutes only (same advance widths -> same line breaks)
FALLBACKS = {
    "calibri": "Carlito",
    "cambria": "Caladea",
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "times new roman": "Liberation Serif",
    "courier new": "Liberation Mono",
}


def _font_dirs() -> list[Path]:
    home = Path.home()
    if platform.system() == "Darwin":
        return [home / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
    return [home / ".fonts", home / ".local/share/fonts", Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]


def user_font_dir() -> Path:
    d = _font_dirs()[0]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _names(font: TTFont) -> tuple[str | None, str | None]:
    name = font["name"]
    fam = name.getDebugName(16) or name.getDebugName(1)
    sub = name.getDebugName(17) or name.getDebugName(2)
    return fam, sub


@lru_cache(maxsize=1)
def font_index() -> dict[str, dict[str, str]]:
    """family(lower) -> {subfamily(lower): path}"""
    cache = settings().workspace / "font_index.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    idx: dict[str, dict[str, str]] = {}
    for d in _font_dirs():
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                fonts = TTCollection(str(p)).fonts if p.suffix.lower() == ".ttc" else [TTFont(str(p), lazy=True)]
                for f in fonts:
                    fam, sub = _names(f)
                    if fam:
                        idx.setdefault(fam.lower(), {}).setdefault((sub or "regular").lower(), str(p))
            except Exception:
                continue
    cache.write_text(json.dumps(idx, ensure_ascii=False))
    return idx


def refresh_index() -> None:
    cache = settings().workspace / "font_index.json"
    cache.unlink(missing_ok=True)
    font_index.cache_clear()


def find_font_file(family: str, bold: bool = False) -> str | None:
    idx = font_index()
    fam = idx.get(family.lower())
    if not fam:
        return None
    prefs = ["bold", "semibold", "medium"] if bold else ["regular", "book", "normal", "roman", "medium"]
    for p in prefs:
        if p in fam:
            return fam[p]
    return next(iter(fam.values()))


def _gf_slug(family: str) -> str:
    return re.sub(r"[^a-z0-9]", "", family.lower())


# system fonts of commercial OSes: never on Google Fonts, don't waste requests
PROPRIETARY = {
    "calibri", "calibri light", "cambria", "consolas", "segoe ui", "arial", "helvetica", "times new roman",
    "verdana", "tahoma", "georgia", "courier new", "sf pro", "sf pro display", "sf pro text",
}
_MISSING: set[str] = set()


def try_download_google_font(family: str) -> bool:
    """Fetch an OFL/Apache family from the google/fonts GitHub mirror by name."""
    if family.lower() in PROPRIETARY or family.lower() in _MISSING:
        return False
    slug = _gf_slug(family)
    base = "https://raw.githubusercontent.com/google/fonts/main"
    candidates = []
    stem = family.replace(" ", "")
    for lic in ("ofl", "apache", "ufl"):
        candidates += [
            (f"{base}/{lic}/{slug}/{stem}-Regular.ttf", f"{stem}-Regular.ttf"),
            (f"{base}/{lic}/{slug}/{stem}-Bold.ttf", f"{stem}-Bold.ttf"),
            (f"{base}/{lic}/{slug}/{stem}%5Bwght%5D.ttf", f"{stem}[wght].ttf"),
        ]
    got = False
    with _LOCK, httpx.Client(timeout=20, follow_redirects=True) as client:
        for url, fname in candidates:
            try:
                r = client.get(url)
            except Exception:
                continue
            if r.status_code == 200 and len(r.content) > 10000:
                (user_font_dir() / fname).write_bytes(r.content)
                got = True
    if got:
        refresh_index()
        log.info("downloaded Google font %s", family)
    else:
        _MISSING.add(family.lower())
    return got


def ensure_font(family: str, allow_download: bool = True) -> tuple[bool, str | None]:
    """Returns (available, file). Tries to make the family available locally."""
    f = find_font_file(family)
    if f:
        return True, f
    alt = FALLBACKS.get(family.lower())
    if alt and find_font_file(alt):
        return False, find_font_file(alt)
    if allow_download and try_download_google_font(family):
        f = find_font_file(family)
        if f:
            return True, f
    return False, None


@lru_cache(maxsize=256)
def pil_font(family: str, size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    f = find_font_file(family, bold) or find_font_file(FALLBACKS.get(family.lower(), ""), bold)
    for cand in (f, find_font_file("Arial", bold), find_font_file("DejaVu Sans", bold), find_font_file("Helvetica", bold)):
        if cand:
            try:
                return ImageFont.truetype(cand, size_px)
            except Exception:
                continue
    return ImageFont.load_default()
