"""Versioned skills & agents.

Layout on disk (nothing prompt-related lives in Python code):

  skills/registry.yaml            active version per skill / agent
  skills/<skill>/<version>.yaml   prompt templates (Jinja2), model params, schema
  skills/agents/<agent>/<version>.yaml   workflow: ordered steps -> skill versions

Every run records {skill: version, sha256} in its manifest so any generated
deck can be traced back to the exact prompts that produced it.
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
        return _env.from_string(self.system).render(**ctx), _env.from_string(self.user).render(**ctx)

    @property
    def ref(self) -> str:
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
        return next((s for s in self.steps if s.get("name") == name), None)

    def enabled(self, name: str) -> bool:
        s = self.step(name)
        return bool(s) and s.get("enabled", True)


def _root() -> Path:
    return settings().skills_dir


@lru_cache(maxsize=1)
def registry() -> dict:
    return yaml.safe_load((_root() / "registry.yaml").read_text(encoding="utf-8"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=64)
def load_skill(name: str, version: str | None = None) -> Skill:
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
    version = version or registry()["agents"][name]["active"]
    path = _root() / "agents" / name / f"{version}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Agent(name=name, version=version, path=path, sha256=_sha(path), steps=data["steps"],
                 description=data.get("description", ""))


def skill_for_step(agent: Agent, step: str, default_skill: str) -> Skill:
    s = agent.step(step) or {}
    ref = s.get("skill", default_skill)
    name, _, ver = ref.partition("@")
    return load_skill(name, ver or None)


def list_versions() -> dict:
    out = {"skills": {}, "agents": {}}
    for name, meta in registry().get("skills", {}).items():
        vers = sorted(p.stem for p in (_root() / name).glob("*.yaml"))
        out["skills"][name] = {"active": meta["active"], "versions": vers}
    for name, meta in registry().get("agents", {}).items():
        vers = sorted(p.stem for p in (_root() / "agents" / name).glob("*.yaml"))
        out["agents"][name] = {"active": meta["active"], "versions": vers}
    return out
