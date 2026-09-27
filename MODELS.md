# MODELS

Ограничения ТЗ: только открытые веса, лицензии Apache 2.0 / MIT, LLM/VLM до 35B, text-to-image
до 20B. Сервис обращается к моделям через **OpenAI-совместимый API** (vLLM, SGLang, Ollama,
LM Studio, облачный провайдер или инференс VK для финалистов) — модель меняется в
`config/default.yaml` / переменных окружения без изменения кода.

| Роль | Модель по умолчанию | Параметры | Лицензия | Где используется | Hugging Face |
|---|---|---|---|---|---|
| LLM (план, тексты, сокращение, исправления) | Qwen3-32B | 32.8B, dense | Apache 2.0 | скиллы `outline`, `slide_writer`, `shortener`, `fixer` | https://huggingface.co/Qwen/Qwen3-32B |
| LLM на хостинге (отборочный этап, по умолчанию в `config/vsegpt.yaml`) | Qwen3.6-35B-A3B | 35B MoE, 3B активных | Apache 2.0 | все текстовые скиллы: `outline`, `slide_writer`, `headline`, `shortener`, `fixer` | https://huggingface.co/Qwen/Qwen3.6-35B-A3B |
| LLM (быстрая альтернатива) | Qwen3-30B-A3B-Instruct-2507 | 30.5B MoE, ≈3.3B активных | Apache 2.0 | те же скиллы при дефиците скорости | https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507 |
| VLM на хостинге (визуальный аудит; модель финала) | Qwen3.8-27B | 27B dense, текст + изображения | Apache 2.0 | скиллы `visual_audit`, `pattern_labeler` | https://huggingface.co/Qwen/Qwen3.8-27B |
| VLM на своём GPU | Qwen2.5-VL-32B-Instruct | 32B | Apache 2.0 | те же скиллы | https://huggingface.co/Qwen/Qwen2.5-VL-32B-Instruct |
| VLM (альтернатива) | Qwen3-VL-30B-A3B-Instruct | 30B MoE | Apache 2.0 | то же | https://huggingface.co/Qwen/Qwen3-VL-30B-A3B-Instruct |
| Text-to-image на хостинге (иллюстрации в слайдах) | FLUX.2 [klein] 4B | 4B | Apache 2.0 | `generation/images.py`, слайды `image_text` | https://huggingface.co/black-forest-labs/FLUX.2-klein-4B |
| Text-to-image на своём GPU | FLUX.1 [schnell] | 12B | Apache 2.0 | то же, 4 шага | https://huggingface.co/black-forest-labs/FLUX.1-schnell |
| Инференс финала | **Qwen 3.8 27B** от VK (ТЗ: «командам топ-10 предоставляется модель Qwen 3.8 27b; использование инференса VK обязательно») | 27B dense, мультимодальная | Apache 2.0 | все LLM-скиллы и визуальный аудит — смена `DECKSMITH_LLM_BASE_URL`/`MODEL`/`VLM_MODEL` | https://huggingface.co/Qwen/Qwen3.8-27B |

Не-генеративные компоненты (детерминированные, CPU): LibreOffice (рендер PPTX→PDF, MPL-2.0),
PyMuPDF (PDF→PNG/SVG, AGPL-3.0 — допустимо для open-source репозитория), python-pptx (MIT),
Pillow/FreeType (замер текста), Lucide icons (ISC).

## Почему так

* **Qwen3-32B** — сильная Apache-2.0 модель ≤35B для русского языка и строгого JSON на своём GPU;
  thinking-режим отключается (`chat_template_kwargs.enable_thinking=false`) ради скорости. На
  хостинге быстрее и без раундов починки отвечает **Qwen3.6-35B-A3B** (замер ниже), а смысловой
  аудит по картинке идёт на **Qwen3.8-27B** — той же модели, что даёт VK в финале.
* **Две стадии генерации** (outline → параллельные slide_writer) вместо одного длинного JSON:
  меньше латентность и меньше риск сломанного вывода; 12 коротких вызовов идут параллельно.
* **VLM только в аудите**: смысловые вопросы Приложения 1 требуют картинку слайда; всё, что
  измеримо по файлу, проверяется детерминированно без модели.
* **FLUX.2 [klein] 4B / FLUX.1-schnell**: дистиллированные модели на несколько шагов диффузии →
  секунды на изображение; изображения генерируются один раз на запуск и переиспользуются тремя
  вариантами. На хостинге VseGPT FLUX.1-schnell недоступна на базовом тарифе, поэтому там работает
  FLUX.2 [klein] 4B (та же лицензия Apache 2.0, ≈3,9 ₽ за картинку 1024×768).

## Системные требования (оценка)

