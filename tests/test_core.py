"""Unit tests: geometry, colours, OOXML inheritance helpers, JSON extraction, skills registry."""
import pytest

from decksmith.core.models import Box
from decksmith.generation.llm import extract_json
from decksmith.parsing.ooxml import color_distance, contrast_ratio, flatten_shapes


def test_box_ops():
    a, b = Box(x=0, y=0, w=10, h=10), Box(x=5, y=5, w=10, h=10)
    assert a.intersection(b) == 25
    assert a.union(b) == Box(x=0, y=0, w=15, h=15)
    assert a.contains(Box(x=1, y=1, w=2, h=2))


def test_contrast_wcag():
    assert round(contrast_ratio("000000", "FFFFFF"), 1) == 21.0
    assert contrast_ratio("8F8F8F", "FFFFFF") < 4.5  # template grey fails WCAG AA
    assert color_distance("0077FF", "0077FF") == 0


def test_group_transform_flattening():
    """Children of a scaled group get absolute slide coordinates."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    g = s.shapes.add_group_shape()
    g.shapes.add_shape(1, Emu(1000), Emu(1000), Emu(1000), Emu(1000))
    recs = [r for r in flatten_shapes(s.shapes) if r.depth == 1]
    assert recs and recs[0].box.w > 0


@pytest.mark.parametrize("raw", [
    '```json\n{"a": 1}\n```',
    'Вот ответ: {"a": 1} надеюсь помог',
    '<think>...</think>{"a": {"b": [1, 2]}}',
    '{"a": [1, 2',  # truncated output is closed
])
def test_extract_json(raw):
    assert "a" in extract_json(raw)


def test_skills_registry_and_rendering():
    from decksmith.generation.skills import list_versions, load_agent, load_skill

    v = list_versions()
    for name in ("outline", "slide_writer", "shortener", "visual_audit", "fixer"):
        assert v["skills"][name]["active"] in v["skills"][name]["versions"]
        sk = load_skill(name)
        assert sk.sha256 and sk.user
    agent = load_agent()
    assert agent.step("outline")["skill"].startswith("outline@")
    _, user = load_skill("outline").render(
        brief="b", purpose="product", purpose_hint="", audience="", n_slides=12, capabilities="- cards",
        context="ctx", language="ru", title_chars=60,
    )
    assert "12" in user and "ctx" in user
