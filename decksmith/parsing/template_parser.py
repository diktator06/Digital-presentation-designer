"""Template analysis orchestrator: .pptx -> TemplateProfile (cached by sha256).

Stages (all deterministic):
  1. structure  : masters, layouts, placeholders, themes
  2. render     : template slides + an "empty layout" probe deck via LibreOffice
  3. CV         : background luminance / color per slide, free content area per
                  layout from an occupancy grid of the rendered empty layout
  4. patterns   : per example slide (+ placeholder-rich layouts)
  5. tokens     : palette, typography scale, margins, grid
  6. brand      : logos / footers / page numbers fixed by masters & layouts
  7. assets     : icon libraries harvested from guide slides

An optional contextual stage (VLM labelling of patterns) lives in
generation/labeler.py and only refines `Pattern.kind`/tags.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat
from pptx import Presentation

from decksmith.core.config import settings
from decksmith.core.fonts import ensure_font
from decksmith.core.models import (
    Box,
    BrandElement,
    IconAsset,
    LayoutInfo,
    Pattern,
    PatternKind,
    TemplateProfile,
)
from decksmith.parsing.elements import Element, extract_elements
from decksmith.parsing.ooxml import Theme, color_distance, parse_theme, rgb_to_hex
from decksmith.parsing.patterns import build_pattern
from decksmith.parsing.tokens import build_tokens
from decksmith.render.soffice import RenderError, pdf_to_pngs, pptx_to_pdf, soffice_path

log = logging.getLogger(__name__)


def _parser_version() -> str:
    """Cache key: changes whenever any parsing module changes."""
    h = hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.read_bytes())
    return "0.4-" + h.hexdigest()[:10]


PARSER_VERSION = _parser_version()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def all_layouts(prs) -> list[tuple[int, int, object]]:
    out = []
    for mi, m in enumerate(prs.slide_masters):
        for layout in m.slide_layouts:
            out.append((len(out), mi, layout))
    return out


def layout_global_index(prs, layout) -> int:
    for gi, _, lay in all_layouts(prs):
        if lay.part is layout.part:
            return gi
    return -1


# ----------------------------------------------------------------------------
# CV helpers
# ----------------------------------------------------------------------------
def image_stats(png: Path) -> tuple[bool, str, float]:
    """(is_dark, border background hex, mean luminance) of a rendered slide."""
    im = Image.open(png).convert("RGB")
    small = im.resize((160, 90))
    lum = ImageStat.Stat(small.convert("L")).mean[0] / 255
    w, h = small.size
    ring = []
    px = small.load()
    for x in range(w):
        ring += [px[x, 1], px[x, h - 2]]
    for y in range(h):
        ring += [px[1, y], px[w - 2, y]]
    ring.sort(key=lambda c: sum(c))
    med = ring[len(ring) // 2]
    return lum < 0.42, rgb_to_hex(med), round(lum, 3)


def free_content_box(png: Path, slide_w: int, slide_h: int, below_y: int, margins, bg_hex: str) -> tuple[Box | None, bool]:
    """Largest empty rectangle below `below_y` on a rendered empty layout -> (box, textured).

    Occupancy = strong edges or colors far from the background, on a 64x36
    grid; the maximal all-free rectangle is found with the histogram method.
    A background that is busy almost everywhere (photo, texture, blueprint grid)
    is reported as `textured`: then the whole area under the title is usable and
    the composer puts content on a plate for legibility.
    """
    gw, gh = 64, 36
    im = Image.open(png).convert("RGB")
    small = im.resize((gw * 4, gh * 4))
    edges = small.convert("L").filter(ImageFilter.FIND_EDGES)
    px, ex = small.load(), edges.load()
    occ = [[False] * gw for _ in range(gh)]
    for gy in range(gh):
        for gx in range(gw):
            busy = 0
            for dy in range(4):
                for dx in range(4):
                    x, y = gx * 4 + dx, gy * 4 + dy
                    if ex[x, y] > 40 or color_distance(rgb_to_hex(px[x, y]), bg_hex) > 120:
                        busy += 1
            occ[gy][gx] = busy >= 3
    y0 = max(0, int(below_y / slide_h * gh) + 1)
    x_min = int(margins.left / slide_w * gw)
    x_max = gw - int(margins.right / slide_w * gw)
    y_max = gh - int(margins.bottom / slide_h * gh)
    cells = [(gx, gy) for gy in range(y0, y_max) for gx in range(x_min, x_max)]
    busy_share = sum(occ[gy][gx] for gx, gy in cells) / max(len(cells), 1)
    default = Box(x=int(x_min / gw * slide_w), y=int(y0 / gh * slide_h), w=int((x_max - x_min) / gw * slide_w),
                  h=int(max(y_max - y0, 1) / gh * slide_h))
    if busy_share > 0.45:
        return default, True
    best = (0, 0, 0, 0, 0)  # area, x, y, w, h
    heights = [0] * gw
    for gy in range(y0, y_max):
        for gx in range(gw):
            heights[gx] = heights[gx] + 1 if (x_min <= gx < x_max and not occ[gy][gx]) else 0
        stack: list[int] = []
        for gx in range(gw + 1):
            hcur = heights[gx] if gx < gw else 0
            while stack and heights[stack[-1]] >= hcur:
                hh = heights[stack.pop()]
                left = stack[-1] + 1 if stack else 0
                width = gx - left
                if hh * width > best[0]:
                    best = (hh * width, left, gy - hh + 1, width, hh)
            stack.append(gx)
    if best[0] == 0:
        return default, True
    _, bx, by, bw, bh = best
    box = Box(x=int(bx / gw * slide_w), y=int(by / gh * slide_h), w=int(bw / gw * slide_w), h=int(bh / gh * slide_h))
    if box.area < 0.18 * default.area:  # only slivers are free: treat the background as texture
        return default, True
    return box, False


def region_color(png: Path, box: Box, slide_w: int, slide_h: int) -> str | None:
    """Dominant rendered colour inside a region (what text placed there will sit on)."""
    im = Image.open(png).convert("RGB")
    sx, sy = im.width / slide_w, im.height / slide_h
    crop = im.crop((int(box.x * sx), int(box.y * sy), max(int(box.r * sx), int(box.x * sx) + 2), max(int(box.b * sy), int(box.y * sy) + 2)))
    q = crop.resize((48, 27)).quantize(colors=4)
    counts = sorted(q.getcolors() or [], reverse=True)
    if not counts:
        return None
    pal = q.getpalette()
    i = counts[0][1]
    return rgb_to_hex((pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]))


def rendered_palette(pngs: list[Path]) -> Counter:
    """Colours the template actually shows (backgrounds, art, bands), weighted by area."""
    acc: Counter = Counter()
    for p in pngs:
        try:
            q = Image.open(p).convert("RGB").resize((96, 54)).quantize(colors=8)
        except Exception:
            continue
        pal = q.getpalette()
        total = 96 * 54
        for n, i in q.getcolors() or []:
            acc[rgb_to_hex((pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]))] += n / total
    return acc


def mark_repeated_brand_texts(slide_elements: list[list[Element]], sw: int, sh: int) -> None:
    """Text repeated at the same edge position on most example slides ("Confidential",
    "© Company", a product name in the corner) is brand furniture, not a content slot."""
    from decksmith.parsing.elements import is_filler_text

    n = len(slide_elements)
    if n < 3:
        return

    def key(e: Element):
        return (re.sub(r"\s+", " ", e.text.strip().lower()), round(e.box.x / (0.02 * sw)), round(e.box.y / (0.02 * sh)))

    counts: Counter = Counter()
    for els in slide_elements:
        counts.update({key(e) for e in els if e.kind == "text" and e.text.strip()})
    need = max(3, -(-n // 2))
    for els in slide_elements:
        for e in els:
            if e.kind != "text" or not e.text.strip() or e.is_title_ph or is_filler_text(e.text):
                continue
            near_edge = e.box.y > 0.85 * sh or e.box.b < 0.15 * sh
            if near_edge and len(e.text) <= 80 and counts[key(e)] >= need:
                e.brand = True


def normalize_input(path: Path, workdir: Path) -> Path:
    """Accept .pptx, .potx/.pptm/.potm (content-type rewrite) and anything LibreOffice
    opens (.ppt, .odp, .otp, .key...) by converting it to .pptx."""
    suffix = path.suffix.lower()
    out = workdir / "template.pptx"
    if suffix == ".pptx":
        shutil.copy(path, out)
        return out
    if suffix in (".potx", ".pptm", ".potm"):
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    data = re.sub(rb"application/vnd\.ms-powerpoint\.(template|presentation)\.macroEnabled\.main\+xml|"
                                  rb"application/vnd\.openxmlformats-officedocument\.presentationml\.template\.main\+xml",
                                  b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", data)
                if item.filename.lower().endswith("vbaproject.bin"):
                    continue  # macros are never executed nor kept
                zout.writestr(item, data)
        return out
    tmp = Path(tempfile.mkdtemp(prefix="decksmith_convert_"))
    src = tmp / f"in{suffix}"
    shutil.copy(path, src)
    subprocess.run([soffice_path(), f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--convert-to",
                    "pptx:Impress MS PowerPoint 2007 XML", "--outdir", str(tmp), str(src)], capture_output=True, timeout=300)
    conv = tmp / "in.pptx"
    if not conv.exists():
        raise RenderError(f"cannot convert {path.name} to .pptx")
    shutil.move(str(conv), out)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


# ----------------------------------------------------------------------------
# Rendering stage
# ----------------------------------------------------------------------------
def _render_template(path: Path, out: Path) -> list[Path]:
    pdf = pptx_to_pdf(path, out)
    pngs = pdf_to_pngs(pdf, out / "slides", dpi=48, prefix="slide")
    pdf.unlink(missing_ok=True)
    return pngs


def _render_layout_probe(path: Path, out: Path) -> list[Path]:
    """One empty slide per layout (placeholders removed) -> PNG per layout."""
    prs = Presentation(str(path))
    sldIdLst = prs.slides._sldIdLst
    for sldId in list(sldIdLst):
        prs.part.drop_rel(sldId.rId)
        sldIdLst.remove(sldId)
    for _, _, layout in all_layouts(prs):
        s = prs.slides.add_slide(layout)
        for ph in list(s.placeholders):
            ph._element.getparent().remove(ph._element)
    tmp = Path(tempfile.mkdtemp(prefix="decksmith_probe_"))
    probe = tmp / "layouts.pptx"
    prs.save(str(probe))
    pdf = pptx_to_pdf(probe, tmp)
    pngs = pdf_to_pngs(pdf, out / "layouts", dpi=48, prefix="layout")
    shutil.rmtree(tmp, ignore_errors=True)
    return pngs


# ----------------------------------------------------------------------------
# Brand elements & icons
# ----------------------------------------------------------------------------
def _brand_elements(container, theme: Theme, sw: int, sh: int, source: str) -> list[BrandElement]:
    out = []
    for e in extract_elements(container, theme, sw, sh, include_empty_placeholders=True):
        # logos / wordmarks: small objects hugging an edge (big edge art is decoration, not a logo)
        near_edge = e.box.y > 0.85 * sh or e.box.b < 0.15 * sh or e.box.x > 0.85 * sw or e.box.r < 0.15 * sw
        small = e.box.area < 0.025 * sw * sh and 0.1 < (e.box.w / max(e.box.h, 1)) < 12
        if e.placeholder == "SLIDE_NUMBER":
            out.append(BrandElement(kind="page_number", box=e.box, source=source, name=e.name))
        elif e.placeholder == "FOOTER":
            out.append(BrandElement(kind="footer", box=e.box, source=source, name=e.name))
        elif e.placeholder == "DATE":
            out.append(BrandElement(kind="date", box=e.box, source=source, name=e.name))
        elif e.placeholder is None and e.kind in ("picture", "group", "text") and small and near_edge and e.box.w > 0:
            out.append(BrandElement(kind="logo", box=e.box, source=source, name=e.name))
    return out


def _harvest_icons(prs, patterns: list[Pattern], out: Path, sw: int, sh: int) -> list[IconAsset]:
    icons: list[IconAsset] = []
    seen: set[str] = set()
    out.mkdir(parents=True, exist_ok=True)
    guide_slides = {p.slide_index for p in patterns if p.kind == PatternKind.guide and "icons" in p.tags}
    for si, slide in enumerate(prs.slides):
        if si not in guide_slides:
            continue
        for sh_ in slide.shapes:
            try:
                img = sh_.image
            except Exception:
                continue
            if img.sha1 in seen:
                continue
            seen.add(img.sha1)
            try:
                pil = Image.open(io.BytesIO(img.blob))
                if max(pil.size) > 600:
                    continue
                p = out / f"icon_{len(icons):03d}.png"
                pil.convert("RGBA").save(p)
            except Exception:
                continue
            icons.append(IconAsset(id=f"icon_{len(icons):03d}", path=str(p), source_slide=si))
    return icons


# ----------------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------------
def analyze_template(path: str | Path, *, name: str | None = None, force: bool = False, render: bool = True) -> TemplateProfile:
    t0 = time.time()
    path = Path(path)
    sha = file_sha256(path)
    tid = sha[:12]
    workdir = settings().workspace / "templates" / tid
    prof_path = workdir / "profile.json"
    if prof_path.exists() and not force:
        prof = TemplateProfile.model_validate_json(prof_path.read_text(encoding="utf-8"))
        if prof.parser_version == PARSER_VERSION:
            return prof
    workdir.mkdir(parents=True, exist_ok=True)
    stored = workdir / "template.pptx"
    if not stored.exists() or force:
        stored = normalize_input(path, workdir)
    warnings: list[str] = []

    prs = Presentation(str(stored))
    sw, sh = int(prs.slide_width), int(prs.slide_height)
    themes = [parse_theme(m) for m in prs.slide_masters]
    layouts_raw = all_layouts(prs)

    # --- 2. render ---------------------------------------------------------------
    slide_pngs: list[Path] = []
    layout_pngs: list[Path] = []
    if render:
        try:
            slide_pngs = _render_template(stored, workdir)
            layout_pngs = _render_layout_probe(stored, workdir)
        except RenderError as e:
            warnings.append(f"render failed: {e}")
    slide_cv = [image_stats(p) for p in slide_pngs]
    layout_cv = [image_stats(p) for p in layout_pngs]

    # --- 1. layouts ----------------------------------------------------------------
    layouts: list[LayoutInfo] = []
    for gi, mi, layout in layouts_raw:
        phs = []
        for ph in layout.placeholders:
            pf = ph.placeholder_format
            phs.append({
                "idx": pf.idx,
                "type": str(pf.type).split(".")[-1].split(" ")[0],
                "name": ph.name,
                "box": Box(x=int(ph.left or 0), y=int(ph.top or 0), w=int(ph.width or 0), h=int(ph.height or 0)).model_dump(),
            })
        types = [p["type"] for p in phs]
        title_ph = next((p for p in phs if p["type"] in ("TITLE", "CENTER_TITLE")), None)
        li = LayoutInfo(
            index=gi,
            master_index=mi,
            name=layout.name,
            placeholders=phs,
            has_title=title_ph is not None,
            body_count=sum(t in ("BODY", "OBJECT") for t in types),
            picture_count=sum(t == "PICTURE" for t in types),
            title_box=Box(**title_ph["box"]) if title_ph else None,
        )
        if gi < len(layout_cv):
            li.dark, li.background_hex, _ = layout_cv[gi]
            li.thumbnail = str(layout_pngs[gi])
        layouts.append(li)

    # --- 4. patterns ------------------------------------------------------------------
    slide_elements: list[list[Element]] = []
    patterns: list[Pattern] = []
    slide_dark, slide_bg = [], []
    slide_layout_idx = []
    for si, slide in enumerate(prs.slides):
        gi = layout_global_index(prs, slide.slide_layout)
        mi = layouts[gi].master_index if gi >= 0 else 0
        if gi >= 0:
            layouts[gi].used_by_slides.append(si)
        slide_layout_idx.append(gi)
        slide_elements.append(extract_elements(slide, themes[mi], sw, sh))
    mark_repeated_brand_texts(slide_elements, sw, sh)
    furniture: dict[tuple, dict] = {}
    for si, els in enumerate(slide_elements):
        for e in els:
            if e.brand and e.kind == "text" and e.placeholder is None and not e.group_path:
                key = (e.text.strip(), round(e.box.x / 50000), round(e.box.y / 50000))
                furniture.setdefault(key, {"slide": si, "shape_id": e.shape_id, "text": e.text.strip()[:60],
                                           "master": layouts[slide_layout_idx[si]].master_index if slide_layout_idx[si] >= 0 else 0})
    for si, slide in enumerate(prs.slides):
        gi = slide_layout_idx[si]
        els = slide_elements[si]
        dark, bg, _ = slide_cv[si] if si < len(slide_cv) else (False, themes[0].scheme("bg1") or "FFFFFF", 1.0)
        slide_dark.append(dark)
        slide_bg.append(bg)
        pat = build_pattern(
            f"s{si + 1:02d}", els, slide_index=si, layout_index=gi, layout_name=slide.slide_layout.name,
            slide_w=sw, slide_h=sh, bottom_margin=int(0.06 * sh), dark=dark,
        )
        if si < len(slide_pngs):
            pat.thumbnail = str(slide_pngs[si])
        patterns.append(pat)

    # placeholder-rich layouts become patterns too (templates with few examples)
    for li in layouts:
        if li.body_count + li.picture_count == 0:
            continue
        layout = layouts_raw[li.index][2]
        els = [e for e in extract_elements(layout, themes[li.master_index], sw, sh) if e.placeholder is not None]
        pat = build_pattern(
            f"l{li.index:02d}", els, slide_index=None, layout_index=li.index, layout_name=li.name,
            slide_w=sw, slide_h=sh, bottom_margin=int(0.06 * sh), dark=li.dark, source="layout",
        )
        pat.thumbnail = li.thumbnail
        pat.score_hint *= 0.85  # examples are richer than bare layouts
        patterns.append(pat)

    # --- fonts -----------------------------------------------------------------------
    fams = Counter(e.style.font for els in slide_elements for e in els if e.kind == "text" and e.style and e.style.font)
    for li in layouts:  # templates with few examples: fonts of layout placeholders count too
        for e in extract_elements(layouts_raw[li.index][2], themes[li.master_index], sw, sh):
            if e.kind == "text" and e.style and e.style.font:
                fams[e.style.font] += 1
    font_avail = {f: ensure_font(f) for f, _ in fams.most_common(6)}

    # --- 5. tokens --------------------------------------------------------------------
    render_colors = rendered_palette(slide_pngs or layout_pngs)
    tokens = build_tokens(
        slide_w=sw, slide_h=sh, theme=themes[0], slide_elements=slide_elements, patterns=patterns,
        slide_dark=slide_dark, slide_bg=slide_bg or [c[1] for c in layout_cv], font_available=font_avail,
        render_colors=render_colors,
    )

    # --- 3. free content area per layout + canvas choice ---------------------------------
    for li in layouts:
        if li.thumbnail:
            below = (li.title_box.b if li.title_box else (tokens.title_box.b if tokens.title_box else int(0.2 * sh)))
            li.content_box, li.textured = free_content_box(Path(li.thumbnail), sw, sh, below, tokens.margins,
                                                           li.background_hex or tokens.background_hex)
            if li.content_box:
                li.content_bg_hex = region_color(Path(li.thumbnail), li.content_box, sw, sh)
    content_kinds = {PatternKind.cards, PatternKind.text, PatternKind.steps, PatternKind.stats, PatternKind.table,
                     PatternKind.chart, PatternKind.image_text, PatternKind.two_column}

    def canvas_score(li: LayoutInfo) -> float:
        uses = sum(1 for p in patterns if p.layout_index == li.index and p.kind in content_kinds)
        area = li.content_box.area / (sw * sh) if li.content_box else 0
        simple = 1.0 if li.body_count + li.picture_count == 0 else 0.7
        titled = 1.0 if li.has_title else 0.55  # a title placeholder keeps the template's title style
        calm = 0.6 if li.textured else 1.0  # prefer layouts whose content area is really empty
        return (uses + 1) * area * simple * titled * calm

    canvas: dict[str, int] = {}
    for tone in ("light", "dark"):
        cands = [li for li in layouts if li.content_box and li.dark == (tone == "dark") and li.content_box.area > 0.2 * sw * sh]
        if cands:
            canvas[tone] = max(cands, key=canvas_score).index
    if not canvas:
        cands = [li for li in layouts if li.content_box] or layouts
        best = max(cands, key=canvas_score) if cands else None
        if best is not None:
            canvas["dark" if best.dark else "light"] = best.index
        warnings.append("no clean canvas layout found; composed slides use the largest free area available")

    # --- 6. brand -----------------------------------------------------------------------
    brand: list[BrandElement] = []
    for mi, m in enumerate(prs.slide_masters):
        brand += _brand_elements(m, themes[mi], sw, sh, f"master:{mi}")
    for li in layouts:
        brand += _brand_elements(layouts_raw[li.index][2], themes[li.master_index], sw, sh, f"layout:{li.index}")

    # dedupe brand elements repeated on many layouts (same kind + same box)
    uniq: dict[tuple, BrandElement] = {}
    for b in brand:
        key = (b.kind, round(b.box.x / 50000), round(b.box.y / 50000), round(b.box.w / 50000), round(b.box.h / 50000))
        if key in uniq:
            if b.source not in uniq[key].source:
                uniq[key].source += "," + b.source
        else:
            uniq[key] = b
    brand = list(uniq.values())

    # --- 7. assets ----------------------------------------------------------------------
    icons = _harvest_icons(prs, patterns, workdir / "icons", sw, sh)

    for f in tokens.fonts:
        if not f.available:
            warnings.append(f"font '{f.family}' not available locally: rendering will substitute it")

    prof = TemplateProfile(
        id=tid,
        name=name or path.stem,
        file=str(stored),
        sha256=sha,
        tokens=tokens,
        layouts=layouts,
        patterns=patterns,
        brand_elements=brand,
        icons=icons,
        canvas_layouts=canvas,
        brand_furniture=list(furniture.values()),
        slide_thumbnails=[str(p) for p in slide_pngs],
        workdir=str(workdir),
        parser_version=PARSER_VERSION,
        warnings=warnings,
    )
    prof_path.write_text(prof.model_dump_json(indent=1), encoding="utf-8")
    log.info("template %s analysed in %.1fs: %d patterns", tid, time.time() - t0, len(patterns))
    return prof


def summarize(prof: TemplateProfile) -> dict:
    kinds = Counter(p.kind.value for p in prof.patterns)
    t = prof.tokens
    return {
        "id": prof.id,
        "name": prof.name,
        "slide_size_in": [round(t.slide_w / 914400, 2), round(t.slide_h / 914400, 2)],
        "patterns": dict(kinds),
        "fonts": {"heading": t.heading_font, "body": t.body_font, "all": [(f.family, f.available) for f in t.fonts]},
        "type_scale": t.type_scale.model_dump(),
        "accent": t.accent_hex,
        "chart_colors": t.chart_colors,
        "background": t.background_hex,
        "dark": t.dark_background,
        "text": t.text_hex,
        "palette": [c.hex for c in t.palette],
        "margins_in": {k: round(v / 914400, 2) for k, v in t.margins.model_dump().items()},
        "canvas_layouts": {k: prof.layouts[v].name for k, v in prof.canvas_layouts.items()},
        "brand_elements": len(prof.brand_elements),
        "icons": len(prof.icons),
        "warnings": prof.warnings,
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    from decksmith.core.logging import setup_logging

    setup_logging()
    p = analyze_template(sys.argv[1], force="--force" in sys.argv)
    print(json.dumps(summarize(p), ensure_ascii=False, indent=1))
