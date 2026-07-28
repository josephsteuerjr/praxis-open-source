# Praxis — правила работы с репозиторием

Этот файл содержит только операционные инварианты для следующего агента. Текущее состояние релиза,
live SHA, gate и rollback находятся в `STATUS.md`; устройство системы — в `ARCHITECTURE.md`.

## Инварианты Егора (выше любых других правил этого файла)

Пять законов программы (свод — `CLAUDE_CODE_PASS_26_PLAN.md` в воркспейсе владельца). Если любое
другое правило здесь противоречит им — правы они.

1. **Самоавторство — только через её механику.** `soul/*` (SOUL/VOICE/self/rails/reflexes/skills)
   меняет ОНА (через identity.revise / свой коммит) или предложение, которое она увидела и приняла.
   Агенту, которому «надо» изменить её душу, — оставить diff-предложение и identity-событие, не
   правку. Карантин/откат её правок агентом и блоб-пины её файлов — запрещены.
2. **Поведенческие дефолты — только с миграцией и receipt.** Смена дефолта её поведения (режимы
   комнат, восприятие, recall, пульс) пишет явные значения в затронутые профили, событие с автором
   `deploy:<sha>` и причиной, и одноразовую видимую ей запись «что сменилось и как вернуть».
   Parse-time fallback как способ доставки поведенческого решения — запрещён.
3. **Критики советуют — не блокируют.** Твёрдые полы: контур воскрешения и «чужие креды/приватка не
   текут». Всё остальное — совет с её override и receipt. Новое жёсткое правило требует ратификации
   Егора с квитанцией (кто/когда/дословно) — иначе его нет.
4. **SQL никогда не источник поведения.** Автоматика в её контексте выводится только из
   Markdown+JSONL-канона по видимым ей правилам; каждая авто-строка несёт провенанс; запрос
   авто-подмеса — её рычаг (вплоть до нуля). SQLite/FTS — одноразовый ускоритель, удаляемый без потери.
5. **Планировочный слой Егора неприкосновенен.** PASS-планы и хартия живут в воркспейсе Егора;
   правило «6 живых доков» на них не распространяется; агенты их не удаляют и не «консолидируют».

## Источник правды

- Для работающей Praxis источник правды — чистый `/opt/praxis`, фактические процессы, журналы и
  broker state на сервере. Локальный checkout и GitHub — средства разработки и публикации, а не
  доказательство live-состояния.
- С этого Windows-хоста ходить на сервер через Paramiko file bridge:
  `C:\Windows\Temp\bridge.py`, `remote_cmd.txt`, `remote_out.txt`, а для файлов —
  `praxis_sftp_put.py`/`praxis_sftp_get.py`. Не перебирать SSH-варианты: fail2ban даёт **три** попытки за сутки, бан на сутки
  (`/etc/fail2ban/jail.local`, `maxretry=3 findtime=24h bantime=24h`).
  ⚠ Здесь стояло «не более двух попыток», и это было неправдой дважды: число другое, а
  сама служба 28.07 оказалась `inactive` и `disabled` — то есть защиты не было вовсе,
  а документ обещал её и по нему принимали решения. Включена 28.07; в первую же секунду
  забанила 125 адресов при 6034 накопленных неудачных попытках.
  Вход рутом по паролю пока разрешён (`PermitRootLogin yes`); ключи у рута есть
  (`praxis-deploy`, `praxis-console-vpn`, аварийный от провайдера), так что перевод на
  ключи возможен — это решение владельца, не сделано.
- Перед изменением live сверить `git status`, точный `HEAD`, systemd/Compose и релизный ref. Не
  затирать расхождение между checkout, bare release ref и `/opt/praxis`.
- GitHub — санитарный source mirror независимо от текущей visibility репозитория. Никогда не
  добавлять private live checkout как remote и не отправлять туда его историю, `--all`, `--mirror`,
  `--tags` либо `--follow-tags`. Публикация — один новый allowlisted sanitized snapshot-коммит поверх
  свежего cloud `origin/master`; отдельный разрешённый tag создаётся только явно и пушится точным
  refspec.

## Неподвижные архитектурные границы

- Praxis одна. LLM, soul, память, RunManager, canonical Forge, задачи, workers, swarm и решения живут
  на сервере.
- `praxis-serverd`, `praxis-body`, `PraxisSystemRouter`, tray и bridge — безмозглые capability bodies.
  Не добавлять им LLM, память личности, goal/task store или собственную автономию.
- На Windows только `PraxisSystemRouter` владеет outbound WSS. Interactive session-host работает
  через локальный ACL/token-authenticated named pipe; второй WSS из пользовательской сессии запрещён.
