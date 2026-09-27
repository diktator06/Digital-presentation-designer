"""Отчёт о визуальной декомпозиции: рамки слотов поверх отрендеренных слайдов шаблона."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from decksmith.core.models import Pattern, SlotRole, TemplateProfile

ROLE_COLORS = {
    SlotRole.title: (230, 40, 40),
    SlotRole.subtitle: (240, 130, 20),
    SlotRole.body: (30, 150, 60),
    SlotRole.item_title: (20, 110, 230),
    SlotRole.item_text: (90, 170, 255),
    SlotRole.number: (160, 40, 200),
    SlotRole.label: (130, 130, 130),
    SlotRole.caption: (130, 130, 130),
    SlotRole.person: (200, 160, 0),
    SlotRole.image: (0, 180, 180),
    SlotRole.icon: (0, 200, 120),
    SlotRole.table: (120, 60, 20),
    SlotRole.chart: (120, 60, 20),
}


def overlay(profile: TemplateProfile, pattern: Pattern, width: int = 640) -> Image.Image:
    """Рендер слайда-примера с рамками слотов паттерна, подписанными по ролям."""
    sw, sh = profile.tokens.slide_w, profile.tokens.slide_h
    height = int(width * sh / sw)
    if pattern.thumbnail and Path(pattern.thumbnail).exists():
        im = Image.open(pattern.thumbnail).convert("RGB").resize((width, height))
    else:
        im = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(im, "RGBA")
    k = width / sw
    for rep in pattern.repeaters:
        for i, b in enumerate(rep.item_boxes):
            d.rectangle([b.x * k, b.y * k, b.r * k, b.b * k], outline=(255, 0, 255, 200), width=1)
            d.text((b.x * k + 2, b.b * k - 11), f"#{i}", fill=(255, 0, 255, 255))
    for s in pattern.slots:
        c = ROLE_COLORS.get(s.role, (0, 0, 0))
        b = s.box
        d.rectangle([b.x * k, b.y * k, b.r * k, b.b * k], outline=c + (255,), width=2, fill=c + (40,))
        d.text((b.x * k + 3, b.y * k + 2), s.role.value[:9], fill=c + (255,))
    d.rectangle([0, 0, width, 16], fill=(0, 0, 0, 170))
    d.text((4, 2), f"{pattern.id} {pattern.kind.value} n={pattern.n_items} | {pattern.reason[:70]}", fill=(255, 255, 255, 255))
    return im


def contact_sheet(profile: TemplateProfile, out: str | Path, cols: int = 4, width: int = 480, only_slides: bool = True) -> Path:
    """Контакт-лист декомпозиции: все паттерны шаблона сеткой в одной картинке."""
    pats = [p for p in profile.patterns if (p.source == "slide" or not only_slides)]
    tiles = [overlay(profile, p, width) for p in pats]
    if not tiles:
        raise ValueError("no patterns")
    tw, th = tiles[0].size
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * (tw + 6), rows * (th + 6)), "white")
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * (tw + 6), (i // cols) * (th + 6)))
    out = Path(out)
    sheet.save(out)
    return out
