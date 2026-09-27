"""Командная строка: decksmith analyze | run | audit | serve | skills."""
from __future__ import annotations

import argparse
import os
import asyncio
import json
import sys
import time
from pathlib import Path

import yaml

from decksmith.core.config import ROOT, override_settings, settings
from decksmith.core.logging import setup_logging


def _p(path: str) -> Path:
    """Путь из аргумента: относительный путь, которого нет в текущем каталоге, ищется от корня проекта."""
    p = Path(path)
    return p if p.is_absolute() else (ROOT / p if not p.exists() else p)


def cmd_analyze(a) -> None:
    """decksmith analyze: разбор шаблона, сводка профиля и картинка декомпозиции."""
    from decksmith.parsing.debug import contact_sheet
    from decksmith.parsing.template_parser import analyze_template, summarize

    prof = analyze_template(a.template, force=a.force)
    print(json.dumps(summarize(prof), ensure_ascii=False, indent=1))
    if a.sheet:
        out = contact_sheet(prof, Path(prof.workdir) / "decomposition.png")
        print("decomposition:", out)


def cmd_run(a) -> None:
    from decksmith.core.models import DeckPlan
    from decksmith.generation.planner import Brief
    from decksmith.pipeline import Pipeline

    cfg = yaml.safe_load(_p(a.config).read_text(encoding="utf-8"))
    # никаких вызовов модели, что бы ни было в .env (офлайн-планировщик, аудит только по правилам)
    if cfg.get("offline"):
        os.environ["DECKSMITH_LLM_PROVIDER"] = "offline"
        settings.cache_clear()
    if cfg.get("settings"):
        override_settings(_p(cfg["settings"]))
    missing = [f for f in cfg.get("templates", []) + cfg.get("content", []) if not _p(f).exists()]
    if missing:
        sys.exit("Нет файлов: " + ", ".join(missing) + "\nШаблоны и ТЗ из датасета кладутся в data/templates/ и "
                 "data/content/ под именами из data/templates/README.md и data/content/README.md.")
    pipe = Pipeline()
    brief = Brief(**cfg["brief"])
    plan = DeckPlan.model_validate_json(_p(cfg["plan"]).read_text(encoding="utf-8")) if cfg.get("plan") else None
    t0 = time.time()
    corpus = pipe.prepare_content([_p(f) for f in cfg.get("content", [])], cfg.get("content_text", ""))
    profiles = [pipe.prepare_template(_p(t)) for t in cfg["templates"]]
    print(f"[pre-Enter] templates={len(profiles)} content chunks={len(corpus.chunks)} in {time.time() - t0:.1f}s")

    async def go():
        """Прогон всех шаблонов конфига по очереди со сводкой результатов."""
        summary = []
        for prof in profiles:
            def emit(ev, _n=prof.name):
                """Печатает ключевые этапы прогресса варианта."""
                if ev.get("status") == "done" and ev.get("stage") in ("plan", "audit", "export", "done"):
                    print(f"  [{_n}] {ev.get('stage')} {ev.get('variant', '')} t={ev.get('t')}s "
                          + (f"score={ev['score']}" if "score" in ev else ""))
            r = await pipe.run(prof, corpus, brief, cfg.get("variants"), emit=emit, plan=plan)
            for v in r.variants:
                summary.append({"template": prof.name, "variant": v.name, "score": v.audit.score if v.audit else None,
                                "pptx": v.pptx, "pdf": v.pdf, "html": v.html, "error": v.error})
            print(f"== {prof.name}: {r.timings['total']}s (plan: {r.plan_mode}) manifest={r.manifest}")
        return summary

    summary = asyncio.run(go())
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def cmd_audit(a) -> None:
    """decksmith audit: аудит готовой колоды по шаблону."""
    from decksmith.audit.context import AuditContext
    from decksmith.audit.engine import run_audit
    from decksmith.parsing.template_parser import analyze_template
    from decksmith.render.soffice import render_pptx

    prof = analyze_template(a.template)
    pdf, pngs = render_pptx(a.deck, Path(a.deck).with_suffix("").as_posix() + "_audit")
    rep = asyncio.run(run_audit(AuditContext(pptx=Path(a.deck), profile=prof, pngs=pngs)))
    print(rep.model_dump_json(indent=1))


def cmd_serve(a) -> None:
    """decksmith serve: UI + API."""
    import uvicorn

    uvicorn.run("decksmith.api.app:app", host=a.host, port=a.port, reload=False)


def cmd_skills(a) -> None:
    """decksmith skills: версии скиллов/агентов и каталог проверок."""
    from decksmith.audit.engine import catalogue
    from decksmith.generation.skills import list_versions

    print(json.dumps({"versions": list_versions(), "audit_checks": catalogue()}, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> None:
    """Точка входа командной строки."""
    setup_logging()
    ap = argparse.ArgumentParser("decksmith")
    ap.add_argument("--config", dest="settings", help="settings YAML (default config/default.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("analyze", help="parse a template into a profile (design tokens + patterns)")
    s.add_argument("template")
    s.add_argument("--force", action="store_true")
    s.add_argument("--sheet", action="store_true", help="write a decomposition contact sheet")
    s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("run", help="generate decks from a run config (templates x variants)")
    s.add_argument("config")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("audit", help="audit an existing deck against a template")
    s.add_argument("deck")
    s.add_argument("--template", required=True)
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("serve", help="start the web service")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("skills", help="list skill/agent versions and audit checks")
    s.set_defaults(fn=cmd_skills)
    a = ap.parse_args(argv)
    if a.settings:
        override_settings(a.settings)
    a.fn(a)


if __name__ == "__main__":
    main(sys.argv[1:])
