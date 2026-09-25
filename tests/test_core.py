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


def test_font_download_is_time_bounded(monkeypatch):
    """No network (or a slow one) must never stall template analysis: one failed request
    switches downloads off for a while."""
    import time

    import httpx

    from decksmith.core import fonts

    calls = []

    class Offline(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(str(request.url))
            raise httpx.ConnectTimeout("offline")

    real_client = httpx.Client
    monkeypatch.setattr(fonts.httpx, "Client", lambda **kw: real_client(transport=Offline(), **kw))
    monkeypatch.setattr(fonts, "_NET_DOWN_UNTIL", 0.0)
    t0 = time.monotonic()
    assert not fonts.try_download_google_font("Some Open Family")
    assert not fonts.try_download_google_font("Another Open Family")
    assert len(calls) == 1 and time.monotonic() - t0 < 2
    assert fonts.split_weight("Montserrat Medium") == ("Montserrat", "medium")
    assert fonts.split_weight("Open Sans Extra Bold") == ("Open Sans", "extrabold")
    assert fonts.split_weight("Open Sans") == ("Open Sans", None)


def test_render_timeout_kills_process_group():
    """A hung renderer whose children keep running must not block past its timeout."""
    import subprocess
    import sys
    import time

    import pytest

    from decksmith.render.soffice import _run

    if sys.platform.startswith("win"):
        pytest.skip("POSIX process groups")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run(["/bin/sh", "-c", "sleep 30 & sleep 30"], 1)
    assert time.monotonic() - t0 < 5


def test_offline_plan_meets_slide_range():
    """Without a model the deck has the requested number of slides (TZ: 10-15 or as set by
    the user) and a clean cover."""
    from pathlib import Path

    from decksmith.content.ingest import ingest
    from decksmith.generation.planner import Brief, plan_offline

    root = Path(__file__).resolve().parents[1]
    corpus = ingest(sorted((root / "data" / "content").glob("*.md")))
    brief = Brief(text="Цифровой дизайнер презентаций — сервис, который по брифу и контент-пакету собирает "
                       "презентацию в фирменном шаблоне компании и сам проверяет её качество.",
                  purpose="product", n_slides=12, language="ru")
    plan = plan_offline(brief, corpus)
    assert 10 <= len(plan.slides) <= 15
    assert plan.title == "Цифровой дизайнер презентаций"
    assert plan.subtitle.startswith("Сервис") and plan.subtitle != "product"
    assert not any(s.title.startswith("#") or "|" in s.title for s in plan.slides)
    for n in (6, 8, 12):
        brief.n_slides = n
        assert len(plan_offline(brief, corpus).slides) == n, n


def test_model_plan_is_completed_to_the_requested_count():
    """A model answering one slide short (seen on a hosted Qwen) still yields the requested count."""
    from decksmith.core.models import DeckPlan, Item, PatternKind, SlideSpec
    from decksmith.generation.planner import Brief, Outline, OutlineSlide, _normalize_outline, complete_plan

    o = Outline(title="Колода", slides=[OutlineSlide(intent="title", title="Колода")]
                + [OutlineSlide(intent="cards", title=f"Вывод {i}") for i in range(8)]
                + [OutlineSlide(intent="thanks", title="Спасибо")])
    o = _normalize_outline(o, Brief(text="Колода", n_slides=12, language="ru"))
    assert len(o.slides) == 12
    assert o.slides[-2].message == "summary" and o.slides[1].intent == "agenda"

    plan = DeckPlan(title="Колода", slides=[
        SlideSpec(id="s1", intent=PatternKind.title, title="Колода"),
        SlideSpec(id="s2", intent=PatternKind.cards, title="Шесть причин", items=[Item(title=f"П{i}") for i in range(6)]),
        SlideSpec(id="s3", intent=PatternKind.text, title="Главное", notes="\n[summary]"),
        SlideSpec(id="s4", intent=PatternKind.thanks, title="Спасибо")])
    complete_plan(plan, 5)
    assert len(plan.slides) == 5 and [s.id for s in plan.slides] == [f"s{i}" for i in range(1, 6)]
    assert [len(s.items) for s in plan.slides[1:3]] == [3, 3], "the longest list is split in two"
    assert plan.slides[3].bullets, "an empty summary lists the conclusions"


def test_every_variant_keeps_the_slide_count():
    """TZ: the number of slides the user asked for holds for all three variants."""
    from pathlib import Path

    from decksmith.core.models import DeckPlan
    from decksmith.layout.selector import load_variants
    from decksmith.pipeline import apply_variant

    plan = DeckPlan.model_validate_json((Path(__file__).parent / "fixtures" / "plan_sample.json").read_text(encoding="utf-8"))
    for name, v in load_variants().items():
        vp = apply_variant(plan, v)
        assert len(vp.slides) == len(plan.slides), name
    dense = apply_variant(plan, load_variants()["dense"])
    assert "[summary]" in (dense.slides[1].notes or ""), "the analytic variant opens with its conclusions"
