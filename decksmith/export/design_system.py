"""Экспорт дизайн-системы шаблона: то, что DeckSmith извлёк из .pptx, в переносимом виде.

Архив содержит:
  tokens.json   дизайн-токены в формате W3C Design Tokens (DTCG): цвета, шрифты, кегли, поля, области
  tokens.css    те же токены как CSS-переменные (--ds-*) для веба и прототипов
  palette.svg   палитра шаблона картинкой: цвета с ролями и hex-кодами
  README.md     что внутри и как пользоваться

Всё строится из профиля шаблона без повторного разбора и без модели.
"""
from __future__ import annotations

import html
import io
import json
import re
import zipfile
from datetime import datetime, timezone

from decksmith.core.models import Box, TemplateProfile

EMU_PER_PX = 9525  # 914400 EMU на дюйм / 96 px на дюйм
PX_PER_PT = 96 / 72

# понятные названия служебных ролей цветов темы и разбора
_ROLE_RU = {
    "text": "текст", "dk1": "тёмный 1", "lt1": "светлый 1", "dk2": "тёмный 2", "lt2": "светлый 2",
    "hlink": "ссылка", "folHlink": "посещённая ссылка",
}


def _px(emu: int | float) -> float:
    """EMU -> пиксели при 96 dpi (округление до сотых)."""
    return round(emu / EMU_PER_PX, 2)


def _num(v: float) -> str:
    """Число без лишнего «.0»: 24.0 -> «24», 10.5 -> «10.5»."""
    return f"{v:g}"


def _hex(h: str) -> str:
    """«RRGGBB» -> «#rrggbb»."""
    return f"#{h.strip('#').lower()}"


def _roles(roles: list[str]) -> str:
    """Роли цвета по-русски через запятую."""
    # акценты темы accent1..accent6 -> «акцент 1»..«акцент 6»
    return ", ".join(_ROLE_RU.get(r) or re.sub(r"^accent(\d)$", r"акцент \1", r) for r in roles)


def _font_stack(family: str) -> list[str]:
    """Семейство шрифта с общим запасным вариантом."""
    return [family, "sans-serif"]


def _dim(px: float, **extra) -> dict:
    """Токен размера DTCG в пикселях; исходные единицы — в расширении."""
    tok = {"$type": "dimension", "$value": f"{_num(px)}px"}
    if extra:
        tok["$extensions"] = {"com.decksmith": extra}
    return tok


def _box_tokens(b: Box) -> dict:
    """Группа токенов области слайда: x, y, ширина, высота."""
    return {"x": _dim(_px(b.x)), "y": _dim(_px(b.y)), "width": _dim(_px(b.w)), "height": _dim(_px(b.h))}


def _slug(name: str) -> str:
    """Имя шаблона для имени файла: буквы, цифры, дефис и подчёркивание."""
    s = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")
    return s or "template"


