# DeckSmith — цифровой дизайнер презентаций

Сервис для кейса VK Tech «Цифровой дизайнер презентаций» (Лидеры цифровой трансформации 2026).
По краткому брифу и контент-пакету генерирует презентацию в **произвольном корпоративном шаблоне
.pptx**: читает шаблон как набор правил (дизайн-токены + композиционные паттерны), строит
структуру колоды, верстает **три варианта** и проверяет их собственным **аудитом** с
визуализацией проблем и выбором исправлений. Экспорт — `.pptx` (только нативные объекты),
`.pdf`, `.html`.

* Работа на незнакомом шаблоне и слепой стресс-тест — [docs/UNIVERSALITY.md](docs/UNIVERSALITY.md),
  таблица по 31 шаблону — [docs/STRESS_RESULTS.md](docs/STRESS_RESULTS.md)
* Архитектура и границы слоёв — [ARCHITECTURE.md](ARCHITECTURE.md)
* Модели, лицензии, требования — [MODELS.md](MODELS.md)
* Проверки аудита и тесты — [AUDIT.md](AUDIT.md)
* Промпты и конфиги скиллов/агентов (версионируются отдельно от кода) — [skills/](skills/)

## Главное

* **Любой шаблон**: `.pptx`, `.potx`, `.ppt`, `.odp`, `.otp`; никаких правил под конкретные файлы
  (линт в тестах). Слепой стресс-тест на 31 шаблоне (23 шаблона LibreOffice, шаблон ЛЦТ-2026,
  4 синтетических с трудными свойствами, 3 шаблона датасета): 93 колоды, 0 падений, средний балл
  аудита 93.4, 0 колод с ошибками, весь прогон — 255 с на M1.
* **Три варианта** на один контент: сбалансированный / визуальный / аналитический — ось
  «плотность × способ визуализации × порядок» ([config/variants.yaml](config/variants.yaml)).
* **Аудит внутри пайплайна**: 29 детерминированных проверок + 12 смысловых вопросов к картинке
  слайда (VLM), подсветка проблем на слайде, выбор исправлений в UI.
* **Экспорт** только нативными объектами: `.pptx` (текст, фигуры, таблицы, диаграммы с данными),
  `.pdf`, самодостаточный `.html`.
* **Модели**: открытые веса Apache 2.0 через OpenAI-совместимый API; путь проверен на эмуляторе в
  тестах, локально на Qwen3-4B и на хостинге (тексты — Qwen3.6-35B-A3B, аудит по картинке —
  Qwen3.8-27B, иллюстрации в слайдах — FLUX.2 [klein] 4B, см. [MODELS.md](MODELS.md)).

## Подготовка данных

Шаблоны и ТЗ из датасета организатора в репозиторий не входят. Для демо-конфигов и тестов положите их
в `data/` под такими именами (таблица соответствия — в [data/templates/README.md](data/templates/README.md)):

```
data/templates/vk_tech.pptx        ← «VK Tech шаблон.pptx»
data/templates/vk_workspace.pptx   ← «VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx»
data/templates/vk_education.pptx   ← «Шаблон презентации VK Education.pptx»
data/content/tz_vk_tech.pdf        ← «4. VK Tech.pdf» (контент-пакет демо)
```

Без них сервис тоже работает: любой шаблон и контент загружаются в интерфейсе.

## Быстрый старт (Docker)

```bash
cp .env.example .env          # вписать endpoint модели (см. «Подключение модели») или оставить офлайн
docker compose up --build     # образ копирует data/, поэтому шаблоны положите до сборки
# UI: http://localhost:8000
```

## Локально

