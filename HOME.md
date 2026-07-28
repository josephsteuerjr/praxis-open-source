# HOME — карта моего дома

> Это навигация, не новый источник истины. Канон остаётся в самих файлах.

## Если я ищу…

| Что | Куда идти |
|---|---|
| Кто я, как говорю, мои правила | `/app/soul/` |
| Навык или памятку «как делать» | `/app/soul/skills/` |
| Человека, комнату, эпизод, желание, прошлый run | `/app/memory/` → начать с `INDEX.md` |
| Живой код Praxis | `/app/*.py`, `/app/body/`, `/app/hands/`, `/app/serverd/` |
| Входящий Telegram-файл | `/app/workspace/inbox/` |
| Файл, который готовлю к отправке | `/app/workspace/outbox/` |
| Черновик, распаковку, временный результат | `/app/workspace/` |
| Самостоятельный проект | `/app/workspace/projects/<slug>/` |
| Текущую рабочую копию Forge | спросить `coding_session` / `coding_inspect`, не угадывать путь |
| Файлы сервера или Windows | открыть Forge с `scope='host'` или `scope='windows'` |

## Мнемоника

**soul — кто я; memory — что помню; root — как работаю; workspace — что мастерю; Forge — где сейчас чиню.**

## Когда известно хотя бы слово из имени

Не блуждать по каталогам: использовать `fs_search`; для Telegram-входящих — `inbox_list` / `inbox_read`; для кода — `code_map` или Forge symbols/references.

## Осторожно

`.env`, `.deploy.env` и `*.session` — секреты и сессии. Они рабочие, но их содержимое нельзя выносить наружу.
