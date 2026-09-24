"""Rendering backend: LibreOffice (headless) for PPTX -> PDF, PyMuPDF for PDF -> PNG/SVG.

Each conversion gets its own LibreOffice user profile so that several decks
(three variants) can be rendered in parallel without profile lock conflicts.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pymupdf

from decksmith.core.config import settings

_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
]


class RenderError(RuntimeError):
    pass


def soffice_path() -> str:
    configured = settings().render.soffice
    if configured and Path(configured).exists():
        return configured
    for c in _CANDIDATES:
        if Path(c).exists():
            return c
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    raise RenderError("LibreOffice (soffice) not found; set render.soffice in config or DECKSMITH_SOFFICE")


def pptx_to_pdf(pptx: str | Path, out_dir: str | Path | None = None, timeout: int = 180) -> Path:
    pptx = Path(pptx).resolve()
    out_dir = Path(out_dir or pptx.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.gettempdir()) / f"decksmith_lo_{uuid.uuid4().hex[:8]}"
    # LibreOffice is sensitive to non-ASCII paths on some platforms: work in a temp dir.
    work = Path(tempfile.mkdtemp(prefix="decksmith_render_"))
    src = work / "deck.pptx"
    shutil.copy(pptx, src)
    cmd = [
        soffice_path(),
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(work),
        str(src),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"})
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"soffice timeout after {timeout}s") from e
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    pdf = work / "deck.pdf"
    if not pdf.exists():
        raise RenderError(f"soffice failed: {proc.stderr.decode(errors='ignore')[-500:]}")
    target = out_dir / (pptx.stem + ".pdf")
    shutil.move(str(pdf), target)
    shutil.rmtree(work, ignore_errors=True)
    return target


def pdf_to_pngs(pdf: str | Path, out_dir: str | Path, dpi: int = 96, prefix: str = "slide") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            p = out_dir / f"{prefix}_{i + 1:02d}.png"
            pix.save(p)
            paths.append(p)
    return paths


def pdf_to_svgs(pdf: str | Path) -> list[str]:
    with pymupdf.open(pdf) as doc:
        return [page.get_svg_image(text_as_path=False) for page in doc]


def render_pptx(pptx: str | Path, out_dir: str | Path, dpi: int = 96) -> tuple[Path, list[Path]]:
    """Render a deck: returns (pdf_path, [png per slide])."""
    out_dir = Path(out_dir)
    pdf = pptx_to_pdf(pptx, out_dir)
    pngs = pdf_to_pngs(pdf, out_dir / "png", dpi=dpi)
    return pdf, pngs
