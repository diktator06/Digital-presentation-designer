"""Бизнес-сценарий от начала до конца (офлайн-планировщик): незнакомый шаблон + контент-пакет -> 3 колоды в срок.

Работает на чистом клоне: синтетический шаблон + собственный пример контент-пакета репозитория.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from decksmith.generation.planner import Brief
from decksmith.pipeline import Pipeline
from decksmith.testing.synthetic import brand_footer

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]


def test_end_to_end_three_variants_under_five_minutes(tmp_path):
    pipe = Pipeline()
    template = brand_footer(tmp_path / "unknown_brand.pptx")
    profile = pipe.prepare_template(template)  # до Enter
    corpus = pipe.prepare_content(sorted((ROOT / "data" / "content").glob("*.md")))  # до Enter
    brief = Brief(text="Сервис генерации презентаций в фирменном шаблоне по брифу", purpose="product", n_slides=12)
    t0 = time.time()
    res = asyncio.run(pipe.run(profile, corpus, brief))
    elapsed = time.time() - t0
    assert elapsed < 300, "3 decks must be ready within 5 minutes after Enter"
    assert len(res.variants) == 3
    for v in res.variants:
        assert not v.error
        for f in (v.pptx, v.pdf, v.html):
            assert f and Path(f).exists() and Path(f).stat().st_size > 1000
        assert v.audit is not None and v.audit.score > 60
    m = json.loads(Path(res.manifest).read_text(encoding="utf-8"))
    assert m["skills"]["outline"]["skill"] == "outline@v3" and m["skills"]["outline"]["sha256"]
    assert all(s["rationale"] for v in m["variants"] for s in v["slides"])
