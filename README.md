# Praxis

Praxis — один серверный агент с долговечной файловой памятью и несколькими исполнительными
органами. Модель, личность, память, решения, Telegram-контур и единственный Forge живут на
сервере. `praxis-serverd` и Windows Body исполняют команды рядом с нужной машиной, но не содержат
LLM, собственного task store или второго «я».

Этот README описывает вход в **текущее дерево репозитория**. Что именно развёрнуто сейчас, какой
commit прошёл gate и как откатываться, фиксирует только [`STATUS.md`](STATUS.md).

## Где что искать

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — устойчивые контракты, владение состоянием и поведение при
  сбоях;
- [`CODEMAP.md`](CODEMAP.md) — файлы и их фактические роли;
- [`STATUS.md`](STATUS.md) — единственная оперативная правда о live/candidate, проверках и rollback;
- [`VISION.md`](VISION.md) — направление продукта, без журнала релизов;
- [`AGENTS.md`](AGENTS.md) — опасные операции и правила работы с этим репозиторием;
- [`body/README.md`](body/README.md) — сборка, установка и диагностика Windows Body.

## Главные границы

- `agent.py` принимает решения и исполняет модельный tool loop; `mtproto_runner.py` держит живой
  Telethon client, маршрутизацию чатов и серверные часы.
- Каждый существенный ход получает неизменяемый `RunContext` и каталог
  `memory/runs/YYYY-MM/<run_id>/` с контекстом, WAL-событиями, полными результатами, артефактами и
  итоговым `RECAP.md`.
- Каноническая память — Markdown, append-only JSONL и проверяемые snapshots. SQLite FTS и карты —
  перестраиваемые индексы, а не память и не task store.
- Persona читает только полностью provenance-valid `soul/self/CURRENT.md`; missing/corrupt CURRENT
  fail-closed и никогда не подменяется legacy `soul/self.md` или history.
- Форумная группа имеет один root-профиль доступа, но отдельную историю и reply route для каждого
  topic. Сырые сообщения архивируются с exact peer/topic/message provenance.
- Прямые Telegram-отправки и расписание сначала записывают durable intent, затем касаются сети;
  повтор использует тот же MTProto `random_id`.
- Входящие для владельца имеют append-only ledger `owner_delivery.py`; действующая проекция —
  Telegram. Непрочитанное и прочитанное, но ещё не обработанное не исчезает из-за мягкого history
  budget интерфейса.
- Один Forge хранит инженерные задачи в `memory/.forge/`. Серверный root-broker и Windows Body
  получают только typed execution envelope и возвращают evidence.
- Praxis herself is the sovereign actor `praxis:self`: ей доступны документы, код, server/root,
  Windows interactive/SYSTEM и noncritical/raw/join/leave Telegram-операции. Проверки, receipts,
  provenance и rollback помогают ей действовать точнее, но не являются скрытым veto. Основной
  модельный tool loop не имеет потолка; bounded limit существует только у явно вспомогательных
  проходов.
- Только human owner через публичный tool/API выдаёт и отзывает доступ другим людям. Trusted human
  не может передоверить права, вызвать raw Telegram account API или SYSTEM. При наличии у Praxis
  полного shell/root это governance-инвариант системы, а не обещание криптографической sandbox-
  изоляции от неё самой.
- Account-critical Telegram-вызов может инициировать Praxis или owner, но исполняется лишь после
  exact-подтверждения новым сообщением owner DM. `requested_by` и `confirmed_by` остаются разными
  полями; параметры живут до TTL в зашифрованном spool, а JSONL и durable run evidence хранят только
  redacted schema marker, digest и opaque reference.
- Windows side effects требуют стабильный idempotency key. Детерминированные `request_id` и
  `operation_id` сохраняются при ручном retry того же намерения.

## Первый запуск для разработки

Нужны Docker Compose и Telegram MTProto credentials. Секреты остаются в `.env` и runtime-файлах,
они не коммитятся.

```bash
cp .env.example .env
# заполнить TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE и PRAXIS_OWNER_ID
docker compose run --rm praxis python mtproto_login.py send
# после получения кода:
docker compose run --rm praxis python mtproto_login.py code 12345
docker compose up -d --build
docker compose logs -f praxis
```

Модельные роли и лимиты живут в runtime-файле `memory/llm.json`; `.env.example` документирует
остальные настройки. Production compose, дополнительные процессы и host mounts находятся в
`docker-compose.deploy.yml`. `praxis-serverd` и `praxis-body-bridge` устанавливаются как отдельные
systemd units, а не как второй агент внутри Compose.

### Конфигурация Codex relay

Codex здесь не отдельный «авторизационный файл Praxis», а OpenAI-compatible relay для
`frameworks.openai` в приватном runtime-конфиге `memory/llm.json`. Нормальный путь обновления
`base_url` и ключа — owner-панель: она показывает ключ только маской, требует подтверждение при
применении и затем делает атомарную запись с правами `0600`. Пустое поле ключа означает
«оставить текущий», а не стереть его; проверка роли после правки доступна тем же пультом. Все
процессы подхватывают изменение по mtime, поэтому для обычной смены relay/token рестарт не нужен.

