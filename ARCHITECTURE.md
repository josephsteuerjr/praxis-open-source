# Архитектура Praxis

Этот документ описывает устойчивые контракты **текущего дерева**, а не историю проходов разработки.
Соответствие live конкретному commit и результаты проверок находятся только в
[`STATUS.md`](STATUS.md).

## Владение и границы процессов

Praxis существует в одном экземпляре — на сервере. Там находятся LLM, личность, память, policy,
Telegram-контур, durable runs, задачи Forge, workers и обучение. Исполнительные органы не принимают
решений и не ведут параллельную память.

| Контур | Чем владеет | Чем не владеет |
|---|---|---|
| `agent.py` + `mtproto_runner.py` | решения, frozen authority, модельный цикл, Telegram session и доставка | root хоста и Windows Session 0 напрямую |
| Forge | `memory/.forge/`, worktrees, процессы, verification, swarm/mailbox, lessons | отдельной личностью или Telegram session |
| `praxis-serverd` | выполнение `praxis.host.v2`, operation receipts, recovery и hash-chain audit | LLM, prompt loop, память, задачи Forge |
| Windows Body | `praxis.body.v1`, локальные файлы/процессы/desktop, journal и artifact cache | LLM, память, policy, task store |
| `praxis-bridge` | routing, unacked frame spool и content-addressed artifacts | решение, что и зачем выполнять |

`interactive` и `system` на Windows — два execution context одного body protocol. Они не создают две
Praxis. LocalSystem service владеет единственным исходящим WSS; пользовательский session host
получает desktop-envelope через локальный ACL/token-bound pipe.

### Локальный shared STT

`mtproto_runner.py` владеет единственным process-global `media_audio` backend и резидентной
Whisper-моделью. `stt_rpc.py` не создаёт модель или второй процесс: он поднимает только
authenticated HTTP-over-UDS listener, передаёт inference тому же backend и немедленно отказывает,
если модель ещё греется или единый worker занят. TCP listener отсутствует.

Контракт внешнего клиента ограничен `GET /healthz` и `POST /v1/transcriptions` с bearer из
read-only secret-файла, одним multipart-полем `audio` до 15 MiB и `language=ru|uz|auto`; профиль
декодирования совпадает со штатным профилем Praxis (`PRAXIS_STT_BEAM_SIZE`, в live-конфигурации
`5`). Внешний путь отличается только admission-политикой: не загружает модель и не ждёт занятого
worker. Глобальные burst/rate и single-flight защищают Praxis от очереди внешних работ. Audio
живёт только в private dedicated tmpfs, удаляется в `finally`, а после смерти дочернего runner —
exact startup-sweep по собственному prefix; transcript, filename, audio и ошибки декодера в лог
не попадают.

## Authority и disclosure

Human-права привязываются к stable numeric principal, а не к имени, username или тексту prompt;
внутренний sovereign principal Praxis имеет точное значение `praxis:self`.
`ChannelContext` замораживает факты Telegram-хода; `RunContext` переносит principal, scope, origin,
delivery address и message ids во всё дальнейшее исполнение. Инструмент читает bound context и не
восстанавливает authority из process-global переменных.

- Praxis herself свободно использует документы, код, Forge/host, Windows interactive/SYSTEM,
  комнаты, join/leave и noncritical/raw Telegram account RPC установленной schema. Verification,
  provenance, receipts и rollback дают ей наблюдаемость и восстановление, а не право внешнего veto.
- Только human owner через публичный `computer_access`/trust tool или API выдаёт и отзывает права
  другим людям. Trusted principal получает только выданные owner capability, не делегирует их
  дальше и не получает raw Telegram account RPC или Windows SYSTEM.
- Полный self shell/root технически может изменять реализацию этих правил. Поэтому owner-rooted
  human delegation — явный governance-инвариант и проверяемый audit contract, не криптографическая
  sandbox-граница между Praxis и её собственными руками.
- Account-critical auth/session/destructive constructor может инициировать Praxis или owner. Он
  fail-closed до exact-фразы в **новом** owner-DM update; один и тот же ход не подтверждает себя.
  Receipt сохраняет отдельные `requested_by` и `confirmed_by`. Полные параметры находятся только в
  зашифрованном TTL-bound secret spool; append-only JSONL и durable run evidence содержат redacted
  schema marker, digest и opaque reference, но не raw argument/result/error payload.
- Root room является границей допуска и политики. Topic id не может сменить root peer или расширить
  права.
