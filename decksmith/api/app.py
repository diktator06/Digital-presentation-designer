"""HTTP API + статический UI.

Шаблоны и контент-пакеты разбираются при загрузке (до того, как пользователь нажмёт
Enter); запуск стартует по POST /api/runs и передаёт прогресс через SSE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from decksmith.audit.context import AuditContext
from decksmith.audit.engine import catalogue, run_audit
from decksmith.audit.fixers import AUTO_SAFE, apply_fixes
from decksmith.content.ingest import ingest
from decksmith.core.config import ROOT, settings
from decksmith.core.logging import setup_logging
from decksmith.core.models import AuditReport, ContentCorpus, DeckPlan, TemplateProfile
from decksmith.generation.llm import LLMClient
from decksmith.generation.planner import Brief
from decksmith.generation.skills import list_versions
from decksmith.parsing.debug import overlay
from decksmith.parsing.template_parser import PARSER_VERSION, analyze_template, summarize
from decksmith.pipeline import Pipeline, apply_rewrites
from decksmith.render.soffice import pdf_to_pngs, pptx_to_pdf
from decksmith.export.exporters import to_html

setup_logging()
log = logging.getLogger("decksmith.api")
app = FastAPI(title="DeckSmith — цифровой дизайнер презентаций", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WS = settings().workspace
TEMPLATES: dict[str, TemplateProfile] = {}
CORPORA: dict[str, ContentCorpus] = {}
RUNS: dict[str, dict] = {}
QUEUES: dict[str, list[asyncio.Queue]] = {}
PENDING: dict[str, dict] = {}


# ----------------------------------------------------------------------------- запуск
def _load_existing() -> None:
    for p in (WS / "templates").glob("*/profile.json"):
        try:
            prof = TemplateProfile.model_validate_json(p.read_text(encoding="utf-8"))
            if prof.parser_version == PARSER_VERSION:  # устаревшие профили разбираются заново по запросу
                TEMPLATES[prof.id] = prof
        except Exception:
            continue
    for p in (WS / "content").glob("*/corpus.json"):
        try:
            c = ContentCorpus.model_validate_json(p.read_text(encoding="utf-8"))
            CORPORA[c.id] = c
        except Exception:
            continue
    for p in (WS / "runs").glob("*/state.json"):
        try:
            RUNS[p.parent.name] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue


@app.on_event("startup")
async def startup() -> None:
    _load_existing()

    async def warm():
        # шаблоны датасета разбираются в фоне при старте (работа до Enter)
        for t in sorted((ROOT / "data" / "templates").glob("*.pptx")):
            try:
                prof = await asyncio.to_thread(analyze_template, t, name=t.stem)
                TEMPLATES[prof.id] = prof
            except Exception as e:
                log.warning("warmup failed for %s: %s", t, e)
    asyncio.create_task(warm())


def _url(path: str | Path | None) -> str | None:
    """URL файла из рабочего каталога для фронтенда (None, если файла нет)."""
    if not path:
        return None
    rel = Path(path).resolve().relative_to(WS.resolve())
    return f"/files/{rel.as_posix()}"


# ----------------------------------------------------------------------------- шаблоны
def _template_card(p: TemplateProfile) -> dict:
    """Карточка шаблона для списка в UI: сводка профиля и миниатюра обложки."""
    s = summarize(p)
    thumb = next((x for x in p.patterns if x.kind.value == "title" and x.thumbnail), None) or (p.patterns[0] if p.patterns else None)
    return {**s, "thumbnail": _url(thumb.thumbnail) if thumb and thumb.thumbnail else None,
            "n_patterns": len(p.usable_patterns()), "n_layouts": len(p.layouts)}


@app.get("/api/templates")
def list_templates():
    """Список разобранных шаблонов, по имени."""
    return [_template_card(p) for p in sorted(TEMPLATES.values(), key=lambda x: x.name)]


@app.post("/api/templates")
async def upload_template(file: UploadFile = File(...)):
    """Загрузка шаблона: сохраняем файл и разбираем его сразу (до нажатия Enter)."""
    if Path(file.filename).suffix.lower() not in (".pptx", ".potx", ".pptm", ".potm", ".ppt", ".pot", ".odp", ".otp", ".key"):
        raise HTTPException(400, "Нужен файл презентации или шаблона: .pptx, .potx, .ppt, .odp, .otp")
    tmp = WS / "uploads" / f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    t0 = time.time()
    prof = await asyncio.to_thread(analyze_template, tmp, name=Path(file.filename).stem)
    TEMPLATES[prof.id] = prof
    return {**_template_card(prof), "analysis_s": round(time.time() - t0, 1)}


@app.get("/api/templates/{tid}")
def get_template(tid: str):
    """Полный профиль шаблона по id."""
    p = TEMPLATES.get(tid) or HTTPException(404)
    if isinstance(p, HTTPException):
        raise p
    data = json.loads(p.model_dump_json())
    for pat in data["patterns"]:
        pat["thumbnail"] = _url(pat["thumbnail"]) if pat.get("thumbnail") else None
        pat["overlay"] = f"/api/templates/{tid}/patterns/{pat['id']}/overlay.png"
    for lay in data["layouts"]:
        lay["thumbnail"] = _url(lay["thumbnail"]) if lay.get("thumbnail") else None
    data["summary"] = _template_card(p)
    return data


@app.get("/api/templates/{tid}/patterns/{pid}/overlay.png")
def pattern_overlay(tid: str, pid: str):
    """Картинка декомпозиции паттерна: рамки слотов поверх рендера слайда-примера."""
    p = TEMPLATES.get(tid)
    if not p:
        raise HTTPException(404)
    out = Path(p.workdir) / "overlays" / f"{pid}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        overlay(p, p.pattern(pid), 800).save(out)
    return FileResponse(out)


# ----------------------------------------------------------------------------- контент
@app.get("/api/content")
def list_content():
    """Список загруженных контент-пакетов."""
    return [{"id": c.id, "files": [Path(f).name for f in c.files], "chunks": len(c.chunks), "tables": len(c.tables),
             "numbers": len(c.numbers), "language": c.language} for c in CORPORA.values()]


@app.post("/api/content")
async def upload_content(files: list[UploadFile] = File(...)):
    """Загрузка контент-пакета: сохраняем файлы и извлекаем текст, таблицы и числа."""
    d = WS / "content" / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        p = d / Path(f.filename).name
        with open(p, "wb") as out:
            shutil.copyfileobj(f.file, out)
        paths.append(p)
    corpus = await asyncio.to_thread(ingest, paths)
    final = WS / "content" / corpus.id
    if final.exists():
        shutil.rmtree(d)
    else:
        d.rename(final)
        corpus.files = [str(final / Path(p).name) for p in corpus.files]
    (final / "corpus.json").write_text(corpus.model_dump_json(), encoding="utf-8")
    CORPORA[corpus.id] = corpus
    return {"id": corpus.id, "files": [Path(f).name for f in corpus.files], "chunks": len(corpus.chunks),
            "tables": len(corpus.tables), "numbers": len(corpus.numbers), "language": corpus.language}


@app.post("/api/content/sample")
async def sample_content():
    """Готовый контент-пакет: все файлы из data/content/ (текст задания в PDF, пример брифа...)."""
    files = sorted(p for p in (ROOT / "data" / "content").glob("*")
                   if p.suffix.lower() in (".pdf", ".md", ".docx", ".txt", ".pptx") and not p.stem.upper().startswith("README"))
    if not files:
        raise HTTPException(404, "data/content is empty")
    corpus = await asyncio.to_thread(ingest, files)
    d = WS / "content" / corpus.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "corpus.json").write_text(corpus.model_dump_json(), encoding="utf-8")
    CORPORA[corpus.id] = corpus
    return {"id": corpus.id, "files": [p.name for p in files], "chunks": len(corpus.chunks), "tables": len(corpus.tables),
            "numbers": len(corpus.numbers), "language": corpus.language}


# ----------------------------------------------------------------------------- запуски
class RunRequest(BaseModel):
    template_id: str
    content_id: str | None = None
    brief: str
    purpose: str = "product"
    n_slides: int = 12
    audience: str = ""
    variants: list[str] | None = None


def _save_state(run_id: str) -> None:
    """Сохраняет состояние запуска на диск (переживает перезапуск сервера)."""
    d = WS / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(RUNS[run_id], ensure_ascii=False), encoding="utf-8")


async def _publish(run_id: str, ev: dict) -> None:
    """Добавляет событие прогресса в историю запуска и рассылает его подписчикам SSE."""
    ev = {**ev, "ts": time.time()}
    RUNS[run_id].setdefault("events", []).append(ev)
    for q in QUEUES.get(run_id, []):
        await q.put(ev)


def _variant_payload(v) -> dict:
    """Данные варианта для UI: стратегия, балл аудита, замечания, ссылки на файлы и слайды."""
    from decksmith.layout.selector import load_variants

    return {
        "name": v.name, "title": v.title, "error": v.error,
        "description": load_variants()[v.name].description if v.name in load_variants() else "",
        "pptx": _url(v.pptx), "pdf": _url(v.pdf), "html": _url(v.html),
        "pngs": [_url(p) for p in v.pngs],
        "audit": json.loads(v.audit.model_dump_json()) if v.audit else None,
        "audit_before_fix": v.audit_before_fix,
        "slides": v.slides, "timings": v.timings, "version": 1,
        "pptx_path": v.pptx,
        "plan": json.loads(v.plan.model_dump_json()) if v.plan else None,
    }


async def _run_job(run_id: str, req: RunRequest) -> None:
    """Фоновая задача запуска: план, три варианта, аудит и экспорт с событиями прогресса."""
    prof = TEMPLATES[req.template_id]
    corpus = CORPORA.get(req.content_id) if req.content_id else ingest([], req.brief)
    brief = Brief(text=req.brief, purpose=req.purpose, n_slides=req.n_slides, audience=req.audience,
                  language=corpus.language if corpus else "ru")
    pipe = Pipeline()
    t0 = time.time()
    try:
        res = await pipe.run(prof, corpus, brief, req.variants, emit=lambda ev: _publish(run_id, ev), run_id=run_id)
        RUNS[run_id].update({
            "status": "done", "elapsed_s": round(time.time() - t0, 1), "plan": json.loads(res.plan.model_dump_json()),
            "plan_mode": res.plan_mode, "timings": res.timings, "manifest": _url(res.manifest),
            "variants": [_variant_payload(v) for v in res.variants],
        })
    except Exception as e:
        log.exception("run failed")
        RUNS[run_id].update({"status": "error", "error": str(e)})
    _save_state(run_id)
    await _publish(run_id, {"stage": "finished", "status": RUNS[run_id]["status"]})


@app.post("/api/runs")
async def start_run(req: RunRequest, bg: BackgroundTasks):
    """Старт запуска по брифу (нажатие Enter): возвращает id, прогресс идёт через SSE."""
    if req.template_id not in TEMPLATES:
        raise HTTPException(404, "template not found")
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    RUNS[run_id] = {"id": run_id, "status": "running", "started": time.time(), "request": req.model_dump(),
                    "template": TEMPLATES[req.template_id].name, "events": []}
    asyncio.create_task(_run_job(run_id, req))
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs():
    """Список запусков, новые сверху."""
    return sorted(({k: v for k, v in r.items() if k in ("id", "status", "template", "elapsed_s", "started", "request")}
                   for r in RUNS.values()), key=lambda r: -r.get("started", 0))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    """Состояние запуска по id."""
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    """Поток событий запуска (SSE): сначала история, затем новые события."""
    if run_id not in RUNS:
        raise HTTPException(404)
    q: asyncio.Queue = asyncio.Queue()
    QUEUES.setdefault(run_id, []).append(q)

    async def stream():
        """Генератор SSE: отдаёт накопленные события, затем ждёт новые до завершения."""
        try:
            for ev in list(RUNS[run_id].get("events", [])):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if RUNS[run_id]["status"] != "running":
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("stage") == "finished":
                    return
        finally:
            QUEUES[run_id].remove(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class FixRequest(BaseModel):
    issue_ids: list[str]


@app.post("/api/runs/{run_id}/variants/{name}/fix")
async def fix_variant(run_id: str, name: str, req: FixRequest):
    """Применяет выбранные пользователем исправления к варианту и заново проводит аудит."""
    r = RUNS.get(run_id)
    if not r or r.get("status") != "done":
        raise HTTPException(404)
    v = next((x for x in r["variants"] if x["name"] == name), None)
    if not v or not v.get("audit"):
        raise HTTPException(404)
    prof = TEMPLATES[r["request"]["template_id"]]
    audit = AuditReport.model_validate(v["audit"])
    chosen = [i for i in audit.issues if i.id in set(req.issue_ids)]
    vdir = Path(v["pptx_path"]).parent
    ver = v.get("version", 1) + 1
    out = vdir / f"{name}_fix{ver}.pptx"
    t0 = time.time()
    async with LLMClient() as llm:
        res = await apply_fixes(v["pptx_path"], chosen, prof, out, llm, (r.get("plan") or {}).get("language", "ru"))
        # аудит сверяет слайды с планом этого варианта, в который внесены правки модели
        plan = DeckPlan.model_validate(v.get("plan") or r["plan"]) if (v.get("plan") or r.get("plan")) else None
        if plan is not None:
            plan = apply_rewrites(plan, [(list(a), list(b)) for a, b in res.get("rewrites", [])])
        corpus = CORPORA.get(r["request"].get("content_id") or "")

        async def check(path: Path, tag: str):
            """Рендер версии и детерминированный аудит по плану варианта."""
            rdir = vdir / f"render_fix{ver}{tag}"
            pdf_ = await asyncio.to_thread(pptx_to_pdf, path, rdir)
            pngs_ = await asyncio.to_thread(pdf_to_pngs, pdf_, rdir / "png", settings().render.dpi)
            ctx = AuditContext(pptx=path, profile=prof, plan=plan, corpus=corpus, pngs=pngs_, brief_text=r["request"]["brief"],
                               slide_kinds=[s["kind"] for s in v["slides"]], slide_sources=[s["source"] for s in v["slides"]])
            return pdf_, pngs_, await run_audit(ctx, None, None, visual=False)

        pdf, pngs, new_audit = await check(out, "")
        # новый текст модели мог не влезть в рамку: тот же безопасный раунд автоисправлений, что в пайплайне
        safe = [i for i in new_audit.issues if i.deterministic and i.fix in AUTO_SAFE and i.severity.value != "info"]
        if safe:
            fixed = vdir / f"{name}_fix{ver}_auto.pptx"
            await apply_fixes(out, safe, prof, fixed)
            out = fixed
            pdf, pngs, new_audit = await check(out, "_auto")
        remaining_ctx = [i for i in audit.issues if not i.deterministic and i.id not in set(res["applied"])]
        new_audit.issues += remaining_ctx
    html = await asyncio.to_thread(to_html, pdf, out, vdir / f"{name}_fix{ver}.html", r.get("plan", {}).get("title", name),
                                   prof.tokens.accent_hex)
    v.update({"pptx": _url(out), "pptx_path": str(out), "pdf": _url(pdf), "html": _url(html),
              "pngs": [_url(p) for p in pngs], "audit": json.loads(new_audit.model_dump_json()), "version": ver,
              "plan": json.loads(plan.model_dump_json()) if plan else v.get("plan"),
              "last_fix": {**{k: x for k, x in res.items() if k != "rewrites"}, "seconds": round(time.time() - t0, 1)}})
    _save_state(run_id)
    return v


# ----------------------------------------------------------------------------- служебное
@app.get("/api/skills")
def skills():
    """Версии скиллов/агентов и каталог проверок аудита."""
    return {"versions": list_versions(), "audit_checks": catalogue()}


@app.get("/api/health")
def health():
    """Проверка работоспособности: подключённые модели и число шаблонов."""
    cfg = settings()
    return {"ok": True, "llm": cfg.llm.model if cfg.llm.enabled else "offline", "vlm": cfg.llm.vlm_model or None,
            "t2i": cfg.image.model if cfg.image.provider != "none" else None, "templates": len(TEMPLATES)}


app.mount("/files", StaticFiles(directory=str(WS)), name="files")
_ui = ROOT / "frontend" / "dist"
if _ui.exists():
    app.mount("/", StaticFiles(directory=str(_ui), html=True), name="ui")
else:
    @app.get("/")
    def root():
        """Заглушка, если фронтенд не собран."""
        return JSONResponse({"ui": "frontend not built: cd frontend && npm i && npm run build"})
