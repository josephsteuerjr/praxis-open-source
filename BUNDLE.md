# Запуск Praxis — бандл

Один compose-файл, один `.env`, две команды на вход в Telegram. Работает одинаково в
Docker Desktop (Windows/macOS) и на сервере: `network_mode: host` не используется
нигде, все адреса — по именам сервисов.

Нужно: Docker с плагином compose (Desktop 4.x или `docker-compose-plugin` на сервере),
около 3 ГБ на образ, аккаунт Telegram и ключ модели.

## 1. Настроить

```bash
cp env.bundle.example .env
```

Заполни в `.env`:

| Переменная | Где взять |
|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | https://my.telegram.org → API development tools |
| `TELEGRAM_PHONE` | номер аккаунта, от которого она будет говорить |
| `TELEGRAM_2FA` | облачный пароль, если он включён |
| `PRAXIS_OWNER_ID` | твой numeric id, спроси у `@userinfobot` |
| `GLM_API_KEY` | ключ провайдера (по умолчанию — совместимый с Anthropic эндпоинт z.ai) |

Ключ читается один раз при первом старте и переносится в `memory/llm.json`. Дальше
модель и роли меняются в её пульте, а не в `.env`.

## 2. Поднять

```bash
docker compose -f docker-compose.bundle.yml up -d --build
```

Первая сборка небыстрая: внутри собирается Rust-бинарь её файловых рук.

## 3. Войти в Telegram (один раз)

Вход двушаговый и неинтерактивный — код приходит в Telegram на тот же аккаунт:

```bash
docker compose -f docker-compose.bundle.yml exec praxis python mtproto_login.py send
```

```bash
docker compose -f docker-compose.bundle.yml exec praxis python mtproto_login.py code 12345
```

Файл сессии останется в каталоге репозитория, так что второй раз это не понадобится.
Дальше:

```bash
docker compose -f docker-compose.bundle.yml logs -f praxis
```

В логе должна появиться строка вида `Praxis на связи как @… мозг: голос=…`. После неё
напиши ей в личку.

## Необязательное

**Relay** (каталог `relay/`, лицензия MIT) — OpenAI-совместимый прокси к **твоей
собственной** подписке ChatGPT/Codex. Нужен только если хочешь ходить через неё вместо
ключа провайдера; проверь, что это не противоречит условиям твоей подписки.

```bash
docker compose -f docker-compose.bundle.yml --profile relay up -d --build
docker compose -f docker-compose.bundle.yml --profile relay run --rm -it relay relay   # вход, один раз
```

Затем в `.env` раскомментируй `OPENAI_BASE_URL=http://relay:5012` и `OPENAI_API_KEY=auto`.
После перезапуска открой пульт **«Мозг»** и для нужных ролей выбери framework
`openai` и подходящую модель: одних переменных `OPENAI_*` недостаточно — первый
старт по умолчанию создаёт роли на GLM/Anthropic. Бинарь relay слушает жёстко
`127.0.0.1:5011`, поэтому рядом с ним поднимается крошечный форвардер `relay-gw`
в той же сетевой неймспейс — он и отдаёт петлю наружу как `relay:5012`. Сам снапшот
relay при этом не правится.

**Ollama** — только при `PRAXIS_EMBEDDINGS=1`. По умолчанию память работает на
full-text и внешнего сервиса не требует.

```bash
docker compose -f docker-compose.bundle.yml --profile ollama up -d
docker compose -f docker-compose.bundle.yml exec ollama ollama pull nomic-embed-text
```

## Что где лежит

Состояние — в каталоге репозитория, а не в анонимном томе: `memory/` (память, дневник,
люди), `soul/` (её самоописание), `workspace/` (рабочие файлы и inbox), `praxis.session`
(вход в Telegram). Удалить контейнер безопасно; удалить каталог — значит стереть её.

Обновление: `git pull && docker compose -f docker-compose.bundle.yml up -d --build`.
Каталог с состоянием при этом не трогается.

## Если не поднялось

| Симптом | Причина |
|---|---|
| падает с жалобой на `PRAXIS_TEST` | убери эту переменную: под ней база уходит в одноразовый каталог и она встаёт без памяти |
| в логе нет строки «на связи» | вход в Telegram не сделан — шаг 3 |
| молчит в группе, отвечает в личке | в группе она держит присутствие, а не рефлекс: `PRAXIS_COOLDOWN_GROUP` и режим комнаты — её рычаги |
| relay отвечает 404 на `/v1/models` | у него чат по `/chat/completions` **без** `/v1`, а модели по `/v1/models` — это его собственный квирк |
