"""Layout layer: deterministic selection, clean output, native objects, three distinct variants."""
import re

import pytest
from pptx import Presentation

from decksmith.layout.builder import DeckBuilder
from decksmith.layout.selector import load_variants, select_layouts

pytestmark = pytest.mark.slow
LEFTOVER = re.compile(r"lorem ipsum|^\s*(заголовок|текст|пункт|описание|имя фамилия|должность)\s*$|вставить фото", re.I | re.M)


def _build(profile, plan, variant, tmp_path):
    decisions = select_layouts(plan, profile, variant)
    b = DeckBuilder(profile, variant)
    rep = b.build(plan, decisions)
    return b.save(tmp_path / f"{profile.name}_{variant.name}.pptx"), decisions, rep


def test_selection_is_deterministic(profiles, plan):
    v = load_variants()["balanced"]
    a = [d.model_dump() for d in select_layouts(plan, profiles["vk_tech"], v)]
    b = [d.model_dump() for d in select_layouts(plan, profiles["vk_tech"], v)]
    assert a == b


@pytest.mark.parametrize("name", ["vk_tech", "vk_workspace", "vk_education"])
def test_build_is_clean_and_native(profiles, plan, tmp_path, name):
    prof = profiles[name]
    path, decisions, rep = _build(prof, plan, load_variants()["balanced"], tmp_path)
    prs = Presentation(str(path))
    assert len(prs.slides) == len(plan.slides)
    layout_names = {l.name for l in prof.layouts}
    charts = tables = 0
    for s in prs.slides:
        assert s.slide_layout.name in layout_names, "every slide sits on a template layout"
        for sh in s.shapes:
            if sh.has_text_frame:
                assert not LEFTOVER.search(sh.text_frame.text), f"template filler left: {sh.text_frame.text!r}"
            charts += bool(getattr(sh, "has_chart", False) and sh.has_chart)
            tables += bool(getattr(sh, "has_table", False) and sh.has_table)
        big_pics = [sh for sh in s.shapes if sh.shape_type == 13 and sh.width * sh.height > 0.85 * prs.slide_width * prs.slide_height]
        texts = [sh for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
        assert not (big_pics and not texts), "slide exported as a single raster image"
    assert charts >= 1 and tables >= 1, "data slides are native chart/table objects"
    assert all(d.rationale for d in decisions)


def test_variants_differ(profiles, plan, tmp_path):
    prof = profiles["vk_education"]
    choices = {}
    for name, v in load_variants().items():
        from decksmith.pipeline import apply_variant

        vplan = apply_variant(plan, v)
        choices[name] = [(d.mode, d.pattern_id or d.compose_kind) for d in select_layouts(vplan, prof, v)]
    assert choices["balanced"] != choices["visual"] != choices["dense"]


def test_unknown_template_builds(unknown_template, plan, tmp_path):
    from decksmith.parsing.template_parser import analyze_template

    prof = analyze_template(unknown_template)
    path, decisions, _ = _build(prof, plan, load_variants()["balanced"], tmp_path)
    assert len(Presentation(str(path)).slides) == len(plan.slides)
