"""Intermediate representation shared by all pipeline layers.

Layer contracts:
  parsing   : .pptx            -> TemplateProfile
  content   : files            -> ContentCorpus
  generation: brief + corpus   -> DeckPlan
  layout    : plan + profile   -> DeckLayout (per variant)  -> .pptx
  audit     : .pptx + profile  -> AuditReport
  export    : .pptx            -> .pdf / .html

All geometry is stored in EMU (English Metric Units, 914400 per inch) to stay
lossless against the source file. Font sizes are stored in points.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

EMU_PER_INCH = 914400
EMU_PER_PT = 12700


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------
class Box(BaseModel):
    x: int
    y: int
    w: int
    h: int

    @property
    def r(self) -> int:
        return self.x + self.w

    @property
    def b(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(self.w, 0) * max(self.h, 0)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def intersection(self, other: "Box") -> int:
        ix = max(0, min(self.r, other.r) - max(self.x, other.x))
        iy = max(0, min(self.b, other.b) - max(self.y, other.y))
        return ix * iy

    def contains(self, other: "Box", tol: int = 0) -> bool:
        return (
            other.x >= self.x - tol
            and other.y >= self.y - tol
            and other.r <= self.r + tol
            and other.b <= self.b + tol
        )

    def union(self, other: "Box") -> "Box":
        x, y = min(self.x, other.x), min(self.y, other.y)
        return Box(x=x, y=y, w=max(self.r, other.r) - x, h=max(self.b, other.b) - y)

    def inset(self, dx: int, dy: int | None = None) -> "Box":
        dy = dx if dy is None else dy
        return Box(x=self.x + dx, y=self.y + dy, w=max(self.w - 2 * dx, 0), h=max(self.h - 2 * dy, 0))


# ----------------------------------------------------------------------------
# Design system (tokens)
# ----------------------------------------------------------------------------
class ColorToken(BaseModel):
    hex: str  # "RRGGBB"
    roles: list[str] = Field(default_factory=list)  # accent1, dk1, text, background...
    usage: int = 0  # frequency across template
    luminance: float = 0.0


class FontToken(BaseModel):
    family: str
    roles: list[str] = Field(default_factory=list)  # heading, body
    usage: int = 0
    available: bool = False  # resolved to a real font file for rendering/metrics
    file: str | None = None


class TypeScale(BaseModel):
    sizes: list[float] = Field(default_factory=list)  # distinct sizes, pt, descending
    title: float = 28.0
    subtitle: float = 18.0
    body: float = 14.0
    caption: float = 10.0
    number: float = 48.0  # big KPI digits


class Margins(BaseModel):
    left: int
    top: int
    right: int
    bottom: int


class DesignTokens(BaseModel):
    slide_w: int
    slide_h: int
    palette: list[ColorToken] = Field(default_factory=list)
    theme_colors: dict[str, str] = Field(default_factory=dict)  # accent1 -> hex
    fonts: list[FontToken] = Field(default_factory=list)
    heading_font: str = "Arial"
    body_font: str = "Arial"
    type_scale: TypeScale = Field(default_factory=TypeScale)
    margins: Margins
    grid_columns: list[int] = Field(default_factory=list)  # x positions of column starts
    title_box: Box | None = None  # canonical title position
    content_box: Box | None = None  # canonical content area below title
    dark_background: bool = False  # dominant background of content slides
    background_hex: str = "FFFFFF"
    text_hex: str = "000000"
    accent_hex: str = "3366CC"
    chart_colors: list[str] = Field(default_factory=list)
    corner_radius: float = 0.0  # typical card corner rounding (0..0.5)
    card_fill_hex: str | None = None


# ----------------------------------------------------------------------------
# Layouts & patterns
# ----------------------------------------------------------------------------
class PatternKind(str, Enum):
    title = "title"
    section = "section"
    agenda = "agenda"
    text = "text"  # title + bullets / paragraph
    two_column = "two_column"
    cards = "cards"  # N similar items (title + text), optional icons
    steps = "steps"  # numbered/timeline items
    stats = "stats"  # big numbers / KPIs
    table = "table"
    chart = "chart"
    image_text = "image_text"
    quote = "quote"
    team = "team"
    contacts = "contacts"
    thanks = "thanks"
    free = "free"
    guide = "guide"  # template documentation slide, not reusable


class SlotRole(str, Enum):
    title = "title"
    subtitle = "subtitle"
    body = "body"
    item_title = "item_title"
    item_text = "item_text"
    number = "number"  # big numbers, step digits
    label = "label"  # short captions, tags, dates
    caption = "caption"
    person = "person"
    image = "image"
    icon = "icon"
    table = "table"
    chart = "chart"
    decor = "decor"


class TextStyle(BaseModel):
    font: str | None = None
    size: float | None = None  # pt, effective
    bold: bool = False
    color_hex: str | None = None
    align: str | None = None


class Slot(BaseModel):
    id: str
    role: SlotRole
    box: Box
    kind: Literal["text", "picture", "table", "chart", "shape"] = "text"
    shape_path: list[int] = Field(default_factory=list)  # index path in spTree (groups)
    shape_id: int | None = None
    placeholder_idx: int | None = None
    sample_text: str = ""
    style: TextStyle = Field(default_factory=TextStyle)
    max_chars: int = 0
    max_lines: int = 0
    item_index: int | None = None  # position inside a repeater
    paragraphs: int = 1  # paragraphs in sample (bullets)
    autofit: bool = False  # frame grows with text when rendered
    # composite text boxes ("Заголовок\nТекст" with different styles per paragraph)
    para_roles: list[SlotRole] = Field(default_factory=list)
    para_styles: list[TextStyle] = Field(default_factory=list)
    # the layout placeholder under this slot paints a fill/outline; some renderers (LibreOffice)
    # draw it even when the slot is removed from the slide, so leaving it unused shows an empty card
    painted: bool = False


class Repeater(BaseModel):
    """A set of visually identical items (cards, steps, KPI tiles)."""

    id: str
    n_items: int
    direction: Literal["row", "column", "grid"] = "row"
    item_boxes: list[Box] = Field(default_factory=list)
    item_shape_ids: list[list[int]] = Field(default_factory=list)  # all shapes belonging to item i
    slot_roles: list[SlotRole] = Field(default_factory=list)  # per-item slot composition


class Pattern(BaseModel):
    id: str
    source: Literal["slide", "layout"] = "slide"
    slide_index: int | None = None  # 0-based in template
    layout_index: int | None = None  # global layout index
    layout_name: str = ""
    kind: PatternKind = PatternKind.free
    n_items: int = 0
    slots: list[Slot] = Field(default_factory=list)
    repeaters: list[Repeater] = Field(default_factory=list)
    containers: dict[str, list[int]] = Field(default_factory=dict)  # slot id -> decor shape ids framing it
    backdrops: dict[str, int] = Field(default_factory=dict)  # title slot id -> badge shape sized to the example's text
    dark: bool = False
    text_capacity: int = 0  # total chars
    has_picture_slot: bool = False
    fill_ratio: float = 0.0
    score_hint: float = 1.0  # prior usefulness (penalise exotic slides)
    tags: list[str] = Field(default_factory=list)
    thumbnail: str | None = None
    reason: str = ""  # why classified this way (transparency)


class LayoutInfo(BaseModel):
    index: int  # global index across masters
    master_index: int
    name: str
    placeholders: list[dict[str, Any]] = Field(default_factory=list)
    painted_idx: list[int] = Field(default_factory=list)  # content placeholders that paint a fill/outline
    dark: bool = False
    has_title: bool = False
    body_count: int = 0
    picture_count: int = 0
    used_by_slides: list[int] = Field(default_factory=list)
    title_box: Box | None = None
    content_box: Box | None = None  # free area (CV occupancy on rendered empty layout)
    content_bg_hex: str | None = None  # rendered colour under the content area
    textured: bool = False  # busy background (photo/texture): content goes on a plate
    title_clear: Box | None = None  # title frame shortened to stay clear of background art (e.g. logo strip)
    background_hex: str | None = None
    thumbnail: str | None = None


class BrandElement(BaseModel):
    kind: Literal["logo", "footer", "page_number", "decor", "date"] = "decor"
    box: Box
    source: str = ""  # "master:0" / "layout:3"
    name: str = ""


class IconAsset(BaseModel):
    id: str
    path: str
    source_slide: int
    color_hex: str | None = None
    label: str | None = None


class TemplateProfile(BaseModel):
    id: str
    name: str
    file: str
    sha256: str
    tokens: DesignTokens
    layouts: list[LayoutInfo] = Field(default_factory=list)
    patterns: list[Pattern] = Field(default_factory=list)
    brand_elements: list[BrandElement] = Field(default_factory=list)
    icons: list[IconAsset] = Field(default_factory=list)
    canvas_layouts: dict[str, int] = Field(default_factory=dict)  # "light"/"dark" -> layout index for composed slides
    # slide-level brand furniture repeated on most examples (notice, page-number box...):
    # copied onto slides built from bare layouts / composed natively
    brand_furniture: list[dict[str, Any]] = Field(default_factory=list)
    slide_thumbnails: list[str] = Field(default_factory=list)
    workdir: str = ""
    parser_version: str = ""
    warnings: list[str] = Field(default_factory=list)

    def pattern(self, pid: str) -> Pattern:
        for p in self.patterns:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def usable_patterns(self) -> list[Pattern]:
        return [p for p in self.patterns if p.kind != PatternKind.guide]


# ----------------------------------------------------------------------------
# Content corpus
# ----------------------------------------------------------------------------
class ContentChunk(BaseModel):
    id: str
    source: str
    text: str
    page: int | None = None


class DataTable(BaseModel):
    id: str
    source: str
    title: str = ""
    columns: list[str]
    rows: list[list[Any]]


class ContentCorpus(BaseModel):
    id: str
    files: list[str] = Field(default_factory=list)
    chunks: list[ContentChunk] = Field(default_factory=list)
    tables: list[DataTable] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)  # normalised numeric facts
    language: str = "ru"

    def full_text(self, limit: int | None = None) -> str:
        text = "\n\n".join(c.text for c in self.chunks)
        return text if limit is None else text[:limit]


# ----------------------------------------------------------------------------
# Deck plan (content before layout)
# ----------------------------------------------------------------------------
class ChartSeries(BaseModel):
    name: str
    values: list[float]


class ChartSpec(BaseModel):
    type: Literal["bar", "column", "line", "pie", "doughnut"] = "column"
    title: str = ""
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    unit: str = ""
    x_title: str = ""
    y_title: str = ""


class TableSpec(BaseModel):
    columns: list[str]
    rows: list[list[str]]


class Item(BaseModel):
    title: str = ""
    text: str = ""
    value: str = ""  # KPI value / date / step number
    icon: str = ""  # icon keyword


class SlideSpec(BaseModel):
    id: str
    intent: PatternKind = PatternKind.text
    title: str
    subtitle: str = ""
    message: str = ""  # one-sentence takeaway
    bullets: list[str] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    quote: str = ""
    quote_author: str = ""
    image_prompt: str = ""
    notes: str = ""
    section: str = ""


class DeckPlan(BaseModel):
    title: str
    subtitle: str = ""
    purpose: str = "product"
    language: str = "ru"
    slides: list[SlideSpec]


# ----------------------------------------------------------------------------
# Layout result
# ----------------------------------------------------------------------------
class SlotFill(BaseModel):
    slot_id: str
    text: str | None = None
    paragraphs: list[str] | None = None
    image_path: str | None = None


class VisualSpec(BaseModel):
    kind: Literal["chart", "table", "diagram", "icons", "image", "kpi"]
    box: Box
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    items: list[Item] = Field(default_factory=list)
    diagram: Literal["process", "cycle", "timeline", "pyramid", "matrix"] | None = None
    image_path: str | None = None


class SlideLayout(BaseModel):
    spec_id: str
    mode: Literal["clone", "compose"] = "clone"
    pattern_id: str | None = None
    compose_kind: str | None = None  # chart | table | kpi | process | cards | bullets | image
    layout_index: int | None = None
    keep_items: int | None = None  # for repeaters: number of items to keep
    fills: list[SlotFill] = Field(default_factory=list)
    visuals: list[VisualSpec] = Field(default_factory=list)
    rationale: str = ""


class DeckLayout(BaseModel):
    variant: str
    template_id: str
    slides: list[SlideLayout]


# ----------------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------------
class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class AuditIssue(BaseModel):
    id: str
    check: str  # check id, e.g. "layout.out_of_bounds"
    category: Literal["layout", "template", "density", "integrity", "content"]
    deterministic: bool = True
    severity: Severity = Severity.warning
    slide: int  # 0-based
    message: str
    boxes: list[Box] = Field(default_factory=list)
    shape_ids: list[int] = Field(default_factory=list)
    fixable: bool = False
    fix: str | None = None  # fixer id
    data: dict[str, Any] = Field(default_factory=dict)


class AuditReport(BaseModel):
    deck: str
    slide_count: int
    issues: list[AuditIssue] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    score: float = 100.0
    duration_s: float = 0.0