| Компонент | bf16 | 4-bit (AWQ/GPTQ) |
|---|---|---|
| Qwen3-32B | ≈ 66 ГБ VRAM (1×H100 80GB / 2×A100 40GB) | ≈ 20 ГБ (1×RTX 4090 / A100 40GB) |
| Qwen3-30B-A3B | ≈ 61 ГБ | ≈ 17 ГБ; высокая скорость декодирования |
| Qwen2.5-VL-32B | ≈ 66 ГБ | ≈ 21 ГБ |
| FLUX.1-schnell | ≈ 24 ГБ (≈ 12 ГБ с CPU-offload) | — |
| FLUX.2 [klein] 4B | ≈ 13 ГБ (потребительская GPU) | — |
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

## Подключение через хостинг (VseGPT) — проверено

Любой OpenAI-совместимый хостинг подключается через `.env` (не хранится в git), без правки кода:

```bash
DECKSMITH_CONFIG=config/vsegpt.yaml          # профиль для хостингов в стиле OpenRouter
DECKSMITH_LLM_BASE_URL=https://api.vsegpt.ru/v1
DECKSMITH_LLM_API_KEY=...
DECKSMITH_LLM_MODEL=qwen/qwen3.6-35b-a3b     # план и тексты
DECKSMITH_VLM_MODEL=qwen/qwen3.8-27b         # смысловой аудит по картинке слайда
DECKSMITH_IMAGE_PROVIDER=openai_images       # иллюстрации в слайдах
DECKSMITH_IMAGE_BASE_URL=https://api.vsegpt.ru/v1
DECKSMITH_IMAGE_API_KEY=...                  # тот же ключ
DECKSMITH_IMAGE_MODEL=img-flux/flux-2-klein-4b
```

Что выяснилось на живом сервисе и учтено в `config/vsegpt.yaml` и клиенте:

* Qwen3 на хостинге по умолчанию «размышляет» (до 2.3 тыс. лишних токенов и ~30 с на ответ);
  переключатель vLLM (`chat_template_kwargs`) хостинг игнорирует, работает параметр OpenRouter
  `reasoning: {enabled: false}`.
* Лимит «не больше 1 запроса в секунду» на ключ: клиент выдерживает темп (`max_rps: 0.7`), ответы
  429/5xx повторяются с паузой (`Retry-After` или экспоненциальная); в первом прогоне без этого
  55 из 91 запроса получили отказ, после — 0.
* Выбор текстовой модели — по замеру на одних и тех же входах (26.09.2026, бриф «КофеБот»,
  шаблон vk_workspace, 14 слайдов; цены VseGPT за 1 тыс. токенов):

  | Модель | Шаг `outline` (строгий JSON, ровно 14 слайдов) | Визуальный аудит одного слайда |
  |---|---|---|
  | `qwen/qwen3-32b` | 122 с, нужен раунд починки, 31 тыс. + 10 тыс. токенов, ≈1.0 ₽; заголовки-темы | — (без зрения) |
  | `qwen/qwen3.6-35b-a3b` | **11 с**, с первого раза, 3.9 тыс. + 1.3 тыс. токенов, ≈0.6 ₽ | 4 с, ≈0.15 ₽, нашла пустые карточки |
  | `qwen/qwen3.8-27b` | 13 с, с первого раза, ≈1.9 ₽; лучшие заголовки-выводы | 9 с, ≈0.30 ₽, нашла пустые карточки |

  Сквозной прогон на `qwen3.6-35b-a3b` (`configs/real_model_smoke.yaml`, 8 слайдов, аудит на
  `qwen3.8-27b`): план и тексты **45 с** (на `qwen3-32b` — около 100 с), колода целиком 67 с,
  23 вызова, 6 из 6 заголовков переписаны в выводы, балл аудита 98 без замечаний. Поэтому на
  хостинге тексты пишет `qwen3.6-35b-a3b` (35B по сумме весов — в пределе ТЗ «до 35B»), а смысловой
  аудит делает `qwen3.8-27b` — модель финала. `qwen3-32b` остаётся рабочей альтернативой.

Сквозной прогон (`decksmith run configs/real_model_e2e.yaml`: шаблон vk_education, контент — ТЗ,
12 слайдов, 3 варианта, аудит по картинке каждого слайда): **2 мин 13 с – 2 мин 53 с** от старта
до трёх колод в PPTX/PDF/HTML, из них план и тексты — 83–123 с (MacBook Air M1: рендер и аудит
локально, модели — на хостинге). Модель со зрением действительно находит смысловые дефекты
Приложения 1: «заголовок называет тему, а не вывод», пустой слайд-раздел, текст под графикой.

Стоимость: основная часть — аудит по картинке (по запросу на каждый слайд каждого варианта) на
более дорогой модели; тексты на `qwen3-32b` дёшевы. В финале инференс VK бесплатен для команды,
но при тестах на платном хостинге аудит по картинке стоит включать выборочно
(`pipeline.visual_audit: false` в профиле).

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
