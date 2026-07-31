# CODEMAP

Карта фактических файлов текущего дерева. Здесь нет live-статуса, дорожной карты и истории
проходов: deployment truth находится в [`STATUS.md`](STATUS.md), устойчивые связи — в
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Ядро и Telegram runtime

| Файл | Роль |
|---|---|
| `agent.py` | Persona/context assembly, модельный tool loop, tool registry, outbound guard, durable run/delivery integration. |
| `mtproto_runner.py` | Живой Telethon client: update routing, buffers, forum topics, media, delivery/outbox replay, membership/account hooks и единые серверные часы. |
| `llm.py` | Конфигурация ролей моделей, Anthropic/OpenAI protocol adapters, fallback, usage и hosted tools. |
| `bootguard.py` | Preflight, supervision, healthy-commit marker и rollback раннего сломанного self-edit. |
| `reflex.py` | Детерминированный pre-model фильтр очевидного шума. |
| `perception.py` | Живые настройки debounce/cooldown/wake и журнал причин, почему голос не был вызван. |
| `turns.py` | Scope-aware журнал прожитых ходов и исходов отправки/удержания. |
| `bufstore.py`, `notes.py` | Персистентный conversation buffer и короткие заметки о недавнем сказанном. |
| `identity.py` | Оркестрация self-authorship: активные load events, night revision, version/provenance и post-change events; legacy deformation scores остаются инертным архивом. |
| `capabilities.py`, `rails.py` | Фактический capability snapshot и provenance/risk registry; его оценки не являются veto для sovereign self. |
| `tool_offerings.py` | Детерминированное формирование полного набора tools для owner, Praxis self и scoped trusted humans. |
| `brain.py`, `appetite.py` | Наблюдаемый выбор model role и учёт/интерпретация вычислительного аппетита без скрытого veto. |
| `media.py` | Typed inbound/outbound media, guarded spool, durable media receipts и cleanup. |
| `media_audio.py` | Local STT и TTS backends с атомарными аудио-артефактами. |
| `stt_rpc.py` | Authenticated HTTP-over-UDS адаптер для bounded hardbot STT; переиспользует resident Whisper из `mtproto_runner.py`, удаляет private temp audio и не персистит transcript. |

## Durable runs и Telegram side effects

| Файл | Роль |
|---|---|
| `run_context.py` | Immutable `RunContext` и `ContextVar`-binding authority/address для одного исполнения. |
| `run_manager.py` | `memory/runs/`: manifest, append-only WAL, ResultRef/artifact storage, control, recovery, RECAP и promotion. |
| `run_resume.py` | Read-only reducer WAL/manifest/checkpoint evidence в строгий resume plan с cumulative event/byte/time budgets, не являющимися tool cap. |
| `run_executor.py` | Исполнитель resume plan с exact revision/event-seq lease и injected callbacks. |
| `process_liveness.py` | Безопасная платформенная проверка PID; на Windows использует process handle/wait, не POSIX signal. |
| `telegram_outbox.py` | Durable intent/acceptance store для direct text/file и scheduled message/note; stable MTProto random id и immutable staged files. |
| `owner_delivery.py` | Общий для PWA/Telegram append-only owner inbox: typed outcome, dedupe/coalesce, CAS states, target-80 soft history budget без attention cap и torn-tail evidence. |
| `telegram_topics.py` | Exact root/topic route и стабильный conversation id для forum messages. |
| `group_context.py` | Root-bounded append-only group archive, topic/participant projection, search, orientation bundle и backfill receipt. |
| `telegram_membership.py` | Fsync JSONL state machine sovereign owner/self join/leave и post-acceptance room projection. |
| `telegram_registry.py` | Discovery установленной Telethon TL-schema; sovereign noncritical/raw dispatch и account-critical confirmation binding. |
| `telegram_confirmation.py` | Отдельный exact-new-owner-DM challenge, one-use proof, encrypted TTL parameter spool и digest-only receipt provenance. |
| `telegram_contacts.py` | Restart-proof адресная книга без хранения access hash. |
| `telegram_followups.py` | Durable ledger просьб написать кому-то и уведомлений о полученном ответе. |
| `rooms.py` | Allowlist/departed mask, freeze/mode и валидируемый root-room context profile. |

