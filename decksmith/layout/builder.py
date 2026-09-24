"""Deck builder: applies layout decisions to a copy of the template.

clone   : duplicate the example slide, shrink its repeater, map content to slots
          by role, fit text in the template type scale, delete unused slots
compose : new slide on the template's canvas layout (title placeholder kept),
          native chart/table/diagram drawn in the content box found by CV

Nothing from the example slide survives unless it is either filled with new
content or is pure decoration (so no "Заголовок"/"Lorem ipsum" leftovers).
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.oxml.ns import qn

from decksmith.core.models import (
    Box,
    DeckPlan,
    Item,
    Pattern,
    PatternKind,
    SlideLayout,
    SlideSpec,
    Slot,
    SlotRole,
    TemplateProfile,
)
from decksmith.layout import compose as C
from decksmith.layout.pptx_ops import (
    delete_shape,
    delete_slides,
    duplicate_slide,
    fill_chart,
    fill_table,
    move_shape,
    replace_picture,
    scale_font_sizes,
    set_font_size,
    set_paragraphs,
    shape_by_id,
    table_has_merges,
)
from decksmith.layout.selector import Variant
from pptx.enum.text import MSO_ANCHOR

from decksmith.layout.textfit import fit_composite, fit_font_size, max_chars_for, measure, snap_down
from decksmith.parsing.ooxml import contrast_ratio, rel_luminance
from decksmith.parsing.template_parser import region_color, region_colors

log = logging.getLogger(__name__)
EMU_PT = 12700


@dataclass
class Overflow:
    slide: int
    shape_id: int
    role: str
    paragraphs: list[str]
    budget: int


@dataclass
class BuildReport:
    slides: list[dict] = field(default_factory=list)
    overflows: list[Overflow] = field(default_factory=list)


def _layouts(prs):
    return [layout for m in prs.slide_masters for layout in m.slide_layouts]


def _is_decorative_picture(shape) -> bool:
    """Transparent PNG art (3D objects, blobs) is brand decoration; opaque photos are content."""
    try:
        blob = shape.image.blob
        im = Image.open(io.BytesIO(blob))
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            a = im.convert("RGBA").getchannel("A").resize((32, 32))
            return a.getextrema()[0] < 200
    except Exception:
        return False
    return False


def number_format(sample: str, i: int) -> str | None:
    s = sample.strip()
    if re.fullmatch(r"0\d", s):
        return f"{i + 1:02d}"
    if re.fullmatch(r"\d{1,2}", s):
        return f"{i + 1}"
    return None


class DeckBuilder:
    def __init__(self, profile: TemplateProfile, variant: Variant):
        self.profile = profile
        self.variant = variant
        self.prs = Presentation(profile.file)
        self.n_src = len(self.prs.slides)
        self.src = list(self.prs.slides)
        self.layouts = _layouts(self.prs)
        self.report = BuildReport()
        self._bold_first: set[str] = set()
        self._inline_values = False
        self._bg_cache: dict[tuple, str | None] = {}
        self._clear_prompt_texts()

    def _clear_prompt_texts(self) -> None:
        """Sample text inside layout/master placeholders ("Click to edit", "Образец текста") is a
        prompt, not design. Some renderers (LibreOffice) draw it on every slide of that layout, so it
        is blanked in the output deck; runs/paragraphs stay, so inherited formatting is unchanged.
        Footer, date and slide-number placeholders keep their content."""
        keep = {"FOOTER", "DATE", "SLIDE_NUMBER"}
        for container in list(self.prs.slide_masters) + self.layouts:
            for ph in container.placeholders:
                try:
                    if str(ph.placeholder_format.type).split(".")[-1].split(" ")[0] in keep:
                        continue
                except Exception:
                    continue
                for t in ph._element.iter(qn("a:t")):
                    t.text = ""

    # ------------------------------------------------------------------ public
    def build(self, plan: DeckPlan, decisions: list[SlideLayout], images: dict[str, str] | None = None,
              icons: dict[str, list[str | None]] | None = None) -> BuildReport:
        images = images or {}
        icons = icons or {}
        specs = {s.id: s for s in plan.slides}
        for n, d in enumerate(decisions):
            spec = specs[d.spec_id]
            before = len(self.prs.slides)
            try:
                if d.mode == "clone" and d.pattern_id:
                    pat = self.profile.pattern(d.pattern_id)
                    slide = self._clone(pat)
                    self._fill_clone(slide, n, pat, spec, d, plan, images.get(spec.id), icons.get(spec.id))
                else:
                    slide = self._compose(n, spec, d.compose_kind or "bullets", images.get(spec.id), icons.get(spec.id))
            except Exception as e:  # never lose a slide: drop the half-built one, fall back to a composed list
                log.exception("slide %s failed (%s), falling back", spec.id, e)
                if len(self.prs.slides) > before:
                    delete_slides(self.prs, list(range(before, len(self.prs.slides))))
                slide = self._compose(n, spec, "bullets", None, None)
                d.rationale += f" | fallback after error: {e}"
            if spec.notes:
                slide.notes_slide.notes_text_frame.text = spec.notes
            for fld in slide._element.iter(qn("a:fld")):  # cached page numbers of cloned examples
                if fld.get("type") == "slidenum" and fld.find(qn("a:t")) is not None:
                    fld.find(qn("a:t")).text = str(n + 1)
            source = None
            kind = d.compose_kind or ""
            if d.mode == "clone" and d.pattern_id:
                pat = self.profile.pattern(d.pattern_id)
                source, kind = pat.slide_index, pat.kind.value
            if spec.intent.value in ("title", "section", "thanks", "quote", "contacts"):
                kind = spec.intent.value
            self.report.slides.append({"spec": spec.id, "mode": d.mode, "pattern": d.pattern_id or d.compose_kind,
                                       "source": source, "kind": kind, "rationale": d.rationale})
        delete_slides(self.prs, list(range(self.n_src)))
        return self.report

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(path))
        return path

    # ------------------------------------------------------------------ clone
    def _clone(self, pat: Pattern):
        if pat.source == "slide" and pat.slide_index is not None:
            return duplicate_slide(self.prs, self.src[pat.slide_index])
        return self._slide_from_layout(self.layouts[pat.layout_index])

    def _slide_from_layout(self, layout):
        """New slide whose placeholders carry explicit layout geometry.

        Some producers write every placeholder with the same idx (e.g. idx=0), so
        inheritance-by-idx would give the body the title's frame; pinning the
        geometry of each placeholder's own layout counterpart removes the ambiguity.
        Brand furniture repeated on the template's examples is copied on as well."""
        import copy as _copy

        slide = self.prs.slides.add_slide(layout)
        cloneable = list(layout.iter_cloneable_placeholders())
        sw, sh_ = self.profile.tokens.slide_w, self.profile.tokens.slide_h
        pad = int(0.01 * sw)
        # both sides in document order (slide.placeholders is sorted by idx, the clones are not)
        for sph, lph in zip([s for s in slide.shapes if s.is_placeholder], cloneable):
            try:
                x, y, w, h = lph.left, lph.top, lph.width, lph.height
                if None in (x, y, w, h):
                    continue
                # our text goes into these frames: keep them on the slide even if the layout bleeds
                x, y = max(int(x), 0), max(int(y), 0)
                w = min(int(w), sw - x - pad) if x + int(w) > sw else int(w)
                h = min(int(h), sh_ - y - pad) if y + int(h) > sh_ else int(h)
                sph.left, sph.top, sph.width, sph.height = x, y, max(w, pad), max(h, pad)
            except Exception:
                continue
        masters = list(self.prs.slide_masters)
        m_idx = next((i for i, m in enumerate(masters) if m.part is layout.slide_master.part), 0)
        for f in self.profile.brand_furniture:
            if f.get("master", 0) != m_idx or f["slide"] >= len(self.src):
                continue
            src_shape = shape_by_id(self.src[f["slide"]], f["shape_id"])
            if src_shape is not None:
                el = _copy.deepcopy(src_shape._element)
                cnv = el.find(".//" + qn("p:cNvPr"))
                if cnv is not None:
                    cnv.set("id", str(slide.shapes._next_shape_id))  # unique id on the new slide
                slide.shapes._spTree.append(el)
        return slide

    def _clear_title_box(self, layout_index: int | None, box: Box) -> Box | None:
        """Title frame shortened to end before background art that the layout draws inside
        the title band (a logo strip baked into the background, corner graphics).
        None when the frame is already clear or is not in that band."""
        layouts = self.profile.layouts
        if layout_index is None or not 0 <= layout_index < len(layouts):
            return None
        tc = layouts[layout_index].title_clear
        if tc is None or box.r <= tc.r:
            return None
        if min(box.b, tc.b) - max(box.y, tc.y) < 0.5 * min(box.h, tc.h):
            return None
        w = tc.r - box.x
        if w < 0.45 * box.w:
            return None
        return Box(x=box.x, y=box.y, w=w, h=box.h)

    def _shape(self, slide, pat: Pattern, slot: Slot):
        if pat.source == "layout":
            # placeholders inherit layout geometry: match by idx when unique, else by position
            phs = list(slide.placeholders)
            by_idx = [ph for ph in phs if ph.placeholder_format.idx == slot.placeholder_idx]
            if len(by_idx) == 1:
                return by_idx[0]

            def dist(ph):
                try:
                    return abs(ph.left - slot.box.x) + abs(ph.top - slot.box.y) + abs(ph.width - slot.box.w) + abs(ph.height - slot.box.h)
                except TypeError:
                    return 1 << 62
            best = min(phs, key=dist, default=None)
            if best is not None and dist(best) < 0.05 * (self.profile.tokens.slide_w + self.profile.tokens.slide_h):
                return best
            return None
        return shape_by_id(slide, slot.shape_id)

    def _reduce_repeater(self, slide, pat: Pattern, keep: int) -> None:
        rep = pat.repeaters[0]
        n = rep.n_items
        removed_ids = [sid for ids in rep.item_shape_ids[keep:] for sid in ids]
        for sid in removed_ids:
            sh = shape_by_id(slide, sid)
            if sh is not None:
                delete_shape(sh)
        # redistribute the remaining items over the original span (top-level shapes only)
        boxes = rep.item_boxes
        if rep.direction not in ("row", "column") or keep < 1:
            return
        shapes = [[shape_by_id(slide, sid) for sid in ids] for ids in rep.item_shape_ids[:keep]]
        if any(s is None or s._element.getparent().tag.endswith("grpSp") for ss in shapes for s in ss):
            return
        if rep.direction == "row":
            span0, span1 = boxes[0].x, boxes[n - 1].r
            w = boxes[0].w
            gap = (span1 - span0 - keep * w) / max(keep - 1, 1) if keep > 1 else 0
            for i in range(keep):
                nx = span0 + i * (w + gap) if keep > 1 else span0 + (span1 - span0 - w) / 2
                for s in shapes[i]:
                    move_shape(s, int(nx - boxes[i].x), 0)
        else:
            span0, span1 = boxes[0].y, boxes[n - 1].b
            h = boxes[0].h
            gap = (span1 - span0 - keep * h) / max(keep - 1, 1) if keep > 1 else 0
            gap = min(gap, h * 0.6)
            for i in range(keep):
                ny = span0 + i * (h + gap)
                for s in shapes[i]:
                    move_shape(s, 0, int(ny - boxes[i].y))

    def _fill_clone(self, slide, n: int, pat: Pattern, spec: SlideSpec, d: SlideLayout, plan: DeckPlan,
                    image: str | None, icons: list[str | None] | None) -> None:
        self._bold_first = set()
        rep = pat.repeaters[0] if pat.repeaters else None
        items = list(spec.items)
        if rep and not items and spec.bullets:
            items = [Item(title=b) for b in spec.bullets]
        keep = d.keep_items if d.keep_items else (rep.n_items if rep else 0)
        if rep and keep < rep.n_items:
            self._reduce_repeater(slide, pat, keep)
        is_cover = spec.intent == PatternKind.title  # a cover pattern reused for a divider keeps the divider's text
        body_queue: list[str] = list(spec.bullets) if not rep else []
        free_numbers = [s for s in pat.slots if s.role == SlotRole.number and s.item_index is None]
        free_texts_for_numbers = items[len([1 for _ in range(keep)]):] if rep else items
        stat_units = list(free_texts_for_numbers) if free_numbers else []
        fills: dict[str, tuple[list[str], list[int] | None]] = {}
        body_slots = sorted([s for s in pat.slots if s.role == SlotRole.body and s.item_index is None], key=lambda s: (s.box.y, s.box.x))

        # which text slots does each item offer? (to pack title+text when only one exists)
        item_text_slots: dict[int, list[Slot]] = {}
        for s in pat.slots:
            if s.kind == "text" and s.item_index is not None and s.role not in (SlotRole.number, SlotRole.label):
                item_text_slots.setdefault(s.item_index, []).append(s)
        is_quote = pat.kind == PatternKind.quote and bool(spec.quote)
        # items with values (KPI) on a pattern without a number slot keep the value in their title
        self._inline_values = not any(s.role == SlotRole.number and s.item_index is not None for s in pat.slots) and not any(
            s.para_roles and s.para_roles[0] == SlotRole.number for s in pat.slots)
        used_item_roles: set[tuple[int, str]] = set()
        used_item_texts: dict[int, set[str]] = {}
        used_texts: set[str] = set()

        def once(txt: str) -> str:
            """Each piece of free text lands on the slide at most once."""
            if not txt or txt in used_texts:
                return ""
            used_texts.add(txt)
            return txt

        for slot in sorted(pat.slots, key=lambda s: (s.box.y, s.box.x)):
            if slot.kind != "text" or slot.role == SlotRole.decor:
                continue
            if slot.item_index is not None:
                if slot.item_index >= keep:
                    continue
                it = items[slot.item_index] if slot.item_index < len(items) else None
                if it is None:
                    continue
                key = (slot.item_index, slot.role.value if not slot.para_roles else "composite")
                if key in used_item_roles:
                    continue  # e.g. timeline text duplicated above/below the axis
                used_item_roles.add(key)
                only = len(item_text_slots.get(slot.item_index, [])) == 1 and not slot.para_roles
                if only and slot.role not in (SlotRole.number, SlotRole.label) and it.title and it.text:
                    fills[slot.id] = ([self._head(it), it.text], [0, 0])
                    self._bold_first.add(slot.id)
                else:
                    paras, tidx = self._item_text(slot, it, slot.item_index)
                    # an item without a title must not show its text twice (title slot + text slot)
                    item_seen = used_item_texts.setdefault(slot.item_index, set())
                    if paras and all(p in item_seen for p in paras if p):
                        continue
                    item_seen.update(p for p in paras if p)
                    fills[slot.id] = (paras, tidx)
                continue
            role = slot.role
            if role == SlotRole.title:
                if is_quote:
                    fills[slot.id] = ([spec.quote], None)
                else:
                    fills[slot.id] = ([plan.title if is_cover else spec.title], None)
            elif role == SlotRole.subtitle:
                txt = once((plan.subtitle if is_cover else "") or spec.subtitle) or once(spec.message)
                if txt:
                    fills[slot.id] = ([txt], None)
            elif role == SlotRole.number and stat_units:
                it = stat_units.pop(0)
                fills[slot.id] = ([it.value or it.title], None)
                # pair with nearest caption-like text slot below the number
                cap = self._nearest_below(slot, pat, fills)
                if cap is not None and (it.text or it.title):
                    fills[cap.id] = ([it.text or it.title], None)
            elif role == SlotRole.body:
                if slot.para_roles:
                    # composite box: styled lead paragraph + body paragraphs
                    rest = list(body_queue[: self.variant.max_bullets]) if body_queue else ([m] if (m := once(spec.message)) else [])
                    head = once(spec.subtitle) if rest else ""
                    if body_queue:
                        body_queue = []
                    if rest:
                        paras = ([head] if head else []) + rest
                        fills[slot.id] = (paras, ([0] if head else []) + [1] * len(rest))
                    continue
                if len(body_slots) > 1 and body_queue:
                    # distribute bullets across several body boxes in reading order
                    k = body_slots.index(slot)
                    per = -(-len(spec.bullets) // len(body_slots))
                    chunk = spec.bullets[k * per:(k + 1) * per]
                    if chunk:
                        fills[slot.id] = (chunk, None)
                elif body_queue:
                    fills[slot.id] = (body_queue[: self.variant.max_bullets], None)
                    body_queue = []
                elif is_quote and spec.quote_author:
                    if once(spec.quote_author):
                        fills[slot.id] = ([spec.quote_author], None)
                elif spec.quote and not is_quote:
                    if once(spec.quote):
                        fills[slot.id] = ([spec.quote], None)
                else:
                    txt = once(spec.message) or once((plan.subtitle if is_cover else "") or spec.subtitle)
                    if txt:
                        fills[slot.id] = ([txt], None)
            elif role in (SlotRole.caption, SlotRole.label, SlotRole.person) and is_quote and spec.quote_author:
                if once(spec.quote_author):
                    fills[slot.id] = ([spec.quote_author], None)

        # quote layouts without a title slot: the quote goes to the largest text frame
        if is_quote and not any(spec.quote in paras for paras, _ in fills.values()):
            frames = sorted([s for s in pat.slots if s.kind == "text" and s.role != SlotRole.decor and s.item_index is None],
                            key=lambda s: -s.box.area)
            if frames:
                displaced = fills.get(frames[0].id)
                fills[frames[0].id] = ([spec.quote], None)
                if displaced and spec.quote_author in displaced[0] and len(frames) > 1:
                    fills[frames[1].id] = ([spec.quote_author], None)

        # apply text + fit
        deleted: set[str] = set()
        for slot in pat.slots:
            if slot.kind != "text":
                continue
            if slot.item_index is not None and slot.item_index >= keep:
                continue
            sh = self._shape(slide, pat, slot)
            if sh is None:
                continue
            if slot.id not in fills or not any(p.strip() for p in fills[slot.id][0] if p):
                delete_shape(sh)
                deleted.add(slot.id)
                continue
            paras, tidx = fills[slot.id]
            paras = [p for p in paras if p is not None]
            set_paragraphs(sh, paras, tidx)
            if slot.id in self._bold_first:
                runs = sh.text_frame.paragraphs[0].runs
                if runs:
                    runs[0].font.bold = True
            fit_slot = slot
            if slot.role == SlotRole.title and slot.item_index is None:
                clear = self._clear_title_box(pat.layout_index, slot.box)
                if clear is not None:
                    x, y, h = sh.left, sh.top, sh.height  # pin all four: placeholders may inherit
                    sh.left, sh.top, sh.width, sh.height = x, y, clear.w, h
                    fit_slot = slot.model_copy(update={"box": clear})
            self._fit(slide, n, sh, fit_slot, paras)
            if slot.id in pat.backdrops:
                self._fit_backdrop(slide, pat.backdrops[slot.id], sh, fit_slot, paras)
            else:
                self._ensure_contrast(sh, slot, pat)
        # pictures
        pics = [s for s in pat.slots if s.kind == "picture" and (s.item_index is None or s.item_index < keep)]
        content_pics = sorted([s for s in pics if s.role == SlotRole.image], key=lambda s: -s.box.area)
        used_image = False
        for slot in content_pics:
            sh = self._shape(slide, pat, slot)
            if sh is None:
                continue
            if image and not used_image and slot.box.area > 0.04 * self.profile.tokens.slide_w * self.profile.tokens.slide_h:
                try:
                    if sh.is_placeholder and not hasattr(sh, "image"):
                        sh.insert_picture(image)
                    else:
                        replace_picture(slide, sh, image)
                    used_image = True
                    continue
                except Exception as e:
                    log.warning("image replace failed: %s", e)
            if getattr(sh, "is_placeholder", False) and sh.shape_type != 13:
                delete_shape(sh)  # empty picture placeholder
                deleted.add(slot.id)
            elif not _is_decorative_picture(sh):
                delete_shape(sh)
                deleted.add(slot.id)
        for slot in pics:
            if slot.role == SlotRole.icon and icons and slot.item_index is not None and slot.item_index < len(icons):
                ic = icons[slot.item_index]
                sh = self._shape(slide, pat, slot)
                if ic and sh is not None and hasattr(sh, "image"):
                    try:
                        replace_picture(slide, sh, ic, mode="contain")
                    except Exception:
                        pass
        # frames/avatars that only served a removed slot go too
        keep_ids = {s.shape_id for s in pat.slots if s.id not in deleted}
        by_container: dict[int, list[str]] = {}
        for sid, cids in pat.containers.items():
            for c in cids:
                by_container.setdefault(c, []).append(sid)
        for cid, sids in by_container.items():
            if cid in keep_ids:
                continue
            if all(s in deleted for s in sids):
                sh = shape_by_id(slide, cid)
                if sh is not None:
                    delete_shape(sh)
        # native tables / charts of the example: refill with our data in the template's style
        t = self.profile.tokens
        for slot in [s for s in pat.slots if s.kind in ("table", "chart")]:
            sh = self._shape(slide, pat, slot)
            if sh is None:
                continue
            table = spec.table or (_chart_as_table(spec).model_copy() if spec.chart and slot.kind == "table" else None)
            try:
                if slot.kind == "table" and table and not table_has_merges(sh):
                    fill_table(sh, table.columns[:5], [r[:5] for r in table.rows[:6]], t.slide_h - t.margins.bottom)
                    continue
                if slot.kind == "chart" and spec.chart and spec.chart.series:
                    fill_chart(sh, spec.chart.categories[:8], [(s.name, s.values) for s in spec.chart.series], spec.chart.unit)
                    continue
            except Exception as e:  # unusual chart/table XML: draw natively in the same box instead
                log.warning("native refill failed (%s), redrawing", e)
            box = slot.box
            delete_shape(sh)
            st = self._style_for(slide_bg=self.profile.tokens.background_hex)
            if slot.kind == "chart" and spec.chart:
                C.draw_chart(slide, box, spec.chart, st)
            elif table:
                C.draw_table(slide, box, table, st)
        # leftover empty placeholders (python-pptx adds none for clones, layouts may keep some)
        for ph in list(slide.placeholders):
            if ph.has_text_frame and not ph.text_frame.text.strip():
                delete_shape(ph)

    def _ensure_contrast(self, sh, slot: Slot, pat: Pattern) -> None:
        """A slot whose own colour is unreadable on what the template renders under it
        (below 3:1, e.g. a purple heading on a purple card) gets the most readable of the
        template's text colours. Moderate cases stay as designed; the audit reports them."""
        col = slot.style.color_hex
        if not col or not pat.thumbnail or not Path(pat.thumbnail).exists():
            return
        key = (pat.id, slot.box.x, slot.box.y, slot.box.w, slot.box.h)
        if key not in self._bg_cache:
            try:
                t = self.profile.tokens
                self._bg_cache[key] = region_color(Path(pat.thumbnail), slot.box, t.slide_w, t.slide_h)
            except Exception:
                self._bg_cache[key] = None
        bg = self._bg_cache[key]
        if bg is None or contrast_ratio(col, bg) >= 3.0:  # WCAG minimum for large text
            return
        own = [s.style.color_hex for s in pat.slots if s.style.color_hex and s.style.color_hex != col]
        cands = own + [self.profile.tokens.text_hex] + [c.hex for c in self.profile.tokens.palette[:8]]
        best = max(cands, key=lambda c: contrast_ratio(c, bg), default=None)
        if best is None or contrast_ratio(best, bg) < 4.5:
            best = "FFFFFF" if rel_luminance(bg) < 0.4 else "000000"
        for r in sh._element.iter(qn("a:r")):
            rpr = r.find(qn("a:rPr"))
            if rpr is None:
                rpr = etree.Element(qn("a:rPr"))
                r.insert(0, rpr)
            for old_fill in rpr.findall(qn("a:solidFill")):
                rpr.remove(old_fill)
            fill = etree.Element(qn("a:solidFill"))
            etree.SubElement(fill, qn("a:srgbClr")).set("val", best)
            # schema order: ln? then fills before effects/latin/ea/cs
            ln = rpr.find(qn("a:ln"))
            rpr.insert(list(rpr).index(ln) + 1 if ln is not None else 0, fill)

    def _fit_backdrop(self, slide, backdrop_id: int, sh, slot: Slot, paras: list[str]) -> None:
        """Resize the badge behind a title to the new text: same padding as in the example,
        never wider than the (clear) title frame."""
        bd = shape_by_id(slide, backdrop_id)
        if bd is None or bd.width is None:
            return
        rpr = sh._element.find(".//" + qn("a:rPr"))
        size = int(rpr.get("sz")) / 100 if rpr is not None and rpr.get("sz") else (slot.style.size or self.profile.tokens.type_scale.title)
        font = slot.style.font or self.profile.tokens.heading_font
        m = measure(paras, font, size, slot.box.w, slot.style.bold)
        x0, pad = int(bd.left), max(int(sh.left) - int(bd.left), 0)
        right = min(int(sh.left) + m.width_emu + pad, slot.box.r + pad)
        bd.width = max(right - x0, 2 * pad + int(0.05 * self.profile.tokens.slide_w))
        if m.lines > 1:  # a wrapped title keeps its plate under every line
            bd.height = max(int(bd.height), m.height_emu + 2 * max(int(sh.top) - int(bd.top), 0))

    def _nearest_below(self, num: Slot, pat: Pattern, fills) -> Slot | None:
        best, bd = None, None
        for s in pat.slots:
            if s.kind != "text" or s.id in fills or s.role in (SlotRole.title, SlotRole.number) or s.item_index is not None:
                continue
            if s.box.y < num.box.y + num.box.h * 0.5:
                continue
            dx = abs(s.box.cx - num.box.cx) + abs(s.box.x - num.box.x)
            dy = s.box.y - num.box.b
            dist = dx + max(dy, 0) * 2
            if bd is None or dist < bd:
                best, bd = s, dist
        return best

    def _head(self, it: Item) -> str:
        """Item heading; the value leads it when the pattern has nowhere else to show numbers."""
        if it.value and it.title and self._inline_values:
            return f"{it.value} {it.title}"
        return it.title or it.value

    def _item_text(self, slot: Slot, it: Item, i: int) -> tuple[list[str], list[int] | None]:
        if slot.para_roles:
            head_role = slot.para_roles[0]
            if head_role == SlotRole.number:
                head = it.value or number_format(slot.sample_text.split("\n")[0], i) or f"{i + 1}"
                body = it.text or it.title
            else:
                head = self._head(it)
                body = it.text
            return ([head, body], [0, 1]) if body else ([head], [0])
        role = slot.role
        if role == SlotRole.number:
            fmt = number_format(slot.sample_text, i)
            return ([it.value or fmt or f"{i + 1}"] if (it.value or fmt) else [it.title], None)
        if role == SlotRole.item_title:
            return ([self._head(it) or it.text], None)
        if role == SlotRole.item_text:
            return ([it.text or it.title or it.value], None)
        if role == SlotRole.label:
            return ([it.value], None) if it.value else ([], None)
        return ([it.text or it.title], None)

    def _fit(self, slide, n: int, sh, slot: Slot, paras: list[str]) -> None:
        if not paras or not any(p.strip() for p in paras):
            return
        size = slot.style.size or self.profile.tokens.type_scale.body
        font = slot.style.font or self.profile.tokens.body_font
        # titles must stay inside their own frame (they sit right above content);
        # other auto-growing boxes may use the free room below them
        if slot.role in (SlotRole.title, SlotRole.subtitle) or not slot.autofit:
            avail_h = slot.box.h
        else:
            avail_h = max(slot.box.h, int(slot.max_lines * size * 1.2 * EMU_PT + 91440))
        box_w = slot.box.w
        if slot.para_roles:
            # multi-style box: shrink all paragraphs by one factor, keeping their size ratios
            sizes = [ps.size or size for ps in slot.para_styles] or [size]
            tidx = [0] + [1] * (len(paras) - 1)
            f = fit_composite(paras, [sizes[min(i, len(sizes) - 1)] for i in tidx], font, box_w, avail_h)
            if f < 0.99:
                scale_font_sizes(sh, f, default_size=size)
            return
        ts = self.profile.tokens.type_scale
        allowed = ts.sizes
        # oversized placeholder defaults (e.g. 32 pt body) may shrink down to the template body size
        floor_ratio = 0.7 if size <= 1.3 * ts.body else max(ts.body * 0.85 / size, 0.35)
        if slot.role == SlotRole.title:
            floor_ratio = min(floor_ratio, 0.6)
        fitted = fit_font_size(paras, font, size, box_w, avail_h, slot.style.bold, floor_ratio, allowed)
        if fitted is None:
            min_size = snap_down(max(size * floor_ratio, ts.caption), allowed)
            set_font_size(sh, min_size)
            self.report.overflows.append(Overflow(slide=n, shape_id=sh.shape_id, role=slot.role.value, paragraphs=paras,
                                                  budget=max_chars_for(box_w, avail_h, font, min_size, slot.style.bold)))
        elif fitted < size - 0.05:
            set_font_size(sh, fitted)

    # ---------------------------------------------------------------- compose
    def _canvas(self) -> tuple[int, bool]:
        prof = self.profile
        tone = self.variant.tone
        if tone == "auto":
            tone = "dark" if prof.tokens.dark_background else "light"
        idx = prof.canvas_layouts.get(tone)
        if idx is None:
            idx = next(iter(prof.canvas_layouts.values()), 0)
        return idx, prof.layouts[idx].dark

    def _region_colors(self, layout_info, region: Box | None) -> list[str]:
        """Colours the layout renders under `region` (several for gradients and glows)."""
        if region is None or not layout_info.thumbnail or not Path(layout_info.thumbnail).exists():
            return []
        try:
            return region_colors(Path(layout_info.thumbnail), region, self.profile.tokens.slide_w, self.profile.tokens.slide_h)
        except Exception:
            return []

    def _style_for(self, slide_bg: str, samples: list[str] | None = None) -> C.Style:
        return C.Style(tokens=self.profile.tokens, bg=slide_bg, dark=rel_luminance(slide_bg) < 0.4,
                       bg_samples=tuple(samples or ()))

    def _title_style(self):
        """Dominant title style of the template's content examples (for layouts without a title placeholder)."""
        from collections import Counter

        styles = Counter()
        for p in self.profile.usable_patterns():
            if p.kind.value in ("title", "section", "thanks", "quote"):
                continue
            for s in p.slots:
                if s.role == SlotRole.title and s.style.size:
                    styles[(s.style.font, s.style.size, s.style.bold, s.style.color_hex)] += 1
        return styles.most_common(1)[0][0] if styles else (None, None, False, None)

    def _compose_section(self, spec: SlideSpec, plan_title: str | None = None):
        """Divider / closing slide when the template has no such example: large title on the
        canvas layout, accent rule, subtitle — all in template tokens."""
        t = self.profile.tokens
        idx, _ = self._canvas()
        layout_info = self.profile.layouts[idx]
        slide = self._slide_from_layout(self.layouts[idx])
        for ph in list(slide.placeholders):
            delete_shape(ph)
        bg = layout_info.content_bg_hex or layout_info.background_hex or t.background_hex
        m = t.margins
        # the divider sits in the layout's free area found by CV (clear of background art), else mid-slide
        region = Box(x=m.left, y=int(0.18 * t.slide_h), w=t.slide_w - m.left - m.right, h=int(0.7 * t.slide_h))
        cb = layout_info.content_box
        if cb is not None and cb.area >= 0.15 * t.slide_w * t.slide_h:
            x0, y0 = max(cb.x, m.left), max(cb.y, m.top)
            x1, y1 = min(cb.r, t.slide_w - m.right), min(cb.b, t.slide_h - m.bottom)
            if x1 - x0 > 0.3 * t.slide_w and y1 - y0 > 0.25 * t.slide_h:
                region = Box(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
        st = self._style_for(bg, self._region_colors(layout_info, region))
        title = plan_title or spec.title
        size = snap_down(min(t.type_scale.title * 1.3, t.type_scale.number), t.type_scale.sizes)
        box = Box(x=region.x, y=region.y + int(0.1 * region.h), w=int(region.w * 0.9), h=int(0.42 * region.h))
        C.add_text(slide, box, [title], st, role="title", size=size, anchor="bottom", bold=True, name="Title")
        rule_y = box.b + int(0.04 * region.h)
        C.add_line(slide, region.x + 45720, rule_y, region.x + int(region.w * 0.18), rule_y, st.accent, 3.0, name="Accent rule")
        sub = spec.subtitle or spec.message
        if sub:
            C.add_text(slide, Box(x=region.x, y=box.b + int(0.09 * region.h), w=int(region.w * 0.85), h=int(0.3 * region.h)),
                       [sub], st, role="subtitle", color=st.muted, name="Subtitle")
        return slide

    def _compose(self, n: int, spec: SlideSpec, kind: str, image: str | None, icons: list[str | None] | None):
        if kind == "section":
            return self._compose_section(spec)
        prof = self.profile
        t = prof.tokens
        idx, dark = self._canvas()
        layout_info = prof.layouts[idx]
        slide = self._slide_from_layout(self.layouts[idx])
        title_ph = None
        for ph in list(slide.placeholders):
            pt = str(ph.placeholder_format.type)
            if "TITLE" in pt and title_ph is None:
                title_ph = ph
            else:
                delete_shape(ph)
        bg = layout_info.content_bg_hex or layout_info.background_hex or t.background_hex
        st = self._style_for(bg)
        region = layout_info.content_box or t.content_box or Box(x=t.margins.left, y=int(0.22 * t.slide_h),
                                                                  w=t.slide_w - t.margins.left - t.margins.right,
                                                                  h=int(0.68 * t.slide_h))
        tb = layout_info.title_box or t.title_box or Box(x=t.margins.left, y=t.margins.top,
                                                         w=t.slide_w - t.margins.left - t.margins.right, h=int(0.13 * t.slide_h))
        allowed = t.type_scale.sizes
        if title_ph is not None:
            title_ph.text_frame.text = spec.title
            tb = Box(x=int(title_ph.left), y=int(title_ph.top), w=int(title_ph.width), h=int(title_ph.height))
            clear = self._clear_title_box(idx, tb)
            if clear is not None:
                title_ph.left, title_ph.top, title_ph.width, title_ph.height = tb.x, tb.y, clear.w, tb.h
                tb = clear
            size, font, bold = t.type_scale.title, t.heading_font, False
        else:
            tb = self._clear_title_box(idx, tb) or tb
            font, size, bold, color = self._title_style()
            font, size = font or t.heading_font, size or t.type_scale.title
        # the title must fit its own frame: overflowing titles grow over content or off the slide
        fitted = fit_font_size([spec.title], font, size, tb.w, tb.h, bold, 0.6, allowed)
        overflow = fitted is None
        if overflow:
            fitted = snap_down(size * 0.6, allowed)
        if title_ph is not None:
            if fitted < size - 0.05:
                set_font_size(title_ph, fitted)
            anchor_bottom = title_ph.text_frame.vertical_anchor == MSO_ANCHOR.BOTTOM
            title_id = title_ph.shape_id
        else:
            tcolor = color if color and C.contrast_ratio(color, bg) >= 3 else st.text
            shp = C.add_text(slide, tb, [spec.title], st, role="title", color=tcolor, bold=bold, size=fitted, font=font,
                             fit=False, name="Title")
            anchor_bottom = False
            title_id = shp.shape_id
        if overflow:  # shortened by the LLM step (shortener skill) when a model is configured
            self.report.overflows.append(Overflow(slide=n, shape_id=title_id, role="title", paragraphs=[spec.title],
                                                  budget=max_chars_for(tb.w, tb.h, font, fitted, bold)))
        need = measure([spec.title], font, fitted, tb.w, bold).height_emu
        title_bottom = tb.b if anchor_bottom else max(tb.b, min(tb.y + need, tb.b + need // 2))
        m = t.margins
        # keep region inside margins and below the (possibly two-line) title;
        # left edge aligned with the title frame so text starts on one guide
        on_title_guide = bool(tb and abs(tb.x - region.x) < 0.06 * t.slide_w)
        if on_title_guide:
            region = Box(x=tb.x, y=region.y, w=region.r - tb.x, h=region.h)
        x0 = region.x if on_title_guide else max(region.x, m.left)
        y0 = max(region.y, title_bottom + int(0.025 * t.slide_h))
        x1 = min(region.r, t.slide_w - m.right)
        # bottom: above the layout's footer / date / number zone, with a small safe area
        foot = [Box(**ph["box"]) for ph in layout_info.placeholders
                if ph.get("type") in ("FOOTER", "DATE", "SLIDE_NUMBER") and ph.get("box")]
        foot_top = min((b.y for b in foot if b.y > 0.75 * t.slide_h and b.h > 0), default=t.slide_h)
        y1 = min(region.b, t.slide_h - m.bottom, foot_top - int(0.015 * t.slide_h), int(0.94 * t.slide_h))
        if x1 - x0 < 0.3 * t.slide_w:  # only a sliver is free: use the width between margins
            x0, x1 = m.left, t.slide_w - m.right
        if y1 - y0 < 0.3 * t.slide_h:  # never push content below the slide: take the area under the title
            y1 = t.slide_h - m.bottom
            y0 = max(title_bottom + int(0.02 * t.slide_h), min(y0, y1 - int(0.3 * t.slide_h)))
        region = Box(x=x0, y=y0, w=max(x1 - x0, 1), h=max(y1 - y0, 1))
        st = self._style_for(bg, self._region_colors(layout_info, region))
        if layout_info.textured:
            # busy background: a quiet plate in template colours keeps text legible
            pad = int(0.02 * t.slide_w)
            plate_color = C.mix(t.background_hex if rel_luminance(t.background_hex) > 0.5 else "FFFFFF", bg, 0.08) \
                if rel_luminance(bg) > 0.35 else C.mix("000000", bg, 0.15)
            C.add_rect(slide, Box(x=region.x - pad, y=region.y - pad, w=region.w + 2 * pad, h=region.h + 2 * pad), plate_color,
                       rounded=True, name="Plate")
            st = self._style_for(plate_color)
        lead = spec.subtitle or (spec.message if kind != "bullets" else "")
        if lead:  # sized to its text (at body size), between 12% and 30% of the region
            need = measure([lead], t.body_font, t.type_scale.body, region.w).height_emu
            lh = min(max(need, int(region.h * 0.12)), int(region.h * 0.3))
            C.add_text(slide, Box(x=region.x, y=region.y, w=region.w, h=lh), [lead], st, role="body", color=st.muted,
                       name="Lead")
            region = Box(x=region.x, y=region.y + lh + int(region.h * 0.03), w=region.w, h=region.h - lh - int(region.h * 0.03))
        items = spec.items or [Item(title=b) for b in spec.bullets]
        if kind == "chart" and spec.chart:
            C.draw_chart(slide, region, spec.chart, st)
        elif kind == "table" and (spec.table or spec.chart):
            table = spec.table or _chart_as_table(spec)
            C.draw_table(slide, region, table, st)
        elif kind == "kpi":
            kitems = items
            if spec.chart and not spec.items:
                s0 = spec.chart.series[0]
                kitems = [Item(value=_fmt_num(v, spec.chart.unit), text=c) for c, v in zip(spec.chart.categories, s0.values)]
            C.draw_kpis(slide, region, kitems, st)
        elif kind == "process" and items:
            C.draw_process(slide, region, items, st)
        elif kind == "cards" and items:
            C.draw_cards(slide, region, items, st, icons)
        elif kind == "image":
            C.draw_image_text(slide, region, image, spec.bullets or [spec.message], st)
        elif kind == "quote":
            C.draw_quote(slide, region, spec.quote or spec.message, spec.quote_author, st)
        else:
            bullets = spec.bullets or [_item_line(i) for i in spec.items] or [spec.message]
            C.draw_bullets(slide, region, bullets, st, self.variant.max_bullets)
        return slide


def _item_line(it: Item) -> str:
    """One bullet for an item: value first (a KPI must not lose its number), then title: text."""
    head = " ".join(x for x in (it.value, it.title) if x)
    return f"{head}: {it.text}" if head and it.text else (head or it.text)


def _fmt_num(v: float, unit: str) -> str:
    s = f"{v:,.0f}".replace(",", " ") if abs(v) >= 100 or float(v).is_integer() else f"{v:.1f}".replace(".", ",")
    return f"{s}{unit if unit in ('%', '₽', '$', '€') else (' ' + unit if unit else '')}"


def _chart_as_table(spec: SlideSpec):
    from decksmith.core.models import TableSpec

    ch = spec.chart
    cols = [ch.x_title or ""] + [s.name + (f", {ch.unit}" if ch.unit else "") for s in ch.series]
    rows = [[c] + [_fmt_num(s.values[i], "") if i < len(s.values) else "" for s in ch.series] for i, c in enumerate(ch.categories)]
    return TableSpec(columns=cols, rows=rows)
