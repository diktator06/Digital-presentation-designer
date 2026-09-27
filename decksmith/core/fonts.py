"""Разрешение шрифтов для точности рендера и замера текста.

Порядок: установленные системные/пользовательские шрифты -> скачанные ранее -> Google Fonts
(семейства OFL/Apache, загрузка по имени семейства) -> метрическая замена.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

import httpx
from fontTools.ttLib import TTCollection, TTFont
from PIL import ImageFont

from decksmith.core.config import settings

log = logging.getLogger(__name__)
_LOCK = threading.Lock()

# только метрически совместимые открытые замены (та же ширина глифов -> те же переносы строк)
FALLBACKS = {
    "calibri": "Carlito",
    "cambria": "Caladea",
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "times new roman": "Liberation Serif",
    "courier new": "Liberation Mono",
}


def _font_dirs() -> list[Path]:
    """Каталоги шрифтов текущей ОС (первый — пользовательский)."""
    home = Path.home()
    if platform.system() == "Darwin":
        return [home / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
    return [home / ".fonts", home / ".local/share/fonts", Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]


def user_font_dir() -> Path:
    """Пользовательский каталог шрифтов (куда кладутся скачанные)."""
    d = _font_dirs()[0]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _names(font: TTFont) -> tuple[str | None, str | None]:
    """(семейство, начертание) из таблицы name шрифта."""
    name = font["name"]
    fam = name.getDebugName(16) or name.getDebugName(1)
    sub = name.getDebugName(17) or name.getDebugName(2)
    return fam, sub


@lru_cache(maxsize=1)
def font_index() -> dict[str, dict[str, str]]:
    """семейство (нижний регистр) -> {начертание (нижний регистр): путь}"""
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
    """Сбрасывает индекс шрифтов после установки новых."""
    cache = settings().workspace / "font_index.json"
    cache.unlink(missing_ok=True)
    font_index.cache_clear()


# слова насыщенности, которые некоторые шаблоны вписывают в имя семейства («Montserrat Medium»)
WEIGHTS = {"thin": 100, "hairline": 100, "extralight": 200, "ultralight": 200, "light": 300, "regular": 400,
           "book": 400, "normal": 400, "medium": 500, "semibold": 600, "demibold": 600, "bold": 700,
           "extrabold": 800, "ultrabold": 800, "black": 900, "heavy": 900}


def split_weight(family: str) -> tuple[str, str | None]:
    """'Montserrat Medium' -> ('Montserrat', 'medium'); 'Open Sans' -> ('Open Sans', None)."""
    words = family.split()
    for k in (2, 1):
        if len(words) > k and "".join(words[-k:]).lower() in WEIGHTS:
            return " ".join(words[:-k]), "".join(words[-k:]).lower()
    return family, None


def find_font_file(family: str, bold: bool = False) -> str | None:
    """Файл шрифта семейства с подходящим начертанием (жирное или обычное)."""
    idx = font_index()
    fam = idx.get(family.lower())
    prefs = ["bold", "semibold", "medium"] if bold else ["regular", "book", "normal", "roman", "medium"]
    if not fam:
        base, weight = split_weight(family)
        fam = idx.get(base.lower()) if weight else None
        if not fam:
            return None
        prefs = [weight] + prefs
    for p in prefs:
        if p in fam:
            return fam[p]
    return next(iter(fam.values()))


def _gf_slug(family: str) -> str:
    """Имя семейства в виде идентификатора Google Fonts."""
    return re.sub(r"[^a-z0-9]", "", family.lower())


# системные шрифты коммерческих ОС: их никогда нет в Google Fonts, не тратим запросы
PROPRIETARY = {
    "calibri", "calibri light", "cambria", "consolas", "segoe ui", "arial", "helvetica", "times new roman",
    "verdana", "tahoma", "georgia", "courier new", "sf pro", "sf pro display", "sf pro text",
}
_MISSING: set[str] = set()
_NET_DOWN_UNTIL = 0.0
DOWNLOAD_BUDGET_S = 15.0  # на разбор одного шаблона: шрифты повышают точность, но никогда не блокируют


def try_download_google_font(family: str, deadline: float | None = None) -> bool:
    """Скачивает открытое (OFL/Apache) семейство из Google Fonts по имени: один CSS-запрос на насыщенность
    указывает файл TrueType (устаревший user agent). Ограничено по времени: короткие таймауты, общий
    дедлайн и пауза 10 минут, если сеть недоступна, поэтому медленная или офлайн-машина никогда не
    задерживает разбор шаблона.
    """
    global _NET_DOWN_UNTIL
    base, weight = split_weight(family)
    if base.lower() in PROPRIETARY or base.lower() in _MISSING or time.monotonic() < _NET_DOWN_UNTIL:
        return False
    if os.environ.get("DECKSMITH_FONT_DOWNLOAD", "1") == "0":
        return False
    got = False
    with _LOCK, httpx.Client(timeout=httpx.Timeout(6.0, connect=2.5), follow_redirects=True,
                             headers={"User-Agent": "Mozilla/4.0"}) as client:
        for w in sorted({400, 700, WEIGHTS.get(weight or "", 400)}):
            if deadline is not None and time.monotonic() > deadline:
                break
            try:
                css = client.get("https://fonts.googleapis.com/css2", params={"family": f"{base}:wght@{w}"})
                if css.status_code != 200:
                    if w == 400:
                        break  # не семейство Google Fonts
                    continue  # у семейства нет такой насыщенности
                urls = re.findall(r"url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)", css.text)
                if urls:
                    r = client.get(urls[0])
                    if r.status_code == 200 and len(r.content) > 10000:
                        (user_font_dir() / f"{_gf_slug(base)}-{w}.ttf").write_bytes(r.content)
                        got = True
            except httpx.TransportError:
                _NET_DOWN_UNTIL = time.monotonic() + 600
                break
    if got:
        refresh_index()
        log.info("downloaded Google font %s", family)
    else:
        _MISSING.add(base.lower())
    return got


def ensure_font(family: str, allow_download: bool = True, deadline: float | None = None) -> tuple[bool, str | None]:
    """Возвращает (доступен, файл). Пытается сделать семейство доступным локально."""
    f = find_font_file(family)
    if f:
        return True, f
    alt = FALLBACKS.get(family.lower())
    if alt and find_font_file(alt):
        return False, find_font_file(alt)
    if allow_download and try_download_google_font(family, deadline):
        f = find_font_file(family)
        if f:
            return True, f
    return False, None


@lru_cache(maxsize=256)
def pil_font(family: str, size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Шрифт Pillow для замера: семейство, его метрическая замена или системный запасной."""
    f = find_font_file(family, bold) or find_font_file(FALLBACKS.get(family.lower(), ""), bold)
    for cand in (f, find_font_file("Arial", bold), find_font_file("DejaVu Sans", bold), find_font_file("Helvetica", bold)):
        if cand:
            try:
                return ImageFont.truetype(cand, size_px)
            except Exception:
                continue
    return ImageFont.load_default()
