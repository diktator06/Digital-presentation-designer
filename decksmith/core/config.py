"""Runtime configuration: one YAML file (+ ${ENV:-default} interpolation).

Resolution order: explicit path -> $DECKSMITH_CONFIG -> config/default.yaml.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE lines of the project's (gitignored) .env become environment defaults, as
    docker-compose does with env_file; variables already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _interpolate(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


class LLMConfig(BaseModel):
    provider: str = "openai_compatible"  # openai_compatible | offline
    base_url: str = ""
    api_key: str = ""
    model: str = "Qwen/Qwen3-32B"
    vlm_model: str = ""  # multimodal model for visual audit / template labelling
    max_concurrency: int = 8
    timeout_s: float = 120.0
    temperature: float = 0.3
    max_retries: int = 2
    extra_body: dict = Field(default_factory=dict)  # e.g. {"chat_template_kwargs": {"enable_thinking": false}}

    @property
    def enabled(self) -> bool:
        return self.provider != "offline" and bool(self.base_url) and bool(self.model)


class ImageConfig(BaseModel):
    provider: str = "none"  # none | openai_images | placeholder
    base_url: str = ""
    api_key: str = ""
    model: str = "black-forest-labs/FLUX.1-schnell"
    size: str = "1024x768"
    steps: int = 4
    max_images_per_deck: int = 4
    timeout_s: float = 60.0


class RenderConfig(BaseModel):
    soffice: str = ""
    dpi: int = 96
    timeout_s: int = 180


class PipelineConfig(BaseModel):
    target_slides: int = 12
    min_slides: int = 10
    max_slides: int = 15
    variants: list[str] = Field(default_factory=lambda: ["balanced", "visual", "dense"])
    deadline_s: float = 285.0
    visual_audit: bool = True
    auto_fix: bool = True


class PathsConfig(BaseModel):
    workspace: str = "workspace"
    skills: str = "skills"
    assets: str = "assets"

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path


class Settings(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @property
    def workspace(self) -> Path:
        p = self.paths.resolve(self.paths.workspace)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def skills_dir(self) -> Path:
        return self.paths.resolve(self.paths.skills)

    @property
    def assets_dir(self) -> Path:
        return self.paths.resolve(self.paths.assets)


def load_settings(path: str | Path | None = None) -> Settings:
    path = path or os.environ.get("DECKSMITH_CONFIG") or ROOT / "config" / "default.yaml"
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _interpolate(data)
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()


def override_settings(path: str | Path) -> Settings:
    settings.cache_clear()
    os.environ["DECKSMITH_CONFIG"] = str(path)
    return settings()
