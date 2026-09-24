# Шаблоны

Шаблоны датасета хакатона (VK Tech, VK WorkSpace, VK Education) распространяются организатором
через личный кабинет участника и не публикуются в репозитории. Положите их сюда:

```
data/templates/vk_tech.pptx
data/templates/vk_workspace.pptx
data/templates/vk_education.pptx
```

Сервис не зависит от этих файлов: любой шаблон (.pptx, .potx, .ppt, .odp, .otp) загружается через
UI или `decksmith analyze <file>`. Для слепой проверки без датасета см. `scripts/stress.py --synthetic`.
