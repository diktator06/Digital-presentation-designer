"""Blind-template stress test.

For every template: analyse (as an unknown template) -> 3 variants of the same
plan -> build -> render -> deterministic audit -> robustness metrics, plus a
contact sheet per template for visual review.

  python scripts/stress.py workspace/blind/lo/*.pptx workspace/blind/lct2026.pptx --synthetic --out workspace/stress
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_one(template: str, out_dir: str, plan_path: str) -> dict:
    import os

    os.environ.setdefault("DECKSMITH_LLM_PROVIDER", "offline")
    # own cache: stress templates must not appear in the service's template list
    os.environ.setdefault("DECKSMITH_WORKSPACE", str(Path(out_dir) / "_workspace"))
    from PIL import Image, ImageDraw

    from decksmith.audit.context import AuditContext
    from decksmith.audit.engine import run_audit
    from decksmith.core.models import DeckPlan
    from decksmith.layout.builder import DeckBuilder
    from decksmith.layout.selector import load_variants, select_layouts
    from decksmith.parsing.template_parser import analyze_template
    from decksmith.pipeline import icons_for_plan, plan_variants
    from decksmith.render.soffice import pdf_to_pngs, pptx_to_pdf

    t = Path(template)
    name = t.stem
    res: dict = {"template": name, "file": str(t)}
    od = Path(out_dir) / name
    od.mkdir(parents=True, exist_ok=True)
    try:
        t0 = time.time()
        prof = analyze_template(t, name=name, force=True)
        res["analyze_s"] = round(time.time() - t0, 1)
        res["size_in"] = [round(prof.tokens.slide_w / 914400, 2), round(prof.tokens.slide_h / 914400, 2)]
        kinds = Counter(p.kind.value for p in prof.usable_patterns())
        res["patterns"] = dict(kinds)
        res["n_usable"] = len(prof.usable_patterns())
        res["canvas"] = {k: prof.layouts[v].name for k, v in prof.canvas_layouts.items()}
        res["warnings"] = prof.warnings
        res["tokens"] = {"accent": prof.tokens.accent_hex, "bg": prof.tokens.background_hex, "dark": prof.tokens.dark_background,
                         "heading": prof.tokens.heading_font, "body": prof.tokens.body_font,
                         "title_pt": prof.tokens.type_scale.title, "body_pt": prof.tokens.type_scale.body}
        plan = DeckPlan.model_validate_json(Path(plan_path).read_text(encoding="utf-8"))
        icons = icons_for_plan(plan, prof.tokens.accent_hex)
        res["variants"] = {}
        choices = {}
        sheets = []
        variants = load_variants()
        chosen = plan_variants(plan, prof, list(variants))
        for vname, v in variants.items():
            vr: dict = {}
            ts = time.time()
            vplan, dec = chosen[vname]
            b = DeckBuilder(prof, v)
            rep = b.build(vplan, dec, {}, icons)
            pptx = b.save(od / f"{vname}.pptx")
            vr["build_s"] = round(time.time() - ts, 2)
            choices[vname] = [(s["mode"], s["pattern"]) for s in rep.slides]
            vr["clone_share"] = round(sum(s["mode"] == "clone" for s in rep.slides) / len(rep.slides), 2)
            vr["fallbacks"] = sum("fallback after error" in s["rationale"] for s in rep.slides)
            ts = time.time()
            pdf = pptx_to_pdf(pptx, od / f"{vname}_r")
            pngs = pdf_to_pngs(pdf, od / f"{vname}_r" / "png", dpi=40)
            vr["render_s"] = round(time.time() - ts, 2)
            ctx = AuditContext(pptx=pptx, profile=prof, plan=vplan, pngs=pngs,
                               slide_kinds=[s["kind"] for s in rep.slides], slide_sources=[s["source"] for s in rep.slides])
            audit = asyncio.run(run_audit(ctx))
            sev = Counter(i.severity.value for i in audit.issues)
            vr["score"] = audit.score
            vr["errors"] = sev.get("error", 0)
            vr["warnings"] = sev.get("warning", 0)
            vr["by_check"] = dict(Counter(i.check for i in audit.issues if i.severity.value != "info"))
            vr["slides"] = len(pngs)
            vr["expected_slides"] = len(vplan.slides)
            res["variants"][vname] = vr
            sheets.append((vname, pngs))
        names = list(choices)
        diffs = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b2 = choices[names[i]], choices[names[j]]
                n = min(len(a), len(b2))
                diffs.append(sum(a[k] != b2[k] for k in range(n)) / max(n, 1))
        res["variant_distinctness"] = round(min(diffs), 2) if diffs else 0
        # contact sheet: one row per variant
        cols = max(len(p) for _, p in sheets)
        tw = 240
        th = int(tw * prof.tokens.slide_h / prof.tokens.slide_w)
        sheet = Image.new("RGB", (cols * (tw + 4) + 90, len(sheets) * (th + 4)), "white")
        d = ImageDraw.Draw(sheet)
        for r, (vname, pngs) in enumerate(sheets):
            d.text((4, r * (th + 4) + th // 2), vname, fill="black")
            for c, p in enumerate(pngs):
                sheet.paste(Image.open(p).convert("RGB").resize((tw, th)), (90 + c * (tw + 4), r * (th + 4)))
        sheet.save(od / "sheet.png")
        res["sheet"] = str(od / "sheet.png")
    except Exception as e:
        res["crash"] = f"{type(e).__name__}: {e}"
        res["trace"] = traceback.format_exc()[-2000:]
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("templates", nargs="*")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "workspace" / "stress"))
    ap.add_argument("--plan", default=str(ROOT / "tests" / "fixtures" / "plan_sample.json"))
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    templates = list(a.templates)
    if a.synthetic:
        from decksmith.testing.synthetic import generate_all

        templates += [str(p) for p in generate_all(Path(a.out) / "_synthetic").values()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(run_one, t, str(out), a.plan): t for t in templates}
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            if "crash" in r:
                print(f"CRASH {r['template']}: {r['crash']}")
            else:
                v = r["variants"]
                print(f"{r['template']:<28} analyze={r['analyze_s']:>5}s usable={r['n_usable']:>3} "
                      + " ".join(f"{k[:3]}={x['score']:>5}/{x['errors']}e" for k, x in v.items())
                      + f" distinct={r['variant_distinctness']}")
    results.sort(key=lambda r: r["template"])
    (out / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = [r for r in results if "crash" not in r]
    allv = [v for r in ok for v in r["variants"].values()]
    errs = Counter()
    for v in allv:
        for k, n in v["by_check"].items():
            errs[k] += n
    summary = {
        "templates": len(results), "crashed": len(results) - len(ok), "decks": len(allv),
        "mean_score": round(sum(v["score"] for v in allv) / max(len(allv), 1), 1),
        "min_score": min((v["score"] for v in allv), default=None),
        "decks_with_errors": sum(v["errors"] > 0 for v in allv),
        "slide_count_mismatch": sum(v["slides"] != v["expected_slides"] for v in allv),
        "mean_distinctness": round(sum(r["variant_distinctness"] for r in ok) / max(len(ok), 1), 2),
        "top_findings": errs.most_common(12),
        "wall_s": round(time.time() - t0, 1),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