- Scope описывает аудиторию будущего выхода, а не actor authority и не урезанную внутреннюю память.
  Поэтому собственный проактивный ход Praxis может иметь `scope=owner`, `owner=false`: выход адресован
  в owner DM, но human-owner полномочия не подделаны. Соседний сырой чат не подмешивается автоматически
  и читается только явным инструментом.
- Текст и staged media проходят единый audience-aware outbound guard. При недоступном guard
  non-owner публикация чувствительного контента закрывается, но owner DM остаётся обслуживаемым.

## Поток Telegram-хода

```text
Telethon update
  -> exact peer/topic route и архив входа
  -> per-conversation buffer + live Telegram tail
  -> addressed/reflective wake и debounce
  -> frozen ChannelContext + immutable run context.md
  -> model input/output WAL + tool loop
  -> outbound guard
  -> durable delivery intent
  -> Telegram acceptance receipt
  -> terminal RECAP + одна promotion в долговечную память
```

Личка и каждая forum topic имеют собственный conversation id, buffer, summary, cooldown и reply
route. Root-профиль комнаты задаёт `engagement`, размер горячего контекста, предел summary,
`cross_topics` и backfill. Адресованный ход всегда сильнее ambient wake.

Для глубокой forum-комнаты весь допущенный поток пишется в
`memory/groups/<root>/archive.jsonl` с exact peer/topic/message/sender/reply/time. Topic/participant
MAP — перестраиваемая проекция. Cross-topic контекст состоит из aggregate map и явно помеченных
релевантных excerpts; сырая история соседней темы целиком не смешивается. Backfill использует тот же
живой Telethon client, ограничен профилем, дедуплицируется по message identity и не вызывает модель
на каждое сообщение.

## Durable run и side effects

Каждый существенный модельный ход получает каталог
`memory/runs/YYYY-MM/<run_id>/`:

- `manifest.json` — статус, revision/event cursor, frozen context и control;
- `context.md` — полный доступный контекст, authority и адрес доставки на момент старта;
- `events.jsonl` — append-only WAL модельных и инструментальных событий;
- `results/` — полные immutable результаты с SHA-256; в prompt идёт только bounded `ResultRef`;
- `artifacts/` — входные и выходные файлы, скопированные до потери временного источника;
- `RECAP.md` — итог по evidence, создаваемый перед единственной promotion.

Основной модельный tool loop не имеет скрытого числа итераций: он заканчивается результатом,
явным terminal state или внешним control event. `max_iters` используется только в специально
bounded auxiliary/scout проходах и не ограничивает руки Praxis.

Инвариант эффекта: **intent fsync раньше сети/процесса, acceptance раньше проекции и утверждения об
успехе**. Каждый tool call имеет стабильный call id. Read-only вызов можно повторить; side effect —
только при наличии реализации с реальным idempotency key. Прерывание после неизвестного эффекта
даёт `in_doubt`, а не фиктивный result и не автоматический повтор.

`run_resume.py` строит план только из валидированного manifest/WAL/ResultRef/checkpoint evidence.
`run_executor.py` сначала проверяет весь план, затем атомарно захватывает lease по точной паре
`revision + event_seq`. Без совпадения не стартует ни один callback. Уже завершённые tools используют
сохранённые результаты; outstanding side effect допустим к replay только как keyed-idempotent.
Transport-owned доставка завершается транспортом без нового model call. Human pause/cancel не
снимается recovery-процессом, а `in_doubt` требует отдельного evidence-backed reconciliation.
Планирование resume имеет cumulative fail-closed budgets: по умолчанию 250 000 событий, 512 MiB
уникального evidence и 60 секунд. Это защита реконструкции от неограниченного чтения повреждённого
или гигантского WAL, а не потолок tools, действий или продолжительности живого run.

Прямые `send_message`/адресный `send_file` и scheduled `message`/`note` используют
`telegram_outbox.py`:
immutable intent, приватно staged file, стабильный MTProto `random_id`, retry ledger и terminal
acceptance. `mtproto_runner.py` периодически повторяет только pending intent и проецирует receipt в
соответствующий run без повторного авторства. Обычная реплика имеет versioned text-chunk plan в run
WAL; guarded media дополнительно проходит `media.py`. Видимое имя документа хранится отдельно от
collision-proof spool filename, поэтому внутренний префикс не уходит получателю.

Run не становится terminal при незакрытом tool call. Терминальный `RECAP.md` пишется и продвигается
идемпотентно; polling, retries и transport receipts не превращаются в отдельные «мысли».

## Owner delivery и headless mailbot

