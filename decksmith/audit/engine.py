"""Audit engine = deterministic rules + contextual (VLM) questions -> AuditReport.

Contextual checks answer the yes/no questions of Annex 1 from the slide image;
they may differ between runs and are therefore reported separately
(`deterministic=False`) and never auto-applied without the user's choice.
"""
from __future__ import annotations

import asyncio
import logging
import time

from decksmith.audit.context import AuditContext
from decksmith.audit.rules import CHECKS, run_rules
from decksmith.core.models import AuditIssue, AuditReport, Severity
from decksmith.generation.llm import LLMClient
from decksmith.generation.skills import Agent, skill_for_step

log = logging.getLogger(__name__)

VLM_QUESTIONS = {
    "q1": ("content.title_is_conclusion", "Заголовок не содержит вывода", "fix_llm"),
    "q2": ("content.matches_title", "Содержимое не соответствует заголовку", "fix_llm"),
    "q3": ("content.one_sentence", "Слайд не пересказывается одним предложением", None),
    "q4": ("content.facts_sourced", "Есть цифры/факты не из материалов", "fix_llm"),
    "q5": ("content.has_content", "На слайде только заголовок", None),
    "q6": ("content.images_relevant", "Картинки/иконки не по теме", None),
    "q7": ("content.no_junk", "Служебный мусор (реплики, куски промпта)", "fix_llm"),
    "q8": ("content.typos", "Опечатки в тексте", "fix_llm"),
    "q10": ("content.table_legend_relevant", "Строки таблицы/легенда не работают на мысль", None),
    "q11": ("content.adjacent_logic", "Нет логической связи с соседними слайдами", None),
    "v1": ("layout.visual_overlap", "Визуально: наложение элементов", "shrink_text"),
    "v2": ("layout.visual_clipping", "Визуально: текст обрезан/вылезает", "shrink_text"),
}
WEIGHTS = {Severity.error: 4.0, Severity.warning: 1.5, Severity.info: 0.4}


def _facts_for(ctx: AuditContext, i: int) -> str:
    nums = ", ".join(ctx.corpus.numbers[:60]) if ctx.corpus else ""
    spec = ctx.plan.slides[i] if ctx.plan and i < len(ctx.plan.slides) else None
    notes = spec.message if spec else ""
    return f"числа в материалах: {nums or '—'}; ключевая мысль по плану: {notes or '—'}"


async def visual_audit(ctx: AuditContext, llm: LLMClient, agent: Agent) -> list[AuditIssue]:
    # contextual checks need a multimodal model: never send slide images to a text-only one
    if not llm.enabled or not ctx.pngs or not llm.cfg.vlm_model:
        return []
    skill = skill_for_step(agent, "audit_visual", "visual_audit")
    n = len(ctx.slides)
    titles = [ctx.plan.slides[i].title if ctx.plan and i < len(ctx.plan.slides) else "" for i in range(n)]

    async def one(i: int) -> list[AuditIssue]:
        kind = ctx.slides[i].kind
        try:
            res = await llm.run_skill(skill, None, images=[ctx.pngs[i]], index=i + 1, total=n, title=titles[i],
                                      prev_title=titles[i - 1] if i else "", next_title=titles[i + 1] if i + 1 < n else "",
                                      facts=_facts_for(ctx, i))
        except Exception as e:
            log.warning("visual audit failed on slide %d: %s", i + 1, e)
            return []
        answers = (res or {}).get("answers", {}) if isinstance(res, dict) else {}
        comments = (res or {}).get("comments", {}) if isinstance(res, dict) else {}
        out = []
        for q, val in answers.items():
            if q not in VLM_QUESTIONS or val is not False:
                continue
            if kind in ("title", "section", "thanks", "quote", "contacts") and q in ("q1", "q3", "q5", "q10"):
                continue  # covers/dividers do not carry an argument
            cid, title, fixer = VLM_QUESTIONS[q]
            out.append(AuditIssue(
                id=f"{cid}#{i}", check=cid, category="content" if q.startswith("q") else "layout", deterministic=False,
                severity=Severity.warning, slide=i, message=f"{title}: {comments.get(q, '')}".strip(": "),
                fixable=fixer is not None, fix=fixer, data={"question": q, "summary": (res or {}).get("summary", "")},
            ))
        return out

    results = await asyncio.gather(*[one(i) for i in range(n)])
    return [x for r in results for x in r]


def score(issues: list[AuditIssue], n_slides: int) -> float:
    penalty = sum(WEIGHTS[i.severity] for i in issues)
    return round(max(0.0, 100.0 - penalty * 10 / max(n_slides, 1)), 1)


async def run_audit(ctx: AuditContext, llm: LLMClient | None = None, agent: Agent | None = None, visual: bool = True) -> AuditReport:
    t0 = time.time()
    issues, ran = run_rules(ctx)
    if visual and llm is not None and agent is not None:
        issues += await visual_audit(ctx, llm, agent)
        if llm.enabled and ctx.pngs and llm.cfg.vlm_model:
            ran += [v[0] for v in VLM_QUESTIONS.values()]
    issues.sort(key=lambda i: (i.slide, list(WEIGHTS).index(i.severity), i.check))
    return AuditReport(deck=str(ctx.pptx), slide_count=len(ctx.slides), issues=issues, checks_run=ran,
                       score=score(issues, len(ctx.slides)), duration_s=round(time.time() - t0, 2))


def catalogue() -> list[dict]:
    """All checks with their nature — shown in UI and AUDIT.md."""
    rows = [{"id": m.id, "category": m.category, "title": m.title, "deterministic": True, "fixer": m.fixer} for m in CHECKS.values()]
    rows += [{"id": cid, "category": "content" if q.startswith("q") else "layout", "title": title, "deterministic": False,
              "fixer": fixer, "question": q} for q, (cid, title, fixer) in VLM_QUESTIONS.items()]
    return rows
