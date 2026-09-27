"""Нативная композиция визуализаций в дизайн-токенах шаблона.

Всё создаётся редактируемыми объектами PowerPoint: диаграммы (части chart с
данными), таблицы, фигуры и текстовые блоки. Ни один слайд не растеризуется.
Схемы в духе SmartArt (процесс, таймлайн, цикл) собираются из нативных фигур,
сгруппированных по ролям, — их одинаково редактируют PowerPoint, LibreOffice и Keynote.
"""
from __future__ import annotations

from dataclasses import dataclass

from lxml import etree
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from decksmith.core.models import TABLE_MAX_COLS, TABLE_MAX_ROWS, Box, ChartSpec, DesignTokens, Item, TableSpec
from decksmith.layout.textfit import DEFAULT_INSET_LR, fit_font_size, measure, snap_down
from decksmith.parsing.ooxml import color_distance, contrast_ratio, hex_to_rgb, rel_luminance, rgb_to_hex

NO_STYLE_TABLE = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"


# ----------------------------------------------------------------------------
# Контекст стиля
# ----------------------------------------------------------------------------
def mix(a: str, b: str, t: float) -> str:
    """Смешение двух цветов в доле t."""
    ra, ga, ba = hex_to_rgb(a)
    rb, gb, bb = hex_to_rgb(b)
    return rgb_to_hex((ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t))


def readable_on(bg: str, candidates: list[str]) -> str:
    """Самый контрастный к фону цвет из кандидатов (или чёрный/белый, если контраста не хватает)."""
    best = max(candidates, key=lambda c: contrast_ratio(c, bg))
    if contrast_ratio(best, bg) < 4.5:
        best = "FFFFFF" if rel_luminance(bg) < 0.4 else "000000"
    return best


@dataclass
class Style:
    tokens: DesignTokens
    bg: str  # фон собираемого слайда
    dark: bool
    bg_samples: tuple[str, ...] = ()  # другие цвета рендера под контентом (градиенты)

    def _readable(self, c: str, ratio: float = 4.5) -> bool:
        """Цвет читается на фоне и на всех его оттенках под контентом."""
        return all(contrast_ratio(c, b) >= ratio for b in (self.bg, *self.bg_samples))

    @property
    def text(self) -> str:
        """Цвет основного текста, читаемый на фоне слайда."""
        t = self.tokens.text_hex
        if self._readable(t):
            return t
        return readable_on(self.bg, [c.hex for c in self.tokens.palette[:8]] + ["FFFFFF", "000000"])

    @property
    def muted(self) -> str:
        """Приглушённый цвет текста для подписей, если он ещё читается."""
        for t in (0.35, 0.2):
            m = mix(self.text, self.bg, t)
            if self._readable(m):
                return m
        return self.text

    @property
    def accent(self) -> str:
        """Акцентный цвет шаблона."""
        a = self.tokens.accent_hex
        return a

    @property
    def accent_text(self) -> str:
        """Акцентный цвет, пригодный для текста на фоне слайда (контраст ≥ 3:1 для крупного текста)."""
        a = self.tokens.accent_hex
        return a if contrast_ratio(a, self.bg) >= 3 else self.text

    @property
    def card(self) -> str:
        cf = self.tokens.card_fill_hex
        # цвет карточки шаблона — только если это спокойная поверхность (близка к фону по светлоте)
        if cf and 6 < color_distance(cf, self.bg) < 140 and contrast_ratio(self.text, cf) >= 7:
            return cf
        return mix(self.bg, self.accent, 0.08) if not self.dark else mix(self.bg, "FFFFFF", 0.10)

    def series(self, i: int) -> str:
        """Цвет i-го ряда: палитра диаграмм шаблона без цветов, которые теряются на фоне этого слайда или
        повторяют предыдущий ряд; дополнительные ряды получают оттенки, сдвинутые к цвету текста, чтобы
        оставаться видимыми.
        """
        pool: list[str] = []
        for c in self.tokens.chart_colors or []:
            if contrast_ratio(c, self.bg) >= 1.5 and all(color_distance(c, p) > 100 for p in pool):
                pool.append(c)
        if not pool:
            pool = [self.accent_text]
        if i < len(pool):
            return pool[i]
        return mix(pool[i % len(pool)], self.text, 0.5 if i < 2 * len(pool) else 0.75)

    @property
    def heading_font(self) -> str:
        """Шрифт заголовков шаблона."""
        return self.tokens.heading_font

    @property
    def body_font(self) -> str:
        """Основной шрифт шаблона."""
        return self.tokens.body_font

    def size(self, role: str) -> float:
        """Кегль роли со шкалы шаблона."""
        ts = self.tokens.type_scale
        return {"title": ts.title, "subtitle": ts.subtitle, "body": ts.body, "caption": ts.caption, "number": ts.number}[role]