## Память, self и намерения

| Файл | Роль |
|---|---|
| `memory_life.py` | Append-only life evidence, episode compacts и hot-memory folding. |
| `memory_provenance.py` | Trust-классы: raw journal/reflections остаются searchable episodic evidence, но не normative provenance. |
| `memory_fts.py` | Rebuildable SQLite FTS по каноническим Markdown/JSONL/snapshots; journal получает отдельный source type. |
| `memory_index.py` | Recall/rerank API, optional embeddings и обновление навигационного индекса. |
| `memory_catalog.py` | Детерминированные bounded `memory/INDEX.md` и карты PEOPLE/ROOMS/PROJECTS/THREADS/RUNS/COMPUTERS. |
| `consolidate.py` | Сохраняет journal как непродвигаемый episodic log, отмечает дни и перестраивает производную навигацию; scheduled normative mutations = 0. |
| `formation.py` | Evidence-backed iterative formation вне критического пути ответа. |
| `sleep.py` | Планировщик formation/identity/index care; normative изменения идут через claims, REM только журналирует кандидаты, rumination no-op. |
| `people.py`, `social.py` | Портреты людей, salience/open loops и социальные роли/допуск. |
| `graph.py` | Markdown-граф связей и bounded traversal/resolve. |
| `self_model.py` | Fail-closed provenance-valid CURRENT, quarantined legacy/history, observations и explicit migrate/revise/rollback. |
| `desires.py` | Append-only conation ledger и rebuildable `memory/desires/CURRENT.md`. |
| `goals.py` | Однонаправленный compatibility/migration bridge удалённой moral-goals системы; живые свободные намерения принадлежат `desires.py`. |
| `computer_memory.py` | Канонические receipts/events Windows-работы, task/device reports и одна life-promotion на завершение. |
| `computer_inventory.py` | Server-observed inventory snapshot и catch-up refresh для Windows device. |
| `computer_access.py` | Owner-rooted human grants по stable Telegram user id; Praxis self sovereign, trusted не делегирует и не получает SYSTEM. |

## Автономная периодика

| Файл | Роль |
|---|---|
| `tasks.py` | Файловые scheduled wake/window/email/message/note occurrences и расчёт due без модели. `wake` — её будильник: живой ход с ОТКРЫТЫМ Telegram (в отличие от window, который транспорт намеренно рвёт). |
| `heartbeat.py` | Контекст автономного owner-window по открытым нитям. |
| `social_pulse.py` | Hourly claim и durable delivery receipts; недавние отправки видимы как контекст, но per-pulse/daily/target cap отсутствует. |
| `unanswered.py` | Персистентный список действительно неразрешённых DM после cooldown. |
| `absence.py` | Политика ответа важным контактам во время явно активного окна отсутствия. |
| `immune.py` | Advisory review self-edits/proposals и очередь post-commit verdicts; red не является veto. |

## Forge и локальные руки

