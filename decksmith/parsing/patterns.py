"""Composition-pattern extraction.

A template is treated as a *library of example compositions*. For every example
slide we detect:
  * the title slot,
  * repeaters: sets of visually identical items (cards, steps, KPI tiles),
    found by clustering text elements on (font size, weight, width) and
    checking their spatial arrangement (row / column / grid),
  * the remaining free slots (subtitle, body, labels, pictures, tables),
  * the pattern kind (title / section / cards / steps / stats / ...),
    with a human-readable reason so the decision is auditable.

Nothing here is specific to the three provided templates: the rules operate
on geometry, typography and generic RU/EN keywords only.
"""
from __future__ import annotations

import re
from collections import defaultdict

from decksmith.core.models import Box, Pattern, PatternKind, Repeater, Slot, SlotRole, TextStyle
from decksmith.parsing.elements import Element, is_filler_text, is_numberish

EMU_PT = 12700

KW = {
    "thanks": r"спасибо|благодар|thank|q\s*&\s*a|вопросы\??$|questions|end slide|closing|финальн",
    "agenda": r"содержани|оглавлени|agenda|contents|повестк|план презентации|table of contents",
    "quote": r"цитат|quote|«.{40,}",
    "section": r"раздел|разделител|section|divider|chapter|перебивк",
    "title": r"титул|обложк|cover|title slide|название\s*презентации|тема презентации|^title$",
    "team": r"имя фамилия|команд|team|спикер|speaker|визитк",
    "contacts": r"qr|call to action|контакт|contact|ссылк|связаться",
    "timeline": r"таймлайн|timeline|этап|stage|roadmap|дорожн|шаг|step|ганта|gantt",
    "chart": r"диаграмм|график|chart|гистограм",
    "table": r"таблиц|table",
}


MEDIA_FILLER = re.compile(r"вставить|вставьте|insert|qr[- ]?(code|код)?$|^qr|фото$|photo|логотип|logo|иллюстрац", re.I)


def _kw(name: str, text: str) -> bool:
    return re.search(KW[name], text, re.IGNORECASE) is not None


def _pt(emu: int) -> float:
    return emu / EMU_PT


def estimate_capacity(box: Box, size_pt: float, avail_h: int | None = None) -> tuple[int, int]:
    """(max_chars, max_lines) for a text box using average glyph advance."""
    size = max(size_pt or 12.0, 4.0)
    w_pt = max(_pt(box.w) - 14.4, size)  # default L/R insets 0.1"
    h_pt = _pt(avail_h if avail_h is not None else box.h) - 7.2
    cpl = max(int(w_pt / (size * 0.55)), 1)
    lines = max(int(h_pt / (size * 1.2)), 1)
    return int(cpl * lines * 0.92), lines


# ----------------------------------------------------------------------------
# Repeater detection
# ----------------------------------------------------------------------------
def _signature_groups(texts: list[Element], slide_w: int) -> list[list[Element]]:
    groups: list[list[Element]] = []
    for t in texts:
        placed = False
        for g in groups:
            ref = g[0]
            if abs(ref.font_size - t.font_size) > 0.6:
                continue
            if (ref.style and t.style) and ref.style.bold != t.style.bold:
                continue
            if abs(ref.box.w - t.box.w) > max(0.08 * max(ref.box.w, t.box.w), 0.01 * slide_w):
                continue
            g.append(t)
            placed = True
            break
        if not placed:
            groups.append([t])
    return groups


def _arrangement(members: list[Element], slide_w: int, slide_h: int) -> str | None:
    """row / column / grid if members are laid out regularly and do not overlap."""
    boxes = [m.box for m in members]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            inter = boxes[i].intersection(boxes[j])
            if inter > 0.2 * min(boxes[i].area, boxes[j].area):
                return None
    ytol, xtol = 0.04 * slide_h, 0.03 * slide_w
    rows = _cluster([b.y for b in boxes], ytol)
    cols = _cluster([b.x for b in boxes], xtol)
    n = len(boxes)
    if len(rows) == 1 and len(cols) == n:
        return "row"
    if len(cols) == 1 and len(rows) == n:
        return "column"
    if len(rows) >= 2 and len(cols) >= 2 and len(rows) * len(cols) >= n >= max(len(rows), len(cols)):
        # a real lattice: every row (except maybe the last) has the same number of cells
        counts = [len(r) for r in rows]
        if len(set(counts[:-1])) <= 1 and counts[-1] <= counts[0]:
            return "grid"
        return None
    # staggered rows (e.g. zig-zag timelines): accept if x positions are distinct
    if len(cols) == n and n >= 3:
        return "row"
    return None