def _rgb(h: str) -> RGBColor:
    """Цвет python-pptx из hex."""
    return RGBColor.from_string(h)


# ----------------------------------------------------------------------------
# Примитивы
# ----------------------------------------------------------------------------
def text_size(paragraphs: list[str], box: Box, st: Style, *, role: str = "body", bold: bool = False,
              size: float | None = None, bullets: bool = False, font: str | None = None) -> float:
    """Наибольший кегль по шкале шаблона (до читаемого минимума), при котором текст помещается в рамку."""
    size = size or st.size(role)
    font = font or (st.heading_font if role in ("title", "subtitle", "number") else st.body_font)
    allowed = st.tokens.type_scale.sizes
    w = box.w - (Emu(228600) if bullets else 0)
    fitted = fit_font_size(paragraphs, font, size, w, box.h, bold, 0.7, allowed)
    if not fitted:  # продолжаем уменьшать по шкале вниз до читаемого минимума
        floor = max(8.0, min(st.size("caption"), size))
        fitted = fit_font_size(paragraphs, font, size, w, box.h, bold, floor / size, allowed + [floor])
    size = fitted or max(8.0, min(st.size("caption"), size * 0.6))
    # слово шире рамки рендерер разорвал бы посередине: для него кегль меньше
    word, room = measure(paragraphs, font, size, w, bold).longest_word_emu, w - 2 * DEFAULT_INSET_LR
    if word > room:
        size = max(9.0, snap_down(int(size * room / word * 2) / 2, allowed, 0.85))
    return size


def add_text(slide, box: Box, paragraphs: list[str], st: Style, *, role: str = "body", color: str | None = None,
             bold: bool = False, align: str = "left", anchor: str = "top", size: float | None = None,
             bullets: bool = False, font: str | None = None, fit: bool = True, name: str = "Text"):
    """Текстовый блок в токенах шаблона с подбором кегля под рамку."""
    font = font or (st.heading_font if role in ("title", "subtitle", "number") else st.body_font)
    size = text_size(paragraphs, box, st, role=role, bold=bold, size=size, bullets=bullets, font=font) if fit else (size or st.size(role))
    tb = slide.shapes.add_textbox(Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h))
    tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[anchor]
    for i, text in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[align]
        r = p.add_run()
        r.text = text
        f = r.font
        f.name = font
        f.size = Pt(size)
        f.bold = bold
        f.color.rgb = _rgb(color or st.text)
        if bullets:
            pPr = p._p.get_or_add_pPr()
            pPr.set("marL", "228600")
            pPr.set("indent", "-228600")
            bu_clr = etree.SubElement(pPr, qn("a:buClr"))
            etree.SubElement(bu_clr, qn("a:srgbClr")).set("val", st.accent)
            etree.SubElement(pPr, qn("a:buFont")).set("typeface", "Arial")
            etree.SubElement(pPr, qn("a:buChar")).set("char", "•")
            p.space_after = Pt(size * 0.45)
    return tb


def add_rect(slide, box: Box, fill: str | None, *, rounded: bool = True, line: str | None = None, name: str = "Card"):
    """Карточка (прямоугольник, по умолчанию скруглённый)."""
    shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,
                                 Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h))
    shp.name = name
    if rounded:
        try:
            shp.adjustments[0] = 0.08
        except Exception:
            pass
    if fill:
        shp.fill.solid()
        shp.fill.fore_color.rgb = _rgb(fill)
    else:
        shp.fill.background()
    if line:
        shp.line.color.rgb = _rgb(line)
        shp.line.width = Pt(1)
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    if shp.has_text_frame:
        shp.text_frame.text = ""
    return shp