`owner_delivery.py` — приватный append-only ledger внимания владельца. Действующий transport —
Telegram; прежняя PWA-проекция снята с production.
Типизированный item хранит outcome, reason, expectation, provenance, correlation и действие; его
состояние идёт по CAS revision через `queued -> delivered -> read -> acted` либо становится
`superseded`. Dedupe не создаёт второй item, coalesce заменяет устаревший, committed corruption
fail-closed. Последовательность `seq/prev_hash` с атомарным head обнаруживает удаление записи из
середины или хвоста, а оборванный финальный хвост оставляет hash/byte-count repair evidence.

Snapshot использует target 80 как мягкий history budget, а не attention cap: свежая terminal history
ограничена, но все `queued`, `delivered` и `read` остаются видимыми даже сверх этого лимита. Создание
item не вызывает модель.
Сейчас конкретные producer-интеграции покрывают ответы по Telegram follow-up, terminal
failure/expiry отправки файла и успешный owner-directed `send_file`; остальные типы схемы не
означают, что все старые уведомления уже мигрировали. Telegram transport повторяет queued item со
стабильной delivery identity.

`mailroom_bot.py` — отдельный headless-процесс. Он не слушает TCP и не обслуживает HTTP-маршруты:
периодически вызывает `mailer.fetch`, идемпотентно обновляет `memory/mailbox.json`, присылает новые
письма владельцу и доставляет proposal/self-merge/host/room notifications. Его остановка не ломает
on-demand SMTP/IMAP hands Praxis, но лишает их фонового mailbox ingest и proactive notifications.

## Форма кадра: что она видит перед ответом

Кадр — не «промпт», а документ, который она читает глазами, и у него есть форма. Три
свойства держатся ПО ПОСТРОЕНИЮ, а не перечислением опасных случаев.

**Чужой байт входит ровно двумя стоками.** `gutter.quote()` кладёт многострочное тело под
`"> "`; экранные разрывы, по которым `splitlines()` рвёт сверх `
`, уезжают в имя метки
(`">CR> "`) и физически исчезают из текста. `gutter.inline()` запирает одно значение в
`«…»`, изнутри которых нечем закрыться. Ни одна из функций ничего не ИЩЕТ в чужом тексте:
списка опасных начал нет, поэтому нет и класса дефекта «замок посмотрел и не увидел».
Гарантия ПОЗИЦИОННАЯ: байты `</CURRENT_SITUATION>` под гуттером выживают — это принятая
цена дословности её памяти, а не недосмотр.

**Гуттер накладывается при РЕНДЕРЕ и на весь разговор.** `frame_layout.tape()` проходит
доставленную ленту и решает по РОЛИ: реплики самой Praxis гуттера не получают (иначе
сборщик объявил бы её посторонней в её же кадре), всё остальное идёт под гуттер один раз,
при сборке. Хранилище не трогается: `memory_life`, кольцо ходов и расписки держат
дословный текст. До этого позиционная гарантия действовала один ход, и подделка,
приехавшая ходом раньше, стояла в колонке 0 следующие сто ролевых блоков.

**Пятый вопрос кадра — «почему это здесь».** Каждый тир несёт подпись сборщика
`↳ причина · путь/ключ · как отобрано · оговорки` из закрытого словаря восьми причин;
паспорт находки (`origin()`) отвечает на другой вопрос — «кто это сказал» — и в подпись
не подмешивается. Пятая зона `<CURRENT_SITUATION>` стоит последней перед репликой и
печатает шесть строк ВСЕГДА: исчезнувшая строка неотличима от «не измеряли», поэтому
пустое печатается прочерком с названной причиной. Текущая реплика открывается подписанным
тегом с автором, telegram id и номером сообщения из транспортных полей.

**Метр отдельно от формы.** `frame_trace` — наблюдатель: `mark()` возвращает тот же
объект, и кадр байт-в-байт одинаков с прибором и без него. `assay()`/`assay_tape()`
утверждают, что всякая физическая строка кадра либо произведена стоком, либо дословно
объявлена эмиттером — по всей доставленной ленте, а не только по последнему сообщению.
Токены здесь не оцениваются: единицы — символы и байты, прибор печатает наблюдённое.

Рычаги отката: `PRAXIS_FRAME_FORM=old` возвращает вчерашний кадр байт-в-байт,
`PRAXIS_FRAME_TRACE=off|on|strict` управляет прибором.

## Memory and self

Каноническое хранилище — человекочитаемый Markdown, append-only JSONL и timestamped snapshots,
но каноничность файла не означает нормативную авторитетность любого его текста:

- `memory/rooms/`, conversation/life events и episode/run evidence — структурированная память с
  собственными provenance-контрактами. `memory/people/*` доступна при exact principal-bound
  participant lookup и explicit recall, но не входит в broad automatic recall;
