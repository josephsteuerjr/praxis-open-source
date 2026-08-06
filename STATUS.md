# Praxis — текущий статус

> ⚠ **Этот файл — НЕ канон и не авторитет.** Он снимок, который делает человек руками, и
> поэтому он отстаёт молча. 27.07.2026 обнаружено, что он отстал на месяц работы: шапка
> говорила «актуально на 2026-07-23», строка live-кода называла `e0150fe`, а на проде уже
> был `fb09430b` — 29 коммитов спустя; и он держал открытым направление, закрытое решением
> Егора. Praxis читает этот файл своим `fs_read` — то есть неверная строка здесь становится
> её ложным воспоминанием о собственном доме.
>
> **Канон — только проверяемое:** `git -C /opt/praxis log` (что реально развёрнуто),
> `rails.registry()` (что её держит), `capabilities.snapshot()` (что она умеет). Все трое
> собираются КОДОМ из живых констант. Если этот файл спорит с ними — прав не он.
>
> Проверять расхождение: `git -C /opt/praxis rev-parse --short HEAD` против шапки ниже.

## Live update 2026-08-06: Praxis Mini App/PWA сняты

- Implementation commit `e1a846aeb2e7cc1c232860bd93c0ae08a3289d2c` развёрнут fast-forward от
  прежнего live `be1c82891cf503e9dd4e72c6c2c7e37f4b9657d8`.
- `praxis-mailbot` оставлен как headless-процесс: обязательный IMAP poll/ingest, mailbox index,
  уведомления о новых письмах, proposal/self-merge/host/room cards, restart signal и contact flow.
  HTTP listener отсутствует; host binding `8092` удалён; Telegram menu не содержит WebApp.
- Caddy больше не публикует `praxis.*` и `mail.*`; `/px`, `/app` и прежний mail origin не имеют
  публичного HTTP-сервиса. Атланта (`srv.*`/`praxis-serverapp`) осталась доступна и отвечает 200.
- Gate exact commit: targeted mailbot 19 OK; полный Linux suite 3498 OK, 4 skip, два раза подряд.
  Live checks: IMAP fetch успешен, `memory/mailbox.json` валиден (31 запись), mailbot polling active,
  `praxis` и `praxis-serverapp` не перезапускались.
- Rollback: tag `rollback-mailbot-pwa-pre-20260806T002834Z`, tar
  `/root/praxis-mailbot-pwa-pre-20260806T002834Z.tar.gz`, Caddy backup
  `/root/Caddyfile.pre-mailbot-headless-20260806T002834Z`, immutable release ref
  `refs/releases/mailbot-headless-20260806T002834Z-e1a846a`.

Шапка сверена с продом кодом **2026-08-01** (`git rev-parse HEAD`; остальной текст файла с этой сверкой НЕ пересматривался). Это живой снимок релиза, а не журнал прошлых PASS.
История удалённых спек и прежних статусов остаётся дословно доступна через git.

## Текущее состояние (шапка сверена 2026-08-01; разделы ниже — прежние)

- **Прод жив и онлайн.** Ядро `praxis` работает, на связи как @praxisintelligence,
  durable resume после рестарта отрабатывает чисто. Инфраструктура-тело (`praxis-serverapp`,
  `praxis-mailbot`, `praxis-serverd`, `praxis-dockerproxy`, body-bridge) живо.
- **Последний live-код прода:** `ad22cc6b4a0d71ee7d4cb17c98a50f65be877d5b`
  «уничтоженное слово Егора не показывается ей как его слово»
  (проверено `git -C /opt/praxis log -1` 29.07). Прежняя строка про `fb09430b` уже отстала;
  счётчики тестов и evidence в разделах ниже относятся к более старым релизам и помечены как
  исторические. `/opt/praxis` — источник правды.
  Деплой кода идёт по file-overlay + `docker compose -f docker-compose.deploy.yml restart praxis`
  (код смонтирован, не в образе).