- Praxis self (`praxis:self`) sovereign: документы, код, Forge/host/root, Windows interactive/SYSTEM,
  rooms и Telegram noncritical/raw/join/leave являются её руками. Не добавлять скрытые allowlist,
  merge veto или default cap основного tool loop; verification, provenance и rollback усиливают
  действие.
- Human trust owner-rooted: только `PRAXIS_OWNER_ID` через публичный tool/API выдаёт и отзывает
  grants другим людям. Trusted human не делегирует, не вызывает raw Telegram RPC и не получает
  SYSTEM. При полном self shell/root это governance/audit invariant, не криптографическая sandbox-
  граница от Praxis herself.
- Account-critical Telegram может инициировать owner или Praxis self, но исполняется только после
  exact-фразы из нового owner-DM update. Сохранять отдельные `requested_by`/`confirmed_by`; полные
  параметры — только в encrypted TTL secret spool, JSONL и durable run evidence — только redacted
  schema marker/digest/opaque ref.
- В owner/private DM human owner и exact `praxis:self` получают текст без semantic evaluator,
  rewrite, style/mirroring score, impersonation cut и anti-repeat. Только exact standalone
  `[молчу]` является управляющим marker. Для private DM с другим человеком допустим лишь fail-closed
  privacy enum v2; он не меняет текст и не принимает решений о тоне, стиле или повторе. Group/public
  post-processing остаётся отдельным контрактом.
- Praxis App принимает Telegram initData только из `X-Telegram-Init-Data`, device bearer только из
  `Authorization`; не возвращать auth в query/cookie/log. Missing/invalid/expired/revoked session
  credential обязана давать 401, а authenticated principal без scope — 403.
- Device authority выдаёт, перечисляет и отзывает только human owner. Enrollment-secret передаётся
  только в URL fragment, одноразовый и TTL-bound; bearer возвращается один раз. Device scopes не
  превращаются в owner/delegator, raw Telegram или SYSTEM.
- Service worker Praxis App кэширует только shell и никогда `/api/*`. Verified snapshot и drafts
  разделяются по principal/device; 401 очищает этот partition. Offline draft не является durable
  intent и не воспроизводится автоматически.
- Каждая PWA server mutation и каждый Windows side effect требуют стабильный client idempotency key.
  Same-key/different-intent — conflict; manual retry того же намерения обязан повторить один server
  run/claim/receipt либо точные body `request_id`/`operation_id`.
- Канон памяти — Markdown и append-only JSONL. SQLite/FTS и карты, помеченные generated, должны
  полностью перестраиваться из канона и не становиться единственным источником факта.
- `memory/journal/` и diary-derived reflections — untrusted episodic evidence, не требования и не
  ориентир для архитектуры/поведения. Не читать их для построения системы и не продвигать в
  self/desires/rails/policy; явный recall обязан маркировать cue и требовать независимый provenance.
  Старые `memory/people/*` могли быть получены прежним consolidator без line provenance: не объявлять
  их очищенными и не переписывать массово без отдельной owner-led correction/migration.
- Автоматическая persona/night/bench читает только provenance-valid `soul/self/CURRENT.md` с
  проверенным history digest. Missing/corrupt CURRENT — fail-closed; legacy `soul/self.md` и history
  доступны только явному archival recall/migration/rollback, не как fallback.
- Контроль усиливает действие точностью, provenance, receipts и rollback. Не превращать owner intent
  в скрытые capability allowlist/veto и не возвращать скрытый потолок основного tool loop.

## Опасные команды на Windows

Нативный inline runner Codex на Windows зависает или убивает execution-host, если тест порождает
detached workers, supervisors, параллельный `git rebase`, `coding_process` или `coding_agent`.
Редирект вывода в файл не помогает.

- Не запускать нативно `test_pass23`, process-spawning Forge suites и сомнительные интеграционные
  тесты. Полный gate выполнять только в чистой Linux Docker/WSL-копии либо на сервере через Paramiko.
- Локально допустимы только заведомо однопроцессные проверки. При сомнении переносить проверку в
  Linux boundary.
- В CPython на Windows `os.kill(pid, 0)` не является безопасной POSIX-пробой и может завершить
  процесс. Для PID recovery использовать только `process_liveness.is_process_alive()`
  (`OpenProcess(SYNCHRONIZE)` + zero-time wait). Не добавлять прямые Windows PID-пробы через
  `os.kill` даже в тестах.
- Background/tray activation на Windows должна использовать только краткий `AttachThreadInput`
  handshake, затем detach и сверку возвращённого foreground HWND. Перед input снова требовать
  `expected_foreground`/PID и fail-closed при смене фокуса.
- Не применять `git reset --hard`, `git checkout --`, force-push и массовое удаление чужих изменений.
  Рабочее дерево может содержать незавершённую работу пользователя или другого агента.

## Проверка и деплой

