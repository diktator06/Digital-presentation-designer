"""Промежуточное представление (IR), общее для всех слоёв пайплайна.

Контракты слоёв:
  parsing   : .pptx            -> TemplateProfile
  content   : файлы            -> ContentCorpus
  generation: бриф + корпус    -> DeckPlan
  layout    : план + профиль   -> list[SlideLayout] (на вариант) -> .pptx
  audit     : .pptx + профиль  -> AuditReport
  export    : .pptx            -> .pdf / .html

Вся геометрия хранится в EMU (English Metric Units, 914400 на дюйм), чтобы не терять
точность относительно исходного файла. Кегли хранятся в пунктах.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

EMU_PER_INCH = 914400
EMU_PER_PT = 12700


# ----------------------------------------------------------------------------
# Геометрия
# ----------------------------------------------------------------------------
class Box(BaseModel):
    x: int
    y: int
    w: int
    h: int

    @property
    def r(self) -> int:
        """Правый край."""
        return self.x + self.w

    @property
    def b(self) -> int:
        """Нижний край."""
        return self.y + self.h

    @property
    def area(self) -> int:
        """Площадь (0 для вырожденной рамки)."""
        return max(self.w, 0) * max(self.h, 0)

    @property
    def cx(self) -> float:
        """Центр по горизонтали."""
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        """Центр по вертикали."""
        return self.y + self.h / 2

    def intersection(self, other: "Box") -> int:
        """Площадь пересечения с другой рамкой."""
        ix = max(0, min(self.r, other.r) - max(self.x, other.x))
        iy = max(0, min(self.b, other.b) - max(self.y, other.y))
        return ix * iy

    def contains(self, other: "Box", tol: int = 0) -> bool:
        """Другая рамка целиком внутри этой (с допуском)."""
        return (
            other.x >= self.x - tol
            and other.y >= self.y - tol
            and other.r <= self.r + tol
            and other.b <= self.b + tol
        )

    def union(self, other: "Box") -> "Box":
        """Наименьшая рамка, охватывающая обе."""
        x, y = min(self.x, other.x), min(self.y, other.y)
        return Box(x=x, y=y, w=max(self.r, other.r) - x, h=max(self.b, other.b) - y)


# ----------------------------------------------------------------------------
# Дизайн-система (токены)
# ----------------------------------------------------------------------------
class ColorToken(BaseModel):
    hex: str  # «RRGGBB»
    roles: list[str] = Field(default_factory=list)  # accent1, dk1, текст, фон...
    usage: int = 0  # частота по всему шаблону
    luminance: float = 0.0


class FontToken(BaseModel):
    family: str
    roles: list[str] = Field(default_factory=list)  # заголовочный, основной
    usage: int = 0
    available: bool = False  # сопоставлен реальному файлу шрифта для рендера и замеров
    file: str | None = None


class TypeScale(BaseModel):
    sizes: list[float] = Field(default_factory=list)  # различные кегли в pt, по убыванию
    title: float = 28.0
    subtitle: float = 18.0
    body: float = 14.0
    caption: float = 10.0
    number: float = 48.0  # крупные цифры KPI


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
    grid_columns: list[int] = Field(default_factory=list)  # координаты x начала колонок
    title_box: Box | None = None  # типовое положение заголовка
    content_box: Box | None = None  # типовая область контента под заголовком
    dark_background: bool = False  # преобладающий фон содержательных слайдов
    background_hex: str = "FFFFFF"
    text_hex: str = "000000"
    accent_hex: str = "3366CC"
    chart_colors: list[str] = Field(default_factory=list)
    corner_radius: float = 0.0  # типичное скругление углов карточек (0..0.5)
    card_fill_hex: str | None = None


# ----------------------------------------------------------------------------
# Макеты и паттерны
# ----------------------------------------------------------------------------
class PatternKind(str, Enum):
    title = "title"
    section = "section"
    agenda = "agenda"
    text = "text"  # заголовок + пункты / абзац
    two_column = "two_column"
    cards = "cards"  # N однотипных элементов (заголовок + текст), иконки по желанию
    steps = "steps"  # нумерованные элементы / таймлайн
    stats = "stats"  # крупные числа / KPI
    table = "table"
    chart = "chart"
    image_text = "image_text"
    quote = "quote"
    team = "team"
    contacts = "contacts"
    thanks = "thanks"
    free = "free"
    guide = "guide"  # слайд-инструкция шаблона, не для повторного использования


class SlotRole(str, Enum):
    title = "title"
    subtitle = "subtitle"
    body = "body"
    item_title = "item_title"
    item_text = "item_text"
    number = "number"  # крупные числа, номера шагов
    label = "label"  # короткие подписи, теги, даты
    caption = "caption"
    person = "person"
    image = "image"
    icon = "icon"
    table = "table"
    chart = "chart"
    decor = "decor"


class TextStyle(BaseModel):
    font: str | None = None
    size: float | None = None  # pt, эффективный
    bold: bool = False
    color_hex: str | None = None
    align: str | None = None
    caps: bool = False  # шаблон рисует текст заглавными
    tracking: float = 0.0  # pt, разрядка между знаками


class Slot(BaseModel):
    id: str
    role: SlotRole
    box: Box
    kind: Literal["text", "picture", "table", "chart", "shape"] = "text"
    shape_path: list[int] = Field(default_factory=list)  # путь индексов в spTree (с учётом групп)
    shape_id: int | None = None
    placeholder_idx: int | None = None
    sample_text: str = ""
    style: TextStyle = Field(default_factory=TextStyle)
    max_chars: int = 0
    max_lines: int = 0
    item_index: int | None = None  # позиция внутри повторителя
    paragraphs: int = 1  # абзацев в примере (пункты)
    autofit: bool = False  # рамка растёт вместе с текстом при рендере
    # составные текстовые рамки («Заголовок\nТекст» с разными стилями абзацев)
    para_roles: list[SlotRole] = Field(default_factory=list)
    para_styles: list[TextStyle] = Field(default_factory=list)
    # плейсхолдер макета под этим слотом рисует заливку/обводку; некоторые рендеры (LibreOffice)
    # рисуют его, даже если слот удалён со слайда, и неиспользованный слот выглядит пустой карточкой
    painted: bool = False


class Repeater(BaseModel):
    """Набор визуально одинаковых элементов (карточки, шаги, плитки KPI)."""

    id: str
    n_items: int
    direction: Literal["row", "column", "grid"] = "row"
    item_boxes: list[Box] = Field(default_factory=list)
    item_shape_ids: list[list[int]] = Field(default_factory=list)  # все фигуры, относящиеся к элементу i
    slot_roles: list[SlotRole] = Field(default_factory=list)  # состав слотов элемента


class Pattern(BaseModel):
    id: str
    source: Literal["slide", "layout"] = "slide"
    slide_index: int | None = None  # с нуля, в шаблоне
    layout_index: int | None = None  # сквозной индекс макета
    layout_name: str = ""
    kind: PatternKind = PatternKind.free
    n_items: int = 0
    slots: list[Slot] = Field(default_factory=list)
    repeaters: list[Repeater] = Field(default_factory=list)
    # id слота -> id декоративных фигур, обрамляющих его
    containers: dict[str, list[int]] = Field(default_factory=dict)
    # id слота заголовка -> плашка, подогнанная под текст примера
    backdrops: dict[str, int] = Field(default_factory=dict)
    dark: bool = False
    text_capacity: int = 0  # символов всего
    has_picture_slot: bool = False
    fill_ratio: float = 0.0
    score_hint: float = 1.0  # априорная полезность (штраф экзотическим слайдам)
    tags: list[str] = Field(default_factory=list)
    thumbnail: str | None = None
    reason: str = ""  # почему классифицирован так (прозрачность решения)


class LayoutInfo(BaseModel):
    index: int  # сквозной индекс по всем мастерам
    master_index: int
    name: str
    placeholders: list[dict[str, Any]] = Field(default_factory=list)
    # плейсхолдеры контента, которые рисуют заливку/обводку
    painted_idx: list[int] = Field(default_factory=list)
    photo_share: float = 0.0  # доля слайда под непрозрачными картинками самого макета (фотоколлажи)
    dark: bool = False
    has_title: bool = False
    body_count: int = 0
    picture_count: int = 0
    used_by_slides: list[int] = Field(default_factory=list)
    title_box: Box | None = None
    content_box: Box | None = None  # свободная зона (CV-занятость отрендеренного пустого макета)
    content_bg_hex: str | None = None  # цвет рендера под областью контента
    textured: bool = False  # пёстрый фон (фото/текстура): контент ставится на подложку
    # рамка заголовка, укороченная до фоновой графики (например, полосы с логотипами)
    title_clear: Box | None = None
    background_hex: str | None = None
    thumbnail: str | None = None


class BrandElement(BaseModel):
    kind: Literal["logo", "footer", "page_number", "decor", "date"] = "decor"
    box: Box
    source: str = ""  # «master:0» / «layout:3»
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
    # «light»/«dark» -> индекс макета для собранных слайдов
    canvas_layouts: dict[str, int] = Field(default_factory=dict)
    # бренд-элементы уровня слайда, повторяющиеся на большинстве примеров (плашка, номер страницы...):
    # переносятся на слайды из голых макетов и на собранные нативно
    brand_furniture: list[dict[str, Any]] = Field(default_factory=list)
    slide_thumbnails: list[str] = Field(default_factory=list)
    workdir: str = ""
    parser_version: str = ""
    warnings: list[str] = Field(default_factory=list)

    def pattern(self, pid: str) -> Pattern:
        """Паттерн по id."""
        for p in self.patterns:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def usable_patterns(self) -> list[Pattern]:
        """Паттерны, пригодные для вёрстки (без страниц-инструкций шаблона)."""
        return [p for p in self.patterns if p.kind != PatternKind.guide]


# ----------------------------------------------------------------------------
# Контент-пакет
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
    numbers: list[str] = Field(default_factory=list)  # нормализованные числовые факты
    language: str = "ru"

    def full_text(self, limit: int | None = None) -> str:
        """Весь текст контент-пакета (с ограничением длины)."""
        text = "\n\n".join(c.text for c in self.chunks)
        return text if limit is None else text[:limit]


# ----------------------------------------------------------------------------
# План колоды (содержание до вёрстки)
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


# Предел плотности таблиц — общий для вёрстки и аудита. В Приложении 1 ТЗ — 7 строк; организаторы
# уточнили в чате участников, что таблица длиннее не ошибка, а до 10 строк (вместе с заголовком)
# таблица остаётся читаемой.
TABLE_MAX_ROWS = 10
TABLE_MAX_COLS = 5


class TableSpec(BaseModel):
    columns: list[str]
    rows: list[list[str]]


class Item(BaseModel):
    title: str = ""
    text: str = ""
    value: str = ""  # значение KPI / дата / номер шага
    icon: str = ""  # ключевое слово иконки


class SlideSpec(BaseModel):
    id: str
    intent: PatternKind = PatternKind.text
    title: str
    subtitle: str = ""
    message: str = ""  # вывод одним предложением
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
    materials_fit: str = "full"  # насколько контент-пакет относится к теме брифа: full | partial | none


# ----------------------------------------------------------------------------
# Результат вёрстки
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
    keep_items: int | None = None  # для повторителей: сколько элементов оставить
    fills: list[SlotFill] = Field(default_factory=list)
    visuals: list[VisualSpec] = Field(default_factory=list)
    rationale: str = ""


# ----------------------------------------------------------------------------
# Аудит
# ----------------------------------------------------------------------------
class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class AuditIssue(BaseModel):
    id: str
    check: str  # id проверки, например «layout.out_of_bounds»
    category: Literal["layout", "template", "density", "integrity", "content"]
    deterministic: bool = True
    severity: Severity = Severity.warning
    slide: int  # с нуля
    message: str
    boxes: list[Box] = Field(default_factory=list)
    shape_ids: list[int] = Field(default_factory=list)
    fixable: bool = False
    fix: str | None = None  # id исправления
    data: dict[str, Any] = Field(default_factory=dict)


class AuditReport(BaseModel):
    deck: str
    slide_count: int
    issues: list[AuditIssue] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    score: float = 100.0
    duration_s: float = 0.0