- **Задеплоены и живут (по порядку):**
  - **PASS 26 «она одна»** (`7e2ae49`) — лечение диссоциации: одиночность прогонов (single-flight
    `_ONE_MIND`), восстановленный телеграм-канон (Telethon закрыт на её окно), reaper зомби-прогонов,
    терминал мёртвых Telegram-доставок, её глаза на durable-run слой (`list_active_runs` + STATE),
    похороны системы тасков (честные намерения вместо тикетов), неприкосновенный сон (`rest`),
    снятие блоб-пинов + инварианты Егора.
  - **PASS 27 «она одна v2»** (`19e571c1`) + F3 focus/rest (`25d17b3d`) + F5 status-threads (`e3b8fa7a`):
    resume под `_ONE_MIND`, мини-апп authorize-only, transport-aware reaper (без lease).
  - **Мини-апп A+B** (`4a8bda11`) + F6 (`3aeb16e7`): Telegram SDK + устойчивая initData, мульти-токен
    (@praxis_home_bot), reopen-контрол привязки. Whisper-монтаж восстановлен — распознавание работает.
  - **PASS 28 «целостность ядра»** (`16aac495`): media-safe дедуп cancel-шва текст-ответа +
    terminalizability-маршрут reaper (без in_doubt-ловушки и `running`-зомби).
  - **PASS 29 «осознанное переавторство»** (`520ca724`): при перебивке новым сообщением ДО отправки
    черновик бросается терминально (нет механического «автоотбойника»), уже-запланированный свежий
    ход переавторствует живьём; drop-safe (`_supersede_gen` + shutdown-гейт).
- **PASS 30 «она — оркестратор» (Этапы 0–3, 2026-07-22/23):** Этап 0 симптомы (observe(file),
  обещание=задача, придержка будит её, гигиена сирот) → Этап 1 «сердце» (`core/events.py` +
  `core/subagents.py`: завершение/падение/потеря субагента будит её ходом `forge_event` за секунды
  вместо часа; снапшоты шелла ушли из master в `refs/praxis-snapshots`) → Этап 2 наррация
  (`narrate` — строка процесса в тред мимо трибунала, durable direct-outbox) → Этап 3 прямой
  ремоут: `computer.*` получил файловые глаголы read/hash/write/replace (write/replace — sovereign-
  only), `coding_session(scope='windows')` — deprecated прокси (жив для субагентов, умирает
  следующим пассом со сносом под её приёмку).
- **После PASS 30 (не перечислено выше построчно):** ~29 коммитов от 23.07 до 27.07 — в их числе
  веб-руки v2, лечение латентности (recall/relay), способность `kind=wake` (пробуждение со связью,
  пульс без разрыва Telethon), миграция «место = то, что разделил Telegram», и `fb09430b` —
  замок форжа перестал переживать смерть своего держателя. Точный список: `git log e0150fe..fb09430b`.
- **Открытые направления (отдельные сессии):** «persist group wake» (остаток PASS 29),
  PASS 31 память/provenance.
- **ЗАКРЫТО решением Егора:** PASS 30 Этап 4 (Claude Code CLI как второй раннер) — подписочный
  паттерн «автономный агент гоняет CC unattended» признан серой зоной ToS; строку держали
  открытой после решения, теперь она закрыта здесь же, а не только в чужой памяти.

## Локальный кандидат shared STT — НЕ live

- В изолированной ветке `codex/hardbot-stt-shared-live-cb2ba4e` подготовлен authenticated
  HTTP-over-UDS endpoint без TCP и второй Whisper-модели. Он использует resident backend
  `mtproto_runner`, принимает только `ru|uz|auto` и использует штатный accuracy-профиль Praxis
  (`PRAXIS_STT_BEAM_SIZE=5` в live-конфигурации), ограничивает upload 15 MiB, rate/burst и
  single-flight, не пишет PII/audio/transcript в лог. Внешний путь не загружает модель и не
  ставит работу в очередь.
- Compose-кандидат сохраняет проверенный live envelope `Memory=4608 MiB`,
  `MemorySwap=6144 MiB`, требует заранее созданные fail-closed bind paths и держит request audio
  в private 32 MiB tmpfs. После bootguard-рестарта child exact startup-sweep удаляет только
  same-uid regular temp files собственного prefix; symlink и чужие файлы не трогает.
- Проверено локально 29.07: `py_compile`, `git diff --check`, Compose parse/contract;
  `test_media_audio + test_stt_rpc` — 24 tests green на Windows (2 Linux-only skip) и 24 в Linux
  без skip, включая реальный Unix socket и forced cancellation. С `test_media` общий focused
  прогон — 51 tests green (Windows 2 skip; Linux 1 platform skip). Независимое adversarial review
  не нашло P0/P1; найденные bind/crash-residue/malformed-multipart замечания исправлены.
