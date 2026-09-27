"""OpenAI-совместимый клиент LLM/VLM (vLLM, SGLang, Ollama, облачные провайдеры, инференс организатора...).

* асинхронный, ограниченный параллелизм (один семафор на процесс)
* JSON-режим с терпимым извлечением + один раунд исправления по схеме
* телеметрия каждого вызова (задержка, токены) собирается в манифест запуска
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from decksmith.core.config import LLMConfig, settings
from decksmith.generation.skills import Skill

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class CallStat:
    skill: str
    model: str
    seconds: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ok: bool = True
    note: str = ""


@dataclass
class Telemetry:
    calls: list[CallStat] = field(default_factory=list)

    def summary(self) -> dict:
        """Сводка телеметрии вызовов модели для манифеста."""
        return {
            "calls": len(self.calls),
            "failed": sum(not c.ok for c in self.calls),
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "completion_tokens": sum(c.completion_tokens for c in self.calls),
            "llm_seconds_total": round(sum(c.seconds for c in self.calls), 1),
        }


def extract_json(text: str) -> Any:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON object in response")
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    # обрезанный вывод: пробуем закрыть скобки
    frag = text[start:]
    frag += "]" * max(frag.count("[") - frag.count("]"), 0) + "}" * max(frag.count("{") - frag.count("}"), 0)
    return json.loads(frag)


class LLMClient:
    def __init__(self, cfg: LLMConfig | None = None, telemetry: Telemetry | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg or settings().llm
        self.telemetry = telemetry or Telemetry()
        self._sem = asyncio.Semaphore(self.cfg.max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._next_start = 0.0  # монотонное время следующего разрешённого старта запроса (max_rps)
        self._client: httpx.AsyncClient | None = None
        self._transport = transport  # тесты подключают сюда эмулятор OpenAI-совместимого API в том же процессе

    @property
    def enabled(self) -> bool:
        """Модель подключена."""
        return self.cfg.enabled

    async def __aenter__(self):
        """Открывает HTTP-клиент."""
        self._client = httpx.AsyncClient(timeout=self.cfg.timeout_s, transport=self._transport)
        return self

    async def __aexit__(self, *exc):
        """Закрывает HTTP-клиент."""
        if self._client:
            await self._client.aclose()

    async def _pace(self) -> None:
        """Разносит старты запросов согласно `max_rps` (хостинговые API отклоняют всплески с HTTP 429)."""
        if self.cfg.max_rps <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            wait = self._next_start - now
            self._next_start = max(now, self._next_start) + 1.0 / self.cfg.max_rps
        if wait > 0:
            await asyncio.sleep(wait)

    def _model(self, kind: str) -> str:
        """Имя модели для вида вызова (текстовая или мультимодальная)."""
        if kind == "vlm":
            return self.cfg.vlm_model or self.cfg.model
        return self.cfg.model

    async def chat(self, messages: list[dict], *, model_kind: str = "default", temperature: float | None = None,
                   max_tokens: int = 2048, json_mode: bool = False, skill: str = "adhoc") -> str:
        if not self.enabled:
            raise LLMError("LLM disabled (offline mode)")
        body: dict[str, Any] = {
            "model": self._model(model_kind),
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.cfg.extra_body or {})
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        # только для наблюдаемости; серверы игнорируют неизвестные заголовки
        headers["X-DeckSmith-Skill"] = skill
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            t0 = time.time()
            try:
                async with self._sem:
                    await self._pace()
                    client = self._client or httpx.AsyncClient(timeout=self.cfg.timeout_s, transport=self._transport)
                    r = await client.post(url, json=body, headers=headers)
                    if self._client is None:
                        await client.aclose()
                if r.status_code == 400 and json_mode and "response_format" in body:
                    body.pop("response_format")  # провайдер без JSON-режима
                    continue
                if r.status_code == 400 and "chat_template_kwargs" in body:
                    body.pop("chat_template_kwargs")
                    continue
                # лимит запросов / перегруженный сервер: пауза и повтор
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("retry-after", "")
                    delay = float(ra) if ra.replace(".", "", 1).isdigit() else min(20.0, 2.0 * 2 ** attempt)
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:160]}")
                    self.telemetry.calls.append(CallStat(skill, body["model"], time.time() - t0, ok=False, note=str(last)[:200]))
                    await asyncio.sleep(min(delay, 30.0))
                    continue
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"].get("content") or ""
                usage = data.get("usage") or {}
                self.telemetry.calls.append(CallStat(skill, body["model"], time.time() - t0,
                                                     usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))
                return content
            except Exception as e:  # сеть / 5xx / лимит запросов
                last = e
                self.telemetry.calls.append(CallStat(skill, body["model"], time.time() - t0, ok=False, note=str(e)[:200]))
                await asyncio.sleep(1.5 * (attempt + 1))
        raise LLMError(f"{skill}: {last}")

    async def run_skill(self, skill: Skill, schema: type[T] | None = None, images: list[str | Path] | None = None,
                        validate=None, **ctx) -> T | dict | str:
        """Рендер + вызов + разбор. `validate(result) -> ошибка | None` добавляет смысловую проверку
        (например, число слайдов), которая запускает тот же единственный раунд исправления, что и ошибка
        схемы.
        """
        system, user = skill.render(**ctx)
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}]
            for img in images:
                b64 = base64.b64encode(Path(img).read_bytes()).decode()
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        want_json = skill.response == "json"
        raw = await self.chat(messages, model_kind=skill.model, temperature=skill.temperature, max_tokens=skill.max_tokens,
                              json_mode=want_json, skill=skill.ref)
        if not want_json:
            return raw.strip()
        try:
            data = extract_json(raw)
            result = schema.model_validate(data) if schema else data
            problem = validate(result) if validate else None
            if problem is None:
                return result
            raise ValueError(problem)
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            # один раунд исправления: показываем модели её ответ и ошибку валидации
            from decksmith.generation.skills import load_skill

            _, repair = load_skill("json_repair").render(error=str(e)[:800])
            fix = [*messages, {"role": "assistant", "content": raw[:12000]}, {"role": "user", "content": repair}]
            raw2 = await self.chat(fix, model_kind=skill.model, temperature=0.1, max_tokens=skill.max_tokens,
                                   json_mode=True, skill=skill.ref + ":repair")
            data = extract_json(raw2)
            return schema.model_validate(data) if schema else data
