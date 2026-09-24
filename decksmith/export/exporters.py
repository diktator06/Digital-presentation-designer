"""Export layer: .pptx (native objects, produced by the builder) -> .pdf and .html.

HTML is a single self-contained file: every slide is a vector SVG converted
from the PDF (identical rendering), plus a hidden text layer for search and
accessibility, keyboard navigation and an overview grid.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pymupdf
from pptx import Presentation

HTML_TEMPLATE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#1b1d22;--fg:#f2f3f5;--accent:#{accent}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;height:100vh;overflow:hidden}}
header{{display:flex;gap:12px;align-items:center;padding:8px 16px;background:#111318}}
header b{{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
button{{background:#2a2d35;color:var(--fg);border:0;border-radius:6px;padding:6px 12px;cursor:pointer}}
button:hover{{background:var(--accent)}}
#stage{{height:calc(100vh - 44px);display:flex;align-items:center;justify-content:center;padding:16px}}
.slide{{display:none;width:100%;height:100%;align-items:center;justify-content:center}}
.slide.active{{display:flex}} .slide svg{{max-width:100%;max-height:100%;height:auto;box-shadow:0 8px 40px #0008;background:#fff}}
.sr{{position:absolute;left:-9999px}}
#grid{{display:none;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;padding:16px;overflow:auto;height:calc(100vh - 44px)}}
#grid.on{{display:grid}} #grid div{{cursor:pointer;background:#fff;border-radius:4px;overflow:hidden}} #grid svg{{width:100%;height:auto;display:block}}
</style></head><body>
<header><b>{title}</b><span id="n"></span><button onclick="go(-1)">←</button><button onclick="go(1)">→</button><button onclick="toggleGrid()">Все слайды</button></header>
<div id="stage">{slides}</div><div id="grid"></div>
<script>
const S=[...document.querySelectorAll('.slide')];let i=0;
function show(k){{i=Math.max(0,Math.min(S.length-1,k));S.forEach((s,j)=>s.classList.toggle('active',j===i));document.getElementById('n').textContent=(i+1)+' / '+S.length;location.hash=i+1}}
function go(d){{show(i+d)}}
function toggleGrid(){{const g=document.getElementById('grid');if(!g.children.length){{S.forEach((s,j)=>{{const d=document.createElement('div');d.innerHTML=s.querySelector('svg').outerHTML;d.onclick=()=>{{g.classList.remove('on');show(j)}};g.appendChild(d)}})}}g.classList.toggle('on')}}
document.addEventListener('keydown',e=>{{if(['ArrowRight','PageDown',' '].includes(e.key))go(1);if(['ArrowLeft','PageUp'].includes(e.key))go(-1);if(e.key==='Home')show(0);if(e.key==='End')show(S.length-1)}});
show((parseInt(location.hash.slice(1))||1)-1);
</script></body></html>"""


def slide_texts(pptx: str | Path) -> list[str]:
    out = []
    for s in Presentation(str(pptx)).slides:
        parts = []
        for sh in s.shapes:
            if sh.has_text_frame and sh.text_frame.text.strip():
                parts.append(sh.text_frame.text.strip())
            if getattr(sh, "has_table", False) and sh.has_table:
                parts.append(" | ".join(c.text for r in sh.table.rows for c in r.cells))
        out.append("\n".join(parts))
    return out


def to_html(pdf: str | Path, pptx: str | Path, out: str | Path, title: str, accent: str = "3366CC", lang: str = "ru") -> Path:
    texts = slide_texts(pptx)
    blocks = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc):
            svg = page.get_svg_image(text_as_path=True)
            svg = svg[svg.find("<svg"):]
            alt = html.escape(texts[i] if i < len(texts) else "")
            blocks.append(f'<section class="slide" aria-label="Слайд {i + 1}">{svg}<div class="sr">{alt}</div></section>')
    out = Path(out)
    out.write_text(HTML_TEMPLATE.format(title=html.escape(title), slides="\n".join(blocks), accent=accent, lang=lang), encoding="utf-8")
    return out


def export_manifest(out_dir: Path, files: dict[str, str]) -> Path:
    p = out_dir / "exports.json"
    p.write_text(json.dumps(files, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