def _cluster(values: list[float], tol: float) -> list[list[float]]:
    out: list[list[float]] = []
    for v in sorted(values):
        if out and abs(out[-1][-1] - v) <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _find_title(elements: list[Element], slide_h: int) -> Element | None:
    phs = [e for e in elements if e.kind == "text" and e.is_title_ph and not e.brand]
    if phs:
        return sorted(phs, key=lambda e: (-(len(e.text) > 0), e.box.y))[0]
    texts = [e for e in elements if e.kind == "text" and e.text and not e.brand and not is_numberish(e.text) and len(e.text) <= 140]
    if not texts:
        return None

    def dominant(cands: list[Element], ratio: float) -> Element | None:
        if not cands:
            return None
        best = max(cands, key=lambda e: (e.font_size, -e.box.y))
        others = sorted(e.font_size for e in texts if e is not best)
        if others and best.font_size < ratio * others[len(others) // 2]:
            return None
        return best

    # 1) largest text in the upper band (content slides), 2) clearly dominant text anywhere
    # (covers / dividers often put the title in the middle or lower half)
    return dominant([e for e in texts if e.box.y < 0.35 * slide_h], 1.15) or dominant(texts, 1.3)


def detect_repeaters(elements: list[Element], title: Element | None, slide_w: int, slide_h: int) -> list[tuple[Repeater, dict]]:
    texts = [e for e in elements if e.kind == "text" and e is not title and (e.text or e.placeholder in ("BODY", "OBJECT"))
             and not MEDIA_FILLER.search(e.text) and not e.brand]
    groups = _signature_groups(texts, slide_w)
    reps: list[tuple[list[Element], str]] = []
    for g in groups:
        if 2 <= len(g) <= 12:
            arr = _arrangement(g, slide_w, slide_h)
            if arr:
                reps.append((g, arr))
            else:
                # irregular group: keep its largest aligned subset (a clean row or column)
                cols = defaultdict(list)
                rows = defaultdict(list)
                for e in g:
                    cols[round(e.box.x / (0.03 * slide_w))].append(e)
                    rows[round(e.box.y / (0.04 * slide_h))].append(e)
                best = max(list(cols.values()) + list(rows.values()), key=len)
                if len(best) >= 2:
                    arr = _arrangement(best, slide_w, slide_h)
                    if arr:
                        reps.append((best, arr))
    # icon/picture repeaters (e.g. icons over theses)
    if not reps:
        return []
    reps.sort(key=lambda r: (-len(r[0]), -r[0][0].font_size, r[0][0].box.y))
    primary, arr = reps[0]
    anchors = sorted(primary, key=lambda e: (round(e.box.y / (0.05 * slide_h)), e.box.x))
    n = len(anchors)
    region = anchors[0].box
    for a in anchors[1:]:
        region = region.union(a.box)
    # spacing between anchors -> assignment radius
    xs = sorted({a.box.cx for a in anchors})
    ys = sorted({a.box.cy for a in anchors})
    dx = min((b - a for a, b in zip(xs, xs[1:]) if b - a > 0.02 * slide_w), default=slide_w)
    dy = min((b - a for a, b in zip(ys, ys[1:]) if b - a > 0.02 * slide_h), default=slide_h)

    assigned: dict[int, list[Element]] = defaultdict(list)
    anchor_ids = {id(a) for a in anchors}
    for e in elements:
        if e is title or id(e) in anchor_ids or e.kind == "group" or e.brand:
            continue
        if e.box.area > 0.5 * slide_w * slide_h:  # full-bleed backgrounds
            continue
        # card backgrounds: decor that contains exactly one anchor
        containing = [i for i, a in enumerate(anchors) if e.box.contains(a.box, tol=int(0.005 * slide_w))]
        if len(containing) == 1 and e.kind in ("decor", "picture"):
            assigned[containing[0]].append(e)
            continue
        if len(containing) > 1:
            continue  # shared container (panel behind all cards)
        # nearest anchor within half spacing
        best, best_d = None, None
        for i, a in enumerate(anchors):
            ddx = abs(e.box.cx - a.box.cx)
            ddy = abs(e.box.cy - a.box.cy)
            if ddx <= max(dx * 0.55, a.box.w * 0.6) and ddy <= max(dy * 0.6, 0.28 * slide_h if arr == "row" else dy * 0.6):
                d = ddx + ddy
                if best_d is None or d < best_d:
                    best, best_d = i, d
        if best is not None and e.box.w < 1.5 * max(dx, anchors[best].box.w * 1.2):
            assigned[best].append(e)
    rep = Repeater(
        id="r0",
        n_items=n,
        direction=arr,  # type: ignore[arg-type]
        item_boxes=[],
        item_shape_ids=[],
    )
    item_texts: list[list[Element]] = []
    for i, a in enumerate(anchors):
        members = [a] + assigned.get(i, [])
        bb = members[0].box
        for m in members[1:]:
            bb = bb.union(m.box)
        rep.item_boxes.append(bb)
        rep.item_shape_ids.append([m.shape_id for m in members])
        item_texts.append([m for m in members if m.kind == "text"])
    return [(rep, {"anchors": anchors, "item_texts": item_texts, "assigned": assigned})]


# ----------------------------------------------------------------------------
# Slot construction
# ----------------------------------------------------------------------------
def _style(e: Element) -> TextStyle:
    if not e.style:
        return TextStyle()
    return TextStyle(font=e.style.font, size=e.style.size, bold=e.style.bold, color_hex=e.style.color)


def _avail_height(e: Element, elements: list[Element], slide_h: int, bottom_margin: int) -> int:
    """For auto-growing boxes: vertical room until the next element below."""
    if not e.autofit:
        return e.box.h
    limit = slide_h - bottom_margin
    for o in elements:
        if o is e or o.kind == "group" or o.box.y <= e.box.y + e.box.h * 0.5:
            continue
        if o.box.contains(e.box):
            limit = min(limit, o.box.b)
            continue
        overlap_x = min(o.box.r, e.box.r) - max(o.box.x, e.box.x)
        if overlap_x > 0.3 * e.box.w:
            limit = min(limit, o.box.y)
    return max(e.box.h, limit - e.box.y)


def _style_differs(a, b) -> bool:
    return abs((a.size or 0) - (b.size or 0)) >= 1.0 or a.bold != b.bold or (a.color or "") != (b.color or "")


def composite_roles(e: Element, in_item: bool) -> list[SlotRole]:
    """Per-paragraph roles for multi-style text boxes, [] for homogeneous ones."""
    if len(e.paragraphs) < 2 or len(e.para_styles) < 2 or not _style_differs(e.para_styles[0], e.para_styles[1]):
        return []
    first = e.paragraphs[0]
    head = SlotRole.number if is_numberish(first) else (SlotRole.item_title if in_item else SlotRole.subtitle)
    rest = SlotRole.item_text if in_item else SlotRole.body
    return [head] + [rest] * (len(e.paragraphs) - 1)


def _text_role_for_item(t: Element, siblings: list[Element]) -> SlotRole:
    comp = composite_roles(t, True)
    if comp:
        return comp[0]
    if is_numberish(t.text) and len(t.text) <= 8:
        return SlotRole.number
    sizes = sorted({s.font_size for s in siblings}, reverse=True)
    if len(siblings) == 1:
        return SlotRole.item_text if len(t.text) > 25 or t.font_size < 13 else SlotRole.item_title
    if t.font_size == sizes[0] or (t.style and t.style.bold and t.font_size >= sizes[-1]):
        # biggest (or bold) and short -> item title
        if len(t.text) <= 40:
            return SlotRole.item_title
    if re.search(r"текст|описани|пояснени|text|description|lorem", t.text, re.I):
        return SlotRole.item_text
    if len(t.text) <= 14 and t.font_size <= sizes[-1]:
        return SlotRole.label
    return SlotRole.item_text


COVER_KINDS = {PatternKind.title, PatternKind.section, PatternKind.thanks, PatternKind.contacts, PatternKind.quote}


def _cover_cleanup(slots: list[Slot], repeaters: list[Repeater], title_slot: Slot | None, slide_h: int) -> list[Repeater]:
    """Covers/dividers have no items: re-role any repeater slots to free roles."""
    for s in slots:
        if s.item_index is None:
            continue
        s.item_index = None
        if s.kind != "text":
            continue
        below_title = title_slot is not None and 0 <= s.box.y - title_slot.box.b < 0.15 * slide_h
        if re.search(r"имя|name|фамили", s.sample_text, re.I):
            s.role = SlotRole.person
        elif below_title and s.role in (SlotRole.item_title, SlotRole.item_text):
            s.role = SlotRole.subtitle
        else:
            s.role = SlotRole.caption
    return []


def _containers(slots: list[Slot], elements: list[Element], slide_w: int, slide_h: int) -> dict[str, list[int]]:
    """Decor shapes that only exist to frame a slot (card behind a text, photo frame
    next to a speaker name). They are removed together with an unused slot."""
    out: dict[str, list[int]] = {}
    area = slide_w * slide_h
    decor = [e for e in elements if e.kind == "decor" and e.box.area < 0.25 * area]
    for s in slots:
        if s.role == SlotRole.title:
            continue
        ids = []
        for d in decor:
            if d.box.contains(s.box, tol=int(0.004 * slide_w)):
                ids.append(d.shape_id)
            elif s.role in (SlotRole.person, SlotRole.caption, SlotRole.label, SlotRole.decor):
                # photo frame / avatar circle beside the text
                v_overlap = min(d.box.b, s.box.b) - max(d.box.y, s.box.y)
                gap = s.box.x - d.box.r
                if v_overlap > 0.3 * min(d.box.h, s.box.h) and 0 <= gap < 0.6 * max(d.box.w, s.box.h) and d.box.w < 0.12 * slide_w:
                    ids.append(d.shape_id)
        if ids or s.role == SlotRole.decor:
            out[s.id] = ids
    return out


def build_pattern(
    pid: str,
    elements: list[Element],
    *,
    slide_index: int | None,
    layout_index: int | None,
    layout_name: str,
    slide_w: int,
    slide_h: int,
    bottom_margin: int,
    dark: bool,
    source: str = "slide",
) -> Pattern:
    title = _find_title(elements, slide_h)
    reps = detect_repeaters(elements, title, slide_w, slide_h)
    slots: list[Slot] = []
    in_items: dict[int, tuple[int, SlotRole]] = {}
    repeaters: list[Repeater] = []
    if reps:
        rep, info = reps[0]
        roles_per_item: list[list[SlotRole]] = []
        for i, texts in enumerate(info["item_texts"]):
            roles = []
            for t in sorted(texts, key=lambda e: (e.box.y, e.box.x)):
                role = _text_role_for_item(t, texts)
                in_items[t.shape_id] = (i, role)
                roles.append(role)
            roles_per_item.append(roles)
        # picture slots inside items (icons / photos)
        for i, members in info["assigned"].items():
            for m in members:
                if m.kind == "picture":
                    in_items[m.shape_id] = (i, SlotRole.icon if m.is_icon else SlotRole.image)
        rep.slot_roles = roles_per_item[0] if roles_per_item else []
        repeaters.append(rep)

    title_bottom = title.box.b if title else int(0.18 * slide_h)
    for e in elements:
        if e.kind in ("group", "line") or e.brand:
            continue  # brand texts (footers, page numbers, repeated notices) stay untouched
        sid = f"{pid}.s{e.shape_id}"
        if title is not None and e is title:
            mc, ml = estimate_capacity(e.box, e.font_size, _avail_height(e, elements, slide_h, bottom_margin))
            slots.append(
                Slot(id=sid, role=SlotRole.title, box=e.box, shape_id=e.shape_id, shape_path=e.path, placeholder_idx=e.placeholder_idx,
                     sample_text=e.text, style=_style(e), max_chars=max(mc, 30), max_lines=ml)
            )
            continue
        if e.kind == "text":
            if e.is_mono:
                continue
            item = in_items.get(e.shape_id)
            if MEDIA_FILLER.search(e.text or ""):
                idx, role = None, SlotRole.decor
            elif item is not None:
                idx, role = item
            else:
                idx = None
                big_content_ph = e.placeholder in ("BODY", "OBJECT") and e.box.area > 0.12 * slide_w * slide_h
                if big_content_ph:
                    role = SlotRole.body  # a large content placeholder is the body even right under the title
                elif e.placeholder in ("SUBTITLE",) or (
                    title is not None and e.box.y >= title.box.y and e.box.y - title_bottom < 0.08 * slide_h
                    and e.font_size < title.font_size and len(e.paragraphs) <= 2 and e.box.x <= title.box.x + 0.05 * slide_w
                ):
                    role = SlotRole.subtitle
                elif re.search(r"имя\s*фамилия|name\s*surname|спикер", e.text, re.I):
                    role = SlotRole.person
                elif is_numberish(e.text):
                    role = SlotRole.number
                elif len(e.paragraphs) >= 2 or e.placeholder in ("BODY", "OBJECT") or e.box.area > 0.06 * slide_w * slide_h:
                    role = SlotRole.body
                elif e.font_size and title and e.font_size >= 0.9 * title.font_size:
                    role = SlotRole.subtitle
                else:
                    role = SlotRole.label if len(e.text) <= 30 else SlotRole.caption
            mc, ml = estimate_capacity(e.box, e.font_size, _avail_height(e, elements, slide_h, bottom_margin))
            para_roles = composite_roles(e, idx is not None)
            slots.append(
                Slot(id=sid, role=role, box=e.box, shape_id=e.shape_id, shape_path=e.path, placeholder_idx=e.placeholder_idx,
                     sample_text=e.text[:200], style=_style(e), max_chars=mc, max_lines=ml, item_index=idx,
                     paragraphs=max(len(e.paragraphs), 1), para_roles=para_roles, autofit=e.autofit,
                     para_styles=[TextStyle(font=ps.font, size=ps.size, bold=ps.bold, color_hex=ps.color) for ps in e.para_styles]
                     if para_roles else [])
            )
        elif e.kind == "picture":
            item = in_items.get(e.shape_id)
            big = e.box.area > 0.08 * slide_w * slide_h
            if e.placeholder == "PICTURE" or big or item is not None:
                role = item[1] if item else (SlotRole.image if (big or e.placeholder == "PICTURE") else SlotRole.icon)
                slots.append(Slot(id=sid, role=role, kind="picture", box=e.box, shape_id=e.shape_id, shape_path=e.path,
                                  placeholder_idx=e.placeholder_idx, item_index=item[0] if item else None))
        elif e.kind == "table":
            slots.append(Slot(id=sid, role=SlotRole.table, kind="table", box=e.box, shape_id=e.shape_id, shape_path=e.path))
        elif e.kind == "chart":
            slots.append(Slot(id=sid, role=SlotRole.chart, kind="chart", box=e.box, shape_id=e.shape_id, shape_path=e.path))

    kind, reason, tags, score = classify(elements, slots, repeaters, title, layout_name, slide_index, slide_w, slide_h)
    if repeaters:
        # items must share one slot composition; otherwise the example is a one-off collage
        per_item: dict[int, list[str]] = {}
        for s in slots:
            if s.item_index is not None and s.kind == "text":
                per_item.setdefault(s.item_index, []).append(s.role.value)
        sigs = {tuple(sorted(v)) for v in per_item.values()}
        dup_roles = any(len(v) != len(set(v)) and v.count("item_title") > 1 for v in per_item.values())
        if len(sigs) > 1 or dup_roles:
            score *= 0.45
            tags = tags + ["irregular_items"]
            reason += "; irregular items"
    tags = tags + [f"chart:{e.chart_type}" for e in elements if e.kind == "chart" and e.chart_type]
    graphics = {e.graphic for e in elements if e.graphic}
    if graphics & {"smartart", "ole"}:
        # SmartArt / OLE sample content cannot be refilled -> the example would leak template text
        score *= 0.25
        tags = tags + sorted(graphics)
        reason += f"; contains {', '.join(sorted(graphics))}"
    if kind in COVER_KINDS and repeaters:
        title_slot = next((s for s in slots if s.role == SlotRole.title), None)
        repeaters = _cover_cleanup(slots, repeaters, title_slot, slide_h)
    containers = _containers(slots, elements, slide_w, slide_h)
    text_cap = sum(s.max_chars for s in slots if s.kind == "text" and s.role != SlotRole.title)
    covered = sum(min(e.box.area, slide_w * slide_h) for e in elements if e.kind in ("text", "picture", "table", "chart"))
    return Pattern(
        id=pid,
        source=source,  # type: ignore[arg-type]
        slide_index=slide_index,
        layout_index=layout_index,
        layout_name=layout_name,
        kind=kind,
        n_items=repeaters[0].n_items if repeaters else 0,
        slots=slots,
        repeaters=repeaters,
        containers=containers,
        dark=dark,
        text_capacity=text_cap,
        has_picture_slot=any(s.role == SlotRole.image for s in slots),
        fill_ratio=round(min(covered / (slide_w * slide_h), 1.0), 3),
        score_hint=score,
        tags=tags,
        reason=reason,
    )


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------
def classify(elements, slots, repeaters, title, layout_name, slide_index, slide_w, slide_h):
    texts = [e for e in elements if e.kind == "text" and e.text]
    pics = [e for e in elements if e.kind == "picture"]
    all_text = " ".join(e.text for e in texts)
    lname = (layout_name or "").lower()
    title_text = title.text if title else ""
    tags: list[str] = []
    area = slide_w * slide_h

    # --- template documentation / asset libraries ---------------------------
    small_pics = [p for p in pics if p.is_icon]
    if len(pics) >= 30 and len(small_pics) >= 0.7 * len(pics):
        return PatternKind.guide, f"icon library: {len(small_pics)} small pictures", ["icons"], 0.0
    mono = [e for e in texts if e.is_mono and re.search(r"[{};:]", e.text)]
    if mono:
        return PatternKind.guide, "code sample block (monospace)", ["code"], 0.0
    swatches = [e for e in elements if e.kind == "decor" and e.fill_hex and e.box.w < 0.12 * slide_w and abs(e.box.w - e.box.h) < 0.02 * slide_w]
    if len({s.fill_hex for s in swatches}) >= 6 and re.search(r"цвет|color|палитр|шрифт|font", all_text, re.I):
        return PatternKind.guide, "palette/typography specimen", ["palette"], 0.0

    n_items = repeaters[0].n_items if repeaters else 0
    has_table = any(s.role == SlotRole.table for s in slots)
    has_chart = any(s.role == SlotRole.chart for s in slots)
    big_pic = [s for s in slots if s.role == SlotRole.image and s.box.area > 0.12 * area]
    title_size = title.font_size if title else 0
    body_slots = [s for s in slots if s.role == SlotRole.body]
    n_text = len([s for s in slots if s.kind == "text"])
    key = f"{lname} {title_text} {all_text}".lower()

    if has_table:
        return PatternKind.table, "native table present", tags, 1.0
    if has_chart:
        return PatternKind.chart, "native chart present", tags, 1.0
    person_slots = [s for s in slots if s.role == SlotRole.person]
    if len(person_slots) >= 3 or (n_items >= 3 and _kw("team", key) and re.search(r"имя|name", all_text, re.I)):
        return PatternKind.team, "several person name slots", tags, 0.6
    if _kw("team", lname) and n_items < 2:
        return PatternKind.team, "person card layout (визитка/speaker)", tags, 0.5
    # tables / charts drawn with plain shapes: reuse is fragile -> low prior,
    # the composer builds native tables/charts in the template style instead
    grid_texts = [e for e in texts if e is not title and len(e.text) <= 40]
    card_grid = bool(repeaters) and repeaters[0].direction in ("grid", "row") and n_items < 9
    if len(grid_texts) >= 12 and not card_grid:
        rows = _cluster([e.box.cy for e in grid_texts], 0.02 * slide_h)
        cols = _cluster([e.box.x for e in grid_texts], 0.02 * slide_w)
        dense_rows = [sum(r) / len(r) for r in rows if len(r) >= 3]
        dense_cols = [c for c in cols if len(c) >= 3]
        gaps = [b - a for a, b in zip(dense_rows, dense_rows[1:])]
        regular = False
        if len(gaps) >= 3:
            mean = sum(gaps) / len(gaps)
            cv = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean if mean else 1
            regular = cv < 0.35
        if len(dense_rows) >= 4 and len(dense_cols) >= 3 and regular:
            return PatternKind.table, f"shape-drawn table {len(dense_rows)}x{len(dense_cols)}", ["shape_table"], 0.15
    num_labels = [e for e in texts if is_numberish(e.text) and e.font_size < 20]
    if len(num_labels) >= 6 and (_kw("chart", key) or len(num_labels) >= 0.4 * len(texts)):
        return PatternKind.chart, f"shape-drawn chart ({len(num_labels)} value labels)", ["shape_chart"], 0.15
    if _kw("thanks", title_text) or (_kw("thanks", lname) and n_items == 0):
        return PatternKind.thanks, "thanks/Q&A keywords", tags, 1.0
    if _kw("agenda", f"{lname} {title_text}"):
        return PatternKind.agenda, "agenda keywords", tags, 1.0
    if _kw("quote", lname) or (texts and texts[0].text.startswith(("«", '"', "“")) and len(texts[0].text) > 60):
        return PatternKind.quote, "quote layout/marks", tags, 0.9
    if _kw("contacts", f"{lname} {title_text}") and n_items == 0:
        return PatternKind.contacts, "call-to-action / QR keywords", tags, 0.8

    few = n_text <= 4 and n_items == 0 and not has_table
    opening = slide_index is not None and slide_index <= 2
    if title is not None and title.placeholder == "CENTER_TITLE" and n_items == 0 and n_text <= 4 and not big_pic:
        return PatternKind.title, "centred-title placeholder (cover layout)", tags, 1.0
    if title is not None and n_text <= 5 and not has_table and (opening or slide_index is None) and _kw("title", f"{lname} {title_text}"):
        return PatternKind.title, "title-slide layout name / cover keywords", tags, 1.0
    if few and (slide_index == 0 or _kw("title", f"{lname} {title_text}")) and title is not None:
        if opening or person_slots or "назван" in title_text.lower():
            return PatternKind.title, "opening slide with large title and few elements", tags, 1.0
    if few and (_kw("section", key) or (title is not None and title_size >= 30 and not body_slots and not big_pic)):
        return PatternKind.section, "large title with no content blocks", tags, 1.0
    if few and title is not None and slide_index is not None and slide_index <= 1:
        return PatternKind.title, "first slide", tags, 1.0

    if n_items >= 2:
        rep = repeaters[0]
        roles = rep.slot_roles
        lines = [e for e in elements if e.kind == "line"]
        numbers_seq = SlotRole.number in roles
        if _kw("timeline", key) or (numbers_seq and lines) or (lines and rep.direction == "row" and n_items >= 3):
            return PatternKind.steps, f"{n_items} items with sequence markers/connectors", ["timeline"], 1.0
        if roles and roles[0] == SlotRole.number and SlotRole.item_title not in roles:
            big = max((s.style.size or 0) for s in slots if s.role == SlotRole.number) if any(s.role == SlotRole.number for s in slots) else 0
            if big >= 24:
                return PatternKind.stats, f"{n_items} big-number tiles", tags, 1.0
        if numbers_seq:
            return PatternKind.steps, f"{n_items} numbered items", tags, 1.0
        if _kw("agenda", key):
            return PatternKind.agenda, "repeated list items with agenda keywords", tags, 1.0
        return PatternKind.cards, f"{n_items} repeated items ({rep.direction})", tags, 1.0
    numbers = [s for s in slots if s.role == SlotRole.number and (s.style.size or 0) >= 28]
    if numbers:
        return PatternKind.stats, "large numeric callouts", tags, 0.9
    if big_pic:
        tags.append("chart_like" if _kw("chart", key) else "photo")
        return PatternKind.image_text, "large picture slot with text", tags, 0.7 if _kw("chart", key) else 0.9
    if len(body_slots) >= 2:
        return PatternKind.two_column, "two body text blocks", tags, 1.0
    if body_slots or n_text >= 2:
        return PatternKind.text, "title + body text", tags, 1.0
    if title is not None and n_text <= 1:
        return PatternKind.section, "title only", tags, 0.6
    return PatternKind.free, "no dominant structure", tags, 0.3