| Файл/каталог | Роль |
|---|---|
| `forge.py` | Единственная Forge state machine: task root/base, inspect/edit/run, checkpoint, workers, swarm, verification, learning и finish. |
| `forge_intelligence.py` | Manifest/source orientation и semantic project model без обязательного LSP. |
| `forge_process.py` | Supervisor одного detached Forge process с полным log/result. |
| `forge_worker.py` | Fresh-context worker protocol и tool dispatch. |
| `forge_swarm.py` | Persistent worker DAG, mailbox и advisory ownership. |
| `forge_verify.py` | Detached verification matrix с bounded parallelism и независимыми logs. |
| `forge_learning.py` | Evidence-linked reusable lessons завершённых задач. |
| `workshop.py` | Python file/shell/dev-tool surface, используемая model tools. |
| `hands.py` | Мост к компилируемому filesystem/process floor с Python fallback. |
| `hands/` | Rust `praxis-hands`, path/write/output rails и генератор согласованного Python registry. |
| `selfgit.py` | Автокоммиты изменений собственного дерева и provenance core edits. |
| `selfdev.py` | Sovereign worktree proposal/test/review/merge/restart workflow с rollback и durable override reason. |
| `_sandbox.py`, `praxis_test.py`, `conftest.py` | Test-only перенаправление `PRAXIS_BASE` в disposable tree до импорта runtime-модулей; bare unittest не является поддержанным локальным entrypoint. |

## Привилегированный Linux host

| Файл | Роль |
|---|---|
| `serverd/broker.py` | Brainless `praxis.host.v2` UDS broker, auth/cgroup binding и routing операций. |
| `serverd/brokerops.py` | Stateless root workspace primitives и persistent operation directories. |
| `serverd/hostproc.py` | Detached root process supervisor, PID identity, logs, limits, stop tree и reaping. |
| `serverd/hostverbs.py` | Typed systemd/docker/pkg/file/net/reboot verbs с before/after evidence. |
| `serverd/hostrecovery.py` | Видимые rollback receipts/timers и explicit confirm. |
| `serverd/auditlog.py` | Append-only hash-chained audit и content-addressed export. |
| `serverd/advisor.py` | Consequence advice и выбранная owner file boundary. |
| `serverd/migrate_v1_tasks.py` | Идемпотентная миграция legacy host tasks в canonical Forge store. |
| `serverd_client.py` | Версионированный клиент UDS broker для Forge. |
| `serverd/install.sh`, `serverd/praxis-serverd.service`, `serverd/serverdctl` | Установка unit и операторский CLI. |
| `hostview.py` | Read-only запросы к серверной обсерватории. |
| `hostops.py`, `hostagent.py` | Сохраняемый legacy proposal/apply-контур host edits; не task store и не основной v2 broker. |

## Windows Body

| Файл/каталог | Роль |
|---|---|
| `body/crates/praxis-protocol/` | Wire types `praxis.body.v1`: identity, invoke/accepted/result, seq/ack и artifact refs. |
| `body/crates/praxis-body/` | Windows capability executor: config/identity, files, artifacts, processes/Job Objects, desktop, local routing, journal и transport. |
| `body/crates/praxis-system-router/` | Automatic LocalSystem service, sole WSS owner и supervision/routing system + interactive envelopes. |
| `body/crates/praxis-tray/` | GUI-subsystem tray icon и hidden interactive-session supervisor без console window. |
| `body/crates/praxis-bridge/` | WSS relay, durable frame spool, controller responses и artifact CAS. |
| `body_client.py` | Server-side synchronous controller: транспорт прямых `computer.*`-глаголов (PASS 30 Этап 3) и Windows backend deprecated wcode-скважины Forge. |
| `body/README.md` | Build/install/operator contract именно Windows Body. |

Внутри `praxis-body/src/`: `fsops.rs` отвечает за guarded file operations, `artifact.rs` — CAS
import/export, `process.rs` — process lifecycle, `desktop.rs` — typed Win32 windows/input/capture/
clipboard/process surface, `journal.rs` — durable operation receipts, `transport.rs` — WSS frame
flow, а `interactive_*`, `system_router.rs` и `local_router.rs` разделяют session routing.
`desktop.rs` активирует background-окно через временный `AttachThreadInput` handshake и затем
fail-closed проверяет фактический foreground HWND/PID перед вводом. `process_liveness.py` использует
Win32 process handle/wait и никогда не пробует Windows PID через `os.kill(pid, 0)`.

## Praxis App, почта и owner surfaces