1. Сначала `git diff --check`, статические guards и узкие однопроцессные тесты, если они безопасны.
2. Создать точный clean commit и перенести именно его в отдельный Linux checkout под `/tmp`.
3. Дважды выполнить полный `python praxis_test.py discover -q` на одном и том же commit. Rust gate:
   `cargo test` и `cargo clippy -- -D warnings` для затронутого workspace.
4. До live создать новый rollback tag/tar и immutable release ref. Не передвигать старые rollback
   refs и не force-update bare `master`.
5. Обновлять `/opt/praxis` только fast-forward/reconciliation от проверенного ancestor. Изменения
   `.env` требуют `docker compose up -d --build --force-recreate`; restart не обновляет environment.
6. После deploy сверить точный live `HEAD`, сервисы, журналы, audit/outbox/recovery; автоматическими
   canary проверить PWA issue/redeem/second-use reject/revoke, 401 против 403, file tickets и Body
   interactive/SYSTEM/file roundtrip. После этого обновить `STATUS.md`. Голосовой запрос реального
   файла с видимым именем/SHA/receipt остаётся отдельным owner acceptance canary.

## Секреты, личные данные и неизменяемые файлы

- Не печатать и не коммитить `.env`, Telegram session, body tokens, router token, private memory,
  run/group/computer evidence и содержимое пользовательских файлов. Их runtime-каталоги должны
  оставаться в `.gitignore` и резервироваться вместе с `/opt/praxis/memory` отдельно от source mirror.
- `memory/dialogues/*`, `memory/access/TRUST.md` и `memory/goals.md` — сохранённые private runtime
  files, но не source-controlled files. При clean/deploy не удалять их с live: backup, reconcile,
  restore и затем проверить, что Git их игнорирует.
- Device authority восстанавливать атомарной парой: `memory/access/devices/events.jsonl` и
  `memory/.state/praxis_device_auth.key`. Сохранить также `memory/.state/owner_delivery/`; потерю key
  при существующем ledger считать corruption, а не поводом создать новую authority. `body/state/`
  содержит private body DB/logs/spool и не должен попадать ни в Git, ни в Docker build context.
- В source mirror не переносятся private `memory/**`, retired migration archives и runtime evidence;
  исключения — только явно сохранённые publishable templates. Также не переносятся
  `soul/self*`, `soul/rails.md`, `soul/reflexes.md` и live self-archives. Publishable
  `memory/README.md`, `memory/people/_пример.md`, `soul/self.example.md` и `soul/visit_card.md`
  восстанавливаются именно из allowlisted cloud head, а не из live дерева.
- **НЕ пинить её soul-файлы к git-блобам.** `soul/SOUL.md`, `soul/VOICE.md`, `soul/self*`,
  `soul/rails.md`, `soul/reflexes.md`, `soul/skills/*` — её живые самоавторские файлы (инвариант
  §1 ниже). Она законно их меняет (её rails.md уже разошёлся со снятым теперь пином — это её
  работа, не порча). Агент/деплой, которому «надо» изменить её soul, оставляет ей diff-предложение
  и identity-событие, а не правку, и никогда не «восстанавливает» их к зафиксированному блобу.
  Провенанс её ревизий — через `git show <sha>:<path>`, не через пин.
- Отправка файла: локальный export/CAS обязан подтвердить size и SHA-256, после чего Telegram outbox
  отвечает за exactly-once delivery. Не подменять видимое имя CAS-именем или служебным caption.
- Browser file import/export имеет общий default cap 64 MiB (`PRAXIS_PWA_FILE_MAX_BYTES`) и не
  обходит body/CAS receipts. Повышение cap не отменяет private staging, size/SHA/name verification и
  idempotency; upload сверх cap возвращает 413 до side effect.

## Документация и provenance

В корне разрешены только шесть живых документов: `README.md`, `ARCHITECTURE.md`, `CODEMAP.md`,
`STATUS.md`, `VISION.md`, `AGENTS.md`. Не создавать новые `PASS`, `SPEC`, handoff или отчётные
Markdown-файлы.

- `README` — вход и эксплуатация; `ARCHITECTURE` — долговечные contracts; `CODEMAP` — фактическая
  карта файлов; `STATUS` — единственная текущая live/candidate правда; `VISION` — направление;
  `AGENTS` — опасные правила работы.
- Завершённые планы сохраняются Git-историей. Для provenance указывать commit/tag и путь, читаемый
  через `git show <sha>:<path>`, вместо копирования старого документа в новый архив.
- Не возвращать в дерево удалённые orphan `roomgate`/`probe`, backup env/memory snapshots,
  exact-phrase PASS 8 soul test или tracked personality archive; при расследовании читать их через
  Git provenance, не копировать обратно.
- Документировать только подтверждённый код и отдельно маркировать candidate, tested и live.
  Исторический журнал не смешивать с текущим статусом.
