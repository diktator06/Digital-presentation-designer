"""Text-to-image for in-slide illustrations (open weights <= 20B, e.g. FLUX.1-schnell, Apache-2.0).

Provider-agnostic: any endpoint implementing OpenAI `/images/generations`
(vLLM-omni, Together, DeepInfra, Nebius, a local diffusers server...).
Images are generated once per run (shared by the three variants), in parallel,
with a hard timeout: a missing image never blocks a deck.
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
    """Wrap a content prompt with the versioned image style (skills/image_style/<ver>.yaml)."""
    from decksmith.generation.skills import load_skill

    _, user = load_skill("image_style").render(prompt=prompt, palette=palette_desc)
    return user


async def _generate(client: httpx.AsyncClient, cfg: ImageConfig, prompt: str, out: Path) -> Path | None:
    body = {"model": cfg.model, "prompt": prompt, "n": 1, "size": cfg.size, "response_format": "b64_json"}
    if cfg.steps:
        body["steps"] = cfg.steps
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        r = await client.post(cfg.base_url.rstrip("/") + "/images/generations", json=body, headers=headers)
        r.raise_for_status()
        d = r.json()["data"][0]
        if d.get("b64_json"):
            out.write_bytes(base64.b64decode(d["b64_json"]))
        elif d.get("url"):
            out.write_bytes((await client.get(d["url"])).content)
        else:
            return None
        return out
    except Exception as e:
        log.warning("image generation failed: %s", e)
        return None


async def generate_images(plan: DeckPlan, out_dir: Path, palette_desc: str, max_images: int | None = None) -> dict[str, str]:
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