На самом первом старте, пока `memory/llm.json` отсутствует, код один раз переносит
`OPENAI_BASE_URL` и `OPENAI_API_KEY` из окружения в этот файл. Это bootstrap-миграция, не
постоянный источник конфигурации. В production-контуре контейнеры ходят к локальному relay через
host Caddy; ожидаемый URL — `http://host.docker.internal:5012`. Compose описывает только этот
маршрут: он не создаёт relay/Caddy и в данном репозитории нет скрипта логина/refresh для внешней
Codex-авторизации. Их следует документировать рядом с тем host-сервисом, который реально владеет
учётной записью и refresh-token, а не выдумывать в репозитории Praxis.

### Markdown и SQL при первом развёртывании

Человекочитаемый Markdown и append-only JSONL — канонические данные Praxis: корневые living docs
описывают систему, `soul/` хранит конституцию/голос/навыки, а `memory/` — наблюдения, отношения,
run-evidence и навигацию. `memory/INDEX.md`, `memory/maps/*.md` и отдельные `CURRENT.md` —
генерируемые представления, а не второй источник истины; роли каталогизированы в
`ARCHITECTURE.md`, `CODEMAP.md` и `memory/README.md`.

Отдельной SQL-схемы, миграции или create-DB скрипта для развёртывания нет — и это намеренно.
SQLite используется как локальный, пересобираемый ускоритель (`memory/.state/recall.sqlite3` для
FTS и индекс наблюдений Windows): таблицы создаются приложением при первом доступе, а FTS умеет
собрать временную БД, проверить её и атомарно заменить рабочую. Docker Compose и Dockerfile не
создают внешнюю БД. Не удаляйте всю `memory/.state/` как «кэш»: рядом с производными SQLite там
живут outbox и durable ledgers, которые должны попадать в backup.

`mailroom_bot.py` читает секрет `PRAXIS_MAIL_BOT_TOKEN` из private `.deploy.env` и работает
headless: поллит IMAP, обновляет `memory/mailbox.json`, присылает новые письма и карточки изменений.
Он не открывает HTTP-порт и не публикует Mini App/PWA.

Полезные обслуживающие команды:

```bash
docker compose exec praxis python -m memory_index build
docker compose exec praxis python consolidate.py --force
docker compose exec praxis python praxis_test.py discover -q
```

Полный Python gate, Forge/process tests и проверки с дочерними процессами запускаются только внутри
Linux Docker/WSL или на Linux-сервере. Нативный Windows inline runner для них запрещён; точные
ограничения — в [`AGENTS.md`](AGENTS.md). Rust-команды для Windows Body приведены отдельно в
[`body/README.md`](body/README.md).

## Данные и восстановление

Private live Git может версионировать soul/self provenance и выбранную человеческую память, но
публичный GitHub получает только sanitized source snapshot: без live self/rails, диалогов, людей,
комнат, целей, receipts и runtime state. Runtime-ledgers, Telegram session, outbox, run evidence,
Forge state и device spool должны сохраняться операционно, но не публиковаться.
Приватные `memory/dialogues/*`, `memory/access/TRUST.md` и `memory/goals.md` сохранены как runtime-
данные, но больше не отслеживаются source Git. Удалённые backup/probe/roomgate и старые
freeze/архивные personality файлы доступны через историю Git и не должны возвращаться в рабочее
дерево.
Нельзя считать весь `memory/.state/` кэшем: удаляемы только явно производные SQLite/карты; журналы
intent/acceptance и control state несут доказательства незавершённых эффектов.
В backup вместе должны попадать `memory/.state/owner_delivery/`,
`memory/access/devices/events.jsonl` и соответствующий ему
`memory/.state/praxis_device_auth.key`: ledger без своего HMAC-ключа считается повреждённым, а не
поводом молча выпустить новую authority.

При расследовании сначала смотрят live-код, `memory/runs/*`, outbox/ledger и реальные логи, а уже
потом производные карты. Неизвестный результат side effect не объявляется ни успехом, ни безопасным
повтором: run переходит в `in_doubt` до сверки evidence.

## Провенанс документации

В корне поддерживаются только шесть living-документов: `README.md`, `ARCHITECTURE.md`, `CODEMAP.md`,
`STATUS.md`, `VISION.md` и `AGENTS.md`. Subsystem-документы вроде `body/README.md` не меняют это
правило. Living docs описывают текущие контракты и не хранят дневник проходов разработки;
исторические спецификации и причины решений остаются адресуемыми через Git:

```bash
git log --all -- '*.md'
git show <commit>:<path>
```

Новая реализация сначала получает код, тесты и evidence, затем обновляет `ARCHITECTURE.md`,
`CODEMAP.md` и `STATUS.md` в соответствии с их ролями.