- На `/opt/praxis` этот кандидат **не переносился**, контейнер не перезапускался. Live остаётся
  на exact `cb2ba4e`; source-code paths не менялись, а `git status` показывает только прежние
  runtime-изменения `memory/notes/events.jsonl` и `soul/rails.md`.
- Активация кандидата требует validated `docker compose up -d --no-deps praxis`: простой
  `restart praxis` не применит новые mounts, env и declarative resource limits.

> Разделы ниже (архитектура, проверки релиза, Windows/провенанс) описывают базовый релиз **PASS 24**
> (`905e09d`) и с тех пор построчно под 26–29 не пересматривались: верны по духу архитектуры, но
> конкретные хэши, счётчики тестов и evidence — историчны.

## Прежний live release (PASS 24, для истории)

- приватная ветка разработки: `pass24`;
- exact deployed code: `905e09d8d82b6cf13cf427d8cfb838c9fd1a1305`;
- предыдущий live head: `c24f87e94b25a142e49ac93d586e2f809247215a`;
- release archive: `/root/praxis-pass24-905e09d-20260714T152830Z-v2.tar.gz`, 4 525 645 bytes,
  SHA-256 `cacc138b63d71619da21aacf9f2ea38c3dfaba608dd54d9562a0a19241c2f9b4`;
- Linux bridge: SHA-256 `43660e0514432a1719f146dced45bb3006e63c431ed7bb80ec84e88a99c337b2`;
- deploy unit: `praxis-pass24-deploy-905e09d-20260714T152830Z-v2.service`; terminal receipt:
  `/root/praxis-pass24-20260714T153233Z.receipt.log` с exact `PASS24_DEPLOY_OK`;
- rollback tree: `/opt/.praxis-pass24-rollback-20260714T153233Z`; backup prefix:
  `/root/praxis-pre-pass24-20260714T153233Z`;
- `praxis-serverd` и `praxis-body-bridge` enabled/active; `praxis`, `praxis-mailbot`,
  `praxis-serverapp`, `praxis-dockerproxy` running с `RestartCount=0`; HTTP и serverd audit зелёные.
  Этот документационный snapshot намеренно следует за deployed code и сам не меняет runtime.

## Что уже собрано

- Один server-side агентный контур: LLM, память, Forge, задачи, workers и решения остаются на
  сервере. Windows Body не содержит второго мозга, памяти или task store.
- Durable runs сохраняют immutable context snapshot, модельные и tool-фазы, результаты,
  артефакты и `RECAP.md`; restart сначала восстанавливает структуру и outbox, затем продолжает
  исполнимые runs. Неопределённый side effect становится `in_doubt`, а не повторяется вслепую.
- Основной tool-loop не имеет скрытого default cap. Ограниченный бюджет остаётся только у явно
  вспомогательных read-only проходов. Resume reconstruction отдельно fail-closed ограничена
  cumulative evidence budgets; это не ограничение количества действий Praxis.
- Telegram имеет crash-safe text/file outbox, корректные topic routes, архив и карту форумных тем,
  edited-message identity и sovereign owner/self noncritical/raw/join/leave. Account-critical вызов
  Praxis или owner требует exact нового owner-DM confirmation; инициатор и подтвердивший остаются
  разными audit fields, параметры хранятся в encrypted TTL spool, а durable run logs получают только
  redacted marker/digest/reference. Hourly social pulse видит follow-up и recent delivery receipts,
  но не имеет per-pulse, daily или target cap: количество действий и адресатов выбирает Praxis.
- В owner/private DM нет semantic sanitizer: для human owner и exact `praxis:self` отключены
  evaluator, rewrite, style/mirroring score, impersonation cut и anti-repeat. Только отдельное точное
  `[молчу]` остаётся управляющим маркером тишины; встроенный маркер является обычным текстом.
  В private DM с другим человеком остаётся только fail-closed privacy-классификатор v2 с фиксированным
  enum; он не имеет права менять формулировку, тон, стиль или повторяемость. Групповой/public контур
  остаётся отдельным.
