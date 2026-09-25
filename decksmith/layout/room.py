"""Room a text frame may grow into.

The organisers clarified the "text did not fit its frame" rule: a text frame may be resized
inside its visual block (card, plate, panel, photo) as long as the text stays within the
block and the slide keeps its structure. So before shrinking type, the builder and the
audit fixer grow the frame: down (or up / both ways, following the text anchor) to the
block's inner padding, or, outside any block, to the content area of the slide, never
closer to other objects than they already were.
"""
from __future__ import annotations

from lxml import etree
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

from decksmith.core.models import Box, DesignTokens
from decksmith.layout.textfit import DEFAULT_INSET_LR, measure
from decksmith.parsing.elements import text_anchor, text_insets
from decksmith.parsing.ooxml import flatten_shapes

_FILLS = ("solidFill", "gradFill", "blipFill", "pattFill")
GAP = int(0.06 * 914400)  # distance kept to the next object when the original one was larger

# (owned box, top, bottom, frame box) in slide coordinates
Room = tuple[Box, int, int, Box]


def frame_visible(el) -> bool:
    """The shape paints its own frame (fill or outline; an explicit noFill wins over the theme style)."""
    sp_pr = el.find(qn("p:spPr"))
    kids = [etree.QName(c).localname for c in sp_pr] if sp_pr is not None else []
    style = el.find(qn("p:style"))
    fill = next((k for k in kids if k in _FILLS or k == "noFill"), None)
    if fill is not None:
        if fill != "noFill":
            return True
    elif style is not None and style.find(qn("a:fillRef")) is not None and style.find(qn("a:fillRef")).get("idx", "0") != "0":
        return True
    ln = sp_pr.find(qn("a:ln")) if sp_pr is not None else None
    if ln is not None:
        lk = [etree.QName(c).localname for c in ln]
        if "noFill" in lk:
            return False
        if any(k in _FILLS for k in lk):
            return True
    ref = style.find(qn("a:lnRef")) if style is not None else None
    return ref is not None and ref.get("idx", "0") != "0"


def is_plate(shape) -> bool:
    """A visible surface that can frame text: a picture or a filled/outlined shape."""
    tag = etree.QName(shape._element).localname
    return tag == "pic" or (tag == "sp" and frame_visible(shape._element))


def draws(shape) -> bool:
    """Anything a grown frame must keep clear of (empty invisible frames draw nothing)."""
    el = shape._element
    if etree.QName(el).localname != "sp":
        return True
    return bool("".join(t.text or "" for t in el.iter(qn("a:t"))).strip()) or frame_visible(el)


def rotated(sh) -> bool:
    """The frame, a group around it or the layout placeholder it inherits from is rotated
    (room is computed for upright boxes only)."""
    node = sh._element
    while node is not None and etree.QName(node).localname in ("sp", "grpSp", "graphicFrame", "pic"):
        xfrm = node.find(qn("p:spPr") + "/" + qn("a:xfrm"))
        if xfrm is None:
            xfrm = node.find(qn("p:grpSpPr") + "/" + qn("a:xfrm"))
        if xfrm is not None and int(xfrm.get("rot", "0")) % 21600000:
            return True
        node = node.getparent()
    base = sh
    for _ in range(2):  # layout, then master placeholder
        try:
            base = base._base_placeholder if getattr(base, "is_placeholder", False) else None
        except Exception:
            base = None
        if base is None:
            break
        xfrm = base._element.find(qn("p:spPr") + "/" + qn("a:xfrm"))
        if xfrm is not None and int(xfrm.get("rot", "0")) % 21600000:
            return True
    return False


def text_room(slide, sh, tokens: DesignTokens, owned_h: int | None = None, keep_clear: list[Box] = ()) -> Room | None:
    """Vertical span the frame `sh` may take. `owned_h` limits the frame's own height when its
    shape is larger than the part it owns (a card-sized heading frame above a body frame);
    `keep_clear` are zones drawn by the layout/master (logo, footer, page number).
    None when the frame cannot be located, is rotated or is itself a visible plate."""
    if frame_visible(sh._element) or rotated(sh):
        return None  # resizing a painted frame would resize a card of the design
    recs = [r for r in flatten_shapes(slide.shapes) if r.shape.shape_type != MSO_SHAPE_TYPE.GROUP]
    me = next((r for r in recs if r.shape._element is sh._element), None)
    if me is None or me.box.w <= 0 or me.box.h <= 0:
        return None
    fb = me.box
    own = Box(x=fb.x, y=fb.y, w=fb.w, h=min(fb.h, owned_h or fb.h))
    area, tol = tokens.slide_w * tokens.slide_h, int(0.004 * tokens.slide_w)
    others = [r for r in recs if r is not me and r.box.area < 0.85 * area]
    block = None
    for r in others:
        b = r.box
        if b.area > own.area and b.x - tol <= own.x and own.r <= b.r + tol and b.y - tol <= own.y < b.b and is_plate(r.shape):
            block = b if block is None or b.area < block.area else block
    if block is not None:
        pad = max(min(own.x - block.x, block.r - own.r), GAP)  # the block's inner padding stays
        top = block.y + min(max(own.y - block.y, 0), pad)
        bottom = block.b - min(max(block.b - own.b, 0), pad)
    else:
        top, bottom = min(own.y, tokens.margins.top), max(own.b, tokens.slide_h - tokens.margins.bottom)
    obstacles = [r.box for r in others if draws(r.shape)] + list(keep_clear)
    for b in obstacles:
        if b.x - tol <= own.x and own.r <= b.r + tol and b.y - tol <= own.y and own.b <= b.b + tol:
            continue  # behind the frame: its block or a panel
        if min(b.r, own.r) - max(b.x, own.x) <= 0.02 * own.w:
            continue
        if b.y >= own.b - tol:
            bottom = min(bottom, b.y - min(GAP, max(b.y - own.b, 0)))
        elif b.b <= own.y + tol:
            top = max(top, b.b + min(GAP, max(own.y - b.b, 0)))
    return own, max(min(top, own.y), 0), min(max(bottom, own.b), tokens.slide_h), fb


def text_height(sh, paragraphs: list[str], font: str, size: float, width: int, bold: bool = False) -> int:
    """Height the text needs in this frame, with the frame's own insets (as the audit measures it)."""
    l, t, r, b = text_insets(sh)
    return measure(paragraphs, font, size, width - (l + r - 2 * DEFAULT_INSET_LR), bold,
                   inset_lr=DEFAULT_INSET_LR, inset_tb=(t + b) // 2).height_emu


def grow_frame(sh, room: Room, need: int) -> bool:
    """Resize the frame to the text height inside its room, keeping the anchored edge
    (top-anchored text stays where it starts, centred text stays centred)."""
    own, top, bottom, fb = room
    if need <= own.h:
        return False
    new_h = min(int(need * 1.04) + 6350, bottom - top)  # slack for renderer line metrics
    if new_h <= fb.h:
        return False  # never shrink a frame
    anchor = text_anchor(sh)
    y = own.b - new_h if anchor == "b" else (own.y + (own.h - new_h) // 2 if anchor == "ctr" else own.y)
    y = min(max(y, top), bottom - new_h)
    try:
        lx, ly, lw, lh = int(sh.left), int(sh.top), int(sh.width), int(sh.height)
    except (TypeError, ValueError):
        return False
    k = fb.h / lh if lh else 1.0  # children of a group live in the group's coordinates
    sh.left, sh.top, sh.width, sh.height = lx, int(ly + (y - fb.y) / k), lw, int(new_h / k)  # pin all four
    return True
