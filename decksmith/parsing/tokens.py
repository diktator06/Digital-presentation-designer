"""Design-token extraction: palette, typography scale, margins, grid, canonical boxes."""
from __future__ import annotations

import colorsys
import statistics
from collections import Counter

from decksmith.core.models import Box, ColorToken, DesignTokens, FontToken, Margins, Pattern, PatternKind, SlotRole, TypeScale
from decksmith.parsing.elements import Element
from decksmith.parsing.ooxml import Theme, color_distance, hex_to_rgb, rel_luminance


def saturation(h: str) -> float:
    r, g, b = (c / 255 for c in hex_to_rgb(h))
    _, l, s = colorsys.rgb_to_hls(r, g, b)
    return s if 0.08 < l < 0.92 else 0.0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[k]


def merge_colors(counter: Counter, threshold: float = 28.0) -> list[tuple[str, float]]:
    merged: list[list] = []
    for hex_, w in counter.most_common():
        for m in merged:
            if color_distance(m[0], hex_) < threshold:
                m[1] += w
                break
        else:
            merged.append([hex_, w])
    return [(h, w) for h, w in merged]


def _hue(h: str) -> float:
    r, g, b = (c / 255 for c in hex_to_rgb(h))
    return colorsys.rgb_to_hls(r, g, b)[0]


def _shift(h: str, dh: float = 0.0, dl: float = 0.0) -> str:
    from decksmith.parsing.ooxml import rgb_to_hex

    r, g, b = (c / 255 for c in hex_to_rgb(h))
    hh, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb((hh + dh) % 1.0, max(0.05, min(0.95, l + dl)), s)
    return rgb_to_hex((r * 255, g * 255, b * 255))


def pick_accent(palette: list[ColorToken], render_colors: Counter, theme: Theme, bg: str) -> tuple[str, list[str]]:
    """Brand accent = the most used saturated colour that is *visible on the content background*.

    Evidence: colours of text/fills in example slides + colours of the rendered slides
    (art, bands, backgrounds). Theme accents only count when the template really uses
    them — unused default themes (e.g. an office-suite default green) are ignored.
    """
    from decksmith.parsing.ooxml import contrast_ratio

    scores: Counter = Counter()
    max_use = max((c.usage for c in palette), default=1) or 1
    for c in palette:
        if c.usage > 0:
            scores[c.hex] += 0.6 * c.usage / max_use
    max_r = max(render_colors.values(), default=1) or 1
    for h, w in render_colors.items():
        scores[h] += 0.4 * w / max_r
    for k in ("accent1", "accent2", "accent3"):
        h = theme.colors.get(k)
        if h and any(color_distance(h, s) < 40 for s in scores):
            scores[h] += 0.05
    cands = []
    for h, s in scores.most_common():
        if saturation(h) < 0.28 or color_distance(h, bg) < 70 or contrast_ratio(h, bg) < 1.6:
            continue
        if any(color_distance(h, c) < 40 for c, _ in cands):
            continue
        cands.append((h, s))
    if not cands:  # nothing saturated in use: fall back to a theme accent that shows on the bg
        for k in ("accent1", "accent2", "accent3", "accent4", "dk2"):
            h = theme.colors.get(k)
            if h and saturation(h) > 0.2 and contrast_ratio(h, bg) >= 1.6:
                cands.append((h, 0.01))
                break
    accent = cands[0][0] if cands else ("3366CC" if rel_luminance(bg) > 0.4 else "66A3FF")
    charts = [accent] + [h for h, _ in cands[1:] if contrast_ratio(h, bg) >= 1.5][:4]
    step = 0
    while len(charts) < 5:  # extend with hue rotations of the accent, tuned to the bg
        step += 1
        cand = _shift(accent, dh=0.13 * step, dl=(-0.08 if rel_luminance(bg) > 0.4 else 0.08) * (step % 2))
        if all(color_distance(cand, c) > 50 for c in charts):
            charts.append(cand)
        if step > 12:
            break
    return accent, charts


