"""Deterministic pattern selection: DeckPlan x TemplateProfile x Variant -> decisions.

Every decision carries a rationale string, so the UI/defence can show *why* a
given template slide was picked. Same inputs always give the same output.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from decksmith.core.config import ROOT
from decksmith.core.models import Box, DeckPlan, Pattern, PatternKind, SlideLayout, SlideSpec, SlotRole, TemplateProfile
from decksmith.layout.room import clear_title_box, owned_height
from decksmith.layout.textfit import fits

K = PatternKind

COMPAT: dict[PatternKind, dict[PatternKind, float]] = {
    K.title: {K.title: 1.0},
    K.section: {K.section: 1.0, K.title: 0.45},
    K.agenda: {K.agenda: 1.0, K.steps: 0.55, K.cards: 0.45},
    K.text: {K.text: 1.0, K.two_column: 0.75, K.image_text: 0.55},
    K.two_column: {K.two_column: 1.0, K.text: 0.7, K.cards: 0.6},
    K.cards: {K.cards: 1.0, K.steps: 0.55, K.stats: 0.25},
    K.steps: {K.steps: 1.0, K.cards: 0.7},
    K.stats: {K.stats: 1.0, K.cards: 0.5},
    K.table: {K.table: 1.0},
    K.chart: {K.chart: 1.0},
    K.image_text: {K.image_text: 1.0, K.text: 0.5},
    K.quote: {K.quote: 1.0, K.section: 0.3},
    K.team: {K.team: 1.0, K.cards: 0.5},
    K.contacts: {K.contacts: 1.0, K.thanks: 0.8},
    K.thanks: {K.thanks: 1.0, K.contacts: 0.7, K.section: 0.35, K.title: 0.4},
}
ITEM_KINDS = {K.cards, K.steps, K.stats, K.agenda, K.team}


@dataclass
class Variant:
    name: str
    title: str
    description: str
    kind_bias: dict[str, float]
    text_density: float = 1.0
    max_bullets: int = 5
    represent: dict[str, list[str]] = field(default_factory=dict)
    drop_sections: bool = False
    exec_summary_first: bool = False
    images: int = 1
    prefer_icons: bool = False
    tone: str = "auto"


@lru_cache(maxsize=4)
def load_variants(path: str | None = None) -> dict[str, Variant]:
    p = Path(path) if path else ROOT / "config" / "variants.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return {k: Variant(name=k, **v) for k, v in data.items()}


def spec_items_count(spec: SlideSpec) -> int:
    if spec.items:
        return len(spec.items)
    if spec.intent in ITEM_KINDS and spec.bullets:
        return len(spec.bullets)
    return 0


def spec_chars(spec: SlideSpec) -> int:
    n = len(spec.subtitle) + len(spec.message)
    n += sum(len(b) for b in spec.bullets)
    n += sum(len(i.title) + len(i.text) + len(i.value) for i in spec.items)
    return n


def _removable(p: Pattern) -> bool:
    return bool(p.repeaters) and p.repeaters[0].direction in ("row", "column", "grid")


@dataclass
class Candidate:
    score: float
    mode: str
    pattern: Pattern | None
    keep_items: int | None
    why: list[str]


def _row_length(p: Pattern) -> int:
    """Items in the first row of a repeater laid out as a grid (0 for a single row or column)."""
    boxes = p.repeaters[0].item_boxes if p.repeaters else []
    if len(boxes) < 4:
        return 0
    top = min(b.y for b in boxes)
    first = [b for b in boxes if b.y - top < 0.3 * b.h]
    return len(first) if 1 < len(first) < len(boxes) else 0


@lru_cache(maxsize=4096)
def _title_ratio(title: str, font: str, size: float, bold: bool, w: int, h: int) -> float:
    """Largest of 100/85/70/60 % of the size at which the title fits the box (0 when none)."""
    return next((r for r in (1.0, 0.85, 0.7, 0.6) if fits([title], font, size * r, w, h, bold)), 0.0)


def score_pattern(p: Pattern, spec: SlideSpec, variant: Variant, used: dict[str, int], prev: str | None,
                  has_image: bool = False, slide_area: int = 0, layout_photo: float = 0.0,
                  layout_uses: int = 0, title_clear: Box | None = None) -> Candidate | None:
    compat = COMPAT.get(spec.intent, {spec.intent: 1.0})
    w = compat.get(p.kind)
    if w is None or p.kind == K.guide or p.score_hint < 0.2:
        return None
    if spec.intent != K.quote and not any(s.role == SlotRole.title for s in p.slots):
        return None  # every non-quote slide must show its title
    if p.kind == K.image_text and not has_image:
        return None  # an empty picture frame (or someone else's photo) is worse than another layout
    why = [f"{spec.intent.value}->{p.kind.value} x{w:.2f}"]
    s = 3.0 * w
    need = spec_items_count(spec)
    keep = None
    if p.kind in ITEM_KINDS:
        if need == 0:
            return None
        if p.n_items == need:
            s += 1.2
            why.append(f"items {need}=={p.n_items}")
        elif p.n_items > need and _removable(p) and p.n_items - need <= 2 and need >= 2 and p.source == "slide":
            # (layout-only patterns cannot hide items: renderers still draw the layout's empty frames)
            s += 0.2
            keep = need
            why.append(f"items {need}<{p.n_items} (hide {p.n_items - need})")
            cols = _row_length(p)
            if cols and need > cols and need % cols == 1:
                s -= 1.0  # 4 cards of a 3-column grid: three in a row and one alone below
                why.append(f"lone item in the last row ({need} in {cols} columns)")
        else:
            return None
    elif need > 0 and spec.intent in ITEM_KINDS:
        s -= 0.8  # items would be flattened into text
    # item structure: titles+texts need either two slots or a composite box
    if p.repeaters and spec.items:
        roles = set(p.repeaters[0].slot_roles)
        comp = any(sl.para_roles for sl in p.slots if sl.item_index == 0)
        has_titles = any(i.title for i in spec.items)
        has_texts = any(i.text for i in spec.items)
        if has_titles and has_texts and not comp and not (SlotRole.item_title in roles and SlotRole.item_text in roles):
            s -= 0.6
            why.append("packs title+text")
        if spec.intent == K.stats and SlotRole.number not in roles and not comp:
            s -= 1.0
            why.append("no number slot")
        first = [sl for sl in p.slots if sl.item_index == 0 and sl.kind == "text"]
        # item titles far longer than the template's title slot would be shrunk to unreadable
        caps = [sl.max_chars for sl in first if sl.role == SlotRole.item_title and not sl.para_roles and sl.max_chars]
        longest = max(len(i.title) for i in spec.items)
        if caps and longest > 1.3 * max(caps):
            s -= min(2.0, (longest / max(caps) - 1.3) * 1.5)
            why.append(f"item titles {longest}>{max(caps)} chars")
        # big per-item slots that this content leaves empty (value labels without values, ...)
        has_values = any(i.value for i in spec.items)
        empty = [sl for sl in first if not sl.para_roles and (
            (sl.role == SlotRole.label and not has_values) or (sl.role == SlotRole.item_text and not has_texts
                                                               and any(x.role == SlotRole.item_title for x in first)))]
        total = sum(sl.box.area for sl in first) or 1
        share = sum(sl.box.area for sl in empty) / total
        if share > 0.3:
            s -= 1.5 * share
            why.append(f"item slots left empty {share:.0%}")
        # cards with a heading and a body (two frames or one two-style frame), for items that bring
        # only one line (a title, or a text that then goes into the heading): every body stays empty
        if (comp or SlotRole.item_title in roles and SlotRole.item_text in roles) and not (has_titles and has_texts):
            s -= 2.0
            why.append("card bodies left empty")
    # a title longer than the example's: the part of the title frame that stays clear (before
    # background art, above a subtitle that starts inside it) may hold it only in smaller type
    # than on the other slides of the deck
    t_slot = next((sl for sl in p.slots if sl.role == SlotRole.title and sl.item_index is None and sl.kind == "text"), None)
    if t_slot is not None and t_slot.style.size and spec.title and spec.intent not in (K.quote,):
        tb = clear_title_box(t_slot.box, title_clear) or t_slot.box
        h = tb.h
        if spec.subtitle or spec.message:
            h = min(h, owned_height(t_slot, [sl for sl in p.slots if sl.kind == "text" and sl.item_index is None
                                             and sl.role in (SlotRole.subtitle, SlotRole.body)]))
        r = _title_ratio(spec.title, t_slot.style.font or "Arial", t_slot.style.size, t_slot.style.bold, tb.w, h)
        if r < 1.0:
            s -= {0.85: 0.4, 0.7: 0.9, 0.6: 1.4}.get(r, 2.0)
            why.append(f"title at {r:.0%} of its size" if r else "title does not fit")
    # free slots that the spec cannot fill leave holes after deletion
    fillable = {SlotRole.title, SlotRole.table, SlotRole.chart, SlotRole.icon, SlotRole.image, SlotRole.decor}
    if spec.subtitle or spec.message:
        fillable |= {SlotRole.subtitle, SlotRole.body}
    if spec.bullets or spec.quote:
        fillable.add(SlotRole.body)
    if spec.intent == K.quote and spec.quote_author:
        fillable |= {SlotRole.caption, SlotRole.label, SlotRole.person}
    holes = [sl for sl in p.slots if sl.item_index is None and sl.kind == "text" and sl.role not in fillable]
    # a divider's / closing slide's lead line (a pitch's call to action) must stay visible: without
    # a subtitle frame it takes a free caption-like frame, without one it would be lost
    if (spec.subtitle or spec.message) and spec.intent in (K.section, K.thanks) and not any(
            sl.kind == "text" and sl.item_index is None and sl.role in (SlotRole.subtitle, SlotRole.body) for sl in p.slots):
        spare = sorted([sl for sl in holes if sl.role in (SlotRole.person, SlotRole.caption, SlotRole.label)], key=lambda sl: -sl.box.area)
        if spare:
            holes.remove(spare[0])
            s -= 0.5  # a caption frame is a smaller stage for it than a subtitle
            why.append("lead line in a caption frame")
        else:
            s -= 2.0
            why.append("no frame for the lead line")
    if holes:
        s -= 0.25 * len(holes)
        why.append(f"{len(holes)} unfilled")
    # slots left empty on painted layout placeholders show as empty cards in some renderers
    painted = [sl for sl in p.slots if sl.painted]
    if painted and slide_area:
        n_items = keep if keep is not None else (need if p.kind in ITEM_KINDS else 0)
        has_values = any(i.value for i in spec.items)
        has_texts = any(i.text for i in spec.items) or bool(spec.bullets)

        def unused(sl) -> bool:
            if sl.kind == "picture":
                return not has_image
            if sl.item_index is not None:
                return (sl.item_index >= n_items or (sl.role == SlotRole.label and not has_values)
                        or (sl.role == SlotRole.item_text and not has_texts))
            return sl.role not in fillable

        share = sum(sl.box.area for sl in painted if unused(sl)) / slide_area
        if share > 0.01:
            s -= min(3.0, 12.0 * share)
            why.append(f"empty painted frames {share:.0%}")
    # photos baked into the layout show on every slide built on it, whatever it is about:
    # fine for a cover or a divider, off-topic next to content, and never twice in a deck
    if layout_photo > 0.15 and spec.intent not in (K.title, K.section, K.thanks):
        s -= 1.0 + 2.0 * layout_uses
        why.append(f"layout photos {layout_photo:.0%}" + (f", used x{layout_uses}" if layout_uses else ""))
    # photo frames stay empty (and are removed) without an image: the freed area is a hole too
    if not has_image and slide_area:
        pic = sum(sl.box.area for sl in p.slots if sl.kind == "picture" and sl.role == SlotRole.image
                  and sl.box.area > 0.04 * slide_area)
        if pic:
            share = min(pic / slide_area, 1.0)
            s -= min(3.0, 10.0 * share)
            why.append(f"empty photo area {share:.0%}")
    # text capacity vs content
    chars = spec_chars(spec) * variant.text_density
    cap = max(p.text_capacity, 1)
    if chars:
        r = chars / cap
        if r > 1.1:
            s -= min(2.5, (r - 1.1) * 2.0)
            why.append(f"tight {r:.1f}x")
        elif r < 0.15 and p.kind not in (K.title, K.section, K.thanks, K.quote):
            s -= 0.4
            why.append("sparse")
    bias = variant.kind_bias.get(p.kind.value, 1.0)
    s += math.log(max(bias, 0.05)) * 1.5
    s += 2.0 * (p.score_hint - 1.0)
    n_used = used.get(p.id, 0)
    if n_used:
        s -= 0.9 * n_used
        why.append(f"reused x{n_used}")
    if prev == p.id:
        s -= 1.5
    if p.source == "layout":
        s -= 0.15
    return Candidate(score=s, mode="clone", pattern=p, keep_items=keep, why=why)


def compose_candidates(spec: SlideSpec, variant: Variant, has_image: bool = False) -> list[Candidate]:
    out = []
    rep = variant.represent
    data_pref = rep.get("data", ["chart", "table", "kpi"])
    item_pref = rep.get("items", ["cards", "steps", "bullets"])

    def pref_bonus(lst, name):
        return (len(lst) - lst.index(name)) * 0.35 if name in lst else 0.0

    if spec.chart and spec.chart.series:
        data = [Candidate(3.2 + pref_bonus(data_pref, "chart"), "compose:chart", None, None, ["native chart from data"])]
        if len(spec.chart.categories) <= 7:
            data.append(Candidate(2.6 + pref_bonus(data_pref, "table"), "compose:table", None, None, ["chart data as table"]))
        if len(spec.chart.series) == 1 and len(spec.chart.categories) <= 4:
            data.append(Candidate(2.5 + pref_bonus(data_pref, "kpi"), "compose:kpi", None, None, ["few values as KPI tiles"]))
        # the variant's first available representation of series data wins (its identity)
        first = next((c for name in data_pref for c in data if c.mode == f"compose:{name}"), None)
        if first is not None:
            first.score += 1.0
            first.why.append("variant's data view")
        out += data
    if spec.table and spec.table.rows:
        out.append(Candidate(3.3 + pref_bonus(data_pref, "table"), "compose:table", None, None, ["native table"]))
    if spec.items:
        n = len(spec.items)
        if spec.intent == K.steps or spec.intent == K.agenda:
            out.append(Candidate(2.4 + pref_bonus(item_pref, "steps"), "compose:process", None, None, [f"{n}-step process diagram"]))
        if spec.intent == K.stats or all(i.value for i in spec.items):
            bonus = 1.2 if spec.intent == K.stats and n <= 4 else 0.0
            out.append(Candidate(2.3 + bonus + pref_bonus(data_pref, "kpi"), "compose:kpi", None, None, ["value tiles"]))
        out.append(Candidate(2.0 + pref_bonus(item_pref, "cards"), "compose:cards", None, None, [f"{n} icon cards"]))
        out.append(Candidate(1.6 + pref_bonus(item_pref, "bullets"), "compose:bullets", None, None, ["items as bullet list"]))
    if spec.bullets and not spec.items:
        out.append(Candidate(1.9 + pref_bonus(item_pref, "bullets"), "compose:bullets", None, None, ["bullet list"]))
    if spec.intent == K.image_text and variant.images and has_image:
        out.append(Candidate(2.2, "compose:image", None, None, ["picture + text"]))
    if spec.intent == K.quote and spec.quote:
        out.append(Candidate(2.6, "compose:quote", None, None, ["large quote in template type"]))
    if spec.intent in (K.section, K.thanks, K.title):
        out.append(Candidate(1.0, "compose:section", None, None, ["divider composed in template type"]))
    return out


def _chart_compatible(p: Pattern, spec: SlideSpec) -> bool:
    """A native chart example is reused only for the same chart family."""
    if p.kind != K.chart:
        return True
    if not spec.chart:
        return False
    kinds = " ".join(t for t in p.tags if t.startswith("chart:")).upper()
    fam = {"bar": ("BAR", "COLUMN"), "column": ("BAR", "COLUMN"), "line": ("LINE",), "pie": ("PIE", "DOUGHNUT"),
           "doughnut": ("PIE", "DOUGHNUT")}[spec.chart.type]
    return any(f in kinds for f in fam)


def _key(c: "Candidate") -> str:
    return c.pattern.id if c.pattern else c.mode


def select_layouts(plan: DeckPlan, profile: TemplateProfile, variant: Variant,
                   avoid: dict[str, set[str]] | None = None, images: set[str] | None = None) -> list[SlideLayout]:
    """`avoid` = choices other variants already made per slide: a near-equal alternative
    is preferred so the three variants differ visibly while staying on-template."""
    patterns = profile.usable_patterns()
    used: dict[str, int] = {}
    photo_uses = 0  # slides already on layouts with baked-in photos (collages often repeat across layouts)
    prev: str | None = None
    out: list[SlideLayout] = []
    avoid = avoid or {}
    images = images or set()
    for spec in plan.slides:
        has_image = spec.id in images
        cands: list[Candidate] = []
        for p in patterns:
            li = p.layout_index if p.layout_index is not None and 0 <= p.layout_index < len(profile.layouts) else None
            c = score_pattern(p, spec, variant, used, prev, has_image, profile.tokens.slide_w * profile.tokens.slide_h,
                              profile.layouts[li].photo_share if li is not None else 0.0, photo_uses,
                              profile.layouts[li].title_clear if li is not None else None)
            if c:
                cands.append(c)
        comp = compose_candidates(spec, variant, has_image)
        # structured data is always drawn natively (charts/tables from data)
        if spec.chart or spec.table:
            cands = [c for c in cands if c.pattern and c.pattern.kind in (K.table, K.chart) and "shape_table" not in c.pattern.tags
                     and "shape_chart" not in c.pattern.tags and _chart_compatible(c.pattern, spec)]
        cands += comp
        taken = avoid.get(spec.id, set()) if spec.intent not in (K.title, K.thanks) else set()  # one brand cover
        if taken and len(cands) > 1:
            for c in cands:
                if (spec.chart or spec.table) and c.pattern is None:
                    continue  # how data is drawn is each variant's own preference (represent.data)
                if _key(c) in taken:
                    c.score -= 1.3
                    c.why.append("taken by another variant")
        if not cands:
            cands = [Candidate(0.0, "compose:bullets", None, None, ["no compatible pattern: composed list"])]
        cands.sort(key=lambda c: (-c.score, c.pattern.id if c.pattern else c.mode))
        best = cands[0]
        alt = ", ".join(f"{(c.pattern.id if c.pattern else c.mode)}={c.score:.2f}" for c in cands[1:3])
        rationale = f"{best.mode}:{best.pattern.id if best.pattern else ''} score={best.score:.2f} [{'; '.join(best.why)}] alt: {alt}"
        if best.pattern:
            used[best.pattern.id] = used.get(best.pattern.id, 0) + 1
            bl = best.pattern.layout_index
            if bl is not None and 0 <= bl < len(profile.layouts) and profile.layouts[bl].photo_share > 0.15 \
                    and spec.intent not in (K.title, K.section, K.thanks):
                photo_uses += 1
            prev = best.pattern.id
            out.append(SlideLayout(spec_id=spec.id, mode="clone", pattern_id=best.pattern.id, keep_items=best.keep_items, rationale=rationale))
        else:
            prev = best.mode
            out.append(SlideLayout(spec_id=spec.id, mode="compose", compose_kind=best.mode.split(":", 1)[1], rationale=rationale))
    return out
