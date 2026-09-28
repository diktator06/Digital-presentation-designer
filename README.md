# DeckSmith — цифровой дизайнер презентаций

Кейс VK Tech «Цифровой дизайнер презентаций» (Лидеры цифровой трансформации 2026). Сервис по брифу и
контент-пакету собирает презентацию в **любом корпоративном шаблоне**: читает шаблон как набор правил,
верстает **три варианта** и сам проверяет их **аудитом**.

| Вход | Выход (≤ 5 мин после Enter) |
|---|---|
| шаблон `.pptx` `.potx` `.ppt` `.odp` `.otp` | 3 варианта: сбалансированный / визуальный / аналитический |
| контент-пакет: PDF, DOCX, PPTX, MD, TXT, CSV, XLSX | `.pptx` только нативными объектами (текст, фигуры, таблицы, диаграммы с данными), `.pdf`, `.html` |
| бриф, назначение (фича / продукт / проект / инициатива), число слайдов | отчёт аудита: подсветка проблем на слайде, выбор и применение исправлений |

Основной код — [decksmith/](decksmith/), интерфейс — [frontend/src/](frontend/src/), карта репозитория —
в разделе [«Структура репозитория»](#структура-репозитория).

Дополнительно: декомпозиция шаблона (токены, паттерны, слоты), скачивание дизайн-системы шаблона
(токены W3C + CSS), удаление шаблонов, иллюстрации в слайдах (text-to-image).

**Результаты.** 9 презентаций (3 шаблона × 3 варианта, `config/runs/demo.yaml`) — в релизе
[v1.0](https://github.com/diktator06/Digital-presentation-designer/releases/tag/v1.0): 125–220 с на
шаблон, 0 ошибок аудита. Слепой стресс-тест на 31 незнакомом шаблоне: 93 колоды, 0 падений, 0 колод с
ошибками, средний балл аудита 94.4 ([docs/STRESS_RESULTS.md](docs/STRESS_RESULTS.md)).

## Запуск

**1. Данные (по желанию).** Шаблоны и ТЗ датасета в репозиторий не входят. Для демо-конфигов положите их так:

| Файл датасета | Путь в проекте |
|---|---|
| `VK Tech шаблон.pptx` | `data/templates/vk_tech.pptx` |
| `VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx` | `data/templates/vk_workspace.pptx` |
| `Шаблон презентации VK Education.pptx` | `data/templates/vk_education.pptx` |
| `4. VK Tech.pdf` | `data/content/tz_vk_tech.pdf` |

Без них сервис тоже работает: шаблон и контент загружаются в интерфейсе.

**2. Ключ API и модели — файл `.env` в корне проекта.**

```bash
cp .env.example .env      # затем откройте .env в любом редакторе
```

Ключ API вставляется **только в `.env`**, в две строки: `DECKSMITH_LLM_API_KEY=` (план, тексты, аудит
по картинке) и `DECKSMITH_IMAGE_API_KEY=` (иллюстрации). В коде и конфигах ключей нет, `.env` в git не
попадает, Docker читает этот же файл. После правки `.env` перезапустите сервис. Без ключа (поля модели
пустые) сервис работает офлайн: экстрактивный план и аудит по правилам.

*Как делали мы — VseGPT.* Ключ создаётся в личном кабинете [vsegpt.ru](https://vsegpt.ru) (раздел
API-ключей, оплата в рублях); один ключ подходит и для текстов, и для иллюстраций. Ориентир расхода:
генерация трёх вариантов на сайте — около 20 ₽, `config/runs/demo.yaml` (9 колод) — 50–60 ₽.

```ini
DECKSMITH_CONFIG=config/vsegpt.yaml
DECKSMITH_LLM_PROVIDER=openai_compatible
DECKSMITH_LLM_BASE_URL=https://api.vsegpt.ru/v1
DECKSMITH_LLM_API_KEY=ваш_ключ_VseGPT
DECKSMITH_LLM_MODEL=qwen/qwen3.6-35b-a3b
DECKSMITH_VLM_MODEL=qwen/qwen3.8-27b
DECKSMITH_IMAGE_PROVIDER=openai_images
DECKSMITH_IMAGE_BASE_URL=https://api.vsegpt.ru/v1
DECKSMITH_IMAGE_API_KEY=ваш_ключ_VseGPT
DECKSMITH_IMAGE_MODEL=img-flux/flux-2-klein-4b
```

*Другой провайдер.* VseGPT не обязателен: подходит любой OpenAI-совместимый API (`/chat/completions`,
для иллюстраций — `/images/generations`). Меняются только адрес, ключ и имена моделей из каталога
провайдера; по ТЗ — открытые веса до 35B.

| Где модель | `DECKSMITH_CONFIG` | `DECKSMITH_LLM_BASE_URL` | Ключ | Модель, пример |
|---|---|---|---|---|
| VseGPT | `config/vsegpt.yaml` | `https://api.vsegpt.ru/v1` | ключ VseGPT | `qwen/qwen3.6-35b-a3b` |
| OpenRouter и другие агрегаторы | `config/vsegpt.yaml` | `https://openrouter.ai/api/v1` | ключ агрегатора | Qwen3 из каталога агрегатора |
| Свой GPU (vLLM) или инференс VK Cloud | пусто | `http://localhost:8001/v1` (адрес своего сервера) | пусто или ключ сервера | `Qwen/Qwen3-32B` |
| Ollama на ноутбуке | `config/local_ollama.yaml` | задан в профиле | не нужен | `qwen3:4b` |

При запуске в Docker сервер модели на этом же компьютере указывается как `http://host.docker.internal:<порт>/v1`
вместо `localhost`.

Иллюстрации можно брать у другого провайдера (свои `DECKSMITH_IMAGE_BASE_URL` и `DECKSMITH_IMAGE_API_KEY`)
или выключить: `DECKSMITH_IMAGE_PROVIDER=none`. Пустой `DECKSMITH_VLM_MODEL` выключает аудит по
картинке. Быстрая проверка ключа и модели: `decksmith run config/runs/real_model_smoke.yaml` (1 вариант, 8 слайдов,
нужны файлы из шага 1; на VseGPT около 6 ₽) или одна генерация в интерфейсе.

Все переменные:

| Переменная | Что вписать | Пример |
|---|---|---|
| `DECKSMITH_CONFIG` | профиль запросов: для хостингов — `config/vsegpt.yaml`, пусто — vLLM | `config/vsegpt.yaml` |
| `DECKSMITH_LLM_PROVIDER` | `openai_compatible` или `offline` | `openai_compatible` |
| `DECKSMITH_LLM_BASE_URL` | OpenAI-совместимый endpoint модели | `https://api.vsegpt.ru/v1` |
| `DECKSMITH_LLM_API_KEY` | ключ endpoint | ключ VseGPT |
| `DECKSMITH_LLM_MODEL` | модель для плана и текстов (открытые веса ≤ 35B) | `qwen/qwen3.6-35b-a3b` |
| `DECKSMITH_VLM_MODEL` | мультимодальная модель для аудита по картинке; пусто — выключен | `qwen/qwen3.8-27b` |
| `DECKSMITH_IMAGE_PROVIDER` | `openai_images` — иллюстрации в слайдах, `none` — выключены | `openai_images` |
| `DECKSMITH_IMAGE_BASE_URL` | endpoint `/images/generations` | `https://api.vsegpt.ru/v1` |
| `DECKSMITH_IMAGE_API_KEY` | ключ этого endpoint | тот же ключ |
| `DECKSMITH_IMAGE_MODEL` | модель text-to-image | `img-flux/flux-2-klein-4b` |
| `DECKSMITH_SOFFICE` | путь к LibreOffice, если не находится сам | `/usr/bin/soffice` |
| `DECKSMITH_WORKSPACE` | каталог кэша, запусков и загрузок | `workspace` |
| `DECKSMITH_FONT_DOWNLOAD` | `0` — не докачивать открытые шрифты из Google Fonts | `1` |

Те же блоки с комментариями — в [.env.example](.env.example).

**3а. Docker.**

```bash
docker compose up --build        # http://localhost:8000 (.env из шага 2)
```

**3б. Локально.** Python 3.11+, Node 20+, LibreOffice (`soffice` в PATH или `DECKSMITH_SOFFICE`).

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: py -3 -m venv .venv; .venv\Scripts\activate
pip install -e ".[dev]"
(cd frontend && npm ci && npm run build)
decksmith serve                                         # http://localhost:8000
```

## Воспроизводимые запуски (конфиг-файлом)

| Команда | Что делает |
|---|---|
| `decksmith run config/runs/demo.yaml` | 3 шаблона × 3 варианта на одном контенте, с моделью из `.env` |
| `decksmith run config/runs/demo_offline.yaml` | то же без модели (для проверки без ключа) |
| `decksmith run config/runs/real_model_smoke.yaml` | быстрая проверка модели: 1 вариант, 8 слайдов |
| `decksmith run config/runs/real_model_check.yaml` | локальная открытая модель через Ollama (`config/local_ollama.yaml`) |
| `decksmith run config/runs/pitch.yaml` | питч проекта, собранный самим DeckSmith на шаблоне ЛЦТ-2026 |

Результаты — в `workspace/runs/<id>/`: колоды PPTX/PDF/HTML, `plan.json`, отчёты аудита, `manifest.json`
(версии промптов с sha256, модели, время этапов, обоснование макета каждого слайда). В конфиге можно
задать готовый план (`plan: путь.json`) — шаг планирования пропускается.

## Интерфейс

* **Генерация** — шаблон, контент-пакет (по умолчанию — только бриф; свои файлы или «Демо-контент»),
  назначение, число слайдов, бриф, Enter. Материалы не по теме брифа на слайды не попадают. Прогресс
  трёх вариантов по этапам, таймер от Enter.
* **Просмотр варианта** — слайды с подсветкой проблем, замечания (детерминированные / контекстные),
  выбор и применение исправлений, скачивание PPTX/PDF/HTML, причина выбора макета под каждым слайдом.
* **Шаблоны** — загрузка, декомпозиция слайдов-примеров, «Скачать дизайн-систему», «Удалить».
* **Скиллы и аудит** — версии промптов и агента, каталог проверок.

## Прочие команды

```bash
decksmith analyze шаблон.pptx --sheet            # профиль шаблона + картинка декомпозиции
decksmith audit колода.pptx --template шаблон.pptx
decksmith skills                                   # версии скиллов/агентов и каталог проверок
pytest -q                                          # 65 тестов (путь с моделью — на эмуляторе, без сети)
python scripts/stress.py <шаблоны...> --synthetic  # слепой стресс-тест
```

## Как устроено

Разбор шаблона (OOXML + рендер LibreOffice + CV) → план и тексты (LLM, 2 этапа, параллельно) →
выбор паттернов и вёрстка 3 вариантов → рендер → аудит (29 правил + 13 вопросов к картинке слайда) →
безопасные автоисправления → экспорт. Промпты и агент — версионируемые YAML в [skills/](skills/)
(подхватываются без перезапуска сервиса).

| Роль | Модель | Лицензия |
|---|---|---|
| план и тексты | Qwen3.6-35B-A3B (хостинг), Qwen3-32B (свой GPU) | Apache 2.0 |
| аудит по картинке слайда | Qwen3.8-27B (модель финала) | Apache 2.0 |
| иллюстрации | FLUX.2 [klein] 4B (хостинг), FLUX.1 [schnell] (свой GPU) | Apache 2.0 |

Подробно: [ARCHITECTURE.md](ARCHITECTURE.md) — пайплайн и слои · [MODELS.md](MODELS.md) — модели,
требования, ссылки на Hugging Face · [AUDIT.md](AUDIT.md) — проверки и тесты ·
[docs/UNIVERSALITY.md](docs/UNIVERSALITY.md) — работа на незнакомых шаблонах.

## Структура репозитория

Основной код — **[decksmith/](decksmith/)** (Python, ≈ 11 тыс. строк), интерфейс — [frontend/src/](frontend/src/).

| Папка | Что внутри |
|---|---|
| [decksmith/](decksmith/) | **сервис**: разбор шаблона, генерация, вёрстка, рендер, аудит, экспорт, API (модули ниже) |
| [frontend/](frontend/) | интерфейс: React + TypeScript (Vite); код — в `frontend/src/` |
| [skills/](skills/) | промпты скиллов и воркфлоу агента со всеми версиями; активные — в `skills/registry.yaml` |
| [config/](config/) | профили моделей (`default` — vLLM, `vsegpt` — хостинги, `local_ollama`), варианты вёрстки; `config/runs/` — конфиги воспроизводимых запусков |
| [data/](data/) | куда класть шаблоны и ТЗ датасета; `sample_brief.md` — демо-контент |
| [tests/](tests/) | тесты pytest; [scripts/](scripts/) — слепой стресс-тест и отчёт по нему |
| [docs/](docs/) | универсальность на незнакомых шаблонах, результаты стресс-теста, план питча |
| корень | документация по ТЗ: README, [ARCHITECTURE](ARCHITECTURE.md), [MODELS](MODELS.md), [AUDIT](AUDIT.md); `Dockerfile`, `docker-compose.yml`, `pyproject.toml` (зависимости Python), `.env.example` |

`workspace/` (кэш разбора шаблонов, запуски, загрузки) создаётся при работе и в git не входит.

| Модуль `decksmith/` | Что делает |
|---|---|
| `pipeline.py` | конвейер после Enter: план → 3 варианта параллельно → рендер → аудит → автоисправления → экспорт |
| `parsing/` | разбор шаблона: OOXML с наследованием стилей, токены дизайна, паттерны и слоты, CV по рендеру |
| `content/ingest.py` | контент-пакет: PDF, DOCX, PPTX, MD, TXT, CSV, XLSX → фрагменты, таблицы, числа |
| `generation/` | работа с моделью: клиент API, скиллы, план и тексты слайдов, иллюстрации |
| `layout/` | выбор макета для слайда и вёрстка: клонирование примеров, нативные композиции, подгонка текста, иконки |
| `render/soffice.py` | рендер PPTX → PDF и PNG через LibreOffice |
| `audit/` | 29 детерминированных правил, вопросы к VLM по картинке слайда, исправления |
| `export/` | HTML-версия колоды и дизайн-система шаблона |
| `api/app.py` | FastAPI: REST и SSE для интерфейса |
| `cli.py` | команды `decksmith serve`, `run`, `analyze`, `audit`, `skills` |
| `core/` | модели данных, настройки, шрифты |
| `testing/` | эмулятор модели и синтетические шаблоны для тестов и стресс-теста |
| `assets/` | иконки Lucide (лицензия ISC) одним файлом |

## Ограничения

* Рендер PDF/HTML — LibreOffice; фирменные шрифты, которых нет в открытом доступе, в превью заменяются
  метрически близкими, `.pptx` ссылается на оригинальные шрифты шаблона.
* SmartArt не генерируется: процессы, таймлайны и карточки собираются нативными фигурами.
* Качество текста зависит от модели; офлайн-режим — экстрактивный запасной вариант.
* Интерфейс — для десктопа (Chrome, Firefox, Safari, Яндекс Браузер), мобильные не поддерживаются (по ТЗ).
