"""Генерация изображений по тексту для иллюстраций на слайдах (открытые веса ≤ 20B, например FLUX.1-schnell, Apache-2.0).

Не зависит от провайдера: подходит любой endpoint с OpenAI `/images/generations`
(vLLM-omni, Together, DeepInfra, Nebius, локальный сервер diffusers...).
Изображения генерируются один раз на запуск (общие для трёх вариантов), параллельно,
с жёстким таймаутом: отсутствующее изображение никогда не блокирует колоду.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx

from decksmith.core.config import ImageConfig, settings
from decksmith.core.models import DeckPlan, PatternKind

log = logging.getLogger(__name__)


def styled_prompt(prompt: str, palette_desc: str) -> str:
    """Оборачивает промпт содержания версионируемым стилем изображений (skills/image_style/<версия>.yaml)."""
    from decksmith.generation.skills import load_skill

    _, user = load_skill("image_style").render(prompt=prompt, palette=palette_desc)
    return user


async def _generate(client: httpx.AsyncClient, cfg: ImageConfig, prompt: str, out: Path) -> Path | None:
    """Один запрос генерации изображения; None при ошибке или таймауте."""
    body = {"model": cfg.model, "prompt": prompt, "n": 1, "size": cfg.size, "response_format": "b64_json"}
    if cfg.steps:
        body["steps"] = cfg.steps
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        r = await client.post(cfg.base_url.rstrip("/") + "/images/generations", json=body, headers=headers)
        r.raise_for_status()
        d = r.json()["data"][0]
        if d.get("b64_json"):
            data = base64.b64decode(d["b64_json"])
        elif d.get("url"):
            data = (await client.get(d["url"])).content
        else:
            return None
        # провайдеры отдают и PNG, и JPEG: расширение файла — по сигнатуре содержимого
        out = out.with_suffix(".jpg" if data[:3] == b"\xff\xd8\xff" else ".png")
        out.write_bytes(data)
        return out
    except Exception as e:
        log.warning("image generation failed: %s", e)
        return None


async def generate_images(plan: DeckPlan, out_dir: Path, palette_desc: str, max_images: int | None = None) -> dict[str, str]:
    """Иллюстрации для слайдов плана, параллельно и с общим таймаутом; пусто, если генерация выключена."""
    cfg = settings().image
    if cfg.provider in ("none", "") or not cfg.base_url:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [s for s in plan.slides if s.image_prompt]
    wanted.sort(key=lambda s: 0 if s.intent == PatternKind.image_text else 1)
    wanted = wanted[: (max_images or cfg.max_images_per_deck)]
    if not wanted:
        return {}
    async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
        tasks = [_generate(client, cfg, styled_prompt(s.image_prompt, palette_desc), out_dir / f"{s.id}.png") for s in wanted]
        res = await asyncio.gather(*tasks)
    return {s.id: str(p) for s, p in zip(wanted, res) if p}
