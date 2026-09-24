"""Deterministic text measurement with real font metrics (Pillow/FreeType).

Used by the layout layer (fit text into slots) and by the audit
(`layout.text_overflow`). Scale: 1pt == 4px to keep sub-point precision.
"""
from __future__ import annotations

from dataclasses import dataclass

from decksmith.core.fonts import pil_font

PX_PER_PT = 4
EMU_PER_PT = 12700
DEFAULT_INSET_LR = 91440  # 0.1"
DEFAULT_INSET_TB = 45720  # 0.05"
LINE_SPACING = 1.2


@dataclass
class Measure:
    lines: int
    height_emu: int
    width_emu: int
    longest_word_emu: int


def _wrap(words: list[str], font, max_px: float) -> tuple[int, float, float]:
    lines, cur, widest, longest = 1, 0.0, 0.0, 0.0
    space = font.getlength(" ")
    for w in words:
        wl = font.getlength(w)
        longest = max(longest, wl)
        if cur == 0:
            cur = wl
        elif cur + space + wl <= max_px:
            cur += space + wl
        else:
            widest = max(widest, cur)
            lines += 1
            cur = wl
    widest = max(widest, cur)
    return lines, widest, longest


def measure(paragraphs: list[str], family: str, size_pt: float, box_w_emu: int, bold: bool = False,
            inset_lr: int = DEFAULT_INSET_LR, inset_tb: int = DEFAULT_INSET_TB, spacing: float = LINE_SPACING,
            bullet_indent_emu: int = 0) -> Measure:
    size_pt = max(size_pt, 1.0)
    font = pil_font(family or "Arial", int(round(size_pt * PX_PER_PT)), bold)
    avail_px = max((box_w_emu - 2 * inset_lr - bullet_indent_emu) / EMU_PER_PT * PX_PER_PT, 1)
    total_lines, widest, longest = 0, 0.0, 0.0
    for p in paragraphs:
        words = p.split()
        if not words:
            total_lines += 1
            continue
        ln, wd, lg = _wrap(words, font, avail_px)
        total_lines += ln
        widest, longest = max(widest, wd), max(longest, lg)
    h_pt = total_lines * size_pt * spacing
    return Measure(
        lines=total_lines,
        height_emu=int(h_pt * EMU_PER_PT + 2 * inset_tb),
        width_emu=int(widest / PX_PER_PT * EMU_PER_PT + 2 * inset_lr),
        longest_word_emu=int(longest / PX_PER_PT * EMU_PER_PT),
    )


def fits(paragraphs: list[str], family: str, size_pt: float, box_w: int, box_h: int, bold: bool = False) -> bool:
    m = measure(paragraphs, family, size_pt, box_w, bold)
    return m.height_emu <= box_h * 1.02 and m.longest_word_emu <= box_w - 2 * DEFAULT_INSET_LR


def fit_font_size(paragraphs: list[str], family: str, size_pt: float, box_w: int, box_h: int, bold: bool = False,
                  min_ratio: float = 0.75, allowed: list[float] | None = None) -> float | None:
    """Largest size <= size_pt that fits; restricted to the template scale when given."""
    candidates = sorted({s for s in (allowed or []) if min_ratio * size_pt <= s <= size_pt}, reverse=True)
    if not candidates:
        candidates = [round(size_pt * r, 1) for r in (1.0, 0.94, 0.88, 0.82, 0.76) if r >= min_ratio]
    if size_pt not in candidates:
        candidates.insert(0, size_pt)
    for s in candidates:
        if fits(paragraphs, family, s, box_w, box_h, bold):
            return s
    return None


def fit_composite(paragraphs: list[str], sizes: list[float], family: str, box_w: int, box_h: int,
                  bold_first: bool = False, min_factor: float = 0.45) -> float:
    """Common shrink factor for a multi-style box (e.g. big number + caption):
    every paragraph keeps its own size ratio; returns 1.0 when it already fits."""
    sizes = (sizes + [sizes[-1] if sizes else 14.0] * len(paragraphs))[: len(paragraphs)]
    f = 1.0
    while f >= min_factor:
        total, ok = 0, True
        for i, (p, s) in enumerate(zip(paragraphs, sizes)):
            m = measure([p], family, s * f, box_w, bold_first and i == 0, inset_tb=0)
            total += m.height_emu
            ok = ok and m.longest_word_emu <= box_w - 2 * DEFAULT_INSET_LR
        if ok and total + 2 * DEFAULT_INSET_TB <= box_h * 1.02:
            return f
        f -= 0.05
    return min_factor


def snap_down(size: float, scale: list[float], floor_ratio: float = 0.6) -> float:
    """Largest template-scale size <= size (keeps typography on the template scale)."""
    cands = [s for s in scale if floor_ratio * size <= s <= size + 0.05]
    return max(cands) if cands else size


def max_chars_for(box_w: int, box_h: int, family: str, size_pt: float, bold: bool = False) -> int:
    """Approximate character budget of a box (for LLM prompts)."""
    font = pil_font(family or "Arial", int(round(size_pt * PX_PER_PT)), bold)
    sample = "Пример текста для оценки ширины символов в строке"
    avg_px = font.getlength(sample) / len(sample)
    w_px = (box_w - 2 * DEFAULT_INSET_LR) / EMU_PER_PT * PX_PER_PT
    lines = max(int(((box_h - 2 * DEFAULT_INSET_TB) / EMU_PER_PT) / (size_pt * LINE_SPACING)), 1)
    return max(int(w_px / avg_px * lines * 0.9), 4)
