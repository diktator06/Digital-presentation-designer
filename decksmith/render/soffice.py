"""Бэкенд рендера: LibreOffice (headless) для PPTX -> PDF, PyMuPDF для PDF -> PNG/SVG.

Каждая конвертация получает свой профиль пользователя LibreOffice, чтобы несколько колод
(три варианта) рендерились параллельно без конфликтов блокировки профиля.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pymupdf

from decksmith.core.config import settings

log = logging.getLogger(__name__)

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
    """Путь к LibreOffice: из настроек, стандартных мест установки или PATH."""
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


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Запускает soffice в собственной группе процессов с выводом в файл, а не в каналы. По таймауту убивается
    вся группа: иначе зависший дочерний процесс, держащий каналы открытыми, мог бы блокировать
    `subprocess.run` намного дольше таймаута.
    """
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"},
                                start_new_session=hasattr(os, "killpg"))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
            raise
        out.seek(0)
        return proc.returncode, out.read()[-2000:].decode(errors="ignore")


def pptx_to_pdf(pptx: str | Path, out_dir: str | Path | None = None, timeout: int = 180) -> Path:
    """PPTX -> PDF. Зависший старт LibreOffice (редко, бывает под сильной параллельной нагрузкой) убивается и
    повторяется один раз со свежим профилем, поэтому одна конвертация никогда надолго не блокирует
    пайплайн.
    """
    pptx = Path(pptx).resolve()
    out_dir = Path(out_dir or pptx.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # LibreOffice на некоторых платформах чувствителен к не-ASCII путям: работаем во временном каталоге.
    work = Path(tempfile.mkdtemp(prefix="decksmith_render_"))
    src = work / "deck.pptx"
    shutil.copy(pptx, src)
    pdf = work / "deck.pdf"
    err = ""
    for attempt in range(2):
        profile = Path(tempfile.gettempdir()) / f"decksmith_lo_{uuid.uuid4().hex[:8]}"
        cmd = [soffice_path(), f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
               "--convert-to", "pdf", "--outdir", str(work), str(src)]
        t0 = time.monotonic()
        try:
            _, err = _run(cmd, timeout if attempt == 0 else max(timeout // 2, 60))
        except subprocess.TimeoutExpired:
            err = f"soffice timeout after {time.monotonic() - t0:.0f}s"
            log.warning("%s (%s), attempt %d", err, pptx.name, attempt + 1)
            continue
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        if pdf.exists():
            break
    if not pdf.exists():
        shutil.rmtree(work, ignore_errors=True)
        raise RenderError(f"soffice failed: {err[-500:]}")
    target = out_dir / (pptx.stem + ".pdf")
    shutil.move(str(pdf), target)
    shutil.rmtree(work, ignore_errors=True)
    return target


def pdf_to_pngs(pdf: str | Path, out_dir: str | Path, dpi: int = 96, prefix: str = "slide") -> list[Path]:
    """PDF -> PNG на каждую страницу."""
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


def render_pptx(pptx: str | Path, out_dir: str | Path, dpi: int = 96) -> tuple[Path, list[Path]]:
    """Рендерит колоду: возвращает (путь к pdf, [png на каждый слайд])."""
    out_dir = Path(out_dir)
    pdf = pptx_to_pdf(pptx, out_dir)
    pngs = pdf_to_pngs(pdf, out_dir / "png", dpi=dpi)
    return pdf, pngs