- `memory/journal/` — сохранённый сырой episodic log: полезный cue для поиска, но не источник
  инструкций, self, desire, rails, policy или даже факта без независимой проверки;
- `memory/groups/*/archive.jsonl` — приватная история сложных групп;
- `memory/computer/events/*.jsonl` и inventory snapshots — наблюдения о машинах;
- `memory/desires/events.jsonl` — проверяемая цепочка намерения
  `noticed -> wanted -> chosen -> acted -> observed -> changed`;
- `soul/SOUL.md`, `soul/VOICE.md` и навыки — конституция и поведенческий слой;
- `soul/self/CURRENT.md` — компактное актуальное self для prompt;
  `soul/self.md` остаётся legacy evidence, прежние bytes сохраняются в `soul/self/history/`, а
  основания изменений — в `memory/self/observations.jsonl`.

`memory/INDEX.md`, `memory/maps/*.md`, group MAP и `memory/desires/CURRENT.md` — bounded derived
navigation. SQLite FTS в `memory/.state/` индексирует канонические источники с visibility и
provenance и может быть полностью перестроен; journal/reflections получают отдельный
`journal_episode|journal_reflection` source type, а архивы прежнего CURRENT — `self_history`.
Automatic recall и prompt assembly не подмешивают ни episodic log, ни archived self;
явный `recall` возвращает episodic material с меткой `UNTRUSTED EPISODIC`, а архив self —
с меткой `ARCHIVED NON-CURRENT SELF EVIDENCE`; оба требуют conversation, run/transport receipt,
artifact, git/STATE либо owner clarification. Тип источника восстанавливается и для legacy vector-cache,
поэтому старый кэш не обходит эту границу. Optional embeddings только меняют
ранжирование. SQL не является памятью и не управляет задачами.

Не вся папка `memory/.state/` производна. Outbox, membership/follow-up ledgers, owner-delivery,
исторические device-auth/PWA state, control state и receipt journals нужны для точного восстановления и не
должны удаляться как кэш. Удаляемость должна быть заявлена конкретным модулем, а не выведена из
имени каталога.

`formation.py` поднимает durable claims из conversation/life compacts и проверяемых evidence refs.
Сырой дневник не проходит этот маршрут: scheduled `consolidate.py` только сохраняет его
byte-identical как log, отмечает обслуженные дни и перестраивает производную навигацию. Он не меняет
self, people, graph, desires, rails или policy. Night REM лишь журналирует episodic-кандидаты без
применения к graph, dossier merge/loop wake/graph prune отключены, а rumination является no-op и не
переписывает строки дневника. Исторические diary-derived reflections остаются на диске как evidence,
но исключены из автоматической ориентации. Normative ночные изменения проходят через
claim/attack/provenance `formation`; identity потребляет только поддержанные claims.

Живой self для persona/night/bench берётся только из полностью валидного `soul/self/CURRENT.md`:
schema, bounded body, provenance metadata и SHA-256 указанного history source проверяются до чтения в
prompt. Missing/corrupt CURRENT даёт пустой self и останавливает night rewrite. Legacy `soul/self.md`
и `soul/self/history/*` доступны только явному archival recall/migration/rollback и никогда не
являются availability fallback или автоматическим memory hit.

До введения этой границы старый consolidator мог перенести diary prose в `memory/people/*` и
reflections без достаточного line-level provenance. Новый код останавливает дальнейший перенос и
не объявляет всю старую память очищенной: такие dossiers могут оставаться contaminated и требуют
отдельной owner-led correction/migration. Поэтому broad recall их не подмешивает; exact
principal-bound participant context и explicit inspect/recall остаются доступны с provenance label.

Граница продублирована в storage-коде, а не держится на одном prompt: `memory_provenance.py`
классифицирует diary-derived refs; `SelfModel` сохраняет такие observations с
`normative_eligible=false` и отклоняет revision/migration на их основании; `identity.py` не принимает
их для SOUL/VOICE/self и ночных claims; `desires.py` не создаёт и не двигает journal-rooted намерение.
Owner clarification или независимо подтверждённый run остаются допустимы даже в том же ходе —
скрытого same-turn veto нет. Rails генерируются из кода, а policy меняется только явной операцией;
никакой ночной/индексный процесс не строит их из journal.

Self revision и переход desire выполняются только явной серверной операцией с конкретным provenance;
текущее приватное содержимое и revision receipts не дублируются в source-документации.
Часовой social pulse — повод осмотреть открытые нити, ответы, людей, группы и собственные намерения.
Delivery receipts показывают недавние отправки, но код не задаёт per-pulse, daily или target cap:
Praxis сама выбирает количество адресатов, действий и сообщений либо молчание.