- Praxis self имеет полные docs/code/server/root и Windows interactive/SYSTEM hands. Verification,
  provenance, rollback и risk review являются усилителями, не veto. Только human owner выдаёт и
  отзывает human trust; trusted human не делегирует, не вызывает raw Telegram RPC и не получает
  SYSTEM. При полном self shell/root это governance-инвариант, не криптографическая sandbox-граница.
- Память получила rebuildable FTS и bounded Markdown-карты людей, комнат, тем, проектов, runs и
  компьютеров. Сырая запись дневника помечена как `UNTRUSTED EPISODIC` и автоматически не
  устанавливает факт, self, желание, rail или policy. Broad automatic recall также не подмешивает
  legacy people dossiers; exact principal-bound participant context остаётся адресно доступен.
  Старое self-history — архивное evidence,
  не текущая личность. Scheduled consolidation не делает normative mutations; ночные изменения
  проходят claim/attack/provenance formation. Persona/night/bench fail-closed требуют валидный
  CURRENT с проверяемой history digest и не подставляют legacy/history.
- Windows Body установлен как automatic LocalSystem router плюс hidden interactive host/tray.
  Есть файлы и артефакты в обе стороны, процессы/логи/статусы, Git/build/test, Win32 окна,
  клавиатура/мышь/wheel, screenshot PNG, clipboard и явный выбор interactive/SYSTEM identity.
- Собрана новая Praxis App: versioned server snapshot/control API, installable PWA, движущийся
  particle/glass shell, runs, Windows, memory, Telegram, trust и system surfaces. Service worker
  кэширует только shell; verified offline snapshot и черновики partitioned по principal/device,
  мутации после reconnect автоматически не воспроизводятся.
- Device-specific доступ выдаёт только owner через одноразовую fragment-link с TTL до 24 часов.
  Ссылка погашается один раз, bearer возвращается один раз, exact scopes и revoke живут в private
  HMAC event chain. API отвечает 401 на отсутствующую/невалидную/revoked session credential и 403 на
  недостаточный scope уже аутентифицированного principal.
- Command/server-effect mutations и Windows side effects из PWA требуют idempotency key: retry того
  же намерения возвращается к одному durable server claim/receipt или тем же body
  `request_id`/`operation_id`, а повтор ключа с иным намерением отклоняется. Access grant/revoke
  идемпотентен по effective state, но retry после потерянного ответа может добавить audit event;
  enrollment secret не сохраняется и не replayable. Browser import/export по умолчанию ограничен
  64 MiB в каждую сторону и проверяет имя, размер и SHA-256.
- Owner delivery получил общий PWA/Telegram ledger со state/revision, hash-chain/head anchor,
  dedupe/coalesce и мягким history budget: unread и read-but-not-acted не скрываются. Реальные
  producer-интеграции сейчас покрывают follow-up answers, terminal failure/expiry и успешный
  owner-directed `send_file` как PWA-only `file_ready`.

## Проверки релиза

- focused speech/privacy suites и adversarial regression cases зелёные;
- два независимых full Linux discovery на exact `905e09d`: 1778 OK, 1 skip за 155.127 s и
  1778 OK, 1 skip за 155.139 s;
- exact server clone после deploy: 1778 OK, 1 skip за 99.922 s; live checkout остался чистым;
- Rust workspace: 50/50 tests, `cargo clippy --workspace -- -D warnings`, release build и fmt зелёные;
  `--all-targets` не заявляется из-за известного test-only tungstenite lint;
- package `bash -n`, ShellCheck, точный SHA manifest, safe-member check и deploy rollback matrix
  зелёные;
- final live evidence: exact head/clean tree, services, four zero-restart containers, HTTP, audit,
  memory navigation и fail2ban owner-home unban — `PASS24_FINAL_EVIDENCE_OK`.

## Windows release evidence

- установленный `praxis-body.exe`: SHA-256
  `17C9032B35CF6E24751BBC2FBDEC21F8B93F3A16845B2D1E76CA5321CDE90CD9`;
- tray: SHA-256 `AFB805C18B5AADDAA8BCD90CAC7B2EF7C93394457E96ECD531E18A89621EC8DE`;
- system router: SHA-256
  `EF2A0C84846FF5DEF344E7D99E1DEDDB9031EA86997C65915BBA3BE76FA05397`;
