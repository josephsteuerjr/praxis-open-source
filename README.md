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
- Входящие для владельца имеют общий append-only ledger `owner_delivery.py`, который одинаково
  проецируется в Telegram и Praxis App. Непрочитанное и прочитанное, но ещё не обработанное не
  исчезает из-за мягкого history budget интерфейса.
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
- `/app` — устанавливаемая owner PWA над тем же server runtime, а не второй агент. Telegram
  авторизуется подписанным initData, отдельное устройство — owner-issued одноразовой ссылкой и
  device-scoped bearer. Service worker хранит только shell; последний проверенный snapshot и
  черновики разделены по principal/device, а offline-мутации никогда не воспроизводятся сами.
- Командные мутации Praxis App и Windows side effects требуют стабильный idempotency key.
  Серверная операция получает durable claim/receipt, а Windows side effect — детерминированные
  `request_id` и `operation_id`, которые сохраняются при ручном retry того же намерения. Прямое
  изменение access имеет идемпотентное effective state, но повтор после потерянного ответа может
  добавить второй audit event. Enrollment-секрет намеренно никогда не сохраняется и не может быть
  воспроизведён: потерянный ответ оставляет только истекающую orphan-ссылку, после чего owner может
  выпустить новую.

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

`mailroom_bot.py` читает секрет `PRAXIS_MAIL_BOT_TOKEN` из private `.deploy.env`. Публичный
`PRAXIS_APP_URL` и стандартный PWA file-transfer cap настраиваются в `.env`; по умолчанию браузерный
import/export ограничен 64 MiB в каждую сторону и всё равно проверяет имя, размер и SHA-256.

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
