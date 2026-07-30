# Деплой Praxis

Три пути: локально одной командой, на сервере с systemd, или production-контур
с body bridge и serverd. База данных — везде SQLite, инициализируется кодом,
отдельных SQL-скриптов нет.

---

## Быстрый старт (локально)

См. [`BUNDLE.md`](BUNDLE.md) — полное руководство. Кратко:

```bash
cp env.bundle.example .env        # заполни TELEGRAM_*, PRAXIS_OWNER_ID, GLM_API_KEY
docker compose -f docker-compose.bundle.yml up -d --build
docker compose -f docker-compose.bundle.yml exec praxis python mtproto_login.py send
docker compose -f docker-compose.bundle.yml exec praxis python mtproto_login.py code 12345
```

Это поднимает только ядро. Relay и Ollama — опциональные profiles (см. BUNDLE.md).

---

## На сервере (systemd + Docker)

### 1. Клонировать

```bash
git clone https://github.com/josephsteuerjr/praxis-open-source.git /opt/praxis
cd /opt/praxis
```

### 2. .env

```bash
cp env.bundle.example .env
# Заполни: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE,
#          PRAXIS_OWNER_ID, GLM_API_KEY
```

### 3. Docker Compose

Используй тот же `docker-compose.bundle.yml` — он работает и на сервере.
`host.docker.internal` резолвится через `extra_hosts`, так что доступ к localhost
хоста есть. Состояние живёт в каталоге репозитория (`memory/`, `soul/`, `workspace/`,
`praxis.session`), а не в анонимном томе.

### 4. Обновление

```bash
cd /opt/praxis
git pull
docker compose -f docker-compose.bundle.yml up -d --build
```

Каталог с состоянием при `git pull` не трогается (всё в `.gitignore`).

---

## База данных

Praxis не использует внешнюю БД. Всё работает на **SQLite**:

- **Каноническая память** — Markdown, append-only JSONL и проверяемые snapshots в
  `memory/`. SQLite здесь — только перестраиваемый full-text индекс.
- **FTS** (`memory_fts.py`) — `recall.sqlite3` в `memory/.state/`. Пересобирается
  из канонических источников по команде или автоматически. Удаляема без потери
  данных: следующий rebuild поднимет её заново.
- **Observations** (`computer_memory.py`) — `CREATE TABLE IF NOT EXISTS` при
  первом доступе. Индексы создаются тем же вызовом.

Никакого `init-db.sql`, отдельной миграции или `create-database` скрипта нет — и
это намеренно. Схема живёт в коде (`CREATE TABLE IF NOT EXISTS`), и первый запуск
создаёт всё автоматически.

---

## praxis-serverd (опционально: root-руки на хосте)

Дает Praxis типизированный доступ к systemd, docker, пакетам и файлам прямо
на хосте. Без него она живёт только внутри контейнера.

```bash
cd serverd/
sudo bash install.sh
```

`install.sh` идемпотентен: создаёт группу `praxis`, ставит демон в
`/opt/praxis-serverd`, генерирует токен, регистрирует systemd unit.

После установки добавь bind-mount в compose:

```yaml
volumes:
  - /opt/praxis-serverd/run:/run/praxis-serverd:rw
```

И пересобери контейнер.

---

## Windows Body (опционально: руки на ПК)

Дает Praxis доступ к Windows-компьютеру: файлы, PowerShell, интерактивный рабочий
стол. Собирается из `body/` отдельным Rust-бинарём.

См. [`body/README.md`](body/README.md) — сборка, установка и диагностика.

Body bridge на сервере разворачивается через `body/deploy/`:

- `body/deploy/praxis-body-bridge.service` — systemd unit для bridge-демона
- `body/deploy/Caddyfile.body.example` — пример reverse proxy (Caddy)

---

## Production-заметки

Живой production-контур использует:

- Docker Compose с тяжёлыми моделями (Whisper, Piper TTS) — не входит в
  публичный репозиторий
- Relay (MIT, каталог `relay/`) — OpenAI-совместимый прокси к подписке
- praxis-serverd — root-демон для типизированных операций на хосте
- praxis-body-bridge — bridge к Windows Body
- Почтовый бот (`mailroom_bot.py`) и server-app (`praxis_app.py`) — отдельные
  контейнеры из того же образа

Всё это опционально. Минимальный запуск — один контейнер из `docker-compose.bundle.yml`.
