"""Эмулятор OpenAI-совместимого API в том же процессе для детерминированного теста пути через LLM.

Подключается к LLMClient через httpx.MockTransport. Отвечает по скиллу (заголовок
X-DeckSmith-Skill), записывает каждый запрос и на первый вызов возвращает структуру,
не проходящую схему, чтобы раунд исправления JSON тоже проверялся.
"""
from __future__ import annotations

import json
import re

import httpx

OUTLINE = {
    "title": "Цифровой дизайнер презентаций",
    "subtitle": "Корпоративные презентации по брифу за минуты",
    "slides": [
        {"intent": "title", "title": "Цифровой дизайнер презентаций", "message": ""},
        {"intent": "agenda", "title": "План выступления", "message": "", "n_items": 4},
        {"intent": "stats", "title": "Одна колода — 10–15 слайдов и не более 5 минут", "message": "Рамки задачи", "n_items": 3,
         "facts": ["10–15 слайдов", "5 минут"], "data": "10–15; 5 минут; 3 варианта"},
        {"intent": "cards", "title": "Сервис разбирает шаблон как набор правил", "message": "Токены и паттерны", "n_items": 4},
        {"intent": "steps", "title": "Пайплайн из пяти слоёв", "message": "Слои разделены", "n_items": 5},
        {"intent": "chart", "title": "Генерация укладывается в лимит", "message": "Время этапов", "data": "время этапов"},
        {"intent": "table", "title": "Аудит покрывает четыре группы проверок", "message": "Группы проверок"},
        {"intent": "text", "title": "Итоги: сервис готов к пилоту", "message": "summary"},
        {"intent": "thanks", "title": "Спасибо!", "message": "Готовы к пилоту"},
    ],
}


def _writer_answer(intent: str, n: int) -> dict:
    """Ответ эмулятора за автора текстов для интента слайда."""
    if intent == "agenda":
        return {"items": [{"title": t} for t in ("Проблема", "Решение", "Архитектура", "Итоги")][:n]}
    if intent == "stats":
        return {"items": [{"value": "10–15", "title": "слайдов", "text": "объём колоды"},
                          {"value": "5 мин", "title": "лимит", "text": "время генерации"},
                          {"value": "3", "title": "варианта", "text": "вёрстки"}]}
    if intent in ("cards", "steps"):
        names = ["Парсинг", "Генерация", "Вёрстка", "Аудит", "Экспорт"]
        return {"items": [{"title": names[i], "text": f"Этап {i + 1} сквозного пайплайна", "icon": "layers"} for i in range(n or 4)]}
    if intent == "chart":
        return {"chart": {"type": "column", "categories": ["План", "Вёрстка", "Рендер", "Аудит"],
                          "series": [{"name": "Время", "values": [40, 10, 20, 30]}], "unit": "с", "y_title": "секунды"}}
    if intent == "table":
        return {"table": {"columns": ["Группа", "Проверок"], "rows": [["Вёрстка", "7"], ["Шаблон", "6"], ["Плотность", "5"], ["Целостность", "8"]]}}
    return {"bullets": ["Шаблон разбирается автоматически", "Три варианта вёрстки", "Аудит встроен в пайплайн"],
            "notes": "Коротко подвести итог."}


class Emulator:
    def __init__(self, fail_visual_on: set[int] | None = None):
        """Эмулятор: журнал запросов и номера слайдов, на которых визуальный аудит находит проблему."""
        self.requests: list[dict] = []
        self.outline_calls = 0
        self.fail_visual_on = fail_visual_on or {2}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        skill = request.headers.get("X-DeckSmith-Skill", "")
        self.requests.append({"skill": skill, "model": body.get("model"), "body": body})
        msgs = body["messages"]
        user = msgs[-1]["content"]
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if p.get("type") == "text")
        if skill.startswith("outline"):
            if skill.endswith(":repair"):
                content = json.dumps(OUTLINE, ensure_ascii=False)
            else:
                self.outline_calls += 1
                # слайд без "title": ошибка схемы
                broken = {"title": OUTLINE["title"], "slides": [{"intent": "title"}]}
                content = json.dumps(broken, ensure_ascii=False)
        elif skill.startswith("slide_writer"):
            m = re.search(r"intent=(\w+)", text)
            n = re.search(r"ровно (\d+) items", text)
            content = json.dumps(_writer_answer(m.group(1) if m else "text", int(n.group(1)) if n else 4), ensure_ascii=False)
        elif skill.startswith("shortener"):
            items = json.loads(re.search(r"(\[.*\])", text, re.S).group(1))
            content = json.dumps({"items": [{"id": it["id"], "paragraphs": [p[: max(8, it["budget"] // max(len(it["paragraphs"]), 1))]
                                                                              for p in it["paragraphs"]]} for it in items]}, ensure_ascii=False)
        elif skill.startswith("visual_audit"):
            idx = int(re.search(r"Слайд (\d+) из", text).group(1)) - 1
            bad = idx in self.fail_visual_on
            content = json.dumps({"answers": {"q1": not bad, "q2": True, "q3": True, "q4": True, "q5": True, "q6": True, "q7": True,
                                              "q8": True, "q10": True, "q11": True, "v1": True, "v2": True},
                                  "comments": {"q1": "заголовок называет тему"} if bad else {}, "summary": "слайд"}, ensure_ascii=False)
        elif skill.startswith("fixer"):
            texts = json.loads(re.search(r"\(id -> абзацы\):\s*(\{.*?\})\s*Замечания", text, re.S).group(1))
            first = next(iter(texts))
            content = json.dumps({"texts": {first: ["Исправленный заголовок-вывод"]}}, ensure_ascii=False)
        else:
            content = "{}"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}],
                                         "usage": {"prompt_tokens": len(text) // 4, "completion_tokens": len(content) // 4}})

    def transport(self) -> httpx.MockTransport:
        """HTTP-транспорт для LLMClient."""
        return httpx.MockTransport(self.handler)