def design_tokens(prof: TemplateProfile) -> dict:
    """Дизайн-токены шаблона в формате W3C Design Tokens (DTCG): группы color, font, layout, radius."""
    t = prof.tokens
    total = sum(c.usage for c in t.palette) or 1
    tokens: dict = {
        "$description": f"Дизайн-система шаблона «{prof.name}», извлечённая DeckSmith из .pptx",
        "$extensions": {"com.decksmith": {
            "template": prof.name,
            "sha256": prof.sha256,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "slide_size_in": [round(t.slide_w / 914400, 3), round(t.slide_h / 914400, 3)],
            "dark_background": t.dark_background,
        }},
    }

    # цвета: основные роли, вся палитра по частоте использования, цвета темы и диаграмм
    color: dict = {
        "accent": {"$type": "color", "$value": _hex(t.accent_hex), "$description": "фирменный акцент"},
        "background": {"$type": "color", "$value": _hex(t.background_hex), "$description": "фон содержательных слайдов"},
        "text": {"$type": "color", "$value": _hex(t.text_hex), "$description": "основной цвет текста"},
    }
    if t.card_fill_hex:
        color["card"] = {"$type": "color", "$value": _hex(t.card_fill_hex), "$description": "заливка карточек"}
    color["palette"] = {
        str(i): {"$type": "color", "$value": _hex(c.hex),
                 "$description": (_roles(c.roles) + "; " if c.roles else "") + f"доля использования {c.usage / total:.0%}"}
        for i, c in enumerate(t.palette, 1)
    }
    if t.theme_colors:
        color["theme"] = {k: {"$type": "color", "$value": _hex(v)} for k, v in t.theme_colors.items()}
    if t.chart_colors:
        color["chart"] = {str(i): {"$type": "color", "$value": _hex(h)} for i, h in enumerate(t.chart_colors, 1)}
    tokens["color"] = color

    # шрифты: семейства заголовков и текста, все шрифты шаблона, кегли ролей и вся шкала
    ts = t.type_scale
    roles = {"title": ts.title, "subtitle": ts.subtitle, "body": ts.body, "caption": ts.caption, "number": ts.number}
    tokens["font"] = {
        "family": {
            "heading": {"$type": "fontFamily", "$value": _font_stack(t.heading_font), "$description": "заголовки"},
            "body": {"$type": "fontFamily", "$value": _font_stack(t.body_font), "$description": "основной текст"},
            "all": {
                str(i): {"$type": "fontFamily", "$value": _font_stack(f.family),
                         "$extensions": {"com.decksmith": {"available": f.available, "roles": f.roles}}}
                for i, f in enumerate(t.fonts, 1)
            },
        },
        "size": {
            **{k: _dim(round(v * PX_PER_PT, 2), pt=v) for k, v in roles.items()},
            "scale": {str(i): _dim(round(v * PX_PER_PT, 2), pt=v) for i, v in enumerate(ts.sizes, 1)},
        },
    }

    # геометрия: размер слайда, поля и типовые области заголовка и контента
    m = t.margins
    layout: dict = {
        "slide": {"width": _dim(_px(t.slide_w)), "height": _dim(_px(t.slide_h))},
        "margin": {"left": _dim(_px(m.left)), "top": _dim(_px(m.top)),
                   "right": _dim(_px(m.right)), "bottom": _dim(_px(m.bottom))},
    }
    if t.title_box:
        layout["title-box"] = _box_tokens(t.title_box)
    if t.content_box:
        layout["content-box"] = _box_tokens(t.content_box)
    tokens["layout"] = layout
    tokens["radius"] = {"card-ratio": {
        "$type": "number", "$value": round(t.corner_radius, 3),
        "$description": "скругление углов карточек как доля меньшей стороны (0 — прямые углы)",
    }}
    return tokens


