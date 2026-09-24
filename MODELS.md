# MODELS

Ограничения ТЗ: только открытые веса, лицензии Apache 2.0 / MIT, LLM/VLM до 35B, text-to-image
до 20B. Сервис обращается к моделям через **OpenAI-совместимый API** (vLLM, SGLang, Ollama,
LM Studio, облачный провайдер или инференс VK для финалистов) — модель меняется в
`config/default.yaml` / переменных окружения без изменения кода.

| Роль | Модель по умолчанию | Параметры | Лицензия | Где используется | Hugging Face |
|---|---|---|---|---|---|
| LLM (план, тексты, сокращение, исправления) | Qwen3-32B | 32.8B, dense | Apache 2.0 | скиллы `outline`, `slide_writer`, `shortener`, `fixer` | https://huggingface.co/Qwen/Qwen3-32B |
| LLM (быстрая альтернатива) | Qwen3-30B-A3B-Instruct-2507 | 30.5B MoE, ≈3.3B активных | Apache 2.0 | те же скиллы при дефиците скорости | https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507 |
| VLM (визуальный аудит, опционально разметка шаблона) | Qwen2.5-VL-32B-Instruct | 32B | Apache 2.0 | скиллы `visual_audit`, `pattern_labeler` | https://huggingface.co/Qwen/Qwen2.5-VL-32B-Instruct |
| VLM (альтернатива) | Qwen3-VL-30B-A3B-Instruct | 30B MoE | Apache 2.0 | то же | https://huggingface.co/Qwen/Qwen3-VL-30B-A3B-Instruct |
| Text-to-image (иллюстрации в слайдах) | FLUX.1 [schnell] | 12B | Apache 2.0 | `generation/images.py`, 4 шага | https://huggingface.co/black-forest-labs/FLUX.1-schnell |
| Инференс финала | модель VK (Qwen, 27B) | ≤ 35B | по условиям организатора | все LLM-скиллы — смена `DECKSMITH_LLM_BASE_URL`/`MODEL` | — |

Не-генеративные компоненты (детерминированные, CPU): LibreOffice (рендер PPTX→PDF, MPL-2.0),
PyMuPDF (PDF→PNG/SVG, AGPL-3.0 — допустимо для open-source репозитория), python-pptx (MIT),
Pillow/FreeType (замер текста), Lucide icons (ISC).

## Почему так

* **Qwen3-32B** — сильнейшая Apache-2.0 модель ≤35B для русского языка и строгого JSON;
  thinking-режим отключается (`chat_template_kwargs.enable_thinking=false`) ради скорости.
* **Две стадии генерации** (outline → параллельные slide_writer) вместо одного длинного JSON:
  меньше латентность и меньше риск сломанного вывода; 12 коротких вызовов идут параллельно.
* **VLM только в аудите**: смысловые вопросы Приложения 1 требуют картинку слайда; всё, что
  измеримо по файлу, проверяется детерминированно без модели.
* **FLUX.1-schnell**: 4 шага диффузии → секунды на изображение; изображения генерируются
  один раз на запуск и переиспользуются тремя вариантами.

## Системные требования (оценка)

| Компонент | bf16 | 4-bit (AWQ/GPTQ) |
|---|---|---|
| Qwen3-32B | ≈ 66 ГБ VRAM (1×H100 80GB / 2×A100 40GB) | ≈ 20 ГБ (1×RTX 4090 / A100 40GB) |
| Qwen3-30B-A3B | ≈ 61 ГБ | ≈ 17 ГБ; высокая скорость декодирования |
| Qwen2.5-VL-32B | ≈ 66 ГБ | ≈ 21 ГБ |
| FLUX.1-schnell | ≈ 24 ГБ (≈ 12 ГБ с CPU-offload) | — |
| Сервис без моделей | 2 CPU, 4 ГБ RAM, LibreOffice | — |

Бюджет времени (после Enter, 3 колоды параллельно): план ≈ 1 + 12 параллельных вызовов LLM,
сокращение — 1 пакетный вызов на вариант, визуальный аудит — по вызову на слайд (параллельно,
ограничение `llm.max_concurrency`). Рендер и детерминированный аудит — секунды.

## Конфигурация

```yaml
llm:
  base_url: ${DECKSMITH_LLM_BASE_URL}     # например http://gpu-host:8000/v1 (vLLM)
  api_key:  ${DECKSMITH_LLM_API_KEY}
  model:    ${DECKSMITH_LLM_MODEL:-Qwen/Qwen3-32B}
  vlm_model: ${DECKSMITH_VLM_MODEL}       # пусто = визуальный аудит выключен
image:
  provider: ${DECKSMITH_IMAGE_PROVIDER:-none}   # openai_images для FLUX
  model:    ${DECKSMITH_IMAGE_MODEL:-black-forest-labs/FLUX.1-schnell}
```

Отключение «мышления» Qwen3 зависит от сервера и задаётся в конфиге, а не в коде:
vLLM — `extra_body: {chat_template_kwargs: {enable_thinking: false}}`, Ollama —
`extra_body: {reasoning_effort: none}` (см. `config/local_ollama.yaml`).

## Проверено на реальной модели

Весь пайплайн прогнан на **Qwen3-4B** (Apache 2.0) локально через Ollama на MacBook Air M1 8 ГБ
(`decksmith run configs/real_model_check.yaml`): план строит модель, 16 вызовов, 0 ошибок,
баллы аудита трёх вариантов 85.6 / 86.9 / 87.9. Время на такой машине — ~9 минут (инференс 4B на CPU/GPU
ноутбука); лимит 5 минут рассчитан на серверную Qwen3-32B. Слабая модель вскрыла и помогла
закрыть проблемы устойчивости (см. docs/UNIVERSALITY.md): лишние поля в ответах, пункты с
переносами строк, заголовки на чужом языке, дубли, мало слайдов, название не по брифу.

Пример локального запуска моделей через vLLM:

```bash
vllm serve Qwen/Qwen3-32B-AWQ --max-model-len 32768 --port 8001
vllm serve Qwen/Qwen2.5-VL-32B-Instruct-AWQ --max-model-len 16384 --port 8002
```
