# Шаблоны

Шаблоны датасета хакатона (VK Tech, VK WorkSpace, VK Education) распространяются организатором
через личный кабинет участника и не публикуются в репозитории. Положите их сюда под такими именами
(их ждут `config/runs/demo*.yaml` и тесты):

| Файл в датасете | Положить как |
|---|---|
| `VK Tech шаблон.pptx` | `data/templates/vk_tech.pptx` |
| `VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx` | `data/templates/vk_workspace.pptx` |
| `Шаблон презентации VK Education.pptx` | `data/templates/vk_education.pptx` |

Сервис не зависит от этих файлов: любой шаблон (.pptx, .potx, .ppt, .odp, .otp) загружается через
UI или `decksmith analyze <file>`. Для слепой проверки без датасета см. `scripts/stress.py --synthetic`.