Требования: Python 3.11+ (проверено на 3.12 и 3.14), Node 20+, LibreOffice (`soffice` в PATH,
в `/Applications` на macOS или путь в `DECKSMITH_SOFFICE`).

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: py -3 -m venv .venv; .venv\Scripts\activate
pip install -e ".[dev]"
(cd frontend && npm ci && npm run build)
cp .env.example .env                # модель (см. «Подключение модели»); без неё — офлайн-режим
decksmith serve                     # UI + API на http://localhost:8000
```

Команда `decksmith` доступна в активированном окружении; без активации — `.venv/bin/decksmith`.

Воспроизводимый запуск из конфиг-файла (3 шаблона × 3 варианта на одном контенте):

```bash
decksmith run configs/demo.yaml            # с моделью из .env
decksmith run configs/demo_offline.yaml    # без модели, даже если она задана в .env (офлайн-планировщик)
decksmith run configs/real_model_check.yaml  # локальная открытая модель через Ollama (config/local_ollama.yaml)
decksmith run configs/real_model_smoke.yaml  # быстрая проверка модели из .env: 1 вариант, 8 слайдов (~6 ₽ на VseGPT)
decksmith run configs/pitch.yaml           # питч проекта на шаблоне ЛЦТ-2026 по готовому плану (без модели)
```

В конфиге запуска можно задать готовый план колоды (`plan: путь.json`) — тогда шаг
планирования пропускается, а вёрстка, аудит, автоисправления и экспорт идут как обычно.
Длинные прогоны на ноутбуке запускайте с `caffeinate -i` (macOS), иначе сон системы искажает замеры времени.

Другие команды:

```bash
decksmith analyze path/to/template.pptx --sheet   # профиль шаблона + картинка декомпозиции
decksmith audit deck.pptx --template template.pptx
decksmith skills                                   # версии скиллов/агентов и каталог проверок
pytest -q                                          # 53 теста
python scripts/stress.py <шаблоны...> --synthetic  # слепой стресс-тест + контакт-листы
```

## Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `DECKSMITH_LLM_PROVIDER` | `openai_compatible` или `offline` | `openai_compatible` |
| `DECKSMITH_LLM_BASE_URL` | OpenAI-совместимый endpoint (vLLM, Ollama, провайдер, инференс VK) | — (пусто = офлайн) |
| `DECKSMITH_LLM_API_KEY` | ключ endpoint | — |
| `DECKSMITH_LLM_MODEL` | модель для текстовых скиллов | `Qwen/Qwen3-32B` (свой GPU), `qwen/qwen3.6-35b-a3b` в профиле `config/vsegpt.yaml` |
| `DECKSMITH_VLM_MODEL` | мультимодальная модель для визуального аудита | — (визуальный аудит выключен) |
| `DECKSMITH_IMAGE_PROVIDER` | `none` / `openai_images` | `none` |
| `DECKSMITH_IMAGE_BASE_URL`, `DECKSMITH_IMAGE_API_KEY`, `DECKSMITH_IMAGE_MODEL` | text-to-image | FLUX.1-schnell (свой GPU), `img-flux/flux-2-klein-4b` в профиле `config/vsegpt.yaml` |
| `DECKSMITH_SOFFICE` | путь к LibreOffice | автопоиск |
| `DECKSMITH_WORKSPACE` | каталог кэша профилей, запусков и загрузок (тесты и стресс-тест используют свои) | `workspace` |
| `DECKSMITH_FONT_DOWNLOAD` | `0` — не докачивать открытые шрифты из Google Fonts | `1` |
| `DECKSMITH_CONFIG` | альтернативный YAML настроек | `config/default.yaml` |

Остальные параметры (параллелизм, лимит времени, число слайдов, варианты) — в
[config/default.yaml](config/default.yaml) и [config/variants.yaml](config/variants.yaml).

## Подключение модели

Модели меняются только в `.env` (файл в git не попадает, ключи храните только там), код не
трогается. После правки перезапустите `decksmith serve`; в шапке интерфейса видно, какая модель
подключена («LLM: …» или «offline»).

```bash
# хостинг VseGPT — так проверено на отборочном этапе (открытые веса ≤ 35B, Apache 2.0)
DECKSMITH_CONFIG=config/vsegpt.yaml           # запросы без «размышлений», темп ≤ 1 запроса/с, повторы при 429
DECKSMITH_LLM_BASE_URL=https://api.vsegpt.ru/v1
DECKSMITH_LLM_API_KEY=<ваш ключ>
DECKSMITH_LLM_MODEL=qwen/qwen3.6-35b-a3b      # план и тексты
DECKSMITH_VLM_MODEL=qwen/qwen3.8-27b          # смысловой аудит по картинке слайда
DECKSMITH_IMAGE_PROVIDER=openai_images        # иллюстрации в слайдах (FLUX.2 [klein] 4B, Apache 2.0)
DECKSMITH_IMAGE_BASE_URL=https://api.vsegpt.ru/v1
DECKSMITH_IMAGE_API_KEY=<ваш ключ>

