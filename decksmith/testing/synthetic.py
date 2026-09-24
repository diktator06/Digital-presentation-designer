"""Synthetic templates for blind robustness tests.

Each generator builds a .pptx the system has never seen, each stressing a
different weak spot of template parsing:

  office_43_empty   stock Office theme, 4:3, *no example slides at all*
  brand_footer      custom theme colours/fonts, master band + footer + page number,
                    cover title as a plain text box, native table & chart examples,
                    "Confidential" footer text box repeated on every slide
  dark_minimal      dark master background, light text, orange accent
  blank_layouts     no placeholders in any layout (Google-Slides-like): every title
                    on example slides is a free text box
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

RT_THEME = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"


def _set_theme(prs, colors: dict[str, str] | None = None, major: str | None = None, minor: str | None = None) -> None:
    for master in prs.slide_masters:
        part = master.part.part_related_by(RT_THEME)
        root = etree.fromstring(part.blob)
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        if colors:
            cs = root.find(".//a:clrScheme", ns)
            for name, hex_ in colors.items():
                el = cs.find(f"a:{name}", ns)
                if el is None:
                    continue
                for c in list(el):
                    el.remove(c)
                etree.SubElement(el, qn("a:srgbClr")).set("val", hex_)
        fs = root.find(".//a:fontScheme", ns)
        if major:
            fs.find("a:majorFont/a:latin", ns).set("typeface", major)
        if minor:
            fs.find("a:minorFont/a:latin", ns).set("typeface", minor)
        part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _to_master(prs, build) -> None:
    """Draw shapes on a scratch slide and move them onto the slide master."""
    scratch = prs.slides.add_slide(prs.slide_layouts[6])
    build(scratch)
    master_tree = prs.slide_master.shapes._spTree
    for sh in list(scratch.shapes):
        master_tree.append(copy.deepcopy(sh._element))
    lst = prs.slides._sldIdLst
    sid = list(lst)[-1]
    prs.part.drop_rel(sid.rId)
    lst.remove(sid)


def _text(slide, x, y, w, h, text, size=14, bold=False, color=None, font=None, align=None):
    tb = slide.shapes.add_textbox(Emu(int(x)), Emu(int(y)), Emu(int(w)), Emu(int(h)))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, line in enumerate(text if isinstance(text, list) else [text]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
        if font:
            r.font.name = font
        if align is not None:
            p.alignment = align
    return tb


def _rect(slide, x, y, w, h, fill, rounded=True):
    s = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE, Emu(int(x)), Emu(int(y)), Emu(int(w)), Emu(int(h)))
    s.fill.solid()
    s.fill.fore_color.rgb = RGBColor.from_string(fill)
    s.line.fill.background()
    return s


def _title_only(prs):
    return next((l for l in prs.slide_layouts if l.name.lower().startswith("title only")), prs.slide_layouts[5])


# ----------------------------------------------------------------------------
def office_43_empty(path: Path) -> Path:
    prs = Presentation()  # 10 x 7.5 in, Office theme, 11 layouts, zero slides
    prs.save(path)
    return path


def brand_footer(path: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    _set_theme(prs, {"accent1": "0B7A75", "accent2": "F2A541", "accent3": "7A4EAB", "dk2": "12343B", "lt2": "EEF4F3"},
               major="Montserrat", minor="DejaVu Sans")

    def master(s):
        _rect(s, 0, 0, W, int(H * 0.035), "0B7A75", rounded=False)
        _text(s, W * 0.05, H * 0.93, W * 0.5, H * 0.05, "ООО «Альфа» · для внутреннего использования", 9, color="7F8C8D")
        _text(s, W * 0.9, H * 0.93, W * 0.06, H * 0.05, "Альфа", 9, bold=True, color="0B7A75")
    _to_master(prs, master)
    to = _title_only(prs)
    blank = prs.slide_layouts[6]

    # cover: title as a plain text box in the lower half
    s = prs.slides.add_slide(blank)
    _rect(s, 0, 0, W, H, "12343B", rounded=False)
    _text(s, W * 0.07, H * 0.52, W * 0.7, H * 0.18, "Название презентации", 44, bold=True, color="FFFFFF", font="Montserrat")
    _text(s, W * 0.07, H * 0.72, W * 0.6, H * 0.08, "Подзаголовок презентации", 18, color="C9E4E2")
    # section
    s = prs.slides.add_slide(to)
    s.shapes.title.text = "Название раздела"
    # three cards
    s = prs.slides.add_slide(to)
    s.shapes.title.text = "Заголовок слайда"
    for i in range(3):
        x = W * 0.06 + i * W * 0.305
        _rect(s, x, H * 0.3, W * 0.28, H * 0.5, "EEF4F3")
        _text(s, x + W * 0.02, H * 0.34, W * 0.24, H * 0.08, "Заголовок", 20, bold=True, color="12343B")
        _text(s, x + W * 0.02, H * 0.44, W * 0.24, H * 0.25, "Описание пункта в две-три строки текста", 14, color="3D4F53")
    # four-step timeline
    s = prs.slides.add_slide(to)
    s.shapes.title.text = "Этапы проекта"
    ln = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int(W * 0.1)), Emu(int(H * 0.42)), Emu(int(W * 0.9)), Emu(int(H * 0.42)))
    ln.line.color.rgb = RGBColor.from_string("0B7A75")
    for i in range(4):
        x = W * 0.08 + i * W * 0.22
        c = s.shapes.add_shape(MSO_SHAPE.OVAL, Emu(int(x + W * 0.06)), Emu(int(H * 0.38)), Emu(int(H * 0.08)), Emu(int(H * 0.08)))
        c.fill.solid()
        c.fill.fore_color.rgb = RGBColor.from_string("0B7A75")
        c.line.fill.background()
        _text(s, x, H * 0.5, W * 0.2, H * 0.06, f"0{i + 1}", 24, bold=True, color="0B7A75")
        _text(s, x, H * 0.57, W * 0.2, H * 0.06, "Этап", 16, bold=True, color="12343B")
        _text(s, x, H * 0.64, W * 0.2, H * 0.15, "Описание этапа работ", 12, color="3D4F53")
    # KPI tiles
    s = prs.slides.add_slide(to)
    s.shapes.title.text = "Ключевые показатели"
    for i in range(3):
        x = W * 0.06 + i * W * 0.305
        _text(s, x, H * 0.35, W * 0.28, H * 0.15, "95%", 54, bold=True, color="0B7A75", font="Montserrat")
        _text(s, x, H * 0.52, W * 0.28, H * 0.1, "Описание показателя", 14, color="3D4F53")
    # native table example
    s = prs.slides.add_slide(to)
    s.shapes.title.text = "Сравнение вариантов"
    t = s.shapes.add_table(4, 3, Emu(int(W * 0.06)), Emu(int(H * 0.28)), Emu(int(W * 0.88)), Emu(int(H * 0.4))).table
    for r in range(4):
        for c in range(3):
            t.cell(r, c).text = ["Параметр", "Вариант А", "Вариант Б"][c] if r == 0 else f"Строка {r}" if c == 0 else str(r * 10 + c)
    # native chart example
    s = prs.slides.add_slide(to)
    s.shapes.title.text = "Динамика выручки"
    cd = CategoryChartData()
    cd.categories = ["2021", "2022", "2023", "2024"]
    cd.add_series("Выручка", (10, 14, 19, 25))
    s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Emu(int(W * 0.06)), Emu(int(H * 0.25)), Emu(int(W * 0.88)), Emu(int(H * 0.6)), cd)
    # quote
    s = prs.slides.add_slide(blank)
    _text(s, W * 0.1, H * 0.25, W * 0.8, H * 0.35, "«Текст цитаты, который занимает несколько строк и выражает главную мысль спикера»", 28, color="12343B")
    _text(s, W * 0.1, H * 0.65, W * 0.6, H * 0.06, "Имя Фамилия, должность", 14, color="0B7A75")
    # thanks
    s = prs.slides.add_slide(blank)
    _rect(s, 0, 0, W, H, "0B7A75", rounded=False)
    _text(s, W * 0.07, H * 0.4, W * 0.8, H * 0.15, "Спасибо за внимание!", 44, bold=True, color="FFFFFF", font="Montserrat")
    # "confidential" text repeated on every example slide (brand text, must survive)
    for sl in prs.slides:
        _text(sl, W * 0.62, H * 0.93, W * 0.25, H * 0.05, "Конфиденциально", 9, color="7F8C8D")
    prs.save(path)
    return path


def dark_minimal(path: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    # swap light/dark so default text is light on a dark background
    _set_theme(prs, {"dk1": "F4F4F4", "lt1": "15171C", "dk2": "D0D0D0", "lt2": "22252C", "accent1": "FF7A1A", "accent2": "3DDC97"})
    bg = prs.slide_master.background.fill
    bg.solid()
    bg.fore_color.rgb = RGBColor.from_string("15171C")
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Название"
    s.placeholders[1].text = "Подзаголовок"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Заголовок"
    tf = s.placeholders[1].text_frame
    tf.text = "Первый пункт"
    for t in ("Второй пункт", "Третий пункт"):
        tf.add_paragraph().text = t
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Заголовок"
    for i in range(2):
        x = W * 0.06 + i * W * 0.46
        _rect(s, x, H * 0.3, W * 0.42, H * 0.5, "22252C")
        _text(s, x + W * 0.03, H * 0.34, W * 0.36, H * 0.08, "Заголовок", 22, bold=True, color="FF7A1A")
        _text(s, x + W * 0.03, H * 0.45, W * 0.36, H * 0.25, "Текст описания", 16, color="F4F4F4")
    prs.save(path)
    return path


def blank_layouts(path: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    for layout in prs.slide_layouts:
        for ph in list(layout.placeholders):
            ph._element.getparent().remove(ph._element)
    blank = prs.slide_layouts[6]
    s = prs.slides.add_slide(blank)
    _text(s, W * 0.08, H * 0.35, W * 0.8, H * 0.2, "Название презентации", 40, bold=True, color="1F2A44")
    _text(s, W * 0.08, H * 0.56, W * 0.7, H * 0.08, "Подзаголовок", 18, color="5B6B8C")
    s = prs.slides.add_slide(blank)
    _text(s, W * 0.06, H * 0.06, W * 0.85, H * 0.1, "Заголовок слайда", 28, bold=True, color="1F2A44")
    for i in range(4):
        x = W * 0.06 + i * W * 0.225
        _rect(s, x, H * 0.3, W * 0.205, H * 0.45, "E9EEF7")
        _text(s, x + W * 0.015, H * 0.33, W * 0.18, H * 0.07, "Заголовок", 16, bold=True, color="1F2A44")
        _text(s, x + W * 0.015, H * 0.41, W * 0.18, H * 0.25, "Описание", 12, color="42506B")
    s = prs.slides.add_slide(blank)
    _text(s, W * 0.06, H * 0.06, W * 0.85, H * 0.1, "Заголовок слайда", 28, bold=True, color="1F2A44")
    _text(s, W * 0.06, H * 0.25, W * 0.85, H * 0.6, ["Пункт списка", "Пункт списка", "Пункт списка"], 18, color="42506B")
    prs.save(path)
    return path


GENERATORS = {"office_43_empty": office_43_empty, "brand_footer": brand_footer, "dark_minimal": dark_minimal,
              "blank_layouts": blank_layouts}


def generate_all(out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return {name: fn(out / f"synthetic_{name}.pptx") for name, fn in GENERATORS.items()}


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s)
