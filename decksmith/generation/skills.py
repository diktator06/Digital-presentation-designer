"""Версионируемые скиллы и агенты.

Раскладка на диске (всё, что касается промптов, живёт вне кода Python):

  skills/registry.yaml                  активная версия каждого скилла / агента
  skills/<скилл>/<версия>.yaml          шаблоны промптов (Jinja2), параметры модели, схема
  skills/agents/<агент>/<версия>.yaml   процесс: упорядоченные шаги -> версии скиллов

Каждый запуск записывает {скилл: версия, sha256} в манифест, поэтому любую
сгенерированную колоду можно отследить до точных промптов, которые её создали.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from decksmith.core.config import settings

_env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False)
_env.filters["tojson_ru"] = lambda v: __import__("json").dumps(v, ensure_ascii=False)


@dataclass
class Skill:
    name: str
    version: str
    path: Path
    sha256: str
    system: str
    user: str
    model: str = "default"  # default | vlm
    temperature: float = 0.3
    max_tokens: int = 2048
    response: str = "json"  # json | text
    description: str = ""
    params: dict = field(default_factory=dict)

    def render(self, **ctx) -> tuple[str, str]:
        """Рендер системного и пользовательского промптов скилла (Jinja2)."""
        return _env.from_string(self.system).render(**ctx), _env.from_string(self.user).render(**ctx)

    @property
    def ref(self) -> str:
        """Ссылка «имя@версия»."""
        return f"{self.name}@{self.version}"


@dataclass
class Agent:
    name: str
    version: str
    path: Path
    sha256: str
    steps: list[dict]
    description: str = ""

    def step(self, name: str) -> dict | None:
        """Шаг процесса агента по имени."""
        return next((s for s in self.steps if s.get("name") == name), None)

    def enabled(self, name: str) -> bool:
        """Шаг агента включён."""
        s = self.step(name)
        return bool(s) and s.get("enabled", True)


def _root() -> Path:
    """Каталог скиллов."""
    return settings().skills_dir


@lru_cache(maxsize=1)
def registry() -> dict:
    """Реестр активных версий скиллов и агентов."""
    return yaml.safe_load((_root() / "registry.yaml").read_text(encoding="utf-8"))


def _sha(p: Path) -> str:
    """Короткий sha256 файла (для манифеста)."""
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=64)
def load_skill(name: str, version: str | None = None) -> Skill:
    """Загружает скилл нужной (по умолчанию активной) версии."""
    version = version or registry()["skills"][name]["active"]
    path = _root() / name / f"{version}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Skill(
        name=name,
        version=version,
        path=path,
        sha256=_sha(path),
        system=data.get("system", ""),
        user=data["user"],
        model=data.get("model", "default"),
        temperature=float(data.get("temperature", 0.3)),
        max_tokens=int(data.get("max_tokens", 2048)),
        response=data.get("response", "json"),
        description=data.get("description", ""),
        params=data.get("params", {}) or {},
    )


@lru_cache(maxsize=8)
def load_agent(name: str = "deck_agent", version: str | None = None) -> Agent:
    """Загружает агента нужной (по умолчанию активной) версии."""
    version = version or registry()["agents"][name]["active"]
    path = _root() / "agents" / name / f"{version}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Agent(name=name, version=version, path=path, sha256=_sha(path), steps=data["steps"],
                 description=data.get("description", ""))


def skill_for_step(agent: Agent, step: str, default_skill: str) -> Skill:
    """Скилл, который агент назначил шагу (или скилл по умолчанию)."""
    s = agent.step(step) or {}
    ref = s.get("skill", default_skill)
    name, _, ver = ref.partition("@")
    return load_skill(name, ver or None)


def list_versions() -> dict:
    """Все версии скиллов и агентов с отметкой активной."""
    out = {"skills": {}, "agents": {}}
    for name, meta in registry().get("skills", {}).items():
        vers = sorted(p.stem for p in (_root() / name).glob("*.yaml"))
        out["skills"][name] = {"active": meta["active"], "versions": vers}
    for name, meta in registry().get("agents", {}).items():
        vers = sorted(p.stem for p in (_root() / "agents" / name).glob("*.yaml"))
        out["agents"][name] = {"active": meta["active"], "versions": vers}
    return out