| Файл/каталог | Роль |
|---|---|
| `mailer.py` | SMTP/IMAP primitives. |
| `mailroom.py` | Mail index, bodies by hash, drafts и owner-approved send state. |
| `mailroom_bot.py` | Telegram bot и HTTP host почты, legacy panel и `/app`; versioned Praxis API различает 401 auth и 403 scope, выдаёт SSE/artifact tickets и bounded multipart import. |
| `mailapp.html` | Почтовый Mini App frontend. |
| `panel.py`, `panelapp.html`, `panel_static/` | Live repo introspection, capabilities/settings and device panel frontend/assets. |
| `praxis_app.py` | Scoped snapshot/control model над тем же runtime; durable command/body idempotency, явные access/enrollment replay boundaries, run/artifact projection, owner inbox и 64 MiB default browser file bridge. |
| `praxis_device_auth.py` | Owner-only one-time enrollment, exact device scopes, HMAC event chain, bearer validation и revoke. |
| `praxisapp.html`, `praxis_static/` | Installable Praxis PWA: glass/particle shell, scoped controls, partitioned verified snapshot/drafts и shell-only service worker. |
| `serverapp.py`, `serverapp.html` | Read-only server observatory backend/frontend. |
| `logsink.py` | Rotated runtime log sink для owner surfaces. |

## Канонические каталоги данных

| Путь | Содержимое |
|---|---|
| `soul/` | Конституция, voice, rails, compact self/history и навыки. |
| `memory/people/`, `memory/rooms/` | Структурированная память. Room/profile context и exact principal-bound participant dossier доступны адресно; legacy people dossiers не входят в broad automatic recall. |
| `memory/journal/`, `memory/reflections.md` | Untrusted episodic evidence: сохраняется и ищется явно, но не входит в auto-orientation/normative derivation. |
| `memory/groups/` | Приватные root-bounded group archives; MAP рядом является derived. |
| `memory/runs/` | Immutable run context, WAL, results, artifacts и RECAP. |
| `memory/computer/` | Windows evidence и inventory snapshots. |
| `memory/desires/`, `memory/self/` | Conation events и provenance self observations. |
| `memory/.forge/` | Единственный task/worker/verification/learning store Forge. |
| `memory/.state/` | Смешанный runtime-контур: rebuildable индексы плюс durable outbox/control/receipt ledgers, owner delivery, device-auth key и private PWA staging; удаляемость определяется модулем. |
| `memory/access/devices/events.jsonl` | Private HMAC-chained issue/redeem/revoke authority; восстанавливается только вместе с `memory/.state/praxis_device_auth.key`. |
| `memory/dialogues/`, `memory/access/TRUST.md`, `memory/goals.md` | Приватные ignored runtime-данные: сохраняются на live/backup, но не отслеживаются source Git. |
| `workspace/` | Рабочие входы/выходы и scoped Telegram inbox, не личность и не task store. |

## Конфигурация и проверки

| Файл | Роль |
|---|---|
| `.env.example` | Публичный перечень runtime-настроек без секретов. |
| `memory/llm.json` | Runtime model roles/limits; обычно gitignored. |
| `docker-compose.yml` | Локальный `praxis` + optional Ollama. |
| `docker-compose.deploy.yml` | Production containers, mounts и owner surfaces. |
| `Dockerfile`, `requirements.txt` | Python runtime и сборка Rust hands floor. |
| `praxis_test.py`, `test_*.py` | Герметичный unittest entrypoint и действующие contract/regression tests; retired phrase/personality-freeze suites не входят в дерево. |
| `serverd/test_broker_v2.py` | Linux broker contract tests. |
| `body/crates/*` tests | Rust protocol/body/bridge/router unit and integration tests. |

Правила запуска опасных Windows/process suites находятся в [`AGENTS.md`](AGENTS.md). Исторические
версии этой карты и удалённые проектные заметки извлекаются из Git через
`git show <commit>:<path>`, а не дублируются в текущем дереве.