# свой GPU (vLLM): DECKSMITH_LLM_BASE_URL=http://localhost:8001/v1, DECKSMITH_LLM_MODEL=Qwen/Qwen3-32B
# финал (инференс VK): DECKSMITH_LLM_BASE_URL=<endpoint VK>, DECKSMITH_LLM_MODEL и DECKSMITH_VLM_MODEL — Qwen 3.8 27B
# без модели: DECKSMITH_LLM_PROVIDER=offline
```

Замеры моделей, лицензии и ссылки на Hugging Face — в [MODELS.md](MODELS.md).

## Как пользоваться

1. **Шаблоны** — шаблоны датасета разбираются при старте; новый `.pptx` загружается и
   разбирается автоматически (рендер, CV, паттерны, токены). Видна декомпозиция каждого
   примера-слайда: слоты по ролям, тип паттерна и причина классификации.
2. **Генерация** — выбрать шаблон и контент-пакет (PDF/DOCX/PPTX/MD/TXT/CSV/XLSX; кнопка
   «Демо-контент» берёт всё из `data/content/`), назначение (фича/продукт/проект/инициатива), число слайдов,
   написать бриф и нажать **Enter**. Таймер идёт от Enter; прогресс трёх вариантов виден
   по этапам.
3. **Аудит и правки** — слайды с подсветкой проблем, список замечаний (детерминированные /
   контекстуальные), выбор и исправление, скачивание PPTX/PDF/HTML. У каждого слайда показано,
   какой паттерн выбран и почему.

## Структура репозитория

```
decksmith/
  core/        IR (pydantic), конфиг, шрифты
  parsing/     разбор шаблона: OOXML-наследование, элементы, паттерны, токены, CV
  content/     приём контент-пакета, BM25-отбор контекста
  generation/  LLM-клиент, реестр скиллов, планировщик, изображения
  layout/      выбор паттернов, clone&fill, нативная композиция, замер текста, иконки
  render/      LibreOffice → PDF → PNG/SVG
  audit/       проверки, VLM-вопросы, фиксеры
  export/      HTML-экспорт
  api/         FastAPI + SSE
  pipeline.py  оркестрация 3 вариантов
skills/        промпты/конфиги скиллов и агента (версии)
config/        настройки и стратегии вариантов
configs/       конфиги воспроизводимых запусков
frontend/      React + TypeScript (Vite)
tests/         pytest
data/          демо-контент; шаблоны датасета кладутся в data/templates/ (в git не входят)
```

## Ограничения

* Шаблоны и ТЗ из датасета организатора в репозиторий не входят (выданы через ЛК): положите их в
  `data/templates/` — сервис и тесты работают и без них.
* Рендер (превью, PDF, контраст по пикселям) выполняет LibreOffice; фирменные шрифты, которых
  нет в открытом доступе (например, VK Sans), заменяются встроенной в шаблон/Google-альтернативой
  (Play) — `.pptx` при этом ссылается на оригинальные шрифты шаблона. Открытые шрифты из Google
  Fonts докачиваются автоматически (не дольше 15 с на шаблон; без сети — сразу замена).
* LibreOffice рисует окрашенные плейсхолдеры макета даже на слайдах, где их нет (PowerPoint —
  нет). Селектор избегает оставлять такие рамки пустыми, но в PDF/HTML редкие пустые подложки
  возможны; эталон — `.pptx`.
* Настоящий SmartArt (dgm-части) не генерируется: схемы (процесс, таймлайн, карточки) строятся
  нативными фигурами — они редактируются во всех редакторах.
* Таблицы/диаграммы, нарисованные в шаблоне фигурами, не переиспользуются как паттерн — данные
  всегда рисуются нативной диаграммой/таблицей в токенах шаблона.
* Качество текста зависит от модели; офлайн-планировщик — экстрактивный фолбэк без модели.
* Интерфейс рассчитан на десктоп (Chrome/Firefox/Safari/Яндекс Браузер, актуальная и предыдущая
  версии), мобильные устройства не поддерживаются (по ТЗ).