def add_circle(slide, box: Box, fill: str, text: str, st: Style, text_color: str | None = None, name: str = "Marker"):
    """Круглый маркер с текстом (номер шага)."""
    shp = slide.shapes.add_shape(MSO_SHAPE.OVAL, Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h))
    shp.name = name
    shp.fill.solid()
    shp.fill.fore_color.rgb = _rgb(fill)
    shp.line.fill.background()
    shp.shadow.inherit = False
    tf = shp.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.name = st.heading_font
    r.font.bold = True
    r.font.size = Pt(max(min(box.h / 12700 * 0.42, 28), 8))
    r.font.color.rgb = _rgb(text_color or readable_on(fill, ["FFFFFF", "000000"]))
    return shp


def add_line(slide, x1: int, y1: int, x2: int, y2: int, color: str, width_pt: float = 1.5, name: str = "Connector"):
    """Линия-соединитель."""
    ln = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(x1), Emu(y1), Emu(x2), Emu(y2))
    ln.name = name
    ln.line.color.rgb = _rgb(color)
    ln.line.width = Pt(width_pt)
    return ln


def add_picture(slide, box: Box, path: str, name: str = "Picture"):
    """Картинка, вписанная в рамку с обрезкой по центру."""
    from PIL import Image

    with Image.open(path) as im:
        iw, ih = im.size
    ratio = iw / ih
    w, h = box.w, box.h
    if w / h > ratio:
        w = int(h * ratio)
    else:
        h = int(w / ratio)
    x = box.x + (box.w - w) // 2
    y = box.y + (box.h - h) // 2
    pic = slide.shapes.add_picture(path, Emu(x), Emu(y), Emu(w), Emu(h))
    pic.name = name
    return pic


# ----------------------------------------------------------------------------
# Составные визуализации
# ----------------------------------------------------------------------------
def draw_chart(slide, box: Box, spec: ChartSpec, st: Style):
    kind = {
        "bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "pie": XL_CHART_TYPE.PIE,
        "doughnut": XL_CHART_TYPE.DOUGHNUT,
    }[spec.type]
    data = CategoryChartData()
    data.categories = spec.categories
    series = spec.series[:5]  # аудит: не больше 5 рядов
    for s in series:
        data.add_series(s.name, [float(v) for v in s.values])
    gf = slide.shapes.add_chart(kind, Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h), data)
    gf.name = "Chart"
    chart = gf.chart
    chart.has_title = False  # заголовок слайда уже формулирует вывод
    chart.font.name = st.body_font
    chart.font.size = Pt(max(st.size("caption"), min(st.size("body"), 14)))
    chart.font.color.rgb = _rgb(st.muted)
    plot = chart.plots[0]
    multi = len(series) > 1
    if spec.type in ("pie", "doughnut"):
        pts = plot.series[0].points
        for i in range(len(spec.categories)):
            pts[i].format.fill.solid()
            pts[i].format.fill.fore_color.rgb = _rgb(st.series(i))
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.RIGHT
        chart.legend.include_in_layout = False
        chart.legend.font.color.rgb = _rgb(st.text)
        plot.has_data_labels = True
        plot.data_labels.number_format = '0"' + (spec.unit if spec.unit in ("%",) else "") + '"'
        plot.data_labels.number_format_is_linked = False
        plot.data_labels.font.color.rgb = _rgb(readable_on(st.series(0), ["FFFFFF", "000000"]))
    else:
        for i, s in enumerate(plot.series):
            fmt = s.format
            if spec.type == "line":
                fmt.line.color.rgb = _rgb(st.series(i))
                fmt.line.width = Pt(2.5)
                s.smooth = False
            else:
                fmt.fill.solid()
                fmt.fill.fore_color.rgb = _rgb(st.series(i))
        if spec.type in ("bar", "column"):
            plot.gap_width = 60
            plot.overlap = -10 if multi else 0
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.font.size = Pt(max(st.size("caption"), 9))
        dl.font.color.rgb = _rgb(st.text)
        dl.number_format = "General"
        if spec.type != "line":
            dl.position = XL_LABEL_POSITION.OUTSIDE_END
        va, ca = chart.value_axis, chart.category_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = _rgb(mix(st.bg, st.text, 0.15))
        va.format.line.fill.background()
        va.tick_labels.font.color.rgb = _rgb(st.muted)
        ca.tick_labels.font.color.rgb = _rgb(st.text)
        ca.format.line.color.rgb = _rgb(mix(st.bg, st.text, 0.3))
        y_title = spec.y_title or spec.unit
        if y_title:
            va.has_title = True
            va.axis_title.text_frame.text = y_title
            r = va.axis_title.text_frame.paragraphs[0].runs[0]
            r.font.size = Pt(max(st.size("caption"), 9))
            r.font.color.rgb = _rgb(st.muted)
            r.font.bold = False
        if spec.x_title:
            ca.has_title = True
            ca.axis_title.text_frame.text = spec.x_title
            r = ca.axis_title.text_frame.paragraphs[0].runs[0]
            r.font.size = Pt(max(st.size("caption"), 9))
            r.font.color.rgb = _rgb(st.muted)
            r.font.bold = False
        chart.has_legend = multi
        if multi:
            chart.legend.position = XL_LEGEND_POSITION.TOP
            chart.legend.include_in_layout = False
            chart.legend.font.color.rgb = _rgb(st.text)
    return gf


