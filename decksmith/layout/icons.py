"""Пиктограммы: набор иконок Lucide (ISC), перекрашенный в акцент шаблона.

LLM выбирает ключевое слово иконки для каждого элемента; `resolve_icon` детерминированно
сопоставляет ключевые слова RU/EN файлам иконок (точное имя -> синонимы -> подстрока).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pymupdf

from decksmith.core.config import settings

SYNONYMS = {
    # ru -> lucide
    "рост": "trending-up", "growth": "trending-up", "деньги": "banknote", "money": "banknote", "выручка": "chart-no-axes-combined",
    "аналитика": "chart-column", "analytics": "chart-column", "данные": "database", "data": "database", "безопасность": "shield-check",
    "security": "shield-check", "скорость": "zap", "speed": "zap", "время": "clock", "time": "clock", "команда": "users",
    "team": "users", "пользователь": "user", "user": "user", "клиент": "handshake", "client": "handshake", "цель": "target",
    "goal": "target", "идея": "lightbulb", "idea": "lightbulb", "запуск": "rocket", "launch": "rocket", "облако": "cloud",
    "cloud": "cloud", "код": "code", "code": "code", "настройки": "settings", "settings": "settings", "проверка": "circle-check",
    "check": "circle-check", "аудит": "clipboard-check", "audit": "clipboard-check", "документ": "file-text", "document": "file-text",
    "шаблон": "layout-template", "template": "layout-template", "макет": "layout-dashboard", "layout": "layout-dashboard",
    "слои": "layers", "layers": "layers", "список": "list", "list": "list", "поиск": "search", "search": "search",
    "ии": "brain-circuit", "ai": "brain-circuit", "модель": "cpu", "model": "cpu", "сервер": "server", "server": "server",
    "интеграция": "plug", "integration": "plug", "экспорт": "download", "export": "download", "картинка": "image", "image": "image",
    "палитра": "palette", "palette": "palette", "дизайн": "pen-tool", "design": "pen-tool", "презентация": "presentation",
    "presentation": "presentation", "график": "chart-line", "chart": "chart-line", "таблица": "table", "table": "table",
    "процесс": "workflow", "process": "workflow", "автоматизация": "bot", "automation": "bot", "качество": "badge-check",
    "quality": "badge-check", "риск": "triangle-alert", "risk": "triangle-alert", "ошибка": "circle-x", "error": "circle-x",
    "обучение": "graduation-cap", "education": "graduation-cap", "книга": "book-open", "book": "book-open", "мир": "globe",
    "global": "globe", "почта": "mail", "mail": "mail", "телефон": "smartphone", "mobile": "smartphone", "календарь": "calendar",
    "calendar": "calendar", "деньги_экономия": "piggy-bank", "экономия": "piggy-bank", "savings": "piggy-bank", "звезда": "star",
    "star": "star", "сердце": "heart", "замок": "lock", "lock": "lock", "ключ": "key", "key": "key", "сеть": "network",
    "network": "network", "версия": "git-branch", "version": "git-branch", "тест": "flask-conical", "test": "flask-conical",
    "масштаб": "maximize", "scale": "maximize", "пазл": "puzzle", "puzzle": "puzzle", "чат": "message-square", "chat": "message-square",
    "flag": "flag", "флаг": "flag", "финиш": "flag", "компания": "building-2", "company": "building-2", "магазин": "store",
}
DEFAULT_ICON = "circle-check"


def icons_dir() -> Path:
    """Каталог иконок."""
    return settings().assets_dir / "icons"


@lru_cache(maxsize=1)
def icon_names() -> list[str]:
    """Имена доступных иконок."""
    return sorted(p.stem for p in icons_dir().glob("*.svg"))


def resolve_icon(keyword: str | None) -> str:
    """Файл иконки по ключевому слову (точное имя -> синонимы -> подстрока)."""
    names = set(icon_names())
    if not keyword:
        return DEFAULT_ICON
    k = keyword.strip().lower().replace(" ", "-").replace("_", "-")
    if k in names:
        return k
    for word in re.split(r"[\s,/\-]+", keyword.lower()):
        if word in SYNONYMS and SYNONYMS[word] in names:
            return SYNONYMS[word]
        if word in names:
            return word
    for n in sorted(names, key=len):
        if k and k in n:
            return n
    return DEFAULT_ICON


def render_icon(name: str, color_hex: str, size_px: int = 256, stroke: float = 2.0) -> str:
    """Растеризует SVG-иконку в заданном цвете (прозрачный PNG), с кэшем."""
    out_dir = settings().workspace / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}_{color_hex}_{size_px}.png"
    if out.exists():
        return str(out)
    src = icons_dir() / f"{name}.svg"
    if not src.exists():
        src = icons_dir() / f"{DEFAULT_ICON}.svg"
    svg = src.read_text(encoding="utf-8")
    svg = svg.replace("currentColor", f"#{color_hex}")
    svg = re.sub(r'stroke-width="[\d.]+"', f'stroke-width="{stroke}"', svg)
    svg = re.sub(r'width="24"\s+height="24"', f'width="{size_px}" height="{size_px}"', svg, count=1)
    doc = pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")
    page = doc[0]
    zoom = size_px / max(page.rect.width, 1)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
    pix.save(str(out))
    doc.close()
    return str(out)


def icon_for(keyword: str | None, color_hex: str) -> str:
    """PNG иконки по ключевому слову в цвете шаблона."""
    return render_icon(resolve_icon(keyword), color_hex)