def build_tokens(
    *,
    slide_w: int,
    slide_h: int,
    theme: Theme,
    slide_elements: list[list[Element]],
    patterns: list[Pattern],
    slide_dark: list[bool],
    slide_bg: list[str],
    font_available: dict[str, tuple[bool, str | None]],
    render_colors: Counter | None = None,
) -> DesignTokens:
    content_idx = [
        p.slide_index for p in patterns
        if p.source == "slide" and p.slide_index is not None
        and p.kind not in (PatternKind.guide, PatternKind.title, PatternKind.section, PatternKind.thanks)
    ]
    content_idx = content_idx or list(range(len(slide_elements)))

    # --- palette ---------------------------------------------------------------
    colors: Counter = Counter()
    text_colors: Counter = Counter()
    fill_colors: Counter = Counter()
    font_usage: Counter = Counter()
    heading_fonts: Counter = Counter()
    size_usage: Counter = Counter()
    card_fills: Counter = Counter()
    area = slide_w * slide_h
    for si, els in enumerate(slide_elements):
        for e in els:
            if e.kind == "text" and e.style:
                n = max(len(e.text), 1)
                if e.style.color:
                    text_colors[e.style.color] += n
                    colors[e.style.color] += n
                if e.style.font and not e.is_mono:
                    font_usage[e.style.font] += n
                    if e.is_title_ph:
                        heading_fonts[e.style.font] += n
                if e.style.size and not e.is_mono:
                    size_usage[round(e.style.size * 2) / 2] += n
            if e.fill_hex and e.kind in ("decor", "text") and e.box.area < 0.9 * area:
                w = max(1, int(200 * e.box.area / area))
                fill_colors[e.fill_hex] += w
                colors[e.fill_hex] += w
                if e.kind == "decor" and e.box.area > 0.015 * area and si in set(content_idx):
                    card_fills[e.fill_hex] += int(1000 * e.box.area / area)
    for bg in slide_bg:
        colors[bg] += 50
    palette: list[ColorToken] = []
    theme_rev = {}
    for k, v in theme.colors.items():
        theme_rev.setdefault(v, []).append(k)
    for hex_, w in merge_colors(colors):
        if w < 2:
            continue
        roles = list(theme_rev.get(hex_, []))
        if hex_ in text_colors:
            roles.append("text")
        palette.append(ColorToken(hex=hex_, roles=roles, usage=int(w), luminance=round(rel_luminance(hex_), 3)))
    # add theme accents that are really used somewhere (or all accents if none used)
    for k in ("accent1", "accent2", "accent3", "dk1", "lt1"):
        h = theme.colors.get(k)
        if h and not any(color_distance(h, c.hex) < 28 for c in palette):
            palette.append(ColorToken(hex=h, roles=[k, "theme"], usage=0, luminance=round(rel_luminance(h), 3)))
    palette.sort(key=lambda c: -c.usage)

    dark_votes = [slide_dark[i] for i in content_idx if i < len(slide_dark)]
    dark = sum(dark_votes) > len(dark_votes) / 2 if dark_votes else False
    bgs = Counter(slide_bg[i] for i in content_idx if i < len(slide_bg)) or Counter(slide_bg)
    bg_hex = bgs.most_common(1)[0][0] if bgs else ("000000" if dark else "FFFFFF")
    dark = rel_luminance(bg_hex) < 0.18 if not dark_votes else dark

    accent, chart_colors = pick_accent(palette, render_colors or Counter(), theme, bg_hex)
    # text color: most used readable text color against the dominant background
    text_hex = "FFFFFF" if dark else "000000"
    for h, _ in text_colors.most_common():
        if (rel_luminance(h) > 0.5) == dark and saturation(h) < 0.35:
            text_hex = h
            break

    # --- typography -----------------------------------------------------------
    fonts = []
    for fam, n in font_usage.most_common(6):
        avail, file = font_available.get(fam, (False, None))
        roles = []
        if heading_fonts and fam == heading_fonts.most_common(1)[0][0]:
            roles.append("heading")
        fonts.append(FontToken(family=fam, usage=n, roles=roles, available=avail, file=file))
    body_font = font_usage.most_common(1)[0][0] if font_usage else theme.minor_font
    heading_font = heading_fonts.most_common(1)[0][0] if heading_fonts else body_font
    for f in fonts:
        if f.family == body_font and "body" not in f.roles:
            f.roles.append("body")

    sizes = sorted([s for s, n in size_usage.items() if n >= 3 and s >= 6], reverse=True)
    title_sizes = [s.style.size for p in patterns if p.slide_index in content_idx for s in p.slots if s.role == SlotRole.title and s.style.size]
    body_sizes = [s.style.size for p in patterns if p.slide_index in content_idx for s in p.slots
                  if s.role in (SlotRole.body, SlotRole.item_text) and s.style.size]
    item_title_sizes = [s.style.size for p in patterns if p.slide_index in content_idx for s in p.slots
                        if s.role == SlotRole.item_title and s.style.size]
    number_sizes = [s.style.size for p in patterns for s in p.slots if s.role == SlotRole.number and s.style.size and s.style.size >= 20]
    mode = lambda xs, d: statistics.mode(xs) if xs else d  # noqa: E731
    h_pt = slide_h / 12700
    clamp = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
    # Evidence from example slides wins; with few examples placeholder defaults (often 32 pt
    # body, 44 pt title) are pulled into a range proportional to the slide height.
    title = clamp(float(mode(title_sizes, sizes[0] if sizes else h_pt * 0.07)), h_pt * 0.04, h_pt * 0.1)
    body_default = h_pt * 0.032
    body = float(mode(body_sizes, body_default)) if len(body_sizes) >= 3 else min(float(mode(body_sizes, body_default)), body_default)
    body = clamp(body, h_pt * 0.02, min(title * 0.8, h_pt * 0.045))
    subtitle = clamp(float(mode(item_title_sizes, body * 1.25)), body, max(body, title * 0.8))
    caption = clamp(float(min([s for s in sizes if s >= 8] or [body * 0.75])), min(8.0, body), body)
    number = clamp(float(statistics.median(number_sizes)) if number_sizes else title * 1.6, title, h_pt * 0.16)
    # role sizes are always part of the scale (compose and fitting only use scale values)
    title, subtitle, body, caption, number = (round(x * 2) / 2 for x in (title, subtitle, body, caption, number))
    sizes = sorted(set(sizes) | {title, subtitle, body, caption, number}, reverse=True)
    scale = TypeScale(sizes=sizes, title=round(title, 2), body=round(body, 2), subtitle=round(subtitle, 2),
                      caption=round(caption, 2), number=round(number, 2))

    # --- geometry -------------------------------------------------------------
    lefts, rights, tops, bottoms = [], [], [], []
    for si in content_idx:
        for e in slide_elements[si]:
            if e.kind not in ("text", "picture", "table", "chart") or e.box.area > 0.6 * area:
                continue
            if e.kind == "picture" and (e.box.x <= 0 or e.box.r >= slide_w):
                continue  # full-bleed art
            if e.box.w < 0.01 * slide_w:
                continue
            lefts.append(e.box.x)
            rights.append(slide_w - e.box.r)
            tops.append(e.box.y)
            bottoms.append(slide_h - e.box.b)
    if len(lefts) >= 6:  # enough evidence: margins as the template uses them
        left = int(_percentile([v for v in lefts if v > 0], 0.08)) or int(0.05 * slide_w)
        right = int(_percentile([v for v in rights if v > 0], 0.08)) or left
        top = int(_percentile([v for v in tops if v > 0], 0.05)) or int(0.06 * slide_h)
        bottom = int(_percentile([v for v in bottoms if v > 0], 0.08)) or int(0.06 * slide_h)
    else:  # one or two examples say nothing about margins: proportional defaults
        left = right = int(0.055 * slide_w)
        top, bottom = int(0.06 * slide_h), int(0.07 * slide_h)
    cl = lambda v, lo, hi: int(max(lo, min(hi, v)))  # noqa: E731
    margins = Margins(left=cl(left, 0.02 * slide_w, 0.12 * slide_w), right=cl(right, 0.02 * slide_w, 0.12 * slide_w),
                      top=cl(top, 0.02 * slide_h, 0.15 * slide_h), bottom=cl(bottom, 0.02 * slide_h, 0.15 * slide_h))
    left, right, top, bottom = margins.left, margins.right, margins.top, margins.bottom

    title_boxes = [s.box for p in patterns if p.slide_index in content_idx for s in p.slots if s.role == SlotRole.title]
    title_box = None
    if title_boxes:
        title_box = Box(
            x=int(statistics.median(b.x for b in title_boxes)),
            y=int(statistics.median(b.y for b in title_boxes)),
            w=int(statistics.median(b.w for b in title_boxes)),
            h=int(statistics.median(b.h for b in title_boxes)),
        )
    ct_top = (title_box.b if title_box else int(0.2 * slide_h)) + int(0.03 * slide_h)
    content_box = Box(x=left, y=ct_top, w=slide_w - left - right, h=max(slide_h - bottom - ct_top, int(0.4 * slide_h)))

    xs = Counter(round(e.box.x / (0.01 * slide_w)) for si in content_idx for e in slide_elements[si] if e.kind == "text")
    grid = sorted(int(k * 0.01 * slide_w) for k, n in xs.items() if n >= 3)

    radius = 0.0
    cf = card_fills.most_common(1)[0][0] if card_fills else None
    return DesignTokens(
        slide_w=slide_w,
        slide_h=slide_h,
        palette=palette[:16],
        theme_colors=dict(theme.colors),
        fonts=fonts,
        heading_font=heading_font,
        body_font=body_font,
        type_scale=scale,
        margins=margins,
        grid_columns=grid[:24],
        title_box=title_box,
        content_box=content_box,
        dark_background=dark,
        background_hex=bg_hex,
        text_hex=text_hex,
        accent_hex=accent,
        chart_colors=chart_colors,
        corner_radius=radius,
        card_fill_hex=cf,
    )