def tokens_css(prof: TemplateProfile) -> str:
    """Те же токены как CSS-переменные :root (кегли в pt, размеры в px при 96 dpi)."""
    t = prof.tokens
    ts = t.type_scale
    lines = [
        f"/* Дизайн-система шаблона «{prof.name}» — извлечена DeckSmith. */",
        "/* Кегли в pt, как в презентации; размеры и поля в px при 96 dpi. */",
        ":root {",
        f"  --ds-color-accent: {_hex(t.accent_hex)};",
        f"  --ds-color-background: {_hex(t.background_hex)};",
        f"  --ds-color-text: {_hex(t.text_hex)};",
    ]
    if t.card_fill_hex:
        lines.append(f"  --ds-color-card: {_hex(t.card_fill_hex)};")
    # палитра по частоте использования, затем цвета темы и серий диаграмм
    lines += [f"  --ds-color-palette-{i}: {_hex(c.hex)};" for i, c in enumerate(t.palette, 1)]
    lines += [f"  --ds-color-theme-{k.lower()}: {_hex(v)};" for k, v in t.theme_colors.items()]
    lines += [f"  --ds-color-chart-{i}: {_hex(h)};" for i, h in enumerate(t.chart_colors, 1)]
    # имена семейств в кавычках: в них бывают пробелы
    lines += [
        f"  --ds-font-heading: \"{t.heading_font}\", sans-serif;",
        f"  --ds-font-body: \"{t.body_font}\", sans-serif;",
    ]
    for k in ("title", "subtitle", "body", "caption", "number"):
        lines.append(f"  --ds-font-size-{k}: {_num(getattr(ts, k))}pt;")
    lines += [f"  --ds-font-scale-{i}: {_num(v)}pt;" for i, v in enumerate(ts.sizes, 1)]
    m = t.margins
    lines += [
        f"  --ds-slide-width: {_num(_px(t.slide_w))}px;",
        f"  --ds-slide-height: {_num(_px(t.slide_h))}px;",
        f"  --ds-margin-left: {_num(_px(m.left))}px;",
        f"  --ds-margin-top: {_num(_px(m.top))}px;",
        f"  --ds-margin-right: {_num(_px(m.right))}px;",
        f"  --ds-margin-bottom: {_num(_px(m.bottom))}px;",
    ]
    for name, b in (("title-box", t.title_box), ("content-box", t.content_box)):
        if b is not None:
            lines += [f"  --ds-{name}-{k}: {_num(_px(v))}px;" for k, v in (("x", b.x), ("y", b.y), ("width", b.w), ("height", b.h))]
    lines.append(f"  --ds-card-radius-ratio: {_num(round(t.corner_radius, 3))};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def palette_svg(prof: TemplateProfile) -> str:
    """Палитра картинкой: плашки цветов с hex-кодом и ролью; отдельный ряд — цвета диаграмм."""
    t = prof.tokens
    rows = [("Палитра", [(c.hex, _roles(c.roles)) for c in t.palette]),
            ("Диаграммы", [(h, f"серия {i}") for i, h in enumerate(t.chart_colors, 1)])]
    rows = [(title, items) for title, items in rows if items]
    cols, cell_w, cell_h, pad = 8, 132, 104, 24
    y, parts = pad, []
    for title, items in rows:
        parts.append(f'<text x="{pad}" y="{y + 16}" font-size="16" font-weight="700" fill="#16181d">{html.escape(title)}</text>')
        y += 28
        for i, (hx, label) in enumerate(items):
            x, yy = pad + (i % cols) * cell_w, y + (i // cols) * cell_h
            # тонкая рамка, чтобы белые и почти белые цвета были видны на белом фоне
            parts.append(f'<rect x="{x}" y="{yy}" width="{cell_w - 12}" height="56" rx="8" fill="{_hex(hx)}" stroke="#d0d4db"/>')
            parts.append(f'<text x="{x}" y="{yy + 74}" font-size="12" font-weight="600" fill="#16181d">{_hex(hx).upper()}</text>')
            if label:
                parts.append(f'<text x="{x}" y="{yy + 90}" font-size="10" fill="#6b7280">{html.escape(label[:22])}</text>')
        y += ((len(items) + cols - 1) // cols) * cell_h + 12
    width = pad * 2 + cols * cell_w - 12
    height = y + pad
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'font-family="Inter, Arial, sans-serif">\n<rect width="100%" height="100%" fill="#ffffff"/>\n'
            + "\n".join(parts) + "\n</svg>\n")


def readme(prof: TemplateProfile) -> str:
    """Короткое описание архива: откуда токены, что в каждом файле и как их подключить."""
    t = prof.tokens
    ts = t.type_scale
    fonts = ", ".join(f"{f.family}{'' if f.available else ' (в DeckSmith рендерился заменой)'}" for f in t.fonts) or "—"
    return "\n".join([
        f"# Дизайн-система шаблона «{prof.name}»",
        "",
        "Извлечена DeckSmith из файла шаблона: палитра и роли цветов, шрифты, шкала кеглей, поля и типовые",
        "области слайда. Значения получены разбором OOXML и рендера, без ручной разметки.",
        "",
        "## Коротко",
        "",
        f"* Слайд: {round(t.slide_w / 914400, 2)}″ × {round(t.slide_h / 914400, 2)}″, фон "
        f"{'тёмный' if t.dark_background else 'светлый'} ({_hex(t.background_hex)}), текст {_hex(t.text_hex)}",
        f"* Акцент: {_hex(t.accent_hex)}; цветов в палитре: {len(t.palette)}; цветов диаграмм: {len(t.chart_colors)}",
        f"* Шрифты: заголовки — {t.heading_font}, текст — {t.body_font}; все шрифты шаблона: {fonts}",
        f"* Кегли (pt): заголовок {_num(ts.title)}, подзаголовок {_num(ts.subtitle)}, текст {_num(ts.body)}, "
        f"подпись {_num(ts.caption)}, крупные числа {_num(ts.number)}",
        "",
        "## Файлы",
        "",
        "* `tokens.json` — токены в формате W3C Design Tokens (DTCG): открывается в Figma через Tokens Studio,",
        "  собирается Style Dictionary. Размеры в px при 96 dpi, исходные pt — в `$extensions`.",
        "* `tokens.css` — те же значения как CSS-переменные `--ds-*`: подключите файл и используйте",
        "  `var(--ds-color-accent)`, `var(--ds-font-heading)`, `var(--ds-font-size-title)`.",
        "* `palette.svg` — палитра картинкой с hex-кодами и ролями цветов.",
        "",
        f"Исходный файл: sha256 `{prof.sha256}`.",
        "",
    ])


def design_system_zip(prof: TemplateProfile) -> tuple[str, bytes]:
    """Архив дизайн-системы: (имя файла, содержимое .zip)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # текстовые файлы в UTF-8: названия ролей и шаблонов бывают кириллическими
        zf.writestr("tokens.json", json.dumps(design_tokens(prof), ensure_ascii=False, indent=2))
        zf.writestr("tokens.css", tokens_css(prof))
        zf.writestr("palette.svg", palette_svg(prof))
        zf.writestr("README.md", readme(prof))
    return f"{_slug(prof.name)}_design-system.zip", buf.getvalue()
