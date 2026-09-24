"""Brief + content corpus -> DeckPlan.

Two LLM stages (fast and robust vs. one huge JSON):
  1. outline      : slide list with intents and conclusion-style titles (1 call)
  2. slide_writer : content of every slide, all calls in parallel

Offline mode (no endpoint configured) uses an extractive, deterministic planner
so the whole pipeline — and the test-suite — runs without a model.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter

from pydantic import BaseModel, Field

from decksmith.content.ingest import select_context
from decksmith.core.models import (
    ChartSeries,
    ChartSpec,
    ContentCorpus,
    DeckPlan,
    Item,
    PatternKind,
    SlideSpec,
    TableSpec,
    TemplateProfile,
)
from decksmith.generation.llm import LLMClient, LLMError
from decksmith.generation.skills import Agent, skill_for_step

log = logging.getLogger(__name__)

class Brief(BaseModel):
    text: str
    purpose: str = "product"
    n_slides: int = 12
    audience: str = ""
    language: str = "ru"
    author: str = ""


class OutlineSlide(BaseModel):
    intent: str = "text"
    title: str
    message: str = ""
    n_items: int = 0
    facts: list[str] = Field(default_factory=list)
    data: str = ""


class Outline(BaseModel):
    title: str
    subtitle: str = ""
    slides: list[OutlineSlide]


class WrittenSlide(BaseModel):
    title: str = ""
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    quote: str = ""
    quote_author: str = ""
    image_prompt: str = ""
    notes: str = ""


VALID = {k.value for k in PatternKind} - {"guide", "free"}


def template_capabilities(profile: TemplateProfile) -> tuple[str, int]:
    """Short human-readable summary of what the template can lay out + title budget."""
    by_kind: dict[str, set[int]] = {}
    for p in profile.usable_patterns():
        if p.score_hint < 0.3:
            continue
        by_kind.setdefault(p.kind.value, set()).add(p.n_items)
    lines = []
    for k, ns in sorted(by_kind.items()):
        ns = sorted(n for n in ns if n)
        lines.append(f"- {k}" + (f": {', '.join(map(str, ns))} элементов" if ns else ""))
    lines.append("- chart, table: строятся нативно в стиле шаблона (из данных)")
    titles = sorted(s.max_chars for p in profile.usable_patterns() for s in p.slots
                    if s.role.value == "title" and p.kind.value not in ("title", "section", "thanks") and s.max_chars)
    budget = titles[len(titles) // 2] if titles else 70
    return "\n".join(lines), max(40, min(budget, 90))


def _normalize_outline(o: Outline, brief: Brief) -> Outline:
    slides = [s for s in o.slides if s.title.strip()]
    for s in slides:
        s.intent = s.intent.strip().lower()
        if s.intent not in VALID:
            s.intent = "text"
    if not slides or slides[0].intent != "title":
        slides.insert(0, OutlineSlide(intent="title", title=o.title, message=o.subtitle))
    if slides[-1].intent not in ("thanks", "contacts"):
        slides.append(OutlineSlide(intent="thanks", title="Спасибо!" if brief.language == "ru" else "Thank you!"))
    lo, hi = max(brief.n_slides - 2, 3), brief.n_slides + 2
    while len(slides) > hi:  # drop from the middle, keep title/agenda/closing
        idx = next((i for i in range(len(slides) - 2, 1, -1) if slides[i].intent == "section"), len(slides) - 2)
        slides.pop(idx)
    o.slides = slides
    return o


def _to_spec(i: int, o: OutlineSlide, w: WrittenSlide | None) -> SlideSpec:
    intent = PatternKind(o.intent)
    spec = SlideSpec(id=f"s{i + 1}", intent=intent, title=(w.title if w and w.title else o.title), message=o.message)
    if o.message.strip().lower() == "summary":
        spec.message = ""
        spec.notes = "summary"
    if w:
        spec.subtitle = w.subtitle
        spec.bullets = [b for b in w.bullets if b.strip()][:6]
        spec.items = [it for it in w.items if (it.title or it.text or it.value)][:8]
        if w.chart and w.chart.series and w.chart.categories:
            n = len(w.chart.categories)
            w.chart.series = [ChartSeries(name=s.name, values=(s.values + [0] * n)[:n]) for s in w.chart.series[:5]]
            spec.chart = w.chart
        if w.table and w.table.columns and w.table.rows:
            spec.table = w.table
        spec.quote, spec.quote_author = w.quote, w.quote_author
        spec.image_prompt = w.image_prompt
        spec.notes = (w.notes or "") + ("\n[summary]" if spec.notes == "summary" else "")
    # consistency: intents that need data fall back gracefully
    if intent == PatternKind.chart and not spec.chart:
        spec.intent = PatternKind.table if spec.table else (PatternKind.stats if spec.items else PatternKind.text)
    if intent == PatternKind.table and not spec.table:
        spec.intent = PatternKind.chart if spec.chart else PatternKind.text
    if intent in (PatternKind.cards, PatternKind.steps, PatternKind.stats, PatternKind.agenda) and not spec.items:
        if spec.bullets:
            spec.items = [Item(title=b) for b in spec.bullets]
        else:
            spec.intent = PatternKind.text
    if intent == PatternKind.quote and not spec.quote:
        spec.intent = PatternKind.text
        if not spec.bullets:
            spec.bullets = [spec.message] if spec.message else []
    if spec.intent == PatternKind.text and not spec.bullets:
        spec.bullets = [f"{it.title}: {it.text}".strip(": ") for it in spec.items] or ([o.message] if o.message else [])
    return _keep_relevant(spec)


# Fields a slide of each intent may carry. Weaker models tend to fill every field of the
# schema; anything outside the intent would be silently dropped by the layout anyway.
_RELEVANT = {
    PatternKind.title: set(), PatternKind.section: set(), PatternKind.thanks: set(),
    PatternKind.agenda: {"items"}, PatternKind.cards: {"items"}, PatternKind.steps: {"items"}, PatternKind.stats: {"items"},
    PatternKind.team: {"items"}, PatternKind.chart: {"chart"}, PatternKind.table: {"table"},
    PatternKind.text: {"bullets"}, PatternKind.two_column: {"bullets"}, PatternKind.image_text: {"bullets"},
    PatternKind.contacts: {"bullets"}, PatternKind.quote: set(),
}
_ITEM_LIMITS = {PatternKind.stats: 4, PatternKind.agenda: 6, PatternKind.cards: 6, PatternKind.steps: 6, PatternKind.team: 8}


_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[•●▪\-–*])\s+")
_VALUE_OK = re.compile(r"^[<>~≈+\-−]?\s*[\d.,\s]+\s*(%|₽|\$|€|x|×|k|m|млн|млрд|тыс\.?|мин|сек|с|ч|дн\.?|B|GB|ТБ)?[\s–\-\d.,%]*$", re.I)


def _clean_lines(items: list[str]) -> list[str]:
    """One point per bullet: split embedded line breaks, drop manual numbering."""
    out = []
    for b in items:
        for line in re.split(r"\s*\n+\s*", b or ""):
            line = _NUMBERED.sub("", line).strip()
            if line:
                out.append(line)
    return out


def _keep_relevant(spec: SlideSpec) -> SlideSpec:
    spec.bullets = _clean_lines(spec.bullets)[:6]
    for it in spec.items:
        it.title, it.text = _NUMBERED.sub("", it.title).strip(), it.text.strip()
        if it.value and not _VALUE_OK.match(it.value.strip()) and len(it.value) > 8:
            # a phrase is not a KPI value: keep it as the item title instead
            it.title, it.value = (f"{it.value} {it.title}".strip() if it.title else it.value), ""
    keep = _RELEVANT.get(spec.intent, {"bullets", "items", "chart", "table"})
    if "items" not in keep:
        spec.items = []
    if "bullets" not in keep:
        spec.bullets = []
    if "chart" not in keep:
        spec.chart = None
    if "table" not in keep:
        spec.table = None
    if spec.intent in _ITEM_LIMITS:
        spec.items = spec.items[: _ITEM_LIMITS[spec.intent]]
    return spec


def _lang_ok(text: str, lang: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 4:
        return True
    cyr = sum(bool(re.match(r"[а-яё]", ch, re.I)) for ch in letters) / len(letters)
    return cyr >= 0.5 if lang == "ru" else cyr < 0.5


def sanitize_plan(plan: DeckPlan, outline: Outline | None = None) -> DeckPlan:
    """Deck-level guards against model slips: wrong-language titles, duplicate slides."""
    seen: set[str] = set()
    out = []
    for i, s in enumerate(plan.slides):
        if not _lang_ok(s.title, plan.language) and outline and i < len(outline.slides) and _lang_ok(outline.slides[i].title, plan.language):
            s.title = outline.slides[i].title
        key = re.sub(r"\W+", " ", s.title.lower()).strip()
        if key in seen and s.intent not in (PatternKind.title, PatternKind.thanks):
            continue  # same claim twice: keep the first slide only
        seen.add(key)
        out.append(s)
    for i, s in enumerate(out):
        s.id = f"s{i + 1}"
    plan.slides = out
    return plan


async def plan_with_llm(brief: Brief, corpus: ContentCorpus, profile: TemplateProfile, llm: LLMClient, agent: Agent) -> DeckPlan:
    caps, title_chars = template_capabilities(profile)
    context = select_context(corpus, brief.text, budget_chars=16000)
    outline_skill = skill_for_step(agent, "outline", "outline")
    hints = outline_skill.params.get("purpose_hints", {})  # lives in skills/outline/<ver>.yaml
    lo = max(brief.n_slides - 2, 3)

    brief_stems = {w[:6] for w in re.findall(r"[a-zа-яё]{5,}", brief.text.lower())}

    def check_outline(o: Outline) -> str | None:
        titles = [re.sub(r"\W+", " ", s.title.lower()).strip() for s in o.slides if s.title.strip()]
        distinct = len(set(titles))
        if distinct < lo:
            return (f"в структуре {distinct} разных слайдов, нужно {brief.n_slides}: у каждого слайда свой "
                    f"заголовок-вывод, без повторов")
        head = {w[:6] for w in re.findall(r"[a-zа-яё]{5,}", f"{o.title} {o.subtitle}".lower())}
        if brief_stems and not head & brief_stems:
            return f"название и подзаголовок презентации не отражают бриф; тема брифа: «{brief.text[:160]}»"
        return None

    o = await llm.run_skill(
        outline_skill, Outline, validate=check_outline, brief=brief.text, purpose=brief.purpose,
        purpose_hint=hints.get(brief.purpose, ""), audience=brief.audience, n_slides=brief.n_slides, capabilities=caps,
        context=context, language=brief.language, title_chars=title_chars,
    )
    o = _normalize_outline(o, brief)
    writer = skill_for_step(agent, "write_slides", "slide_writer")
    wctx = select_context(corpus, brief.text, budget_chars=9000)

    async def write(i: int, s: OutlineSlide) -> WrittenSlide | None:
        if s.intent in ("title", "section", "thanks"):
            return WrittenSlide(title=s.title, subtitle=o.subtitle if s.intent == "title" else "")
        try:
            return await llm.run_skill(
                writer, WrittenSlide, language=brief.language, max_words=15, max_bullets=5, deck_title=o.title,
                purpose=brief.purpose, outline=[x.model_dump() for x in o.slides], index=i + 1, slide=s.model_dump(),
                context=wctx, title_chars=title_chars,
            )
        except (LLMError, Exception) as e:  # one bad slide must not kill the deck
            log.warning("slide_writer failed for %d: %s", i + 1, e)
            return None

    written = await asyncio.gather(*[write(i, s) for i, s in enumerate(o.slides)])
    specs = [_to_spec(i, s, w) for i, (s, w) in enumerate(zip(o.slides, written))]
    plan = DeckPlan(title=o.title, subtitle=o.subtitle, purpose=brief.purpose, language=brief.language, slides=specs)
    return sanitize_plan(plan, o)


# ----------------------------------------------------------------------------
# Offline, deterministic planner
# ----------------------------------------------------------------------------
_HEAD_RE = re.compile(r"^\s*(\d+(\.\d+)*\.?\s+)?[A-ZА-ЯЁ][^.!?]{2,70}$")
_BULLET_RE = re.compile(r"^\s*([•●▪\-–*]|\d+[.)])\s+")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n", text)
    return [p.strip(" •●-–;") for p in parts if 25 <= len(p.strip()) <= 220]


def _short(s: str, words: int = 14) -> str:
    w = s.split()
    return s if len(w) <= words else " ".join(w[:words]).rstrip(",;:") + "…"


def plan_offline(brief: Brief, corpus: ContentCorpus) -> DeckPlan:
    lines = [ln.rstrip() for c in corpus.chunks for ln in c.text.split("\n")]
    sections: list[tuple[str, list[str]]] = []
    cur_title, cur = None, []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if _HEAD_RE.match(s) and not _BULLET_RE.match(s) and len(s.split()) <= 9 and not s.endswith(":"):
            if cur_title and cur:
                sections.append((cur_title, cur))
            cur_title, cur = re.sub(r"^\d+(\.\d+)*\.?\s+", "", s), []
        else:
            cur.append(s)
    if cur_title and cur:
        sections.append((cur_title, cur))
    if not sections:
        sents = _sentences(corpus.full_text())
        sections = [(f"Ключевой тезис {i + 1}", sents[i * 4:(i + 1) * 4]) for i in range(max(1, len(sents) // 4))]
    # rank sections by amount of content, keep document order
    body_n = max(3, brief.n_slides - 3)
    ranked = sorted(range(len(sections)), key=lambda i: -sum(len(x) for x in sections[i][1]))[:body_n]
    chosen = [sections[i] for i in sorted(ranked)]
    title = brief.text.split("\n")[0][:80] if brief.text else (chosen[0][0] if chosen else "Презентация")
    specs: list[SlideSpec] = [SlideSpec(id="s1", intent=PatternKind.title, title=title, subtitle=brief.purpose)]
    if brief.n_slides >= 10 and len(chosen) >= 3:
        specs.append(SlideSpec(id="s2", intent=PatternKind.agenda, title="Содержание",
                               items=[Item(title=_short(t, 4)) for t, _ in chosen[:6]]))
    for t, body in chosen:
        bullets = [_BULLET_RE.sub("", b) for b in body if _BULLET_RE.match(b)]
        sents = bullets or _sentences(" ".join(body))
        sents = [_short(s) for s in sents][:6]
        nums = [s for s in sents if re.search(r"\d", s)]
        sid = f"s{len(specs) + 1}"
        if 3 <= len(bullets) <= 6:
            items = [Item(title=_short(b, 4), text=_short(b, 12)) for b in bullets]
            specs.append(SlideSpec(id=sid, intent=PatternKind.cards, title=t, items=items))
        elif len(nums) >= 3:
            specs.append(SlideSpec(id=sid, intent=PatternKind.text, title=t, bullets=nums[:5]))
        else:
            specs.append(SlideSpec(id=sid, intent=PatternKind.text, title=t, bullets=sents[:5] or [t]))
    specs.append(SlideSpec(id=f"s{len(specs) + 1}", intent=PatternKind.thanks, title="Спасибо за внимание!"))
    return DeckPlan(title=title, subtitle=brief.purpose, purpose=brief.purpose, language=corpus.language, slides=specs)


async def make_plan(brief: Brief, corpus: ContentCorpus, profile: TemplateProfile, llm: LLMClient, agent: Agent) -> tuple[DeckPlan, str]:
    if llm.enabled:
        try:
            return await plan_with_llm(brief, corpus, profile, llm, agent), "llm"
        except Exception as e:
            log.exception("LLM planning failed, using offline planner: %s", e)
    return plan_offline(brief, corpus), "offline"


def dominant_language(plan: DeckPlan) -> str:
    text = " ".join([s.title for s in plan.slides] + [b for s in plan.slides for b in s.bullets])
    c = Counter("cyr" if re.match(r"[а-яё]", ch, re.I) else "lat" for ch in text if ch.isalpha())
    return "ru" if c["cyr"] >= c["lat"] else "en"