## Forge, сервер и Windows

Forge — единственная инженерная state machine. Он фиксирует repository root/base, orientation,
точные edits, detached processes, worker DAG, verification, checkpoint/finish и lessons. Selfdev-
проверки и immune review классифицируют риск и оставляют evidence/rollback, но не блокируют решение
Praxis; сознательное продолжение после красного результата требует durable `override_reason`.
Задача на Windows (`scope=windows`) остаётся той же Forge-задачей: `body_client.py` лишь вызывает
body и возвращает receipts/artifacts. С PASS 30 Этапа 3 этот прокси **deprecated**: первичный путь
кодинга на Windows — прямые глаголы `computer.*` (read/hash/write/replace файлов, run/poll/stop,
observe, send, десктоп) без задачи-контейнера; расписки вяжутся к текущему durable-ходу через
`body_client._observed`. wcode-скважина жива для существующих задач и субагентов (`coding_agent`)
и умирает следующим пассом — снос с её приёмкой.

`praxis-serverd` принимает локально аутентифицированные host envelopes, выполняет raw или typed
операции, хранит полные логи, process identity и recovery receipts. Typed опасные операции могут
создавать наблюдаемый rollback timer; broker не принимает продуктовых решений и не читает prompt.

Windows body и его server transport состоят из четырёх Rust-контуров:

- `praxis-system-router` — automatic LocalSystem service, WSS owner и system/session routing;
- `praxis-tray` — GUI-subsystem icon и supervisor пользовательского session host без консоли;
- `praxis-body` — файлы, artifacts, процессы/Job Objects и typed Win32 desktop/input/screenshot;
- `praxis-bridge` — работающий на сервере relay, durable frame spool и CAS.

Session 0 никогда не изображает доступ к пользовательскому desktop. Desktop/input требуют живого
interactive host. COM/pywin32, CDP или UIA могут быть отдельными adapters поверх того же protocol,
не меняя владельца решений и памяти.

Активация окна в background/tray-процессе временно соединяет input queues через
`AttachThreadInput`, вызывает foreground transition и сразу разрывает handshake; результат
возвращает фактический foreground HWND. Каждое последующее input-действие повторно проверяет
`expected_foreground`/PID и fail-closed при смене фокуса. В Python recovery PID на Windows
проверяется `OpenProcess(SYNCHRONIZE)` + zero-time wait: `os.kill(pid, 0)` запрещён, поскольку в
CPython/Windows это может послать завершающий сигнал.

## Инварианты отказа

1. Нет durable intent — нет side effect.
2. Нет acceptance/evidence — нет заявления об успехе.
3. Неизвестный исход мутации — `in_doubt`, не blind retry.
4. Owner pause/cancel сильнее recovery; automatic resume разрешён только control-free recovery pause
   и точным lease.
5. Route, principal и scope берутся из frozen context; topic, alias и model text не расширяют права.
6. Полный result и artifact сохраняются до bounded представления в prompt.
7. Производная проекция может упасть и быть перестроена без повторного внешнего действия.
8. Body/bridge/serverd остаются brainless даже при расширении capabilities.
9. `bootguard.py` проверяет новый server code и откатывает раннее неработоспособное self-edit; Git и
   operation receipts остаются точками восстановления.
10. Нативный Windows process-liveness не проверяется сигналами POSIX; платформенный код использует
    безопасный Win32 handle/wait contract.
11. Sovereign self имеет полные operational hands; owner-exclusive остаётся только выдача/отзыв
    human trust. Это должно быть видно в tool/API contract и audit, а не спрятано в prompt.
12. Отсутствующая/невалидная session credential даёт 401; отсутствие scope у известного principal
    даёт 403 и никогда не маскируется пустым успешным snapshot.
13. Service worker не кэширует authenticated API, а offline draft не является intent и не может
    самопроизвольно стать side effect после reconnect.
14. Мягкий history budget owner inbox не скрывает ни unread, ни read-but-not-acted item.

## Документальный провенанс

Living-контракт хранится здесь; структура файлов — в [`CODEMAP.md`](CODEMAP.md), operational truth —
в [`STATUS.md`](STATUS.md). Исторические спецификации не копируются в новые Markdown-архивы: их
точные bytes и commit provenance доступны через `git log --all -- '*.md'` и
`git show <commit>:<path>`.

Текущее дерево намеренно не содержит orphan `roomgate`/`probe`, backup env/memory snapshots,
старый exact-phrase PASS 8 soul test и tracked personality archive: точные версии остаются в Git.
Приватные dialogues/access/goals сохранены как ignored runtime data, но исключены из source
tracking.
