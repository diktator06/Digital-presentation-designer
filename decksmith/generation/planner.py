"""Бриф + контент-пакет -> DeckPlan.

Два этапа LLM (быстро и надёжно вместо одного огромного JSON):
  1. outline      : список слайдов с интентами и заголовками-выводами (1 вызов)
  2. slide_writer : содержимое каждого слайда, все вызовы параллельно

Офлайн-режим (endpoint не задан) использует экстрактивный детерминированный планировщик,
поэтому весь пайплайн — и тесты — работают без модели.
"""
from __future__ import annotations

import asyncio
import logging
import re

from pydantic import BaseModel, Field

from decksmith.content.ingest import NUM_RE, normalize_number, select_context
from decksmith.core.config import settings
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
    """Короткое понятное описание того, что умеет вёрстка шаблона, и бюджет длины заголовка."""
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
    titles = []
    for p in profile.usable_patterns():
        if p.kind.value in ("title", "section", "thanks"):
            continue
        li = profile.layouts[p.layout_index] if p.layout_index is not None and 0 <= p.layout_index < len(profile.layouts) else None
        for s in p.slots:
            if s.role.value == "title" and s.max_chars:
                k = 1.0  # заголовки укорачиваются там, где в полосе заголовка стоит фоновая графика
                if li is not None and li.title_clear is not None and s.box.r > li.title_clear.r and s.box.w:
                    k = max(li.title_clear.r - s.box.x, 0) / s.box.w
                titles.append(int(s.max_chars * k))
    titles.sort()
    budget = titles[len(titles) // 2] if titles else 70
    return "\n".join(lines), max(40, min(budget, 90))


def section_cap(n_slides: int) -> int:
    """Разделители окупаются только в длинных колодах: до 8 слайдов — ни одного, всего не больше двух."""
    return min(2, max(0, (n_slides - 6) // 3))


def limit_sections(o: Outline, cap: int) -> int:
    """Детерминированный запасной ход после раунда исправления: разделители сверх предела и разделители, за
    которыми нет содержания, становятся содержательными слайдами (их заполняет автор текстов по
    материалам), так что колода сохраняет заданное число слайдов. Возвращает число преобразованных.
    """
    kept, converted = 0, 0
    for i, s in enumerate(o.slides):
        if s.intent != "section":
            continue
        nxt = o.slides[i + 1].intent if i + 1 < len(o.slides) else "end"
        if kept < cap and nxt not in ("section", "thanks", "contacts", "end"):
            kept += 1
            continue
        s.intent = "text"
        converted += 1
    return converted


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
    hi = max(brief.n_slides, 3)  # ТЗ: столько слайдов, сколько попросил пользователь
    while len(slides) > hi:  # удаляем из середины, сохраняя обложку, содержание и финал
        idx = next((i for i in range(len(slides) - 2, 1, -1) if slides[i].intent == "section"), len(slides) - 2)
        slides.pop(idx)
    # модель и после раунда исправления ответила короче: недостающие слайды — те, что колода такого
    # размера имела бы всё равно, — выводы перед финалом и содержание
    ru = brief.language == "ru"
    if len(slides) < hi and not any(s.message.strip().lower() == "summary" for s in slides):
        slides.insert(len(slides) - 1, OutlineSlide(intent="text", title="Главное" if ru else "Key takeaways", message="summary"))
    if len(slides) < hi and hi >= 6 and not any(s.intent == "agenda" for s in slides):
        slides.insert(1, OutlineSlide(intent="agenda", title="Содержание" if ru else "Agenda"))
    o.slides = slides
    return o


def complete_plan(plan: DeckPlan, n: int) -> None:
    """Детерминированная страховка после написания текстов: пустой итоговый слайд получает список выводов
    содержательных слайдов, а план, которому всё ещё не хватает слайдов до заданного числа (ТЗ: столько,
    сколько попросил пользователь), делит самые длинные списки на два слайда.
    """
    content = [s for s in plan.slides if s.intent.value in _CONTENT_INTENTS]
    for s in plan.slides:
        if "summary" in (s.notes or "") and not s.bullets and not s.items:
            s.intent = PatternKind.text
            s.bullets = [c.title for c in content if c is not s][:6]
    cont = " (продолжение)" if plan.language == "ru" else " (continued)"
    while len(plan.slides) < n:
        longest = max(plan.slides, key=lambda s: max(len(s.items), len(s.bullets)) if s.intent.value in _CONTENT_INTENTS else 0)
        field = "items" if len(longest.items) >= len(longest.bullets) else "bullets"
        rows = getattr(longest, field)
        if len(rows) < 4:
            break
        half = -(-len(rows) // 2)
        second = longest.model_copy(deep=True, update={"title": longest.title + cont, field: rows[half:]})
        setattr(longest, field, rows[:half])
        plan.slides.insert(plan.slides.index(longest) + 1, second)
    for i, s in enumerate(plan.slides):
        s.id = f"s{i + 1}"


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
    # согласованность: интенты, которым нужны данные, аккуратно откатываются к другим
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
        spec.bullets = [f"{' '.join(x for x in (it.value, it.title) if x)}: {it.text}".strip(": ") for it in spec.items] \
            or ([o.message] if o.message else [])
    return _keep_relevant(spec)


# Поля, которые может нести слайд каждого интента. Слабые модели склонны заполнять все поля
# схемы; всё вне интента вёрстка всё равно молча отбросила бы.
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
    """Один пункт — одна мысль: разбиваем встроенные переносы строк, убираем ручную нумерацию."""
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
            # фраза — не значение KPI: оставляем её заголовком элемента
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
    """Текст на языке колоды (по доле букв нужного алфавита)."""
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 4:
        return True
    cyr = sum(bool(re.match(r"[а-яё]", ch, re.I)) for ch in letters) / len(letters)
    return cyr >= 0.5 if lang == "ru" else cyr < 0.5


# служебные слова, которые модель иногда ставит вместо заголовка (ru, en)
_SERVICE_TITLES = {
    "summary": ("Главное", "Key takeaways"), "conclusion": ("Главное", "Key takeaways"),
    "conclusions": ("Главное", "Key takeaways"), "key takeaways": ("Главное", "Key takeaways"),
    "agenda": ("Содержание", "Agenda"), "outline": ("Содержание", "Agenda"), "contents": ("Содержание", "Agenda"),
    "introduction": ("Введение", "Introduction"), "intro": ("Введение", "Introduction"),
}
_CONTENT_INTENTS = {"text", "cards", "steps", "stats", "chart", "table", "image_text", "two_column"}


def _service_title(title: str) -> bool:
    """Заголовок — служебное слово вместо смысла («Слайд», «Заголовок»...)."""
    return re.sub(r"[^a-zа-яё ]+", "", title.lower()).strip() in _SERVICE_TITLES


def _digits(text: str) -> set[str]:
    """Нормализованные числа текста (та же нормализация, что в аудите чисел)."""
    return {re.sub(r"[^\d.]", "", normalize_number(m.group(0))).lstrip("0") for m in NUM_RE.finditer(text or "")}


def _unsourced(text: str, known: set[str]) -> list[str]:
    """Числа из `text` (от 2 цифр, не годы), которых нет среди `known`."""
    out = []
    for m in NUM_RE.finditer(text or ""):
        d = re.sub(r"[^\d.]", "", normalize_number(m.group(0).strip()))
        if len(d.replace(".", "")) < 2 or re.fullmatch(r"(19|20)\d\d", d):
            continue
        if d.lstrip("0") not in known:
            out.append(m.group(0).strip())
    return out


def drop_unsourced(plan: DeckPlan, corpus: ContentCorpus, brief_text: str) -> int:
    """Фильтр фактов до вёрстки: пункты и элементы с числами, которых нет в материалах (модели придумывают
    правдоподобные метрики), удаляются, если на слайде остаётся содержание. Возвращает число удалённых
    строк; аудит чисел всё равно сообщает об оставшемся.
    """
    known = {re.sub(r"[^\d.]", "", n).lstrip("0") for n in corpus.numbers} | _digits(brief_text)
    dropped = 0
    for s in plan.slides:
        bullets = [b for b in s.bullets if not _unsourced(b, known)]
        if bullets and len(bullets) < len(s.bullets):
            dropped += len(s.bullets) - len(bullets)
            s.bullets = bullets
        items = [it for it in s.items if not _unsourced(f"{it.value} {it.title} {it.text}", known)]
        if items and len(items) < len(s.items):
            dropped += len(s.items) - len(items)
            s.items = items
    return dropped


def sanitize_plan(plan: DeckPlan, outline: Outline | None = None) -> DeckPlan:
    """Проверки уровня колоды против промахов модели: служебные слова и заголовки на другом языке,
    дублирующиеся слайды.
    """
    seen: set[str] = set()
    out = []
    for i, s in enumerate(plan.slides):
        if _service_title(s.title):
            ru, en = _SERVICE_TITLES[re.sub(r"[^a-zа-яё ]+", "", s.title.lower()).strip()]
            s.title = ru if plan.language == "ru" else en
        if not _lang_ok(s.title, plan.language) and outline and i < len(outline.slides) and _lang_ok(outline.slides[i].title, plan.language):
            s.title = outline.slides[i].title
        key = re.sub(r"\W+", " ", s.title.lower()).strip()
        if key in seen and s.intent not in (PatternKind.title, PatternKind.thanks):
            continue  # одно утверждение дважды: оставляем только первый слайд
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
    hints = outline_skill.params.get("purpose_hints", {})  # хранится в skills/outline/<версия>.yaml
    lo = max(brief.n_slides, 3)  # слайдов меньше, чем просили -> один раунд исправления

    brief_stems = {w[:6] for w in re.findall(r"[a-zа-яё]{5,}", brief.text.lower())}

    cap = section_cap(brief.n_slides)

    def check_outline(o: Outline) -> str | None:
        """Все проблемы сразу (исправление одной не должно ломать другую) плюс требование к числу слайдов."""
        problems = []
        titles = [re.sub(r"\W+", " ", s.title.lower()).strip() for s in o.slides if s.title.strip()]
        distinct = len(set(titles))
        if distinct < lo:
            problems.append(f"в структуре {distinct} разных слайдов, нужно {brief.n_slides}: у каждого слайда свой "
                            f"заголовок-вывод, без повторов")
        head = {w[:6] for w in re.findall(r"[a-zа-яё]{5,}", f"{o.title} {o.subtitle}".lower())}
        if brief_stems and not head & brief_stems:
            problems.append(f"название и подзаголовок презентации не отражают бриф; тема брифа: «{brief.text[:160]}»")
        intents = [s.intent.strip().lower() for s in o.slides]
        if intents.count("section") > cap:
            problems.append(f"слишком много слайдов-разделов (section): {intents.count('section')}, допустимо не больше "
                            f"{cap}; замени лишние содержательными слайдами (cards, steps, stats, text) с фактами из материалов")
        if any(a == "section" and b in ("section", "thanks", "contacts", "end")
               for a, b in zip(intents, intents[1:] + ["end"])):
            problems.append("после слайда-раздела должен идти содержательный слайд: убери раздел перед финалом и разделы подряд")
        service = [s.title for s in o.slides if _service_title(s.title)]
        if service:
            problems.append(f"«{service[0]}» — служебное слово, а не заголовок: у каждого слайда заголовок-вывод "
                            f"на языке {brief.language}")
        if not problems:
            return None
        return "; ".join(problems) + f". В ответе должно быть ровно {brief.n_slides} слайдов."


    img = settings().image
    o = await llm.run_skill(
        outline_skill, Outline, validate=check_outline, brief=brief.text, purpose=brief.purpose,
        purpose_hint=hints.get(brief.purpose, ""), audience=brief.audience, n_slides=brief.n_slides, capabilities=caps,
        context=context, language=brief.language, title_chars=title_chars,
        # слайд-иллюстрацию просим, только если изображения действительно будут сгенерированы
        with_images=img.provider not in ("none", "") and bool(img.base_url),
    )
    o = _normalize_outline(o, brief)
    limit_sections(o, cap)
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
        except (LLMError, Exception) as e:  # один неудачный слайд не должен ронять всю колоду
            log.warning("slide_writer failed for %d: %s", i + 1, e)
            return None

    written = await asyncio.gather(*[write(i, s) for i, s in enumerate(o.slides)])
    specs = [_to_spec(i, s, w) for i, (s, w) in enumerate(zip(o.slides, written))]
    plan = DeckPlan(title=o.title, subtitle=o.subtitle, purpose=brief.purpose, language=brief.language, slides=specs)
    plan = sanitize_plan(plan, o)
    complete_plan(plan, brief.n_slides)
    dropped = drop_unsourced(plan, corpus, brief.text)
    if dropped:
        log.info("fact guard: %d lines with numbers absent from the materials dropped", dropped)
    if agent.enabled("headlines"):
        try:
            await _headlines(plan, llm, agent, title_chars, brief.language)
        except Exception as e:  # заголовки от автора текстов сохраняются
            log.warning("headlines step failed: %s", e)
    return plan


async def _headlines(plan: DeckPlan, llm: LLMClient, agent: Agent, title_chars: int, language: str) -> None:
    """Один вызов переписывает заголовки содержательных слайдов в выводы из их собственного содержимого (ТЗ,
    Приложение 1, вопрос 1). Новый заголовок принимается, только если он на том же языке, укладывается в
    бюджет и не приносит чисел, которых нет на слайде.
    """
    targets = [(i, s) for i, s in enumerate(plan.slides) if s.intent.value in _CONTENT_INTENTS]
    if not targets:
        return
    rows, source = [], {}
    for i, s in targets:
        body = s.bullets or [" ".join(x for x in (it.value, it.title, it.text) if x) for it in s.items]
        if not body and s.chart:
            body = [f"{s.chart.y_title or s.chart.unit}: " + ", ".join(f"{c} {v}" for c, v in
                                                                 zip(s.chart.categories, s.chart.series[0].values))]
        if not body and s.table:
            body = [", ".join(s.table.columns)] + [", ".join(str(c) for c in r) for r in s.table.rows[:4]]
        content = "; ".join(body)[:320] or s.message
        rows.append({"n": i + 1, "intent": s.intent.value, "title": s.title, "content": content})
        source[i] = f"{s.title} {content} {s.subtitle}"
    skill = skill_for_step(agent, "headlines", "headline")
    res = await llm.run_skill(skill, None, deck_title=plan.title, slides=rows, title_chars=title_chars, language=language)
    got = [t for t in (res.get("titles", []) if isinstance(res, dict) else []) if isinstance(t, dict)]
    ns = [i + 1 for i, _ in targets]
    keys = []
    for t in got:
        try:
            keys.append(int(t.get("n", 0)))
        except (TypeError, ValueError):
            keys.append(0)
    if keys and all(k in ns for k in keys):  # ответ пронумерован номерами слайдов
        by_n = {k: str(t.get("title", "")).strip().rstrip(".") for k, t in zip(keys, got)}
    elif len(got) == len(targets):  # пронумерован позицией в списке
        by_n = {n: str(t.get("title", "")).strip().rstrip(".") for n, t in zip(ns, got)}
    else:
        by_n = {}
    changed = 0
    for i, s in targets:
        new = by_n.get(i + 1, "")
        if not new or new == s.title or len(new) > title_chars + 20 or not _lang_ok(new, language) or _service_title(new):
            continue
        if _unsourced(new, _digits(source[i])):
            continue  # заголовок не должен приносить собственных чисел
        stems = {w[:5] for w in re.findall(r"[a-zа-яё]{5,}", source[i].lower())}
        if stems and not {w[:5] for w in re.findall(r"[a-zа-яё]{5,}", new.lower())} & stems:
            continue  # нет общих слов со своим слайдом: ответ предназначался другому слайду
        s.title = new
        changed += 1
    log.info("headlines: %d of %d titles rewritten as conclusions", changed, len(targets))


# ----------------------------------------------------------------------------
# Офлайн-планировщик (детерминированный)
# ----------------------------------------------------------------------------
_HEAD_RE = re.compile(r"^\s*(\d+(\.\d+)*\.?\s+)?[A-ZА-ЯЁ][^.!?]{2,70}$")
_BULLET_RE = re.compile(r"^\s*([•●▪\-–*]|\d+[.)])\s+")


def _sentences(text: str) -> list[str]:
    """Предложения текста подходящей длины (для экстрактивного плана)."""
    parts = re.split(r"(?<=[.!?;])\s+|\n", text)
    return [p.strip(" •●-–;") for p in parts if 25 <= len(p.strip()) <= 220]


def _short(s: str, words: int = 14) -> str:
    """Обрезает текст до `words` слов с многоточием."""
    w = s.split()
    return s if len(w) <= words else " ".join(w[:words]).rstrip(",;:") + "…"


_PURPOSE_RU = {"feature": "Презентация фичи", "product": "Презентация продукта", "project": "Презентация проекта",
               "initiative": "Презентация инициативы"}
_NUM_RE = re.compile(r"\d[\d.,]*(?:\s?[–-]\s?\d[\d.,]*)?(?:\s?(?:%|₽|(?:млн|млрд|тыс|руб|мин|сек|час|дн|раз)[а-яё]*))?")


def _cut(s: str, n: int) -> str:
    """Не больше n символов, обрезка по границе слова (никогда посреди слова)."""
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:—–- ") + "…"


def _brief_title(brief: Brief) -> tuple[str, str]:
    """Заголовок и подзаголовок обложки из первой фразы брифа («X — что это такое»)."""
    text = brief.text.strip()
    first = re.split(r"(?<=[.!?])\s+|\n", text)[0] if text else ""
    head, sep, rest = first.partition(" — ")
    if not sep:
        head, sep, rest = first.partition(": ")
    title = _cut((head if sep else first).rstrip("."), 70)
    sub = _cut(rest.rstrip("."), 140) if sep else ""
    return title, (sub[:1].upper() + sub[1:]) if sub else _PURPOSE_RU.get(brief.purpose, "")


def plan_offline(brief: Brief, corpus: ContentCorpus) -> DeckPlan:
    """Экстрактивный запасной вариант без модели: разделы документа становятся слайдами; колода получает
    заданное пользователем число слайдов (по умолчанию 10–15, ТЗ): остаются самые насыщенные разделы или
    длинные делятся, добавляются слайды ключевых чисел и итогов из того же материала. Ничего не
    выдумывается.
    """
    lines = [ln.rstrip() for c in corpus.chunks for ln in c.text.split("\n")]
    sections: list[tuple[str, list[str]]] = []
    cur_title, cur = None, []
    for ln in lines:
        s = ln.strip()
        if not s or re.fullmatch(r"[|:\-\s]+", s):  # пустая строка / разделитель таблицы markdown
            continue
        md_head = re.match(r"^#{1,6}\s+(.+)", s)
        if s.startswith("|"):  # строка таблицы markdown -> «ячейка — ячейка»
            s = " — ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        s = re.sub(r"[`*]{1,2}", "", md_head.group(1) if md_head else s)
        if md_head or (_HEAD_RE.match(s) and not _BULLET_RE.match(s) and len(s.split()) <= 9 and not s.endswith(":")):
            if cur_title and cur:
                sections.append((cur_title, cur))
            cur_title, cur = re.sub(r"^\d+(\.\d+)*\.?\s+", "", s), []
        else:
            cur.append(s)
    if cur_title and cur:
        sections.append((cur_title, cur))
    if not sections:
        sents = _sentences(corpus.full_text() or brief.text)
        sections = [(_cut(sents[i * 4], 60), sents[i * 4 + 1:(i + 1) * 4] or sents[i * 4:i * 4 + 1])
                    for i in range(max(1, len(sents) // 4))] if sents else []

    # единицы-слайды в порядке документа: (заголовок, строки, являются ли строки пунктами)
    units: list[list] = []
    for t, body in sections:
        bullets = [_BULLET_RE.sub("", b) for b in body if _BULLET_RE.match(b)]
        sents = bullets or _sentences(" ".join(body))
        if sents:
            units.append([t, [_short(x) for x in sents], bool(bullets)])
    # ТЗ: 10–15 слайдов «или заданный пользователем» -> побеждает число пользователя
    target = min(max(brief.n_slides, 4), 20)
    agenda = len(units) >= 3 and target >= 8  # короткая колода не тратит слайд на содержание
    need = target - 2 - (1 if agenda else 0)  # обложка, финал, содержание
    if len(units) > need:  # самые насыщенные разделы, порядок документа сохраняется
        keep = sorted(sorted(range(len(units)), key=lambda i: -len(units[i][1]))[:need])
        units = [units[i] for i in keep]
    all_lines = [x for u in units for x in u[1]]
    numbers = [x for x in all_lines if re.search(r"\d", x)]
    extra = (1 if len(numbers) >= 3 else 0) + (1 if len(units) >= 3 else 0)
    while len(units) + extra < need:  # самый длинный раздел делится на два
        i = max(range(len(units)), key=lambda k: len(units[k][1]), default=None)
        if i is None or len(units[i][1]) < 4:
            break  # материала не хватает: лучше меньше слайдов, чем выдуманные
        t, ls, b = units[i]
        half = (len(ls) + 1) // 2
        # список продолжается под своим заголовком; текст — под следующим предложением
        second = [f"{_cut(t, 50)} (продолжение)", ls[half:], b] if b else [_cut(ls[half].rstrip("…"), 60), ls[half + 1:] or ls[half:], b]
        units[i:i + 1] = [[t, ls[:half], b], second]

    title, subtitle = _brief_title(brief)
    title = title or (units[0][0] if units else "Презентация")
    specs: list[SlideSpec] = [SlideSpec(id="s1", intent=PatternKind.title, title=title, subtitle=subtitle)]
    if agenda:
        specs.append(SlideSpec(id="s2", intent=PatternKind.agenda, title="Содержание",
                               items=[Item(title=_short(t, 4)) for t, _, _ in units[:6]]))
    for t, ls, is_bullets in units:
        nums = [x for x in ls if re.search(r"\d", x)]
        sid = f"s{len(specs) + 1}"
        if is_bullets and 3 <= len(ls) <= 6:
            items = [Item(title=_short(b, 4), text=_short(b, 12)) for b in ls]
            specs.append(SlideSpec(id=sid, intent=PatternKind.cards, title=t, items=items))
        elif len(nums) >= 3:
            specs.append(SlideSpec(id=sid, intent=PatternKind.text, title=t, bullets=nums[:5]))
        else:
            specs.append(SlideSpec(id=sid, intent=PatternKind.text, title=t, bullets=ls[:5] or [t]))
    if len(numbers) >= 3 and len(specs) < target - 1:
        items = []
        for x in numbers[:4]:
            m = _NUM_RE.search(x)
            value = m.group(0).strip() if m else ""
            rest = re.sub(r"\s+([:,.;])", r"\1", re.sub(r"\s{2,}", " ", x.replace(value, "", 1))).strip(" :—–-,")
            items.append(Item(value=value, title=_short(rest, 6), text=""))
        specs.append(SlideSpec(id=f"s{len(specs) + 1}", intent=PatternKind.stats, title="Ключевые цифры", items=items))
    if len(units) >= 3 and len(specs) < target - 1:
        specs.append(SlideSpec(id=f"s{len(specs) + 1}", intent=PatternKind.text, title="Главное",
                               bullets=[u[1][0] for u in units[:5]]))
    specs.append(SlideSpec(id=f"s{len(specs) + 1}", intent=PatternKind.thanks, title="Спасибо за внимание!"))
    return DeckPlan(title=title, subtitle=subtitle, purpose=brief.purpose, language=corpus.language, slides=specs)


async def make_plan(brief: Brief, corpus: ContentCorpus, profile: TemplateProfile, llm: LLMClient, agent: Agent) -> tuple[DeckPlan, str]:
    """План колоды: через модель, при её отсутствии или сбое — офлайн-планировщик. Возвращает (план,
    источник).
    """
    if llm.enabled:
        try:
            return await plan_with_llm(brief, corpus, profile, llm, agent), "llm"
        except Exception as e:
            log.exception("LLM planning failed, using offline planner: %s", e)
    return plan_offline(brief, corpus), "offline"


