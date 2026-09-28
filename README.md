# DeckSmith — цифровой дизайнер презентаций

Кейс VK Tech «Цифровой дизайнер презентаций» (Лидеры цифровой трансформации 2026). Сервис по брифу и
контент-пакету собирает презентацию в **любом корпоративном шаблоне**: читает шаблон как набор правил,
верстает **три варианта** и сам проверяет их **аудитом**.

| Вход | Выход (≤ 5 мин после Enter) |
|---|---|
| шаблон `.pptx` `.potx` `.ppt` `.odp` `.otp` | 3 варианта: сбалансированный / визуальный / аналитический |
| контент-пакет: PDF, DOCX, PPTX, MD, TXT, CSV, XLSX | `.pptx` только нативными объектами (текст, фигуры, таблицы, диаграммы с данными), `.pdf`, `.html` |
| бриф, назначение (фича / продукт / проект / инициатива), число слайдов | отчёт аудита: подсветка проблем на слайде, выбор и применение исправлений |

Дополнительно: декомпозиция шаблона (токены, паттерны, слоты), скачивание дизайн-системы шаблона
(токены W3C + CSS), удаление шаблонов, иллюстрации в слайдах (text-to-image).

**Результаты.** 9 презентаций (3 шаблона × 3 варианта, `configs/demo.yaml`) — в релизе
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

**2. Файл `.env`:** `cp .env.example .env` и заполнить переменные ниже. Если поля модели пустые,
сервис работает офлайн (экстрактивный план, аудит по правилам).

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

Готовые блоки для VseGPT, своего GPU (vLLM, инференс VK) и Ollama — в [.env.example](.env.example).

**3а. Docker.**

```bash
cp .env.example .env
docker compose up --build        # http://localhost:8000
```

**3б. Локально.** Python 3.11+, Node 20+, LibreOffice (`soffice` в PATH или `DECKSMITH_SOFFICE`).

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: py -3 -m venv .venv; .venv\Scripts\activate
pip install -e ".[dev]"
(cd frontend && npm ci && npm run build)
cp .env.example .env
decksmith serve                                         # http://localhost:8000
```

## Воспроизводимые запуски (конфиг-файлом)

| Команда | Что делает |
|---|---|
| `decksmith run configs/demo.yaml` | 3 шаблона × 3 варианта на одном контенте, с моделью из `.env` |
| `decksmith run configs/demo_offline.yaml` | то же без модели (для проверки без ключа) |
| `decksmith run configs/real_model_smoke.yaml` | быстрая проверка модели: 1 вариант, 8 слайдов |
| `decksmith run configs/real_model_check.yaml` | локальная открытая модель через Ollama (`config/local_ollama.yaml`) |
| `decksmith run configs/pitch.yaml` | питч проекта, собранный самим DeckSmith на шаблоне ЛЦТ-2026 |

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

```
decksmith/   parsing · generation · layout · render · audit · export · api · pipeline.py
skills/      промпты скиллов и конфиг агента (версии)
config/      настройки и профили моделей; configs/ — конфиги запусков
frontend/    React + TypeScript (Vite)
tests/       pytest · scripts/ — стресс-тест · docs/ — результаты и питч
```

## Ограничения

* Рендер PDF/HTML — LibreOffice; фирменные шрифты, которых нет в открытом доступе, в превью заменяются
  метрически близкими, `.pptx` ссылается на оригинальные шрифты шаблона.
* SmartArt не генерируется: процессы, таймлайны и карточки собираются нативными фигурами.
* Качество текста зависит от модели; офлайн-режим — экстрактивный запасной вариант.
* Интерфейс — для десктопа (Chrome, Firefox, Safari, Яндекс Браузер), мобильные не поддерживаются (по ТЗ).
