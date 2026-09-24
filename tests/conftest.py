import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("DECKSMITH_LLM_PROVIDER", "offline")  # tests never call a model

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "data" / "templates").glob("*.pptx"))


@pytest.fixture(scope="session")
def plan():
    from decksmith.core.models import DeckPlan

    return DeckPlan.model_validate_json((ROOT / "tests" / "fixtures" / "plan_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def profiles():
    """The organiser's dataset templates (not redistributed in the repo: put them into data/templates/)."""
    from decksmith.parsing.template_parser import analyze_template

    if not TEMPLATES:
        pytest.skip("dataset templates not present in data/templates/")
    return {t.stem: analyze_template(t) for t in TEMPLATES}


@pytest.fixture(scope="session")
def unknown_template(tmp_path_factory) -> Path:
    """A template the system has never seen: stock Office theme with a few example slides."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Название презентации"
    s.placeholders[1].text = "Подзаголовок"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Заголовок"
    s.placeholders[1].text_frame.text = "Пункт"
    for _ in range(3):
        s.placeholders[1].text_frame.add_paragraph().text = "Пункт"
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Заголовок"
    for i in range(3):
        box = s.shapes.add_shape(5, Inches(0.8 + i * 4.1), Inches(2.2), Inches(3.8), Inches(3.5))
        box.fill.solid()
        box.fill.fore_color.rgb = __import__("pptx.dml.color", fromlist=["RGBColor"]).RGBColor(0xEE, 0xF2, 0xF8)
        t = s.shapes.add_textbox(Inches(1.0 + i * 4.1), Inches(2.4), Inches(3.4), Inches(0.6))
        t.text_frame.text = "Заголовок"
        t.text_frame.paragraphs[0].runs[0].font.size = Pt(20)
        t.text_frame.paragraphs[0].runs[0].font.bold = True
        d = s.shapes.add_textbox(Inches(1.0 + i * 4.1), Inches(3.1), Inches(3.4), Inches(1.6))
        d.text_frame.text = "Описание"
        d.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    p = tmp_path_factory.mktemp("unknown") / "unknown_office.pptx"
    prs.save(p)
    return p