CELL_INSET_LR, CELL_INSET_TB = int(0.08 * 914400), int(0.03 * 914400)


def table_rows(cols: list[str], rows: list[list[str]], widths: list[int], max_h: int, st: Style) -> tuple[float, list[int]]:
    """Кегль ячеек и высоты строк: наибольший кегль по шкале шаблона (для основного текста — до минимума
    подписей), при котором каждая строка с переносами по ширине колонки помещается в рамку. Строки получают
    измеренную высоту, поэтому сохранённая таблица такой же высоты, как её рисуют рендереры (строка растёт
    под текст); лишняя высота равномерно распределяется до комфортной.
    """
    base = min(st.size("body"), 16)
    floor = min(max(st.size("caption"), 10.0), base)
    sizes = sorted({s for s in st.tokens.type_scale.sizes if floor <= s <= base} | {base, floor}, reverse=True)
    need: list[int] = []
    for size in sizes:
        need = [max(measure([str(v)], st.body_font, size, widths[c], r == 0, inset_lr=CELL_INSET_LR,
                            inset_tb=CELL_INSET_TB).height_emu for c, v in enumerate(row))
                for r, row in enumerate([cols] + rows)]
        if sum(need) <= max_h:
            break
    n = len(need)
    even = int(min(max_h / n, size * 12700 * 2.6))
    heights = [max(h, even) for h in need]
    if sum(heights) > max_h >= sum(need):
        heights = [h + (max_h - sum(need)) // n for h in need]
    return size, heights


def draw_table(slide, box: Box, spec: TableSpec, st: Style):
    rows = spec.rows[: TABLE_MAX_ROWS - 1]  # аудит: шапка + 9 строк, не больше 5 колонок
    cols = spec.columns[:TABLE_MAX_COLS]
    rows = [r[: len(cols)] + [""] * (len(cols) - len(r[: len(cols)])) for r in rows]
    n_r, n_c = len(rows) + 1, len(cols)
    # первая колонка шире, если в ней подписи
    lens = [max([len(str(cols[c]))] + [len(str(r[c])) for r in rows]) for c in range(n_c)]
    total = sum(max(l, 4) for l in lens)
    widths = [int(box.w * max(l, 4) / total) for l in lens]
    size, heights = table_rows(cols, rows, widths, box.h, st)
    gf = slide.shapes.add_table(n_r, n_c, Emu(box.x), Emu(box.y), Emu(box.w), Emu(sum(heights)))
    gf.name = "Table"
    tbl = gf.table
    tblPr = gf._element.graphic.graphicData.tbl.tblPr
    sid = tblPr.find(qn("a:tableStyleId"))
    if sid is None:
        sid = etree.SubElement(tblPr, qn("a:tableStyleId"))
    sid.text = NO_STYLE_TABLE
    tbl.first_row = True
    tbl.horz_banding = False
    for c in range(n_c):
        tbl.columns[c].width = Emu(widths[c])
    for r in range(n_r):
        tbl.rows[r].height = Emu(heights[r])
    head_fill = st.accent
    head_text = readable_on(head_fill, ["FFFFFF", "000000", st.bg])
    band = mix(st.bg, st.text, 0.06 if not st.dark else 0.12)
    for r in range(n_r):
        for c in range(n_c):
            cell = tbl.cell(r, c)
            text = str(cols[c]) if r == 0 else str(rows[r - 1][c])
            cell.text = text
            cell.margin_left = cell.margin_right = Emu(CELL_INSET_LR)
            cell.margin_top = cell.margin_bottom = Emu(CELL_INSET_TB)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            fill = head_fill if r == 0 else (band if r % 2 == 0 else st.bg)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(fill)
            p = cell.text_frame.paragraphs[0]
            numeric = r > 0 and _is_num(text)
            p.alignment = PP_ALIGN.RIGHT if numeric else PP_ALIGN.LEFT
            for run in p.runs:
                run.font.name = st.body_font
                run.font.size = Pt(size)
                run.font.bold = r == 0
                run.font.color.rgb = _rgb(head_text if r == 0 else readable_on(fill, [st.text, "000000", "FFFFFF"]))
    return gf


def _is_num(s: str) -> bool:
    """Строка — число (допускает знак, пробелы и единицу)."""
    t = s.replace(" ", "").replace(" ", "").replace(",", ".").rstrip("%₽$€").lstrip("+-−~≈<>")
    try:
        float(t)
        return True
    except ValueError:
        return False


def _columns(box: Box, n: int, gap_ratio: float = 0.04) -> list[Box]:
    """Делит рамку на n колонок с промежутками."""
    gap = int(box.w * gap_ratio)
    w = int((box.w - gap * (n - 1)) / n)
    return [Box(x=box.x + i * (w + gap), y=box.y, w=w, h=box.h) for i in range(n)]


def _grid(box: Box, n: int) -> list[Box]:
    """Сетка рамок для n элементов (до 4 — в один ряд)."""
    if n <= 4:
        return _columns(box, n)
    cols = 3 if n in (5, 6, 9) else 4
    rows = -(-n // cols)
    gap_x, gap_y = int(box.w * 0.03), int(box.h * 0.05)
    w = int((box.w - gap_x * (cols - 1)) / cols)
    h = int((box.h - gap_y * (rows - 1)) / rows)
    return [Box(x=box.x + (i % cols) * (w + gap_x), y=box.y + (i // cols) * (h + gap_y), w=w, h=h) for i in range(n)]


def draw_kpis(slide, box: Box, items: list[Item], st: Style):
    """Плитки KPI: крупное значение и подпись."""
    items = items[:4]
    cells = _columns(box, len(items), 0.05)
    num_size = max(st.size("number"), st.size("title") * 1.8)
    labels = [p for it in items for p in (it.title if it.value else "", it.text) if p]
    label_size = comfortable_size(labels, st, Box(x=0, y=0, w=cells[0].w, h=int(cells[0].h * 0.4)), fill=0.9)
    for it, c in zip(items, cells):
        nh = int(min(c.h * 0.45, num_size * 12700 * 1.25))
        value = it.value or it.title
        # плитка без числа показывает заголовок кеглем заголовка, а не кеглем числа
        start = min(num_size, nh / 12700 / 1.2) if it.value else min(st.size("title"), nh / 12700 / 1.2)
        num_font_size = fit_font_size([value], st.heading_font, start, c.w, nh, True, 0.4) or start * 0.5
        num_font_size = snap_down(num_font_size, st.tokens.type_scale.sizes)
        add_text(slide, Box(x=c.x, y=c.y, w=c.w, h=nh), [value], st, role="number", color=st.accent_text, bold=True,
                 size=num_font_size, fit=False, name="KPI value")
        add_line(slide, c.x + 91440, c.y + nh + 30000, c.x + int(c.w * 0.35), c.y + nh + 30000, st.accent, 2.0, name="KPI rule")
        label = [p for p in (it.title if it.value else "", it.text) if p]
        add_text(slide, Box(x=c.x, y=c.y + nh + 90000, w=c.w, h=c.h - nh - 90000), label or [""], st, role="body",
                 size=label_size, name="KPI label")


def draw_process(slide, box: Box, items: list[Item], st: Style, numbered: bool = True):
    """Горизонтальный процесс / таймлайн: маркеры на линии + заголовок/текст под каждым."""
    items = items[:6]
    n = len(items)
    cells = _columns(box, n, 0.03)
    d = int(min(box.h * 0.2, cells[0].w * 0.35, 914400 * 0.75))
    cy = box.y + d // 2
    add_line(slide, cells[0].x + d // 2, cy, cells[-1].x + d // 2, cy, mix(st.accent, st.bg, 0.5), 2.0, name="Process line")
    for i, (it, c) in enumerate(zip(items, cells)):
        marker = it.value if (it.value and len(it.value) <= 4) else f"{i + 1}"
        add_circle(slide, Box(x=c.x, y=box.y, w=d, h=d), st.accent, marker if numbered else "", st, name="Step marker")
        ty = box.y + d + int(box.h * 0.06)
        title_h = int(box.h * 0.22)
        if it.value and len(it.value) > 4:
            add_text(slide, Box(x=c.x, y=ty - int(box.h * 0.02), w=c.w, h=int(box.h * 0.1)), [it.value], st, role="caption",
                     color=st.accent_text, bold=True, name="Step label")
            ty += int(box.h * 0.08)
        add_text(slide, Box(x=c.x, y=ty, w=c.w, h=title_h), [it.title], st, role="subtitle", bold=True, name="Step title")
        if it.text:
            add_text(slide, Box(x=c.x, y=ty + title_h, w=c.w, h=box.b - ty - title_h), [it.text], st, role="body",
                     color=st.muted, name="Step text")


def draw_cards(slide, box: Box, items: list[Item], st: Style, icons: list[str | None] | None = None):
    # короткий пункт без заголовка сам становится заголовком карточки (мелкий текст один в высокой карточке
    # выглядит пустой карточкой)
    items = [it.model_copy(update={"title": it.text, "text": ""}) if not it.title and it.text and len(it.text) <= 70 else it
             for it in items[:8]]
    cells = _grid(box, len(items))
    row = len(items) <= 4
    # карточки остаются компактными (не растягиваются на всю область), если тексту не нужно больше места
    compact = min(cells[0].h, int(box.h * 0.62)) if row else cells[0].h
    pad = int(min(cells[0].w, compact) * 0.08)
    w, gap = cells[0].w - 2 * pad, int(pad * 0.4)
    card = st.card
    on_card = Style(tokens=st.tokens, bg=card, dark=rel_luminance(card) < 0.4)
    allowed = st.tokens.type_scale.sizes
    start = min(st.size("subtitle"), st.size("body") * 1.3)
    icon_s = int(min(compact * 0.22, cells[0].w * 0.25, 914400 * 0.55))
    # для каждой карточки по компактной высоте: иконка, отступ значения/заголовка, высота значения, заголовок,
    # кегль, высота
    parts = []
    for i, it in enumerate(items):
        icon = icons[i] if icons and i < len(icons) else None
        top = pad + (icon_s + int(pad * 0.6) if icon else 0)
        vh = int((compact - top) * 0.34) if it.value else 0
        head = it.title or it.value
        rest = compact - top - vh - pad
        size = th = 0
        if it.text:
            # заголовок занимает нужное число строк (не больше 40% места), текст начинается сразу под ним
            size = fit_font_size([head], st.heading_font, start, w, int(rest * 0.4), True, 0.6, allowed + [start]) \
                or snap_down(start * 0.6, allowed)
            th = min(measure([head], st.heading_font, size, w, True).height_emu, int(rest * 0.4))
        parts.append((icon, top, vh, head, size, th))
    # один кегль на весь ряд: наибольший, при котором текст помещается в каждую карточку максимальной высоты;
    # затем ряд карточек растёт от компактного ровно настолько, насколько нужно тексту (текст не выходит из
    # карточки)
    body = min((text_size([it.text], Box(x=0, y=0, w=w, h=max(c.h - pad - top - vh - th - gap, 1)), on_card)
                for it, c, (_, top, vh, _, _, th) in zip(items, cells, parts) if it.text), default=st.size("body"))
    if row:
        need = [top + vh + th + gap + measure([it.text], st.body_font, body, w).height_emu + pad
                for it, (_, top, vh, _, _, th) in zip(items, parts) if it.text]
        h = min(max([compact] + need), cells[0].h)
        cells = [Box(x=c.x, y=c.y, w=c.w, h=h) for c in cells]
    for it, c, (icon, top, vh, head, size, th) in zip(items, cells, parts):
        add_rect(slide, c, card, rounded=True, name="Card")
        if icon:
            add_picture(slide, Box(x=c.x + pad, y=c.y + pad, w=icon_s, h=icon_s), icon, name="Icon")
        y = c.y + top
        if it.value:
            add_text(slide, Box(x=c.x + pad, y=y, w=w, h=vh), [it.value], on_card, role="number",
                     color=on_card.accent_text, bold=True,
                     size=snap_down(min(st.size("number"), vh / 12700 / 1.25), st.tokens.type_scale.sizes), name="Card value")
            y += vh
        if not it.text:
            add_text(slide, Box(x=c.x + pad, y=y, w=w, h=int((c.b - y) * 0.9)), [head], on_card, role="subtitle",
                     bold=True, name="Card title")
            continue
        add_text(slide, Box(x=c.x + pad, y=y, w=w, h=th), [head], on_card, role="subtitle", bold=True, size=size,
                 fit=False, name="Card title")
        add_text(slide, Box(x=c.x + pad, y=y + th + gap, w=w, h=max(c.b - pad - y - th - gap, 1)), [it.text], on_card,
                 role="body", color=on_card.muted, size=body, fit=False, name="Card text")


def draw_quote(slide, box: Box, quote: str, author: str, st: Style):
    """Цитата с крупной кавычкой и автором."""
    mark_h = int(box.h * 0.22)
    add_text(slide, Box(x=box.x, y=box.y, w=int(box.w * 0.2), h=mark_h), ["«"], st, role="number", color=st.accent_text,
             bold=True, size=min(st.size("number") * 1.4, mark_h / 12700 / 1.3), fit=False, name="Quote mark")
    qh = int(box.h * 0.55)
    add_text(slide, Box(x=box.x, y=box.y + mark_h, w=int(box.w * 0.85), h=qh), [quote], st, role="subtitle",
             size=st.size("title") * 0.85, name="Quote")
    if author:
        add_text(slide, Box(x=box.x, y=box.y + mark_h + qh, w=int(box.w * 0.85), h=int(box.h * 0.15)), [author], st,
                 role="body", color=st.accent_text, name="Quote author")


def comfortable_size(paragraphs: list[str], st: Style, box: Box, bullets: bool = False, fill: float = 0.7) -> float:
    """Наибольший кегль шкалы шаблона между основным и ~1,7× основного, который заполняет не больше `fill`
    рамки.
    """
    body = st.size("body")
    cands = sorted({s for s in st.tokens.type_scale.sizes if body <= s <= body * 1.7} | {body}, reverse=True)
    w = box.w - (228600 if bullets else 0)
    for s in cands:
        m = measure(paragraphs, st.body_font, s, w, spacing=1.2 * 1.35 if bullets else 1.2)
        if m.height_emu <= box.h * fill and m.longest_word_emu < w:
            return s
    return body


def draw_bullets(slide, box: Box, bullets: list[str], st: Style, max_bullets: int = 6):
    """Список пунктов комфортным кеглем."""
    bullets = [b for b in bullets if b.strip()][:max_bullets]
    size = comfortable_size(bullets, st, box, bullets=True)
    return add_text(slide, box, bullets, st, role="body", bullets=True, size=size, name="Bullets")


def draw_image_text(slide, box: Box, image: str | None, paragraphs: list[str], st: Style, image_left: bool = False):
    """Картинка и текст в две колонки."""
    cols = _columns(box, 2, 0.05)
    img_box, txt_box = (cols[0], cols[1]) if image_left else (cols[1], cols[0])
    if image:
        add_picture(slide, img_box, image, name="Illustration")
    else:
        txt_box = box
    draw_bullets(slide, txt_box, paragraphs, st)