- все три installed hash-named executable совпадают с release source artifacts;
- SCM service: Automatic, LocalSystem; scheduled tray task: Highest/Interactive; console windows
  отсутствуют; dial fallback настроен на tunnel endpoint и внешний endpoint;
- exact-live canary: interactive high/session 1 и SYSTEM/session 0, оба elevated; UTF-8 process output
  точен, двусторонний file import/edit/export сохранил имена, размер и SHA-256; canary-файлы удалены;
- live bridge и server code подтверждены exact `905e09d`; canary script SHA-256
  `341ef0d91bfe416a11eb10d68484090893af93a7f30c520930f4c81cfaeddf99`;
- более ранний изолированный scroll-canary подтвердил `0 -> 18` и window PNG `760x520`, но после
  exact-final reinstall desktop был non-input/locked (`foreground=null`, `cursor=null`). Поэтому
  scroll на exact-final binary честно остаётся pending до активной desktop session.

## Провенанс и открытые ограничения

- Содержимое старого дневника не использовалось как ориентир этой постройки. Старые
  `memory/people/*` могут содержать загрязнённые выводы и ждут owner-led correction; они не были
  молча переписаны и исключены из broad automatic recall.
- Старые root Markdown удалены из текущего дерева, а не переписаны задним числом. Живые
  документы дерева: `README.md`, `ARCHITECTURE.md`, `CODEMAP.md`, `STATUS.md`, `VISION.md`,
  `AGENTS.md`. ⚠ Прежде эта строка называла их «каноническими», и `STATUS.md` тем самым
  объявлял каноном сам себя. Ни один из них не собирается кодом и не сверяется автоматически —
  все шесть отстают молча. Канон о развёрнутом состоянии — git на `/opt/praxis`; канон о её
  ограничениях — `rails.registry()`; канон о её возможностях — `capabilities.snapshot()`.
- Orphan `roomgate`/`probe`, старые env/memory backups, exact-phrase PASS 8 soul test и tracked
  personality archive убраны из текущего дерева; provenance остаётся в Git. Приватные dialogues,
  access trust и goals сняты с source tracking, но сохранены как live runtime data.
- Device bootstrap использует owner-issued one-time enrollment и scoped bearer, а не mTLS. Это
  текущий реализованный контракт, не заявление о криптографической привязке к hardware.
- PASS 24 server/PWA/Telegram и Windows bridge развёрнуты на exact `905e09d`. PWA live canary
  подтвердил one-use enrollment, second-use reject, exact scopes, одноразовые event/artifact tickets,
  file import replay contract и немедленную 401 после revoke; isolated ledger удалён. Canary script
  SHA-256 `c30336dd431f185a20ea04cea422484971ece8850529aa64fce4223a3780f730`.
- Карты `memory/INDEX.md`, PEOPLE/ROOMS/PROJECTS/THREADS/RUNS/COMPUTERS и
  `memory/computer/MAP.md` пересобраны из live-канона; FTS содержит 10 810 chunks из 351 source.
  Private review archive находится только локально вне Git/cloud:
  `praxis-memory-review-905e09d-20260714T154136Z.tar.gz`, 781 388 bytes, 342 members / 339 source
  files, SHA-256 `c3cf4ba11c91c38dd6df4f54029b236a39f9d55c5afaba382409a306a7ccd8b5`.
  SQLite/FTS не включён: это rebuildable index, не каноническая память.
- Real-browser visual/install/offline gate сейчас недоступен: browser runtime не поднят. Статические
  и HTTP-контракты проверяются, но успешная установка и визуальная проверка в настоящем браузере не
  объявлены завершёнными.
- PWA server-operation claim имеет durable heartbeat/lease и никогда не повторяет callback после
  смерти или неоднозначности executor. Отдельный основной durable-run executor пока не имеет
  универсальной автоматической orphan recovery после смерти процесса; это остаётся release risk
  долгих runs вне server-operation контура.
- Не все старые ad-hoc owner notifications переведены в новый delivery ledger. Логи Windows ещё без
  rotation. Same-key browser export сериализован durable run ledger и даёт один artifact/ticket, но
  физический cold reboot после текущей установки ещё не повторён.
- Автоматические PWA/Body canaries завершены. Остаётся один человеческий owner canary: голосом
  попросить Praxis прислать реальный файл и проверить видимое имя, topic/reply route, SHA/размер и
  durable delivery receipt.
