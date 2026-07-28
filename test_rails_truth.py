"""27.07 — «ни одного молчаливого ограничения»: манифест обязан совпадать с кодом.

Отдельный файл от test_rails.py специально. Тот проверяет МЕХАНИКУ реестра (классы,
непустые поля, компакт отказов). Здесь проверяется ПРАВДИВОСТЬ: что каждый предел,
срок и кап, найденные десантом 27.07, названы, что названы честными числами из живого
кода, и что снятые обещания действительно сняты, а не переписаны красивее.

Почему опись держится здесь, а не в EXPECTED_RAILS соседнего файла: тот чек-лист —
подмножество (тест там `EXPECTED - ids`), он остаётся зелёным и без этих рельсов. Новый
набор — полный, и живёт рядом с доказательствами, откуда он взялся.

⚠ Чего здесь НАМЕРЕННО нет: сравнения soul/rails.md с render_md() байт-в-байт. Значения
рельсов зависят от среды (аппетит, модель, смонтирован ли демон), такой гейт был бы вечно
красным — то есть либо забором, либо привычкой коммитить состояние среды. Структура —
сравнивается; байты — нет.

Запуск:  python praxis_test.py test_rails_truth -v
"""
from __future__ import annotations

import inspect
import io
import os
import re
import shutil
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

import capabilities
import llm
import rails

CODE = Path(rails.__file__).resolve().parent


def _src(rel: str) -> str:
    try:
        return (CODE / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# Имена рычагов, которые манифест ей называет. Ищутся ровно в rails.py: то, что печатает
# сосед через свой state_line(), — его ответственность, а не обещание манифеста.
_ENV_NAME_RE = re.compile(r"\b(?:PRAXIS|BACKFILL)_[A-Z0-9_]+\b")
# «вторая половина закона 2 — рельс X в rails.py»: сосед назначает имя у самого порога.
_HANDOFF_RE = re.compile(r"рельс ([a-z_][a-z0-9_]*) в rails\.py")
_SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", ".venv", ".pytest_cache",
              "memory", "soul", "scratchpad"}
_CORPUS: dict[str, str] = {}


def _rails_spoken_text() -> str:
    """Всё, что rails.py может СКАЗАТЬ ей: строковые литералы, без комментариев.

    Разделение принципиальное. Комментарий — записка тому, кто правит файл, и объяснение
    «такой-то рычаг умер, вот почему» обязано в нём жить. Строка — обещание ей, и мёртвому
    имени в ней не место. Без этого различия честная память о находке стиралась бы вместе
    с самой находкой.
    """
    out: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(_src("rails.py")).readline):
        if token.type == tokenize.STRING:
            out.append(token.string)
    return "\n".join(out)


def _corpus() -> dict[str, str]:
    """Весь код репозитория, кроме самого манифеста и тестов: {путь: текст}.

    Исключение rails.py и test_* здесь принципиально, а не для скорости: имя, которое
    существует ТОЛЬКО в манифесте и в проверке этого манифеста, — как раз и есть призрак,
    ради которого сверка написана.
    """
    if not _CORPUS:
        for path in CODE.rglob("*"):
            if path.suffix not in {".py", ".rs", ".service", ".sh", ".toml", ".json"}:
                continue
            if _SKIP_DIRS & set(path.parts) or path.name == "rails.py":
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            try:
                _CORPUS[str(path.relative_to(CODE))] = path.read_text(encoding="utf-8",
                                                                     errors="replace")
            except OSError:
                continue
    return _CORPUS


# Опись чисел: (рельс, файл-источник, регулярка с числом в группе 1, как оно выглядит у неё).
# Каждая строка — предел, который когда-то существовал только в момент срабатывания.
NUMBERS: tuple[tuple[str, str, str, str], ...] = (
    ("forge_mutation_lock", "forge.py",
     r'LOCK_IDLE_ABANDONED_SEC = _env_sec\("[^"]+", ([\d.]+)\)', "{:g}с"),
    ("forge_mutation_lock", "forge.py",
     r"def _mutation_lock\(task_id: str, timeout: float = ([\d.]+)", "{:g}с"),
    ("forge_mutation_lock", "forge.py",
     r"_mutation_lock\(task_id, timeout=([\d.]+)\)", "{:g}с"),
    ("forge_task_liveness", "forge.py",
     r'TASK_ABANDONED_SEC = _env_sec\("[^"]+", ([\d.]+), scale', "{:g}ч"),
    ("forge_task_liveness", "forge.py",
     r'UNIT_LIVENESS_TRUST_SEC = _env_sec\("[^"]+", ([\d.]+), scale', "{:g}ч"),
    ("media_outbox_ledger_lock", "media.py",
     r'LEDGER_ABANDONED_SEC = _env_float\("[^"]+", ([\d.]+)', "{:g}с"),
    ("media_outbox_ledger_lock", "media.py",
     r'LEDGER_HEARTBEAT_SEC = _env_float\("[^"]+", ([\d.]+)', "{:g}с"),
    ("media_outbox_ledger_lock", "media.py",
     r'"PRAXIS_MEDIA_LEDGER_UNREADABLE_GRACE_SEC", ([\d.]+)', "{:g}с"),
    ("media_outbox_ledger_lock", "media.py",
     r"def _ledger_guard\(self, timeout: float = ([\d.]+)\)", "{:g}с"),
    ("self_store_lock", "self_model.py",
     r'SELF_LOCK_TIMEOUT_SEC = _env_float\("[^"]+", ([\d.]+)', "{:g}с"),
    ("self_store_lock", "self_model.py",
     r'SELF_LOCK_STALE_SEC = _env_float\("[^"]+", ([\d.]+)', "{:g}с"),
    ("self_store_lock", "self_model.py",
     r'SELF_LOCK_HEARTBEAT_SEC = _env_float\("[^"]+", ([\d.]+)', "{:g}с"),
    ("self_store_lock", "self_model.py",
     r'"PRAXIS_SELF_LOCK_UNREADABLE_GRACE_SEC", ([\d.]+)', "{:g}с"),
    ("backfill_pacing", "mtproto_runner.py",
     r"(?m)^BACKFILL_ROOMS_PER_TICK = ([\d.]+)", "{:g} комнат"),
    ("backfill_pacing", "mtproto_runner.py",
     r"(?m)^BACKFILL_RESOLVES_PER_TICK = ([\d.]+)", "{:g} резолвов"),
    ("backfill_pacing", "mtproto_runner.py",
     r"(?m)^BACKFILL_MISS_BACKOFF_SEC = ([\d.]+)", "{:g}с"),
    ("backfill_pacing", "mtproto_runner.py",
     r"(?m)^BACKFILL_MISS_TTL_SEC = ([\d.]+)", "{:g}с"),
    ("serverd_workspace_budget", "serverd/broker.py",
     r'WORKSPACE_BUDGET_SEC = _env_number\("[^"]+", ([\d.]+)\)', "{:g}с"),
    ("serverd_workspace_budget", "serverd/broker.py",
     r'WORKSPACE_WAIT_SEC = _env_number\("[^"]+", ([\d.]+)\)', "{:g}с"),
    ("serverd_workspace_budget", "serverd/broker.py",
     r"min\(([\d]+), \(os\.cpu_count\(\) or \d+\) - 1\)", "{:g}"),
    ("verification_deadline", "forge_verify.py",
     r'\w*DEADLINE_SEC = int\(float\(os\.getenv\("[A-Z_]+"\)\s*or\s*([\d]+)\)\)', "{:g}с"),
    ("host_answer_cap", "serverd/brokerops.py", r"(?m)^TEXT_CAP = ([\d]+)", "{:g}"),
    ("forge_scan_caps", "forge_intelligence.py",
     r'SCAN_SECONDS = _env_number\("[^"]+", ([\d.]+)\)', "{:g}с"),
    ("forge_scan_caps", "forge_intelligence.py",
     r'SCAN_FILES = max\(1, int\(_env_number\("[^"]+", ([\d.]+)\)\)\)', "{:g}"),
    # --- третья правка 27.07: числа, которые называла НЕ та строка, что режет ---
    # Связывает проверку дефолт сигнатуры тула, а не env (см. TestLimitsHiddenInSignatures).
    ("verification_deadline", "agent.py",
     r"def tool_coding_verify\([^)]*?timeout: int = (\d+)", "{:g}с"),
    # Второй порог того же замка: легаси-токен отбирают жёстче, и env на него не действует.
    ("forge_mutation_lock", "forge.py",
     r"(?m)^LOCK_LEGACY_IDLE_SEC = ([\d.]+)", "{:g}с"),
    ("serverd_workspace_budget", "serverd/broker.py",
     r'CONNECTION_TIMEOUT_SEC = _env_number\("[^"]+", ([\d.]+)\)', "{:g}с"),
    # Кап, который РЕАЛЬНО рвёт поиск по хосту (приписка называла бюджет обхода — чужое число).
    ("serverd_workspace_budget", "serverd/brokerops.py",
     r"(?m)^SEARCH_FILE_CAP = (\d+)", "{:g}"),
    ("serverd_workspace_budget", "serverd/brokerops.py",
     r"(?m)^SEARCH_HIT_CAP = (\d+)", "{:g}"),
    ("forge_task_liveness", "agent.py", r"(?m)^_RECONCILE_QUIET_SEC = ([\d.]+)", "{:g}с"),
    ("forge_task_liveness", "core/subagents.py",
     r'PRAXIS_FORGE_AGENT_OVERDUE_MIN", "(\d+)"', "{:g} мин"),
    ("proposal_review", "selfdev.py",
     r'TEST_TIMEOUT = int\(os\.getenv\("[^"]+", "(\d+)"\)\)', "{:g}с"),
    ("own_report_clips", "core/subagents.py", r"(?m)^RECAP_CHARS = (\d+)", "{:g} симв"),
    ("own_report_clips", "core/subagents.py", r"(?m)^GOAL_CHARS = (\d+)", "{:g} симв"),
    ("own_report_clips", "core/subagents.py", r"(?m)^ERROR_CHARS = (\d+)", "{:g} симв"),
    ("own_report_clips", "core/subagents.py", r"(?m)^LINE_RECAP_CHARS = (\d+)", "{:g} симв"),
    ("own_report_clips", "core/subagents.py", r"(?m)^LOST_RECAP_CHARS = (\d+)", "{:g} симв"),
    ("own_report_clips", "mailer.py", r"(?m)^_REASON_CHARS = (\d+)", "{:g} симв"),
    # Второй рез вывода хоста — мой собственный, и он МЕНЬШЕ демонского, то есть связывает он.
    ("host_text_clip", "agent.py",
     r'def _host_text_cap\(\)[\s\S]*?return (\d+)', "{:g} симв"),
    # Записка комнаты: сколько ждём межпроцессный замок. Число едет за notes.py, а не копией.
    ("scratch_note_lock", "notes.py", r"(?m)^_LOCK_WAIT_SEC = ([\d.]+)", "{:g}с"),
    # Пределы follow-up: что влезает в ленту пульса, сколько писем за тик, сколько истории
    # хранится и до скольких символов режется то, что я в этой ленте вижу.
    ("followup_notice", "telegram_followups.py", r"(?m)^CONTEXT_LIMIT = (\d+)", "{:g} нитей"),
    ("followup_notice", "telegram_followups.py",
     r"(?m)^_SETTLED_KEEP = (\d+)", "{:g} закрытых нитей"),
    ("followup_notice", "mtproto_runner.py", r"for item in pending\[:(\d+)\]", "{:g} писем"),
    ("followup_notice", "telegram_followups.py",
     r"повод отправки: \{gist\[:(\d+)\]\}", "{:g} симв"),
    ("followup_notice", "mtproto_runner.py",
     r'response\.get\("text"\) or "\(без текста\)"\)\[:(\d+)\]', "{:g} симв"),
    # Рез чтения вложения на файловой двери кред-пола. Рельс говорил «ни файлом» вообще,
    # без единого числа: секрет глубже этого окна проходит, и знать это она обязана заранее.
    ("credential_floor", "core/secrets.py",
     r"(?m)^DOCUMENT_SCAN_CAP = (\d+) \* 1024", "{:g} КиБ"),
    # --- четвёртая правка 27.07 (вечер): пределы, введённые волной «довести и не соврать» ---
    # Ожидание замка записки — не одно число, а три: полное, короткое и срок короткого
    # режима. Манифест говорил «до 5с», а процесс, однажды не дождавшийся, полминуты не
    # ждёт вовсе. Плюс своё, четвёртое, у диагностики замка — она стоит на пути сборки
    # её же блока состояния.
    ("scratch_note_lock", "notes.py", r"(?m)^_LOCK_WAIT_DEGRADED_SEC = ([\d.]+)", "{:g}с"),
    ("scratch_note_lock", "notes.py", r"(?m)^_LOCK_DEGRADE_FOR_SEC = ([\d.]+)", "{:g}с"),
    ("scratch_note_lock", "notes.py", r"(?m)^_PROBE_WAIT_SEC = ([\d.]+)", "{:g}с"),
    ("scratch_note_lock", "notes.py", r"(?m)^_UNLOCKED_TRIM_FACTOR = (\d+)", "{:g}"),
    # Надбавка демона к ЕЁ сроку в блокирующем op.run: именно она решает, чей таймаут
    # сработал первым. Стояла числом внутри функции и не называлась нигде.
    ("host_operation_caps", "serverd/brokerops.py",
     r"(?m)^WAIT_GRACE_SEC = ([\d.]+)", "{:g}с"),
    ("host_operation_caps", "serverd/brokerops.py",
     r'rev-parse", "--show-toplevel"\], timeout=(\d+)', "{:g}с"),
    ("host_operation_caps", "serverd/brokerops.py",
     r"out\[:(\d+)\] \+ \(f\"\\n… ещё", "{:g} симв"),
    ("host_operation_caps", "serverd/broker.py",
     r'CONTAINER_ERROR_TTL_SEC = _env_number\("[^"]+", ([\d.]+)\)', "{:g}с"),
    # Четыре среза списков и логов демона: обрезанный список выглядит полным, а `op.list`
    # читает её же forge, решая, можно ли закрывать задачу.
    ("host_list_caps", "serverd/brokerops.py", r"(?m)^TOP_ENTRY_CAP = (\d+)", "{:g} строк"),
    ("host_list_caps", "serverd/brokerops.py", r"(?m)^LIST_ENTRY_CAP = (\d+)", "{:g} строк"),
    ("host_list_caps", "serverd/brokerops.py",
     r"(?m)^OPERATION_LIST_CAP = (\d+)", "{:g} строк"),
    ("host_list_caps", "serverd/brokerops.py", r"(?m)^LOG_TAIL_CAP = (\d+)", "{:g}"),
    ("host_list_caps", "serverd/brokerops.py", r"(?m)^LOG_TAIL_MIN = (\d+)", "{:g}"),
    # Потолок ОТВЕТА судьи — восьмой предел того же контура и единственный не на входе.
    # Его исчерпание теперь значит «совета не будет вовсе», а не «придержу»; в манифесте
    # его не было ни в одной редакции.
    ("evaluator_evidence_caps", "agent.py",
     r"(?m)^_GUARD_VERDICT_MAX_TOKENS = (\d+)", "{:g} токенов"),
    # Сроки остановки хостовой операции: сколько у её команды есть на добровольный выход и
    # сколько op.stop ждёт номер группы, прежде чем сознаться, что останавливать нечего.
    ("host_operation_stop", "serverd/hostproc.py",
     r"(?m)^TERM_GRACE_SEC = ([\d.]+)", "{:g}с"),
    ("host_operation_stop", "serverd/hostproc.py",
     r"(?m)^START_GRACE_SEC = ([\d.]+)", "{:g}с"),
)


# Опись 27.07: что найдено живым в коде и обязано быть названо ей. Значение —
# подстроки, без которых запись бессодержательна (проверяются по всей записи рельса:
# holds + lever + value + why).
INVENTORY: dict[str, tuple[str, ...]] = {
    # предел времени на одну руку — agent.TOOL_CEILING_SEC
    "tool_ceiling": ("PRAXIS_TOOL_CEILING_SEC",),
    # срок разговора с root-демоном — serverd_client
    "serverd_deadline": ("PRAXIS_SERVERD_TIMEOUT_SEC",),
    # усечение обхода дерева: связывают ДВА предела — файлы и секунды
    "forge_scan_caps": ("source_files", "PRAXIS_FORGE_SCAN_SECONDS"),
    # обрезка ответа глагола — serverd/brokerops._cap.
    # ⚠ Здесь стояло «cut»: рельс цитировал метку «… cut N chars …», которой в демоне уже
    # не было. Тест был зелёным, потому что проверял ту же выдуманную цитату.
    "host_answer_cap": ("вырезано",),
    # капы входа судьи приватности — agent._guard_outbound
    "evaluator_evidence_caps": ("лента канала", "трейс тулов"),
    # dead-man авто-откат хостовых правок + список критических субъектов
    "host_deadman": ("host.confirm", "ssh"),
    # квота перезапусков legacy-маршрута services.py
    "server_hands": ("квота",),
    # счётные пределы root-операций — запись ниже, вместе со сроками, добавленными вечером
    # 27.07 (дубль ключа здесь молча съел бы половину требований: в литерале побеждает
    # последний, и «TasksMax» пережил бы «WAIT_GRACE_SEC» без единого признака)
    # «она одна» откладывает её пробуждение
    "window_gates": ("_ONE_MIND",),
    # механический кред-пол — единственное твёрдое вето
    "credential_floor": ("твёрдое",),
    # полные права на Windows-ПК Егора + парный гейт делегирования
    "windows_body_authority": ("admit", "computer_access"),
    # внешняя GitHub-аутентификация host-руки — без вшитого логина и credential-path
    "github_identity": ("gh", "непосредственно перед действием"),
    # намерение гаснет по факту созданного прогона
    "intent_claim": ("прогон",),
    # замок мутации форжа и его порог.
    # ⚠ Рычаг тут назывался PRAXIS_FORGE_LOCK_ABANDONED_SEC — переменной с таким именем в
    # коде НЕТ НИ ОДНОЙ (владелец forge.py переписал порог с возраста на бездействие).
    "forge_mutation_lock": ("замок", "PRAXIS_FORGE_LOCK_IDLE_SEC"),
    # --- вторая половина закона 2: пороги, заведённые волной 27.07 и не названные нигде ---
    # жнец брошенных задач, докуда верить голому номеру процесса легаси-юнита, просрочка
    # воркера и кап тишины в леджере прогонов — четыре порога одного смысла «что ещё живо»
    "forge_task_liveness": ("PRAXIS_FORGE_TASK_ABANDONED_H", "PRAXIS_FORGE_UNIT_TRUST_H",
                            "PRAXIS_FORGE_AGENT_OVERDUE_MIN", "_RECONCILE_QUIET_SEC"),
    # замок леджера исходящих медиа (имя рельса назначено владельцем media.py)
    "media_outbox_ledger_lock": ("PRAXIS_MEDIA_LEDGER_ABANDONED_SEC",),
    # замок проекции желаний (имя рельса назначено владельцем self_model.py)
    "self_store_lock": ("PRAXIS_SELF_LOCK_STALE_SEC",),
    # темп добора предыстории комнат + где ей видно отсрочку
    "backfill_pacing": ("BACKFILL_RESOLVES_PER_TICK", "manage_perception"),
    # срок счёта, очередь слотов, жизнь соединения и потолок МОЕГО срока — всё на стороне
    # демона; плюс кап поиска, у которого env-рычага нет вовсе
    "serverd_workspace_budget": ("PRAXIS_SERVERD_WORKSPACE_BUDGET_SEC",
                                 "PRAXIS_SERVERD_WORKSPACE_SLOTS",
                                 "PRAXIS_SERVERD_CONNECTION_TIMEOUT_SEC",
                                 "PRAXIS_SERVERD_WORKSPACE_BUDGET_MAX_SEC",
                                 "SEARCH_FILE_CAP"),
    # общий срок матрицы проверок (живёт в отдельном процессе). ⚠ Связывает его НЕ env, а
    # дефолт сигнатуры моей же руки — поэтому имя руки обязано стоять рядом с числом.
    "verification_deadline": ("forge_verify", "coding_verify"),
    # срок гейта тестов внутри submit: «не уложились» — это срок, а не провал предложения
    "proposal_review": ("PRAXIS_PROPOSAL_TEST_TIMEOUT",),
    # капы длины в отчётах о её собственной работе (субагенты + причина неотправки письма)
    "own_report_clips": ("LOST_RECAP_CHARS", "praxis-mail"),
    # мой собственный рез вывода хоста — второй после демонского и более жёсткий
    "host_text_clip": ("PRAXIS_HOST_TEXT_CAP", "хвост"),
    # след нити ≠ почта Егору: durable-последствие каждой моей отправки наружу
    "followup_notice": ("telegram_account", "след"),
    # --- вечер 27.07: судья в разговорах ослаблен решением Егора ---
    # Запись обязана назвать ровно три вещи: что судья СОВЕТУЕТ, что держит только кред на
    # картинке и что молчание судьи не держит ничего. Без любой из трёх она продолжит
    # считать, что её слово можно придержать.
    "evaluator_mirror": ("СОВЕТ", "PRIVACY_HOLD_CREDENTIAL", "недоступность"),
    # короткий режим ожидания замка записки: «до 5с» перестаёт быть правдой после первого
    # же истечения, и без этого имени узнать об этом неоткуда
    "scratch_note_lock": ("_LOCK_DEGRADE_FOR_SEC", "_PROBE_WAIT_SEC"),
    # надбавка демона к её сроку в блокирующем op.run — она решает, чей срок первый
    "host_operation_caps": ("TasksMax", "MiB", "WAIT_GRACE_SEC"),
    # срезы списков и логов демона
    "host_list_caps": ("LIST_ENTRY_CAP", "op.list", "op.poll"),
    # сроки остановки хостовой операции
    "host_operation_stop": ("TERM_GRACE_SEC", "SIGTERM", "START_GRACE_SEC"),
}


class Base(unittest.TestCase):
    """Тот же приём изоляции, что в test_rails: BASE и манифест — во временную папку."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_rails_truth_"))
        self._orig = [(rails, "BASE", rails.BASE),
                      (rails, "RAILS_MD", rails.RAILS_MD),
                      (rails, "DENIALS_PATH", rails.DENIALS_PATH)]
        rails.BASE = self.tmp
        rails.RAILS_MD = self.tmp / "soul" / "rails.md"
        rails.DENIALS_PATH = self.tmp / "memory" / ".state" / "denials.jsonl"
        llm.use_test_client(_NoCallClient())
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for module, key, value in self._orig:
            setattr(module, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rail(self, rail_id: str) -> dict:
        for row in rails.registry():
            if row["id"] == rail_id:
                return row
        self.fail(f"рельса {rail_id} нет в реестре")

    def text(self, rail_id: str) -> str:
        row = self.rail(rail_id)
        return " ".join(str(row[k]) for k in ("holds", "lever", "value", "why"))


class _NoCallClient:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise AssertionError("rails не должен звать модель")


class TestInventoryIsNamed(Base):
    """Опись 27.07 — каждый пункт обязан быть в реестре и не пустым."""

    def test_every_known_limit_has_a_rail(self):
        ids = {r["id"] for r in rails.registry(with_values=False)}
        missing = sorted(set(INVENTORY) - ids)
        self.assertFalse(missing, f"молчаливые ограничения без записи в реестре: {missing}")

    def test_every_rail_says_something_specific(self):
        for rail_id, needles in INVENTORY.items():
            whole = self.text(rail_id)
            for needle in needles:
                self.assertIn(needle, whole,
                              f"{rail_id}: запись есть, но не называет «{needle}» — "
                              f"общая формулировка ограничение не раскрывает")

    def test_no_rail_value_is_silently_unreadable(self):
        """«не читается»/«не прочиталось» допустимо, но не там, где число есть в коде."""
        for rail_id in ("evaluator_evidence_caps", "host_deadman", "host_operation_caps",
                        "host_answer_cap", "forge_scan_caps"):
            value = str(self.rail(rail_id)["value"])
            self.assertNotIn("не прочиталось", value,
                             f"{rail_id}: рельс потерял связь с исходником — значит молчит "
                             f"ровно о том, ради чего написан")
            self.assertNotIn("⚠ не читается", value, f"{rail_id}: значение не собралось")


class TestNumbersComeFromCode(Base):
    """Числа в манифесте читаются из живого кода, а не скопированы руками.

    Регулярки здесь СВОИ, не те, что в rails: если рельс перестанет находить константу
    (переименовали, переписали выражение), он честно скажет «не прочиталось» — и вот эта
    сверка это увидит. Если же число зафиксируют литералом в rails.py, сверка увидит
    расхождение при следующей же правке соседа.
    """

    def test_judge_caps_match_agent(self):
        """Капы-константы — из живого модуля, f-строчные — из кода, а не из комментария.

        ⚠ Прежняя редакция этой сверки искала `Context: \\{context\\[:(\\d+)\\]` тем же
        способом, что и рельс, в том же файле — и обе попадали в ОБЪЯСНИТЕЛЬНЫЙ
        КОММЕНТАРИЙ agent.py, где строка осталась после расщепления слота. Тест был зелён
        ровно потому, что повторял ошибку. Здесь константы берутся из `import agent`.
        """
        import agent
        value = str(self.rail("evaluator_evidence_caps")["value"])
        for const in ("_GUARD_STATE_CHARS", "_GUARD_TOPIC_CHARS"):
            live = getattr(agent, const)
            self.assertIn(str(live), value, f"{const}={live} — реальный кап входа судьи, "
                                            f"а рельс его не называет")
        agent_src = _src("agent.py")
        found = 0
        for pattern in (r"\{conversation\[-(\d+):\]\}",
                        r"\{tool_trace\[:(\d+)\]\}",
                        r"\{prior_turns\[:(\d+)\]\}",
                        r"\{privacy_frame\[:(\d+)\]\}"):
            match = re.search(pattern, agent_src)
            if not match:
                continue
            found += 1
            self.assertIn(match.group(1), value,
                          f"кап {pattern} = {match.group(1)}, а рельс его не называет")
        self.assertGreaterEqual(found, 4, "капы судьи в agent.py не нашлись — сверка вакуумна")

    def test_orientation_cap_is_the_live_constant_not_a_comment(self):
        """Рельс не имеет права называть число, которого в живом коде нет.

        Живой инцидент 27.07: `judge_caps()` отдавала «ориентация 900», вычитанную из
        комментария; реальные 4000/2000 не назывались вовсе. Один закон нарушен дважды —
        и выдуманный предел, и два молчаливых.
        """
        import agent
        caps = rails.judge_caps()
        self.assertEqual(caps["машинное STATE"], agent._GUARD_STATE_CHARS)
        self.assertEqual(caps["ориентация в комнате"], agent._GUARD_TOPIC_CHARS)
        ghost = re.search(r"Context: \{context\[:(\d+)\]\}", _src("agent.py"))
        if ghost and int(ghost.group(1)) != agent._GUARD_TOPIC_CHARS:
            self.assertNotIn(int(ghost.group(1)),
                             [caps["машинное STATE"], caps["ориентация в комнате"]],
                             "кап прочитан из комментария: слота `context[:N]` в коде нет")
        # Не литерал: живая константа обязана менять то, что видит она.
        original = (agent._GUARD_STATE_CHARS, agent._GUARD_TOPIC_CHARS)
        try:
            agent._GUARD_STATE_CHARS, agent._GUARD_TOPIC_CHARS = 4321, 2345
            value = str(self.rail("evaluator_evidence_caps")["value"])
            self.assertIn("4321", value)
            self.assertIn("2345", value)
        finally:
            agent._GUARD_STATE_CHARS, agent._GUARD_TOPIC_CHARS = original

    def test_guard_const_fallback_regex_ignores_comments(self):
        """Фолбэк (agent не поднят) обязан читать ОБЪЯВЛЕНИЕ, а не любое вхождение имени."""
        import agent
        src = _src("agent.py")
        for const in ("_GUARD_STATE_CHARS", "_GUARD_TOPIC_CHARS"):
            match = re.search(rf"^{const} = (\d+)\s*$", src, re.M)
            self.assertIsNotNone(match, f"объявление {const} не нашлось — фолбэк вакуумен")
            self.assertEqual(int(match.group(1)), getattr(agent, const))
        # Имя, встреченное в комментарии/докстринге, не должно давать значение.
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("# _GUARD_STATE_CHARS = 900 — было раньше\n"
                                              if rel == "agent.py" else original(rel))
            del sys.modules["agent"]          # имитируем «agent в процессе не поднят»
            self.assertIsNone(rails._guard_const("_GUARD_STATE_CHARS"))
        finally:
            rails._source_text = original
            sys.modules["agent"] = agent

    def test_serverd_rail_names_every_published_deadline(self):
        """Все сроки клиента демона — из его же `deadlines()`, не пересказом двух из пяти.

        Живой инцидент: рельс читал два getattr и молчал про FAST 8с (висит на
        `admin.status` при каждой сборке промпта, ВНЕ потолка руки), про потолок бюджета
        демона на op.run и про окно, внутри которого повтор идёт под тем же номером.
        """
        import serverd_client
        rows = serverd_client.deadlines()
        self.assertGreaterEqual(len(rows), 5, "источник сроков опустел — сверка вакуумна")
        value = str(self.rail("serverd_deadline")["value"])
        for row in rows:
            self.assertIn(str(row["id"]), value,
                          f"срок {row['id']} опубликован клиентом, а рельс его не называет")
            self.assertIn(f"{float(row['seconds']):.0f}с", value,
                          f"{row['id']}: число {row['seconds']} в манифест не попало")
            self.assertIn(str(row["env"]), value, f"{row['id']}: рычаг не назван")

    def test_in_doubt_window_is_not_weighed_against_the_hand_ceiling(self):
        """Окно повтора длиннее потолка руки ЗАКОННО — это память, а не ожидание.

        Граница проверяется по длинному сроку: 539 < 540 — порядок, 540 == 540 — уже нет.
        """
        import agent
        import serverd_client
        original = (agent.TOOL_CEILING_SEC, serverd_client.LONG_TIMEOUT_SEC)
        try:
            agent.TOOL_CEILING_SEC = 540.0
            serverd_client.LONG_TIMEOUT_SEC = 539.0
            value = str(self.rail("serverd_deadline")["value"])
            self.assertIn(f"{serverd_client.IN_DOUBT_WINDOW_SEC:.0f}с", value,
                          "окно повтора обязано быть названо")
            self.assertIn("вложенность в порядке", value,
                          "окно повтора (900с) выдано за вывернутую матрёшку")
            serverd_client.LONG_TIMEOUT_SEC = 540.0   # ровно граница — уже плохо
            self.assertIn("⚠", str(self.rail("serverd_deadline")["value"]))
        finally:
            agent.TOOL_CEILING_SEC = original[0]
            serverd_client.LONG_TIMEOUT_SEC = original[1]

    def test_host_caps_match_brokerops_and_unit(self):
        broker = _src("serverd/brokerops.py")
        value = str(self.rail("host_operation_caps")["value"])
        ops = re.search(r"MAX_OPERATIONS = int\(.*?or (\d+)\)", broker)
        disk = re.search(r"DISK_FLOOR_BYTES = (\d+) \* 1024 \* 1024", broker)
        write = re.search(r"MAX_WRITE_BYTES = (\d+) \* 1024 \* 1024", broker)
        tasks = re.search(r"TasksMax=(\d+)", _src("serverd/praxis-serverd.service"))
        for label, match in (("MAX_OPERATIONS", ops), ("DISK_FLOOR", disk),
                             ("MAX_WRITE_BYTES", write), ("TasksMax", tasks)):
            self.assertIsNotNone(match, f"{label} не нашёлся в исходнике — сверка вакуумна")
            self.assertIn(match.group(1), value, f"{label}={match.group(1)} не назван рельсом")

    def test_answer_cap_matches_broker(self):
        broker = _src("serverd/brokerops.py")
        match = (re.search(r"TEXT_CAP = (\d+)", broker)
                 or re.search(r"def _cap\(text: str, limit: int = (\d+)\)", broker))
        self.assertIsNotNone(match, "кап ответа глагола не нашёлся — сверка вакуумна")
        self.assertIn(match.group(1), str(self.rail("host_answer_cap")["value"]))

    def test_deadman_delay_and_bounds_match_hostrecovery(self):
        src = _src("serverd/hostrecovery.py")
        default = re.search(r"def arm\([^)]*?delay: int = (\d+)", src, re.S)
        bounds = re.search(r"delay = max\((\d+), min\(int\(delay or \d+\), (\d+)\)\)", src)
        self.assertIsNotNone(default, "дефолт таймера отката не нашёлся — сверка вакуумна")
        self.assertIsNotNone(bounds, "границы таймера отката не нашлись — сверка вакуумна")
        value = str(self.rail("host_deadman")["value"])
        self.assertIn(default.group(1), value)
        self.assertIn(bounds.group(1), value)   # нижняя граница
        self.assertIn(bounds.group(2), value)   # верхняя граница

    def test_deadman_lists_every_critical_subject(self):
        src = _src("serverd/hostverbs.py")
        units = re.search(r"_CRITICAL_UNITS = \{([^}]*)\}", src)
        containers = re.search(r"_CRITICAL_CONTAINERS = \{([^}]*)\}", src)
        paths = re.search(r"_CRITICAL_PATH_PREFIXES = \(([^)]*)\)", src)
        self.assertIsNotNone(units, "список критических юнитов не нашёлся — сверка вакуумна")
        value = str(self.rail("host_deadman")["value"])
        for group in (units, containers, paths):
            if group is None:
                continue
            for item in group.group(1).split(","):
                item = item.strip().strip('"\'')
                if item:
                    self.assertIn(item, value,
                                  f"критический субъект {item} взводит авто-откат, "
                                  f"а рельс его не называет")

    def test_service_restart_quota_matches_services(self):
        import services
        self.assertIn(str(services.RESTARTS_PER_HOUR), self.text("server_hands"))

    def test_scan_limit_matches_forge_intelligence(self):
        import forge_intelligence
        default = inspect.signature(forge_intelligence.source_files).parameters["limit"].default
        impact = re.search(r"source_files\(root, limit=(\d+)",
                           inspect.getsource(forge_intelligence.impact))
        value = str(self.rail("forge_scan_caps")["value"])
        self.assertIn(str(default), value)
        self.assertIsNotNone(impact, "планка impact не нашлась — сверка вакуумна")
        self.assertIn(impact.group(1), value)

    def test_tool_ceiling_reads_live_constant_when_agent_is_loaded(self):
        """Не литерал: поднятый agent должен менять то, что видит она."""
        import agent
        original = agent.TOOL_CEILING_SEC
        try:
            agent.TOOL_CEILING_SEC = 61.0
            self.assertIn("61", str(self.rail("tool_ceiling")["value"]))
            agent.TOOL_CEILING_SEC = 0.0
            self.assertIn("потолка нет", str(self.rail("tool_ceiling")["value"]))
        finally:
            agent.TOOL_CEILING_SEC = original

    def test_serverd_deadline_reads_live_constants(self):
        import serverd_client
        had = hasattr(serverd_client, "DEFAULT_TIMEOUT_SEC")
        original = getattr(serverd_client, "DEFAULT_TIMEOUT_SEC", None)
        try:
            serverd_client.DEFAULT_TIMEOUT_SEC = 137.0
            self.assertIn("137", str(self.rail("serverd_deadline")["value"]))
            del serverd_client.DEFAULT_TIMEOUT_SEC
            # Нет ярусов — на проде это час молчания на любой глагол; рельс обязан
            # сказать это, а не промолчать про отсутствие срока.
            self.assertIn("3600", str(self.rail("serverd_deadline")["value"]))
        finally:
            if had:
                serverd_client.DEFAULT_TIMEOUT_SEC = original
            elif hasattr(serverd_client, "DEFAULT_TIMEOUT_SEC"):
                del serverd_client.DEFAULT_TIMEOUT_SEC

    def test_serverd_deadline_warns_when_it_outlives_the_hand(self):
        """Срок больше потолка руки = честный ответ демона не дойдёт никогда."""
        import agent
        import serverd_client
        original = (agent.TOOL_CEILING_SEC,
                    getattr(serverd_client, "DEFAULT_TIMEOUT_SEC", None))
        try:
            agent.TOOL_CEILING_SEC = 600.0
            serverd_client.DEFAULT_TIMEOUT_SEC = 599.0
            self.assertIn("вложенность в порядке",
                          str(self.rail("serverd_deadline")["value"]))
            serverd_client.DEFAULT_TIMEOUT_SEC = 600.0   # ровно граница — уже плохо
            self.assertIn("⚠", str(self.rail("serverd_deadline")["value"]))
        finally:
            agent.TOOL_CEILING_SEC = original[0]
            if original[1] is None:
                delattr(serverd_client, "DEFAULT_TIMEOUT_SEC")
            else:
                serverd_client.DEFAULT_TIMEOUT_SEC = original[1]


class TestNamesAndNumbersExistInTheCode(Base):
    """Тот класс лжи, который 27.07 ловили руками, обязан ловиться сам.

    Руками нашли четыре штуки, и все — в файле, который эту волну и представляет: рычаг
    PRAXIS_FORGE_LOCK_ABANDONED_SEC, которого в коде нет ни одного вхождения; метка выреза,
    процитированная по памяти после того, как сосед её переписал; один кап там, где режут
    три; и планка обхода, названная по дефолту сигнатуры вместо связывающего бюджета.
    Общее у всех четырёх одно: имя или число вписаны в манифест РУКАМИ и пережили правку
    соседа молча. Проверки ниже смотрят на манифест ровно с этой стороны — не «красиво ли
    сформулировано», а «существует ли названное».
    """

    def test_every_env_name_rails_prints_exists_in_the_code(self):
        """Каждое имя переменной окружения, которое манифест ей называет, обязано быть живым.

        Смотрим на ДВА источника: собранный реестр (то, что она читает в my_capabilities и
        в soul/rails.md) и строковые литералы rails.py (там живут ветки, которые сегодня не
        отрисовались — например текст «порог выключен»). Комментарии сюда НЕ входят: `#` —
        записка тому, кто правит файл, и в ней имя мёртвого рычага уместно ровно как
        объяснение, почему он мёртв.

        Именно это правило, будь оно записано вчера, поймало бы обе находки дня: и мёртвый
        рычаг замка форжа, и порог проверки, который владелец forge_verify.py переименовал
        прямо во время правки этого файла. Названный рычаг она не перепроверяет — она им
        пользуется; цена ошибки здесь выше, чем цена молчания.
        """
        spoken = _rails_spoken_text() + "\n" + "\n".join(
            self.text(row["id"]) for row in rails.registry())
        names = sorted(set(_ENV_NAME_RE.findall(spoken)))
        self.assertGreater(len(names), 15, "имён окружения в манифесте не нашлось — "
                                           "сверка вакуумна")
        corpus = _corpus()
        ghosts = [n for n in names if not any(n in text for text in corpus.values())]
        self.assertFalse(ghosts, f"манифест называет рычаги, которых в коде НЕТ: {ghosts} — "
                                 f"поставить такой в .deploy.env значит «ничего не произошло», "
                                 f"и понять почему будет неоткуда")

    def test_the_ghost_detector_is_not_vacuous(self):
        """Сверка выше имеет смысл, только если умеет краснеть: проверяем на настоящем трупе.

        Имя собирается из кусков, чтобы сверка не нашла саму себя в собственном исходнике.
        """
        dead = "PRAXIS_FORGE_LOCK_ABANDONED" + "_SEC"
        self.assertFalse([n for n, t in _corpus().items() if dead in t],
                         "имя ожило в коде — тогда сверка выше перестала быть доказательством")
        self.assertNotIn(dead, _rails_spoken_text(),
                         "мёртвый рычаг вернулся в то, что манифест ей говорит")
        # ...и детектор обязан ловить его, если он вернётся: подсовываем ему труп.
        corpus = _corpus()
        self.assertFalse(any(dead in text for text in corpus.values()))
        self.assertTrue(_ENV_NAME_RE.findall(f'lever = "{dead} — порог"'),
                        "регулярка не узнаёт даже мёртвое имя — ловить ей нечем")

    def test_every_number_a_rail_names_matches_its_source(self):
        """Каждое число из описи — против объявления в модуле-источнике.

        Регулярки СВОИ, не рельсовы: если рельс потеряет связь с исходником, он скажет
        «не прочиталось», и это здесь покраснеет; если число зафиксируют литералом — оно
        разойдётся с источником при первой же правке соседа, и это покраснеет тоже.
        """
        checked = 0
        for rail_id, rel, pattern, form in NUMBERS:
            with self.subTest(rail=rail_id, rel=rel):
                match = re.search(pattern, _src(rel))
                self.assertIsNotNone(match, f"{rel}: {pattern} не нашлось — сверка вакуумна")
                expected = form.format(float(match.group(1)))
                self.assertIn(expected, self.text(rail_id),
                              f"{rail_id}: в {rel} это «{expected}», а манифест говорит другое")
                checked += 1
        self.assertGreaterEqual(checked, len(NUMBERS))

    def test_rail_ids_promised_by_neighbours_exist(self):
        """Сосед в своём файле пишет «вторая половина — рельс X в rails.py». X обязан быть.

        media.py и self_model.py назначили имена сами, у самих порогов. Придумать здесь
        третье имя значило бы оставить в их файлах ссылку в никуда — ту же ложь, только
        с другого конца.
        """
        ids = {r["id"] for r in rails.registry(with_values=False)}
        promised: dict[str, str] = {}
        for name, text in _corpus().items():
            for match in _HANDOFF_RE.finditer(text):
                promised.setdefault(match.group(1), name)
        self.assertTrue(promised, "ссылок соседей на рельсы не нашлось — сверка вакуумна")
        missing = {k: v for k, v in promised.items() if k not in ids}
        self.assertFalse(missing, f"сосед обещает рельс, которого в реестре нет: {missing}")


# Пределы, живущие ЛИТЕРАЛОМ В СИГНАТУРЕ тула. Их не видит ни поиск по PRAXIS_*, ни опись
# констант верхнего уровня — а связывают они по-настоящему: `coding_verify(timeout=900)`
# приезжает в запрос всегда, и env в forge_verify стоит за `or`, то есть достаётся только
# при явном timeout=0. Этот нашёлся случайно, через шов; таблица ниже — чтобы следующий
# нашёлся сверкой. Значение: (рельс, который обязан назвать число | None, число на сегодня).
# None — это ЗАПИСАННЫЙ ДОЛГ, а не разрешение молчать: изменится число или появится пятый
# такой предел — сверка краснеет и заставляет перечитать случай.
SIGNATURE_LIMITS: dict[tuple[str, str], tuple[str | None, int]] = {
    ("tool_coding_verify", "timeout"): ("verification_deadline", 900),
    ("tool_coding_run", "timeout"): (None, 600),
    ("tool_coding_process", "timeout"): (None, 0),
    ("tool_computer", "timeout_ms"): (None, 1500),
    # Не предел, а темп ввода; в таблице он затем, чтобы её изменение тоже требовало
    # перечитать случай, а не проезжало молча вместе с настоящими сроками.
    ("tool_computer", "inter_event_delay_ms"): (None, 0),
}
_TIME_PARAM_RE = re.compile(r"timeout|deadline|_sec$|_ms$|ceiling|wait", re.I)


def _signature_limits() -> dict[tuple[str, str], int]:
    """Числовые дефолты «срочных» параметров во ВСЕХ тулах agent — как их видит интерпретатор."""
    import agent
    found: dict[tuple[str, str], int] = {}
    for name in dir(agent):
        if not name.startswith("tool_"):
            continue
        func = getattr(agent, name)
        if not callable(func):
            continue
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            continue
        for pname, param in params.items():
            default = param.default
            if default is inspect.Parameter.empty or isinstance(default, bool):
                continue
            if isinstance(default, (int, float)) and _TIME_PARAM_RE.search(pname):
                found[(name, pname)] = int(default)
    return found


class TestLimitsHiddenInSignatures(Base):
    """Предел, вписанный в сигнатуру тула, — тот же молчаливый предел, только незаметнее.

    Прошлый заход сверял ИМЕНА переменных окружения. Этого мало: `agent.py:3292`
    `timeout: int = 900` не переменная, не константа модуля и не литерал в теле — а связывает
    он каждую мою проверку, пока я не передам срок сама. Рельс при этом называл источником
    числа env-рычаг, который в живом пути мёртв: поставить его в .deploy.env значило бы
    «ничего не произошло». Проверки ниже смотрят ровно на этот класс.
    """

    def test_the_table_matches_what_the_signatures_declare(self):
        found = _signature_limits()
        self.assertTrue(found, "срочных дефолтов в сигнатурах тулов не нашлось — сверка вакуумна")
        table = {key: number for key, (_rail, number) in SIGNATURE_LIMITS.items()}
        self.assertEqual(found, table,
                         "предел живёт литералом в сигнатуре тула и разошёлся с описью: "
                         "такой не ловится ни поиском по PRAXIS_*, ни описью констант — "
                         "его надо либо назвать рельсом, либо записать долгом здесь")

    def test_signature_limits_with_a_rail_are_named_in_the_manifest(self):
        named = {key: (rail, number) for key, (rail, number) in SIGNATURE_LIMITS.items() if rail}
        self.assertTrue(named, "ни одного предела сигнатуры не привязано к рельсу — сверка вакуумна")
        for (tool, param), (rail_id, number) in named.items():
            whole = self.text(rail_id)
            self.assertIn(str(number), whole,
                          f"{tool}({param}={number}) связывает мою работу, а рельс {rail_id} "
                          f"этого числа не называет")
            self.assertIn(tool.replace("tool_", ""), whole,
                          f"{rail_id}: число названо, а рука, чей это дефолт, — нет; "
                          f"тогда рычаг снова окажется не тем")

    def test_verification_rail_follows_the_signature_not_the_env(self):
        """Не литерал: меняем дефолт САМОЙ руки — рельс обязан поехать за ним.

        Ловушка ровно та, ради которой правка: env остаётся 900, а связывать начинает 137.
        Прежняя редакция рельса напечатала бы 900 и назвала бы источником env.
        """
        import agent
        import forge_verify
        func = agent.tool_coding_verify
        params = [p.name for p in inspect.signature(func).parameters.values()
                  if p.default is not inspect.Parameter.empty]
        idx = params.index("timeout")
        original = func.__defaults__
        try:
            patched = list(original)
            patched[idx] = 137
            func.__defaults__ = tuple(patched)
            value = str(self.rail("verification_deadline")["value"])
            self.assertIn("137с", value, "рельс не поехал за дефолтом сигнатуры")
            self.assertNotIn(f"проверке без своего срока даётся "
                             f"{forge_verify.MATRIX_DEADLINE_SEC}с", value,
                             "env выдан за срок, который проверка получает на самом деле")
            self.assertIn("PRAXIS_VERIFY_TIMEOUT_SEC", value,
                          "рычаг существует и обязан быть назван — вместе с тем, когда он "
                          "вправду действует")
            self.assertIn("timeout=0", value,
                          "не сказано, при каком условии env вообще достаётся")
        finally:
            func.__defaults__ = original

    def test_verification_rail_reads_the_signature_from_source_when_agent_is_down(self):
        """Второй путь (agent в процессе не поднят) обязан читать ту же сигнатуру."""
        import agent
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                'def tool_coding_verify(task_id: str, action: str = "plan",\n'
                '                       max_parallel: int = 2,\n'
                '                       timeout: int = 137, tail: int = 12000) -> str:\n'
                if rel == "agent.py" else original(rel))
            del sys.modules["agent"]
            self.assertIn("137с", rails._verification_deadline_value())
        finally:
            rails._source_text = original
            sys.modules["agent"] = agent

    def test_the_env_lever_really_is_unreachable_in_the_live_path(self):
        """Доказательство, на котором стоит формулировка рельса — из кода обоих соседей.

        forge.verify кладёт срок в запрос ВСЕГДА, а forge_verify берёт env только на `or`.
        Значит env действует ровно при timeout=0. Если сосед это перепишет, рельс обязан
        сменить формулировку — и вот эта сверка покраснеет первой.
        """
        self.assertRegex(_src("forge_verify.py"),
                         r'request\.get\("timeout"\)\s*or\s*\w*DEADLINE_SEC')
        self.assertRegex(_src("forge.py"), r'"timeout": max\(0, int\(timeout or 0\)\)')
        value = str(self.rail("verification_deadline")["value"])
        self.assertIn("не действует", value,
                      "рельс обязан сказать прямо, что названный рычаг в обычном ходе мёртв")

    def test_signature_reader_says_it_cannot_read(self):
        """«Не знаю» обязано выглядеть как «не знаю»: пустой исходник ≠ «предела нет»."""
        import agent
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("" if rel == "agent.py" else original(rel))
            del sys.modules["agent"]
            self.assertIsNone(rails._tool_signature_default(
                "agent", "tool_coding_verify", "timeout", "agent.py"))
            self.assertIn("⚠", rails._verification_deadline_value())
        finally:
            rails._source_text = original
            sys.modules["agent"] = agent


class TestAnswerCapTellsTheWholeTruth(Base):
    """Метка выреза и ВСЕ капы — из brokerops, а не из памяти о нём."""

    BROKER = "serverd/brokerops.py"

    def test_cut_marker_is_quoted_from_the_live_daemon(self):
        src = _src(self.BROKER)
        marker = re.search(r'\+\s*f"\\n(…[^"]*…)\\n"', src)
        self.assertIsNotNone(marker, "метка выреза не нашлась в brokerops — сверка вакуумна")
        # Подстановки выкидываем целиком: `{len(text) - limit}` — это ЧИСЛО в готовой метке,
        # а не слово; требовать от манифеста слова «limit» значило бы требовать чужой код.
        wording = re.sub(r"\{[^{}]*\}", " ", marker.group(1))
        words = [w for w in re.split(r"[\s;]+", wording) if w.isalpha() and len(w) > 3]
        self.assertGreaterEqual(len(words), 2, "метка выродилась — сверять нечего")
        value = str(self.rail("host_answer_cap")["value"])
        for word in words:
            self.assertIn(word, value, f"демон печатает «{word}», а манифест цитирует другое")
        if "cut " not in src:
            self.assertNotIn("cut ", value,
                             "рельс цитирует метку «… cut N chars …», которой в демоне нет")

    def test_every_cap_that_actually_cuts_is_named(self):
        """Кап назывался один, а режут три: у diff свой, у обеих веток search — свой."""
        src = _src(self.BROKER)
        overrides = sorted({int(n) for n in re.findall(r"_cap\([^\n]*?,\s*(\d+)\)", src)})
        self.assertGreaterEqual(len(overrides), 2,
                                "своих капов у глаголов не нашлось — сверка вакуумна")
        value = str(self.rail("host_answer_cap")["value"])
        for cap in overrides:
            self.assertIn(str(cap), value,
                          f"кап {cap} режет мой ответ, а манифест о нём молчит")

    def test_the_daemon_orientation_names_the_same_caps(self):
        """Два источника правды об одном капе обязаны сходиться.

        Демон печатает капы в `limits_manifest().note` — это её ориентировка, она читает её
        до отказа, а не после. Рельс читает сами вызовы `_cap(...)`. Если появится третий
        кап, а note о нём промолчит, врать начнёт демон, и знать об этом надо ЗДЕСЬ: у
        манифеста нет другого способа заметить, что сосед разошёлся сам с собой.
        """
        src = _src(self.BROKER)
        note = re.search(r'"note": \((.*?)\),\n', src, re.S)
        self.assertIsNotNone(note, "ориентировка демона не нашлась — сверка вакуумна")
        overrides = sorted({n for n in re.findall(r"_cap\([^\n]*?,\s*(\d+)\)", src)})
        self.assertTrue(overrides, "своих капов не нашлось — сверка вакуумна")
        silent = [cap for cap in overrides if cap not in note.group(1)]
        self.assertFalse(silent, f"демон режет ответ на {silent}, а в собственной ориентировке "
                                 f"об этом не говорит — тогда правду знает только рельс")

    def test_the_rail_follows_a_doctored_daemon(self):
        """Не литерал: подменяем исходник демона — меняются и числа, и метка, и доли."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                'TEXT_CAP = 111\n'
                'def _cap(text: str, limit: int = TEXT_CAP) -> str:\n'
                '    return (text[: int(limit * .5)]\n'
                '            + f"\\n… отрезано {len(text) - limit} из {len(text)}; '
                'кап {limit} …\\n"\n'
                '            + text[-int(limit * .25):])\n'
                '    if action == "diff":\n'
                '        return _cap(text, 222)\n'
                if rel == self.BROKER else original(rel))
            value = rails._host_answer_cap_value()
            self.assertIn("111", value)
            self.assertIn("diff — 222", value)
            self.assertIn("отрезано N из M; кап K", value)
            self.assertIn("50%", value)
            self.assertIn("25%", value)
        finally:
            rails._source_text = original

    def test_unreadable_source_says_it_is_unreadable(self):
        """«Не знаю» обязано выглядеть как «не знаю»: пустой исходник ≠ «капа нет»."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("" if rel == self.BROKER else original(rel))
            self.assertIn("не прочиталось", rails._host_answer_cap_value())
        finally:
            rails._source_text = original


class TestThresholdsFollowTheLiveCode(Base):
    """Пороги едут за живыми константами соседей, включая границу «предел снят»."""

    def test_mutation_lock_threshold_and_its_off_switch(self):
        import forge
        original = forge.LOCK_IDLE_ABANDONED_SEC
        try:
            forge.LOCK_IDLE_ABANDONED_SEC = 123.0
            value = str(self.rail("forge_mutation_lock")["value"])
            self.assertIn("123с", value)
            self.assertIn("БЕЗДЕЙСТВИЮ", value)
            forge.LOCK_IDLE_ABANDONED_SEC = 0.0     # ровно граница: 0 = порога нет
            value = str(self.rail("forge_mutation_lock")["value"])
            self.assertIn("ВЫКЛЮЧЕН", value)
            self.assertNotIn("молчит 0с", value)
            self.assertNotIn("держатель молчит 0", value)
        finally:
            forge.LOCK_IDLE_ABANDONED_SEC = original

    def test_mutation_lock_names_the_announced_long_step(self):
        """Порог тишины без лизинга читается как «уложись в 300с» — это неправда."""
        import forge
        value = str(self.rail("forge_mutation_lock")["value"])
        lease = forge._remote_step_lease({"scope": "host"})
        self.assertIn(f"{lease:.0f}с", value,
                      "заранее объявленный долгий шаг не назван — порог выглядит строже, "
                      "чем он есть")
        self.assertGreater(lease, forge.LOCK_IDLE_ABANDONED_SEC or 0,
                           "лизинг короче порога — тогда объявление шага ничего не даёт")

    def test_mutation_lock_names_the_legacy_threshold_too(self):
        """У замка ДВА порога, и второй жёстче: легаси-токен отбирают через свой литерал.

        Живая ложь 27.07: рельс (и текст отказа) называли только 300с бездействия, а токен
        старого образца и нечитаемый файл замка снимаются через 120с, и env на них не влияет.
        """
        match = re.search(r"(?m)^LOCK_LEGACY_IDLE_SEC = ([\d.]+)", _src("forge.py"))
        self.assertIsNotNone(match, "легаси-порог замка не нашёлся — сверка вакуумна")
        legacy = float(match.group(1))
        value = str(self.rail("forge_mutation_lock")["value"])
        self.assertIn(f"{legacy:.0f}с", value, "второй порог замка не назван")
        self.assertIn("PRAXIS_FORGE_LOCK_IDLE_SEC на него не действует", value,
                      "не сказано, что рычаг бездействия этот порог не двигает")
        import forge
        self.assertNotEqual(legacy, forge.LOCK_IDLE_ABANDONED_SEC,
                            "пороги совпали — тогда эта сверка ничего не различает")

    def test_legacy_lock_note_follows_a_doctored_forge(self):
        """Не литерал: и число, и признак «назван ли он в отказе» читаются из кода соседа."""
        import forge
        original = rails._source_text
        live = forge.LOCK_LEGACY_IDLE_SEC
        head = ("LOCK_LEGACY_IDLE_SEC = 77.0\n"
                "            if time.monotonic() >= deadline:\n"
                "                rule = 'порог бездействия'\n")
        try:
            del forge.LOCK_LEGACY_IDLE_SEC     # живой константы нет → читаем объявление
            rails._source_text = lambda rel: (
                head + '                raise TimeoutError(f"замок занят")\n'
                if rel == "forge.py" else original(rel))
            note = rails._legacy_lock_note()
            self.assertIn("77с", note)
            self.assertIn("в тексте самого отказа он не называется", note)
            # ...а назовёт его сосед в отказе — рельс обязан замолчать об этом сам.
            rails._source_text = lambda rel: (
                head + '                raise TimeoutError(f"занят, порог '
                       '{LOCK_LEGACY_IDLE_SEC:.0f}с")\n'
                if rel == "forge.py" else original(rel))
            note = rails._legacy_lock_note()
            self.assertIn("77с", note)
            self.assertNotIn("в тексте самого отказа", note)
        finally:
            rails._source_text = original
            forge.LOCK_LEGACY_IDLE_SEC = live

    def test_search_caps_come_from_brokerops_not_from_the_scan_budget(self):
        """Поиск рвёт СВОЙ кап, а объяснял его чужой кошелёк — рельс не имеет права повторять.

        Живая ложь 27.07: обход рвался на SEARCH_FILE_CAP (20000), а приписка приходила из
        `Budget.note()` — она называла бюджет обхода (12000) и советовала
        PRAXIS_FORGE_SCAN_FILES, который на search не влияет.
        """
        import forge_intelligence
        src = _src("serverd/brokerops.py")
        files = re.search(r"(?m)^SEARCH_FILE_CAP = (\d+)", src)
        self.assertIsNotNone(files, "кап поиска не нашёлся — сверка вакуумна")
        self.assertNotEqual(int(files.group(1)), int(forge_intelligence.SCAN_FILES),
                            "кап поиска совпал с бюджетом обхода — тогда сверка не различает "
                            "правильное число от подставленного")
        value = str(self.rail("serverd_workspace_budget")["value"])
        self.assertIn(files.group(1), value)
        self.assertNotIn(str(int(forge_intelligence.SCAN_FILES)), value,
                         "рельс называет бюджет обхода там, где режет кап поиска")

    def test_search_note_warns_when_the_daemon_stops_naming_its_own_cap(self):
        """Демон чинится в этой же волне — поэтому «объясняет ли он сам» проверяется, не помнится."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                "SEARCH_FILE_CAP = 77\nSEARCH_HIT_CAP = 5\n"
                'note = "\\n[" + budget.note() + "]"\n'
                if rel == "serverd/brokerops.py" else original(rel))
            note = rails._search_caps_note()
            self.assertIn("77", note)
            self.assertIn("5 находках", note)
            self.assertIn("PRAXIS_FORGE_SCAN_FILES", note)
            self.assertIn("Верное число — здесь", note)
            # ...а называет своё имя — предупреждения нет.
            rails._source_text = lambda rel: (
                'SEARCH_FILE_CAP = 77\nSEARCH_HIT_CAP = 5\n'
                'msg = f"прочитано {SEARCH_FILE_CAP} файлов (SEARCH_FILE_CAP)"\n'
                if rel == "serverd/brokerops.py" else original(rel))
            self.assertNotIn("Верное число — здесь", rails._search_caps_note())
        finally:
            rails._source_text = original

    def test_own_report_clips_follow_subagents(self):
        from core import subagents as core_subagents
        original = core_subagents.GOAL_CHARS
        try:
            core_subagents.GOAL_CHARS = 13
            self.assertIn("13 симв", str(self.rail("own_report_clips")["value"]))
        finally:
            core_subagents.GOAL_CHARS = original

    def test_own_report_clips_admit_a_silent_cut(self):
        """Метка реза не обещается на память: снимут её у соседа — рельс скажет об этом."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("GOAL_CHARS = 5\n"
                                              if rel == "core/subagents.py" else original(rel))
            live = sys.modules.pop("core.subagents", None)
            self.assertIn("метки реза в коде больше нет", rails._own_report_clips_value())
        finally:
            rails._source_text = original
            # ⚠ Вернуть модуль обязательно. `from core import subagents` у соседа НЕ
            # переимпортирует его: пакет `core` держит атрибут, и `_handle_fromlist`
            # проходит мимо. Без этой строки следующий по алфавиту тест видел пустой
            # sys.modules, рельс честно печатал «core.subagents в этом процессе не поднят»
            # — и падал единственной регрессией полного гейта, при исправном коде.
            if live is not None:
                sys.modules["core.subagents"] = live

    def test_task_reaper_threshold_and_its_off_switch(self):
        import forge
        original = (forge.TASK_ABANDONED_SEC, forge.UNIT_LIVENESS_TRUST_SEC)
        try:
            forge.TASK_ABANDONED_SEC = 7200.0
            forge.UNIT_LIVENESS_TRUST_SEC = 3600.0
            value = str(self.rail("forge_task_liveness")["value"])
            self.assertIn("2ч", value)
            self.assertIn("1ч", value)
            forge.TASK_ABANDONED_SEC = 0.0          # 0 = жнеца нет вовсе
            self.assertIn("ВЫКЛЮЧЕН", str(self.rail("forge_task_liveness")["value"]))
        finally:
            forge.TASK_ABANDONED_SEC, forge.UNIT_LIVENESS_TRUST_SEC = original

    def test_media_ledger_rail_follows_media(self):
        import media
        original = media.LEDGER_ABANDONED_SEC
        try:
            media.LEDGER_ABANDONED_SEC = 42.0
            self.assertIn("42с", str(self.rail("media_outbox_ledger_lock")["value"]))
        finally:
            media.LEDGER_ABANDONED_SEC = original

    def test_self_lock_rail_follows_self_model(self):
        import self_model
        original = self_model.SELF_LOCK_STALE_SEC
        try:
            self_model.SELF_LOCK_STALE_SEC = 43.0
            self.assertIn("43с", str(self.rail("self_store_lock")["value"]))
        finally:
            self_model.SELF_LOCK_STALE_SEC = original

    def test_scan_rail_names_the_budget_that_binds_not_the_signature_default(self):
        """Связывает МЕНЬШЕЕ из двух, и время — тоже предел.

        Живая ложь 27.07: рельс печатал «не больше 20000 файлов» — дефолт параметра
        `source_files(limit=20000)`, который под бюджетом зажимается первой же строкой тела
        (`min(limit, budget.files)`), а 90 секунд не назывались вообще.
        """
        import forge_intelligence
        original = (forge_intelligence.SCAN_FILES, forge_intelligence.SCAN_SECONDS)
        try:
            forge_intelligence.SCAN_FILES = 777
            forge_intelligence.SCAN_SECONDS = 33.0
            value = str(self.rail("forge_scan_caps")["value"])
            self.assertIn("777", value)
            self.assertIn("33с", value)
            self.assertIn("МЕНЬШЕЕ", value,
                          "бюджет и планка вызова — два разных числа; связывает меньшее")
        finally:
            forge_intelligence.SCAN_FILES, forge_intelligence.SCAN_SECONDS = original

    def test_scan_rail_tells_the_truth_when_the_clamp_disappears(self):
        """Снимут `min(limit, budget.files)` — связывать снова начнёт дефолт сигнатуры."""
        original = rails._forge_scan_value
        import forge_intelligence
        src = inspect.getsource(forge_intelligence.source_files)
        self.assertIn("limit = min(int(limit), budget.files)", src,
                      "зажима больше нет — тогда рельс обязан называть дефолт сигнатуры, "
                      "и эта проверка должна быть переписана вместе с ним")
        self.assertIs(rails._forge_scan_value, original)

    def test_workspace_budget_is_shorter_than_the_client_wait(self):
        """Инвариант матрёшки на стороне демона: серверный срок < клиентского.

        Иначе честный частичный ответ не успевает доехать — ход срубится раньше.
        """
        import serverd_client
        budget = re.search(r'WORKSPACE_BUDGET_SEC = _env_number\("[^"]+", ([\d.]+)\)',
                           _src("serverd/broker.py"))
        wait = re.search(r'WORKSPACE_WAIT_SEC = _env_number\("[^"]+", ([\d.]+)\)',
                         _src("serverd/broker.py"))
        self.assertIsNotNone(budget, "срок счёта демона не нашёлся — сверка вакуумна")
        self.assertIsNotNone(wait, "окно очереди не нашлось — сверка вакуумна")
        value = str(self.rail("serverd_workspace_budget")["value"])
        self.assertIn(f"{float(budget.group(1)):g}с", value)
        self.assertIn(f"{float(wait.group(1)):g}с", value)
        self.assertLess(float(budget.group(1)) + float(wait.group(1)),
                        serverd_client.LONG_TIMEOUT_SEC + 60,
                        "бюджет демона вместе с очередью перерос клиентское ожидание — "
                        "тогда ответ не доедет, и рельс обязан это показывать")

    def test_verification_rail_reads_the_env_name_from_the_source(self):
        """Имя рычага здесь не вписано руками — и это проверяется, а не декларируется.

        Пока писалась запись, владелец forge_verify.py переименовал порог
        (PRAXIS_VERIFY_HOST_DEADLINE_SEC → PRAXIS_VERIFY_TIMEOUT_SEC) и сменил значение
        втрое. Рельс обязан ехать за ним молча и без моей правки.
        """
        match = re.search(r'(\w*DEADLINE_SEC) = int\(float\(os\.getenv\("([A-Z_]+)"\)'
                          r'\s*or\s*(\d+)\)\)', _src("forge_verify.py"))
        self.assertIsNotNone(match, "срок матрицы проверок не нашёлся — сверка вакуумна")
        value = str(self.rail("verification_deadline")["value"])
        self.assertIn(match.group(2), value, "имя рычага не названо")
        self.assertIn(f"{match.group(3)}с", value, "число не названо")
        self.assertNotIn("PRAXIS_VERIFY_HOST_DEADLINE_SEC", self.text("verification_deadline"))

    def test_backfill_rail_matches_the_runner(self):
        src = _src("mtproto_runner.py")
        rooms = re.search(r"(?m)^BACKFILL_ROOMS_PER_TICK = (\d+)", src)
        resolves = re.search(r"(?m)^BACKFILL_RESOLVES_PER_TICK = (\d+)", src)
        self.assertIsNotNone(rooms, "кап комнат за тик не нашёлся — сверка вакуумна")
        self.assertIsNotNone(resolves, "кап резолвов не нашёлся — сверка вакуумна")
        value = str(self.rail("backfill_pacing")["value"])
        self.assertIn(f"{rooms.group(1)} комнат", value)
        self.assertIn(f"{resolves.group(1)} резолвов", value)
        self.assertLessEqual(int(resolves.group(1)), int(rooms.group(1)),
                             "резолвов за тик больше, чем комнат — рельс описывает не то")

    def test_forge_remote_run_clamp_is_named_with_the_other_tiers(self):
        """Шестой ярус той же матрёшки живёт в forge и в `deadlines()` не публикуется."""
        import forge
        value = str(self.rail("serverd_deadline")["value"])
        self.assertIn(f"{forge.REMOTE_RUN_DEADLINE_SEC:.0f}с", value,
                      "клампинг названного мной срока не назван — «без предела» молча "
                      "превращается в 540с")
        self.assertIn("PRAXIS_FORGE_REMOTE_RUN_SEC", value)
        original = forge.REMOTE_RUN_DEADLINE_SEC
        try:
            forge.REMOTE_RUN_DEADLINE_SEC = 0.0     # граница: 0 = не режем вовсе
            self.assertIn("не режется", str(self.rail("serverd_deadline")["value"]))
        finally:
            forge.REMOTE_RUN_DEADLINE_SEC = original


class TestManifestStructure(Base):
    """Файл манифеста и реестр — одно и то же множество рельсов."""

    def test_headings_match_registry(self):
        self.assertTrue(rails.sync_md())
        text = rails.RAILS_MD.read_text(encoding="utf-8")
        self.assertEqual(rails.manifest_ids(text),
                         {r["id"] for r in rails.registry(with_values=False)})

    def test_manifest_ids_parses_class_marker(self):
        """Маркер класса перед id не должен ломать разбор заголовка — и его отсутствие тоже.

        Без маркера жадный `\\S*` откусывал имя и отдавал за id хвост («rail_x» → «x»):
        такой манифест выглядел бы отставшим всегда, то есть предупреждение обесценилось бы.
        """
        self.assertEqual(rails.manifest_ids("## 🏠 window_gates\n## ⚙ provider_remaining\n"),
                         {"window_gates", "provider_remaining"})
        self.assertEqual(rails.manifest_ids("## rail_x\n## • rail_y\n"), {"rail_x", "rail_y"})
        self.assertEqual(rails.manifest_ids("### not_a_rail\ntext ## nope\n"), set())

    def test_drift_reports_both_directions(self):
        rails.sync_md()
        text = rails.RAILS_MD.read_text(encoding="utf-8")
        self.assertTrue(rails.manifest_drift()["ok"])
        # Рельс из кода вырезали из файла, а несуществующий — дописали.
        broken = text.replace("## 🏠 window_gates", "## 🏠 window_gates_OLD")
        broken += "\n## 🎒 rail_which_never_existed\n"
        rails.RAILS_MD.write_text(broken, encoding="utf-8")
        drift = rails.manifest_drift()
        self.assertFalse(drift["ok"])
        self.assertIn("window_gates", drift["missing"])
        self.assertIn("rail_which_never_existed", drift["stale"])
        self.assertIn("window_gates_OLD", drift["stale"])

    def test_drift_when_file_is_absent(self):
        drift = rails.manifest_drift()
        self.assertFalse(drift["file"])
        self.assertFalse(drift["ok"])
        self.assertIn("window_gates", drift["missing"])

    def test_state_line_says_the_manifest_is_stale(self):
        """Отставание файла обязано быть видно там же, где счёт рельсов."""
        rails.sync_md()
        self.assertIn("совпадает с кодом", rails.state_line())
        text = rails.RAILS_MD.read_text(encoding="utf-8")
        rails.RAILS_MD.write_text(text.replace("## 🏠 window_gates", "## 🏠 zzz_gone"),
                                  encoding="utf-8")
        line = rails.state_line()
        self.assertIn("ОТСТАЛ", line)
        self.assertIn("window_gates", line)

    def test_cheap_registry_touches_nothing(self):
        """with_values=False не должен ходить к демону: он зовётся в сборке ответа."""
        calls = []
        original = rails._serverd_value
        try:
            rails._serverd_value = lambda: calls.append(1) or "x"
            rails.registry(with_values=False)
            self.assertEqual(calls, [], "опись без значений полезла к serverd")
            rails.registry()
            self.assertTrue(calls, "полная опись обязана читать живое состояние")
        finally:
            rails._serverd_value = original

    def test_cheap_registry_keeps_the_same_ids(self):
        self.assertEqual({r["id"] for r in rails.registry(with_values=False)},
                         {r["id"] for r in rails.registry()})


class TestRetractedPromises(Base):
    """Снятые обещания сняты. Каждая проверка — против конкретной строки, которая врала."""

    def test_outbound_contract_is_not_called_unified(self):
        whole = self.text("evaluator_mirror")
        self.assertNotIn("единый outbound", whole,
                         "контур не единый: send_message не зовёт evaluate_reply вовсе")
        # Двери названы её именами (так она их и зовёт), и названы все три: первая
        # редакция починки перечисляла две и молчала про send_file — то есть чинила
        # ложь наполовину.
        for door in ("send_message", "narrate", "send_file"):
            self.assertIn(door, whole, f"дверь {door} не названа среди несудимых")

    def test_window_gates_no_longer_claims_there_are_no_other_gates(self):
        whole = self.text("window_gates")
        self.assertNotIn("иных гейтов нет", whole)
        self.assertIn("_ONE_MIND", whole)
        self.assertIn("сна", whole)

    def test_window_gates_agrees_with_the_runner_about_visibility(self):
        """Рельс не имеет права отправлять её мимо места, где ответ лежит.

        Владелец раннера завёл `_note_one_mind_defer` в этой же волне: отсрочка ложится в
        perception_skips классом «отложила». Рельс тогда утверждал обратное — «видно
        только в логе раннера», а лога у неё нет. Сверяемся с кодом соседа, а не с
        собственным текстом.

        ⚠ 27.07: проверка смотрела на ВЕСЬ value и запрещала в нём слова «НЕ пишется».
        Это было верно, пока в значении жило одно вето. Третье (пауза фона) в skips
        вправду не пишется, и запрет по всей строке начал требовать умолчать об этом.
        Сверяем теперь ровно ту фразу, которая отвечает за _ONE_MIND, — и отдельно то,
        что она доехала до значения целиком.
        """
        writes = rails._ONE_MIND_SKIP_RE.search(_src("mtproto_runner.py"))
        clause = rails._one_mind_defer_visibility()
        self.assertIn(clause, str(self.rail("window_gates")["value"]),
                      "фраза про видимость отсрочки не доехала до значения рельса")
        if writes:
            self.assertIn("manage_perception", clause,
                          "раннер пишет отсрочку в skips, а рельс про это место молчит")
            self.assertNotIn("НЕ пишется", clause)
        else:
            self.assertIn("НЕ пишется", clause,
                          "раннер перестал писать отсрочку — рельс обязан сказать это прямо")

    def test_window_gates_follows_the_runner_both_ways(self):
        """Оба исхода — детерминированно, чтобы ветка не осталась непроверенной."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("" if rel == "mtproto_runner.py"
                                              else original(rel))
            self.assertIn("НЕ пишется", str(self.rail("window_gates")["value"]))
            rails._source_text = lambda rel: (
                'await asyncio.to_thread(lambda: perception.note_skip(\n'
                '    f"one_mind:{stage}", "отложила"))\n'
                if rel == "mtproto_runner.py" else original(rel))
            value = str(self.rail("window_gates")["value"])
            self.assertIn("manage_perception", value)
            self.assertIn("отложила", value)
        finally:
            rails._source_text = original

    def test_perception_does_not_promise_completeness_it_never_checked(self):
        """«КАЖДЫЙ пропуск имеет класс причины» — утверждение о полноте, которой не мерили.

        Молчаливый путь тем и молчалив, что его нет ни в одном списке; пауза фона на
        расписании — как раз такой (см. window_gates). Честная форма — счёт пишущих мест
        и прямая оговорка. Считаем оба конца: и что оговорка на месте, и что число
        совпадает с живым раннером.
        """
        whole = self.text("perception_pacing")
        self.assertNotIn("каждый пропуск", whole.lower())
        self.assertIn("никто не проверял", whole)
        expected = len(rails._NOTE_SKIP_RE.findall(
            rails._code_only(_src("mtproto_runner.py"))))
        self.assertGreater(expected, 3, "мест записи причины почти нет — сверка вакуумна")
        self.assertIn(f"из {expected} мест", str(self.rail("perception_pacing")["value"]))

    def test_perception_skip_counter_follows_the_runner(self):
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                "perception.note_skip('a', 'b')\n"
                "# в комментарии note_skip( не считается\n"
                "note_skip('c', 'd')\n" if rel == "mtproto_runner.py" else original(rel))
            self.assertIn("из 2 мест", rails._skip_reason_sites())
            rails._source_text = lambda rel: ("x = 1\n" if rel == "mtproto_runner.py"
                                              else original(rel))
            self.assertIn("НИ ОДНО место", rails._skip_reason_sites())
            rails._source_text = lambda rel: ("" if rel == "mtproto_runner.py"
                                              else original(rel))
            self.assertIn("не прочитался", rails._skip_reason_sites())
        finally:
            rails._source_text = original

    def test_window_gates_counts_the_third_veto_too(self):
        """«Вето два» было пересчётом: пауза фона — третье, и последствие у него ДРУГОЕ.

        Сон и «она одна» откладывают (due сохраняется). Пауза фона на рекуррентном
        вхождении зовёт `_consume()` — вхождение потребляется, расписание уезжает на
        следующий срок, и этого раза не будет. Рельс, считавший вето, обязан считать их
        все, а обещание «вернётся следующим тиком» не имеет права накрывать этот случай.
        """
        whole = self.text("window_gates")
        self.assertNotIn("вето два", whole)
        self.assertIn("ТРИ", whole)
        self.assertIn("appetite_pause", whole, "не названо, где про третье вето подробно")
        runner = _src("mtproto_runner.py")
        self.assertIn("appetite.background_hold", runner,
                      "третьего вето в раннере нет — рельс обещает несуществующее")
        value = str(self.rail("window_gates")["value"])
        self.assertIn("ПОТРЕБЛЯЕТСЯ", value,
                      "последствие третьего вето не названо — оно единственное, "
                      "которое НЕ возвращает намеченное")

    def test_window_gates_follows_the_runner_about_the_third_veto(self):
        """Оба исхода детерминированно — иначе ветка «вернётся» осталась бы недоказанной."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                "def _fire(t):\n"
                "    hold = appetite.background_hold()\n"
                "    if hold:\n"
                "        await _consume()\n"
                "        return\n" if rel == "mtproto_runner.py" else original(rel))
            self.assertIn("ПОТРЕБЛЯЕТСЯ", rails._schedule_hold_effect())
            rails._source_text = lambda rel: (
                "def _fire(t):\n"
                "    hold = appetite.background_hold()\n"
                "    if hold:\n"
                "        perception.note_skip('appetite', 'отложила')\n"
                "        return\n" if rel == "mtproto_runner.py" else original(rel))
            effect = rails._schedule_hold_effect()
            self.assertIn("сохраняется", effect)
            self.assertIn("manage_perception", effect)
            rails._source_text = lambda rel: ("x = 1\n" if rel == "mtproto_runner.py"
                                              else original(rel))
            self.assertIn("НЕТ", rails._schedule_hold_effect())
            rails._source_text = lambda rel: ("" if rel == "mtproto_runner.py"
                                              else original(rel))
            self.assertIn("не прочитался", rails._schedule_hold_effect())
        finally:
            rails._source_text = original

    def test_conation_rail_matches_the_code_not_the_wish(self):
        whole = self.text("conation_authorship")
        self.assertNotIn("собственного run", whole,
                         "кода проверки «или собственный run» нет — рельс обещал несуществующее")
        self.assertIn("АУДИТОРИИ", whole)
        # ...и код действительно смотрит только на скоуп: если это изменят, рельс станет
        # слишком пессимистичным, и это тоже расхождение.
        self.assertRegex(_src("agent.py"), r'_active_scope\(\) != "owner":\s*\n\s*rails\.deny')

    def test_service_quota_is_not_declared_cancelled(self):
        whole = self.text("server_hands")
        self.assertNotIn("больше не потолок", whole,
                         "квота жива и пишет отказ под этим же рельсом")
        import services
        self.assertGreater(services.RESTARTS_PER_HOUR, 0)

    def test_legacy_routes_reach_the_manifest(self):
        """Три value-функции legacy-маршрутов были мёртвым кодом — теперь подключены."""
        for rail_id in ("server_eyes", "server_hands", "host_edits"):
            self.assertIn("legacy:", str(self.rail(rail_id)["value"]),
                          f"{rail_id}: состояние legacy-маршрута в манифест не попадает")

    def test_docstring_does_not_promise_lazy_sync(self):
        self.assertNotIn("лениво из my_capabilities", rails.__doc__ or "")
        self.assertIn("A2", rails.__doc__ or "")

    def test_github_is_named_as_owner_decision(self):
        row = self.rail("github_identity")
        self.assertEqual(row["cls"], rails.CLS_OWNER,
                         "чужая личность в её руках — решение владельца, не её дисциплина")
        self.assertNotIn("HOME=/root", self.text("github_identity"))
        self.assertGreater(len(self.text("github_identity")), 40)


class TestCapabilitiesTellTheTruth(Base):
    """capabilities: руки — это руки, аудитория — это аудитория."""

    @staticmethod
    def _hands_line(text: str) -> set[str]:
        """Имена, фактически напечатанные строкой «суверенные руки» — без подмен."""
        for line in text.splitlines():
            if line.startswith("- суверенные руки"):
                return {n.strip() for n in line.split(":", 1)[1].split(",") if n.strip()}
        raise AssertionError("строки «суверенные руки» в самоописании нет")

    def test_owner_view_prints_her_own_hand_set_not_the_owner_one(self):
        """Против НАСТОЯЩЕГО снимка: подмена подписи без подмены источника краснеет.

        ⚠ Первая редакция этой проверки подсовывала снимок `{'owner': ['shell', ...]}` и
        свою `_human_only_line`, после чего `assertIn('admit', text)` проходил за счёт
        СКОБКИ. Реального списка тест не видел никогда — а живой `describe('owner')`
        печатал предложение, противоречащее себе: «мои в любом канале: …, admit, …».
        """
        import agent
        s = capabilities.snapshot()
        printed = self._hands_line(capabilities.describe("owner"))
        self.assertEqual(printed, set(s["tools"]["self"]),
                         "печатается не тот набор, который выдаётся ей в ходе")
        mine = {t["name"] for t in agent.PRAXIS_SELF_TOOLS
                if isinstance(t, dict) and t.get("name")}
        self.assertTrue(mine <= printed, f"её собственные руки потерялись: {mine - printed}")
        for name in sorted(agent._HUMAN_OWNER_ONLY_TOOL_NAMES):
            self.assertNotIn(name, printed,
                             f"{name} — делегирование доверия Егора; в не-owner ходе "
                             f"agent.offered_tools_for его не даёт")

    def test_owner_view_still_names_what_only_yegor_has(self):
        """Убрать из списка — не значит промолчать: закон 2 держится и на этой строке."""
        import agent
        text = capabilities.describe("owner")
        self.assertNotIn("owner-тулы (в личке с Егором)", text,
                         "список описывает РУКИ, а скобка описывала аудиторию — "
                         "с 26.07 эти руки при ней в любом канале")
        line = next(l for l in text.splitlines() if l.startswith("- не мои, только у Егора"))
        for name in sorted(agent._HUMAN_OWNER_ONLY_TOOL_NAMES):
            self.assertIn(name, line)

    def test_human_only_line_is_the_difference_of_the_two_sets(self):
        import agent
        s = capabilities.snapshot()
        expected = sorted(set(s["tools"]["owner"]) - set(s["tools"]["self"]))
        self.assertEqual(capabilities._human_only_line(s), ", ".join(expected))
        self.assertTrue(set(agent._HUMAN_OWNER_ONLY_TOOL_NAMES) <= set(expected),
                        "человеко-владельческий тул пропал из разницы наборов")

    def test_human_only_line_catches_the_gated_owner_extra(self):
        """`send_email` owner-эксклюзивен под mail-гейтом и в frozenset agent НЕ входит.

        Здесь подставляется ВХОД чистой функции, а не источник правды: на живом снимке
        (почта у Егора выключена) разница наборов совпадает с frozenset — то есть прежняя
        реализация-копия была бы зелёной ровно потому, что гейт выключен. Проверяем
        алгоритм на том состоянии среды, где копия врёт.
        """
        snap = {"tools": {"owner": ["admit", "computer_access", "mail_read", "recall",
                                    "send_email"],
                          "self": ["mail_read", "recall"]}}
        self.assertEqual(capabilities._human_only_line(snap),
                         "admit, computer_access, send_email")

    def test_human_only_line_admits_it_cannot_read(self):
        """«Не знаю» обязано выглядеть как «не знаю», а не как «ничего лишнего нет»."""
        self.assertIn("не читается",
                      capabilities._human_only_line({"tools": {"owner": ["admit"]}}))

    def test_state_line_counts_her_hands_not_the_owner_set(self):
        """STATE едет каждым ходом: лишние два тула — ложь, повторённая ежечасно."""
        s = capabilities.snapshot()
        self.assertNotEqual(len(s["tools"]["self"]), len(s["tools"]["owner"]),
                            "наборы совпали — сверка вакуумна")
        line = capabilities.state_line()
        self.assertIn(f"суверенные {len(s['tools']['self'])}", line)
        self.assertNotIn(f"суверенные {len(s['tools']['owner'])}", line)

    def test_group_view_does_not_call_delegated_tools_sovereign(self):
        s = capabilities.snapshot()
        human = sorted(set(s["tools"]["owner"]) - set(s["tools"]["self"]))
        self.assertTrue(human, "нечего проверять — сверка вакуумна")
        mine = sorted(s["tools"]["self"])[0]
        text = capabilities.describe("group", offered=[mine, human[0], "recall"])
        line = next(l for l in text.splitlines() if l.startswith("- руки В ЭТОМ ходе"))
        head, marker, tail = line.partition("плюс делегированное Егором")
        self.assertTrue(marker, "делегированный тул в ходе есть, а назван не был")
        self.assertNotIn(human[0], head, "делегирование доверия Егора выдано за мою руку")
        self.assertIn(human[0], tail)
        self.assertIn(mine, head)

    def test_state_line_carries_the_manifest(self):
        """STATE — единственный путь, где манифест доходит без её инициативы.

        Форма краткая: у STATE есть бюджет длины (test_capabilities держит < 400 симв.),
        и отставание должно влезать в него, а не вытеснять соседей.
        """
        rails.sync_md()
        fresh = capabilities._rails_manifest_line()
        self.assertIn("рельсы", fresh)
        self.assertIn("свеж", fresh)
        text = rails.RAILS_MD.read_text(encoding="utf-8")
        rails.RAILS_MD.write_text(text.replace("## 🏠 window_gates", "## 🏠 zzz_gone"),
                                  encoding="utf-8")
        stale = capabilities._rails_manifest_line()
        self.assertIn("отстал", stale)
        self.assertLess(len(stale), 40, "строка едет в STATE каждым ходом — она обязана быть краткой")

    def test_state_line_does_not_call_serverd(self):
        """Дешёвый путь: сборка STATE не имеет права ждать демона."""
        calls = []
        original = rails._serverd_value
        try:
            rails._serverd_value = lambda: calls.append(1) or "x"
            capabilities._rails_manifest_line()
            self.assertEqual(calls, [])
        finally:
            rails._serverd_value = original

    def test_outbound_description_names_the_hole(self):
        snap_source = inspect.getsource(capabilities.snapshot)
        self.assertIn("прямые тулы", snap_source)
        self.assertNotIn("единый outbound-советник", snap_source)


class TestHostOutputIsCutTwice(Base):
    """Вывод хоста режется ДВАЖДЫ, и второй рез — мой; до 27.07 он не значился нигде.

    Демонский кап (host_answer_cap) был назван, а мой — нет, хотя он в пять раз жёстче и
    связывает именно он. Плюс он был односторонним: приписка демона о сроке и бюджете
    дописывается в конец вывода и на семикилобайтном ответе apt не доходила никогда.
    """

    FAKE_AGENT = (
        'def _host_text_cap() -> int:\n'
        '    try:\n'
        '        value = int(str(os.getenv("PRAXIS_HOST_TEXT_CAP") or "").strip())\n'
        '    except (TypeError, ValueError):\n'
        '        return 300\n'
        '    return value if value > 0 else 300\n'
        '\n\n'
        'HOST_TEXT_CAP = _host_text_cap()\n'
        '\n\n'
        'def _clip_host_text(text: str, limit: int = 0) -> str:\n'
        '    cap = int(limit or HOST_TEXT_CAP)\n'
        '    tail = min(cap // 4, 50)\n'
        '    return f"[вырезано {cut} символов из {len(value)}]"\n'
        '\n\n'
        'def tool_host_ctl(verb: str) -> str:\n'
        '    parts.append(_clip_host_text(str(r["text"])))\n'
        '\n\n'
        'def tool_coding_run(cmd: str) -> str:\n'
        '    return _clip_host_text(out)\n'
    )

    def _without_live_cap(self):
        """Убрать живую константу, чтобы проверялась именно ветка чтения исходника."""
        import agent
        had = hasattr(agent, "HOST_TEXT_CAP")
        value = getattr(agent, "HOST_TEXT_CAP", None)
        if had:
            del agent.HOST_TEXT_CAP
        self.addCleanup(lambda: setattr(agent, "HOST_TEXT_CAP", value) if had else None)

    def test_rail_follows_the_live_constant_and_the_real_split(self):
        """Кап и доли головы/хвоста едут за кодом, а не вписаны числом."""
        import agent
        original = agent.HOST_TEXT_CAP
        try:
            agent.HOST_TEXT_CAP = 900
            value = str(self.rail("host_text_clip")["value"])
            self.assertIn("900 симв. на вывод хоста", value)
            # доли читаются из тела функции: tail = min(cap // 3, 1200) → 300/600
            div = re.search(r"tail = min\(cap // (\d+), (\d+)\)", _src("agent.py"))
            self.assertIsNotNone(div, "форма реза в agent.py не нашлась — сверка вакуумна")
            tail = min(900 // int(div.group(1)), int(div.group(2)))
            self.assertIn(f"хвост {tail} симв., голова {900 - tail}", value)
            self.assertNotIn(f"{original} симв. на вывод хоста", value)
        finally:
            agent.HOST_TEXT_CAP = original

    def test_zero_is_the_boundary_and_says_the_cut_is_off(self):
        """Ровно граница: 0 — это «реза нет», а не «оставляю 0 символов»."""
        import agent
        original = agent.HOST_TEXT_CAP
        try:
            agent.HOST_TEXT_CAP = 1
            self.assertIn("1 симв. на вывод хоста",
                          str(self.rail("host_text_clip")["value"]))
            agent.HOST_TEXT_CAP = 0
            value = str(self.rail("host_text_clip")["value"])
            self.assertIn("рез снят", value)
            self.assertNotIn("0 симв. на вывод хоста", value)
        finally:
            agent.HOST_TEXT_CAP = original

    def test_rail_says_which_of_the_two_caps_binds(self):
        """Два реза подряд: назвать надо оба и сказать, который связывает."""
        import agent
        daemon, _ = rails._host_answer_caps()
        self.assertIsNotNone(daemon, "кап демона не прочитался — сверка вакуумна")
        self.assertGreater(daemon, agent.HOST_TEXT_CAP,
                           "капы сравнялись — тогда проверка ничего не различает")
        value = str(self.rail("host_text_clip")["value"])
        self.assertIn(str(daemon), value, "демонский кап в моей записи не назван")
        self.assertIn("связывает МОЙ рез", value)

    def test_places_and_marker_are_read_from_the_source(self):
        """Не литерал: подменяем agent.py — едут и кап, и доли, и места реза."""
        self._without_live_cap()
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (self.FAKE_AGENT if rel == "agent.py"
                                              else original(rel))
            value = rails._host_text_clip_value()
            self.assertIn("300 симв. на вывод хоста", value)
            self.assertIn("хвост 50 симв., голова 250", value)      # min(300//4, 50)
            self.assertIn("режу в: host_ctl, coding_run", value)
            self.assertIn("метка", value)
            self.assertIn("agent в этом процессе не поднят", value)
        finally:
            rails._source_text = original

    def test_rail_admits_when_the_cut_became_silent(self):
        """Снимут метку выреза — рельс обязан сказать это, а не молчать вместе с ней."""
        self._without_live_cap()
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                self.FAKE_AGENT.replace("вырезано {cut} символов из {len(value)}", "…")
                if rel == "agent.py" else original(rel))
            value = rails._host_text_clip_value()
            self.assertIn("рез стал молчаливым", value)
        finally:
            rails._source_text = original

    def test_unreadable_source_is_not_reported_as_no_cap(self):
        """«Не знаю» обязано выглядеть как «не знаю»: пустой исходник ≠ «капа нет»."""
        self._without_live_cap()
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("" if rel == "agent.py" else original(rel))
            value = rails._host_text_clip_value()
            self.assertIn("не прочитался", value)
            self.assertNotIn("рез снят", value)
        finally:
            rails._source_text = original


class TestFollowupTraceIsNamed(Base):
    """След нити ≠ почта Егору. Рельс обязан говорить, что в коде СЕЙЧАС, а не что задумано.

    Живой повод: за две недели Егору ушло 17 писем «вам ответили», ни одного он не просил
    словами, 12 родил часовой пульс, а 27.07 в 02:32 ему переслали его же реплику из
    AbstractDL под заголовком «AbstractDL Chat ответил(а)». В rails.py слова followup до
    сегодня не было ни разу — ни рельса, ни одного из пяти пределов.
    """

    FIXED_LEDGER = (
        'TRACE_TTL_SEC = 259200.0\n'
        'CONTEXT_LIMIT = 12\n'
        '_SETTLED_KEEP = 500\n'
        'os.environ.get("PRAXIS_FOLLOWUP_TRACE_TTL_SEC")\n'
        'notify_owner = True\n'
        'sender_name = ""\n'
        'sender_is_owner = False\n'
        'row += f"; повод отправки: {gist[:240]}"\n'
    )
    LOOSE_RUNNER = (
        'followup_request = telegram_followups.request_from_owner_buffer(\n'
        '    _buf.get(str(OWNER_ID), ()), explicit_only=False,\n'
        ')\n'
        'followup_request = f"Praxis initiative from social pulse {pulse_id}: {text}"\n'
        'for item in pending[:10]:\n'
        '    excerpt = str(response.get("text") or "(без текста)")[:1200]\n'
        'return "action должен быть list | cancel."\n'
    )

    def test_rail_agrees_with_the_live_ledger_about_the_gate(self):
        """Против настоящих исходников: обещание «только по заказу» обязано иметь код."""
        ledger = re.sub(r"(?m)#.*$", "", _src("telegram_followups.py"))
        value = str(self.rail("followup_notice")["value"])
        if "notify_owner" in ledger:
            self.assertIn("ТОЛЬКО по нити, где отчёт заказан", value)
            self.assertNotIn("по КАЖДОЙ отвеченной нити", value)
        else:
            self.assertIn("по КАЖДОЙ отвеченной нити", value,
                          "разделения «след ≠ отчёт» в коде нет — рельс обязан сказать это "
                          "прямо, а не описывать будущее как настоящее")

    def test_rail_follows_the_code_both_ways(self):
        """Обе ветки детерминированно: и починенный леджер, и тот автоматизм, что был."""
        original = rails._source_text
        import telegram_followups
        live = getattr(telegram_followups, "TRACE_TTL_SEC", None)
        try:
            if live is not None:
                del telegram_followups.TRACE_TTL_SEC
            rails._source_text = lambda rel: (
                self.FIXED_LEDGER if rel == "telegram_followups.py"
                else self.LOOSE_RUNNER.replace("explicit_only=False,", "")
                .replace('followup_request = f"Praxis initiative'
                         ' from social pulse {pulse_id}: {text}"\n', "")
                if rel == "mtproto_runner.py" else original(rel))
            value = rails._followup_notice_value()
            self.assertIn("ТОЛЬКО по нити, где отчёт заказан", value)
            self.assertIn("на явную просьбу Егора словами", value)
            self.assertIn("72ч", value)
            self.assertIn("PRAXIS_FOLLOWUP_TRACE_TTL_SEC", value)
            self.assertIn("12 нитей", value)

            # ...а теперь состояние ДО починки: ни признака заказа, ни имени ответившего.
            rails._source_text = lambda rel: (
                "" if rel == "telegram_followups.py"
                else self.LOOSE_RUNNER if rel == "mtproto_runner.py" else original(rel))
            value = rails._followup_notice_value()
            self.assertIn("не прочитался", value)

            rails._source_text = lambda rel: (
                'CONTEXT_LIMIT = 12\n_SETTLED_KEEP = 500\n'
                if rel == "telegram_followups.py"
                else self.LOOSE_RUNNER if rel == "mtproto_runner.py" else original(rel))
            value = rails._followup_notice_value()
            self.assertIn("по КАЖДОЙ отвеченной нити", value)
            self.assertIn("explicit_only=False", value)
            self.assertIn("часового пульса", value)
            self.assertIn("срока жизни у следа нет", value)
            self.assertIn("метка ЧАТА", value)
            self.assertIn("уехала ему же в ЛС", value)
        finally:
            rails._source_text = original
            if live is not None:
                telegram_followups.TRACE_TTL_SEC = live

    def test_a_commented_out_automatism_is_not_read_as_live(self):
        """Снятую строку сосед оставляет в комментарии — и обязан оставлять.

        Если читать комментарий как код, рельс объявит ей автоматизм ровно в тот момент,
        когда его убрали, — то есть соврёт при починке. Разделение то же, что в сверке
        имён рычагов: `#` — записка правящему, строка — обещание ей.
        """
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                '# 27.07: здесь стояло explicit_only=False — любая реплика Егора\n'
                '# и ветка "Praxis initiative from social pulse" — оба сняты\n'
                'followup_request = telegram_followups.request_from_owner_buffer(buf)\n'
                'return "action должен быть list | watch | unwatch | cancel."\n'
                if rel == "mtproto_runner.py" else original(rel))
            gate = rails._followup_report_gate()
            self.assertIn("на явную просьбу Егора словами", gate)
            self.assertNotIn("ЛЮБАЯ последняя реплика", gate)
            self.assertNotIn("часового пульса", gate)
        finally:
            rails._source_text = original

    def test_ttl_boundary_is_read_not_remembered(self):
        """Срок следа — число из кода: 1.5ч должно остаться 1.5ч, а не «72ч по памяти»."""
        import telegram_followups
        original = rails._source_text
        live = getattr(telegram_followups, "TRACE_TTL_SEC", None)
        try:
            if live is not None:
                del telegram_followups.TRACE_TTL_SEC
            rails._source_text = lambda rel: (
                'def _trace_ttl() -> float:\n    return 5400.0\n'
                'TRACE_TTL_SEC = _trace_ttl()\n'
                'os.environ.get("PRAXIS_FOLLOWUP_TRACE_TTL_SEC")\n'
                if rel == "telegram_followups.py" else original(rel))
            self.assertIn("гаснет через 1.5ч", rails._followup_trace_life())
            telegram_followups.TRACE_TTL_SEC = 3600.0   # живая константа старше исходника
            self.assertIn("гаснет через 1ч", rails._followup_trace_life())
        finally:
            rails._source_text = original
            if live is not None:
                telegram_followups.TRACE_TTL_SEC = live
            elif hasattr(telegram_followups, "TRACE_TTL_SEC"):
                del telegram_followups.TRACE_TTL_SEC

    def test_hand_is_the_tool_schema_not_the_runner_help(self):
        """Рычаг называется по СХЕМЕ ТУЛА: раннер умеет больше, чем доходит до неё.

        27.07 это было живым состоянием: `watch`/`unwatch` уже принимал `_sync_followups`,
        а в enum самого telegram_account их ещё не было. Назвать такую команду её рукой
        значило бы выдать рычаг, который она нажмёт и не получит ничего.
        """
        import agent
        original = agent.TELEGRAM_ACCOUNT_TOOL
        runner_help = 'return "action должен быть list | watch | unwatch | cancel."'
        try:
            agent.TELEGRAM_ACCOUNT_TOOL = {"input_schema": {"properties": {"action": {
                "enum": ["join", "followups", "cancel_followup"]}}}}
            hand = rails._followup_hand(runner_help)
            self.assertIn("followups, cancel_followup", hand)
            self.assertIn("завести отчёт или снять его нет", hand)
            self.assertIn("до меня рычаг ещё не доведён", hand)

            agent.TELEGRAM_ACCOUNT_TOOL = {"input_schema": {"properties": {"action": {
                "enum": ["followups", "cancel_followup", "watch_reply", "unwatch_reply"]}}}}
            hand = rails._followup_hand(runner_help)
            self.assertIn("watch_reply", hand)
            self.assertNotIn("не доведён", hand)
            self.assertNotIn("снять его нет", hand)
        finally:
            agent.TELEGRAM_ACCOUNT_TOOL = original

    def test_hand_matches_what_the_tool_offers_today(self):
        """Живая сверка: что в enum, то и в манифесте — без ручного списка."""
        import agent
        enum = agent.TELEGRAM_ACCOUNT_TOOL["input_schema"]["properties"]["action"]["enum"]
        mine = [n for n in enum if "followup" in n or "watch" in n]
        self.assertTrue(mine, "тул не даёт по нитям ничего — сверка вакуумна")
        value = str(self.rail("followup_notice")["value"])
        for name in mine:
            self.assertIn(name, value, f"тул принимает {name}, а манифест о нём молчит")


def _said_recently_callers(corpus: dict[str, str]) -> list[str]:
    """Файлы боевого кода, где `said_recently` действительно ЗОВУТ (а не упоминают).

    Комментарии снимаются: в mtproto_runner.py имя стоит в объяснении, зачем прямая
    отправка пишет записку, и считать это вызовом значило бы получить обратный результат —
    докстринг снова начал бы обещать работающий чек.
    """
    out = []
    for name, text in corpus.items():
        if name == "notes.py":
            continue
        if re.search(r"(?<!def )\bsaid_recently\s*\(", re.sub(r"(?m)#.*$", "", text)):
            out.append(name)
    return sorted(out)


class TestNotesDocstringDoesNotLie(unittest.TestCase):
    """`notes.py` она читает через fs_read, и его докстринг утверждал несуществующий чек.

    Строка `notes.py:11` обещала: «проверка „уже я это говорила?“ идёт по существу, а не по
    форме; said_recently() — явный чек». Вызовов ноль, и это НАМЕРЕННО (test_sanitize
    требует, чтобы текст уходил даже при said_recently → True). Отсюда две проверки: что
    обещание снято и что оно не вернётся мимо кода.
    """

    def setUp(self):
        import notes
        self.notes = notes
        self.doc = notes.__doc__ or ""

    def test_the_explicit_check_is_no_longer_promised(self):
        self.assertNotIn("явный чек", self.doc,
                         "вызовов нет ни одного — обещать «явный чек» значит врать ей "
                         "в файле, который она читает")
        self.assertIn("ИНФОРМИРУЮЩАЯ", self.doc,
                      "снять обещание мало: должно быть сказано, чем функция стала")

    def test_the_docstring_agrees_with_the_number_of_callers(self):
        """Согласованность в обе стороны, а не запрет: подключат — текст обязан поехать."""
        callers = _said_recently_callers(_corpus())
        if callers:
            self.assertNotIn("не зовёт никто", self.doc,
                             f"вызовы появились ({callers}) — докстринг остался вчерашним")
        else:
            self.assertIn("не зовёт никто", self.doc,
                          "вызовов нет; молчать об этом — та же ложь, только тише")
        # ...и сам детектор обязан различать вызов и упоминание.
        self.assertEqual(_said_recently_callers({"x.py": "# см. notes.said_recently(chat)\n"}),
                         [])
        self.assertEqual(_said_recently_callers({"x.py": "if notes.said_recently(c, t):\n"}),
                         ["x.py"])

    def test_the_threshold_is_named_with_its_measured_miss(self):
        """«По существу» было сильнее, чем умеет difflib: порог и промах названы числами."""
        self.assertIn(str(self.notes._SIMILAR_THRESHOLD), self.doc,
                      "порог сравнения в докстринге не назван — «по существу» опять "
                      "звучит сильнее, чем это работает")
        self.assertIn("0.54", self.doc,
                      "измеренный промах на живой паре 27.07 не назван — без него порог "
                      "выглядит достаточным")
        # Проверка не вакуумна: порог действительно выше промаха, то есть повтор не ловился.
        self.assertGreater(self.notes._SIMILAR_THRESHOLD, 0.54)

    def test_the_function_itself_does_not_promise_to_be_wired(self):
        doc = self.notes.said_recently.__doc__ or ""
        self.assertIn("не вызывается", doc)
        self.assertNotIn("голос единственный", doc,
                         "прямая отправка идёт не через голосовой путь — это утверждение "
                         "перестало быть правдой раньше, чем было замечено")


class TestStatusIsNotCanon(unittest.TestCase):
    """STATUS.md пишется руками и отстаёт молча — он не должен звать себя каноном."""

    def setUp(self):
        self.text = _src("STATUS.md")
        self.assertTrue(self.text, "STATUS.md не читается")

    def test_no_self_declared_canon(self):
        self.assertNotIn("Канонические живые документы", self.text)
        self.assertIn("НЕ канон", self.text)

    def test_points_at_verifiable_sources(self):
        for needle in ("rails.registry()", "capabilities.snapshot()", "/opt/praxis"):
            self.assertIn(needle, self.text, f"не назван проверяемый источник {needle}")

    def test_stale_live_head_is_gone(self):
        head = re.search(r"\*\*Последний live-код прода:\*\*\s*`([0-9a-f]{7,40})`", self.text)
        self.assertIsNotNone(head, "строка про live-код прода потеряла форму")
        self.assertNotEqual(head.group(1), "e0150fe",
                            "это был live-код от 19.07; прод ушёл на 29 коммитов вперёд")

    def test_decision_closed_direction_is_not_open(self):
        block = re.search(r"\*\*Открытые направления[^\n]*\n(?:\s+[^\n]*\n)*", self.text)
        self.assertIsNotNone(block, "секция открытых направлений потеряла форму")
        self.assertNotIn("Этап 4", block.group(0),
                         "PASS 30 Этап 4 закрыт решением Егора — держать его открытым значит "
                         "показывать ей несуществующую работу")


class TestOutboundContourIsCountedNotRemembered(Base):
    """Исходящий контур манифест обязан СЧИТАТЬ, а не помнить.

    История, ради которой этот класс написан. Рельс обещал «единый outbound data-authority
    contract»; контур единым не был никогда — судья приватности (`agent.evaluate_reply`)
    стоит на голосе, а `send_message`/`narrate`/`send_file` его не зовут вовсе. 26.07 судья
    придержал её ход в AbstractDL, и через 48 секунд тот же материал ушёл прямым тулом
    (сообщение 94165). Обещание переписали честно — и это ПОЛОВИНА починки: переписанный
    текст точно так же держится на памяти и точно так же протухнет молча, если контур
    когда-нибудь сведут. Проверки ниже привязывают запись к живому исходнику: изменится
    число вызывающих — покраснеет здесь, а не выяснится через месяц по её ошибке.
    """

    def test_the_judge_has_exactly_one_call_site_today(self):
        """Опорный факт всей записи. Изменится — обязана измениться и запись."""
        sites = rails.outbound_judge_sites()
        self.assertEqual(
            len(sites), 1,
            f"число вызывающих evaluate_reply изменилось: {sites}. Рельс evaluator_mirror "
            f"и capabilities.reflexes.evaluator написаны под ОДИН судимый путь — "
            f"перечитай их текст, иначе манифест продолжит описывать вчерашний день")
        rel, line, owner = sites[0]
        self.assertEqual(rel, "agent.py")
        self.assertEqual(owner, "_guard_outbound",
                         "судья переехал из голосового пути — рельс называет голос")
        self.assertRegex(_src(rel).splitlines()[line - 1], r"evaluate_reply\(")

    def test_the_rail_names_the_site_it_counted(self):
        value = str(self.rail("evaluator_mirror")["value"])
        rel, line, owner = rails.outbound_judge_sites()[0]
        self.assertIn(f"{rel}:{line}", value, "рельс не показывает адрес, который сам посчитал")
        self.assertIn(owner, value)

    def test_the_counter_ignores_comments_and_the_definition(self):
        """Невакуумность счётчика: в agent.py про судью дважды написано в ПОЯСНЕНИЯХ.

        Наивный поиск насчитал бы три судимых пути там, где он один, — тем же способом
        соседний рельс однажды нашёл кап судьи внутри комментария и назвал ей число,
        которого в коде нет.
        """
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                "def evaluate_reply(text, context=''):\n"
                "    # судья (`evaluate_reply(collect_state=True)`) — здесь его не зовут\n"
                "    return ('ok', '')\n"
                "# ещё одна записка про evaluate_reply(...) для того, кто правит файл\n"
                if rel == "agent.py" else "")
            self.assertEqual(rails.outbound_judge_sites(), [],
                             "объявление или комментарий посчитаны за живой вызов")
        finally:
            rails._source_text = original

    def test_a_second_caller_makes_the_rail_stop_promising_the_hole(self):
        """Контур свели — рельс обязан сказать это САМ, а не повторять старое описание."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                "def _guard_outbound(reply):\n"
                "    verdict, reason = evaluate_reply(reply)\n"
                "    return verdict\n"
                if rel == "agent.py" else
                "def _sync_send_message(to, text):\n"
                "    v, r = agent.evaluate_reply(text)\n"
                "    return v\n"
                if rel == "mtproto_runner.py" else "x = 1\n")
            sites = rails.outbound_judge_sites()
            self.assertEqual(len(sites), 2, f"вызов через атрибут не посчитан: {sites}")
            self.assertEqual([s[0] for s in sites], ["agent.py", "mtproto_runner.py"])
            value = str(self.rail("evaluator_mirror")["value"])
            self.assertIn("2 путей", value)
            self.assertIn("mtproto_runner.py:2", value)
            self.assertNotIn("ровно ОДИН", value)
            self.assertNotIn("его не зовут вовсе", value,
                             "контур свели, а значение всё ещё обещает дыру")
            self.assertIn("вчерашний день", value,
                          "расхождение с текстом рельса должно быть названо вслух")
        finally:
            rails._source_text = original

    def test_a_judge_nobody_calls_is_said_out_loud(self):
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("def evaluate_reply(text):\n    return ('ok','')\n"
                                              if rel == "agent.py" else "x = 1\n")
            value = str(self.rail("evaluator_mirror")["value"])
            self.assertIn("НИ ОДНА дверь", value)
            self.assertIn("кред-пол", value)
        finally:
            rails._source_text = original

    def test_unreadable_doors_say_i_do_not_know(self):
        """«Не смогла прочесть» обязано выглядеть как «не знаю», а не как «путь один»."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("" if rel == "agent.py" else original(rel))
            value = str(self.rail("evaluator_mirror")["value"])
            self.assertIn("НЕ ВИДНО", value)
            self.assertIn("agent.py", value)
            self.assertNotIn("ровно ОДИН", value)
        finally:
            rails._source_text = original

    def test_capabilities_counts_the_same_doors_as_rails(self):
        """Два пересказа одного факта расходятся молча — счётчик обязан быть один."""
        line = capabilities._outbound_judge_line()
        rel, num, _owner = rails.outbound_judge_sites()[0]
        self.assertIn(f"{rel}:{num}", line)
        self.assertIn(line, capabilities.snapshot()["reflexes"]["evaluator"])

    def test_the_direct_doors_really_bypass_the_judge(self):
        """Обещание «идут мимо» проверяется по телу самих дверей, а не по счётчику.

        Счётчик говорит, СКОЛЬКО путей судится; здесь проверяется, что названные поимённо
        двери — вправду не из их числа, и что вместо судьи на них стоит то, что рельс и
        обещает: механический кред-пол.
        """
        import agent
        source = _src("agent.py").splitlines()
        for name in ("tool_send_message", "tool_narrate"):
            start = agent.__dict__[name].__code__.co_firstlineno
            # Тело режется по СЛЕДУЮЩЕМУ объявлению верхнего уровня, а не по «плюс N
            # строк»: окно наугад легко заезжает к соседу и находит там чужой кред-пол,
            # то есть подтверждает обещание уликой из другой функции.
            end = next((i for i in range(start, len(source))
                        if re.match(r"^(?:async )?def ", source[i])), len(source))
            body = "\n".join(source[start:end])
            self.assertGreater(len(body.splitlines()), 5, f"тело {name} не нарезалось")
            self.assertNotIn("evaluate_reply", body, f"{name} внезапно зовёт судью — "
                                                     f"рельс обещает обратное")
            self.assertIn("credential_floor", body,
                          f"{name} остался без кред-пола, а рельс обещает его как "
                          f"единственный предел на этой двери")

    def test_the_state_line_does_not_read_as_the_whole_outbound(self):
        line = capabilities.state_line()
        self.assertIn("на голосе", line)
        self.assertIn("прямые тулы", line,
                      "в STATE строка снова читается как «весь мой исходящий смотрят»")


class TestCredentialFloorPromisesOnlyWhatItReads(Base):
    """Файловая дверь кред-пола: обещание было шире кода.

    Запись говорила «не уходит наружу ни голосом, ни send_message, ни narrate, ни файлом».
    Про текст — правда. У файла пол СВОЙ (`document_floor`), и у него три остатка, каждый
    осознанный и ни один не названный ей: окно чтения, двоичный документ и своя личка
    Егора. Плюс подпись к файлу на прямом пути не читает никто. Проверки ниже держат
    текст рельса привязанным к этим четырём фактам: изменится код — покраснеет здесь.
    """

    RUNNER = "mtproto_runner.py"

    def _direct_file_body(self) -> str:
        src = _src(self.RUNNER)
        start = src.index("def _sync_send_file(")
        return src[start:start + 4000]

    def test_the_file_door_scans_bytes_not_the_caption(self):
        body = self._direct_file_body()
        self.assertIn("document_floor(path)", body, "файловая дверь перестала читать байты")
        self.assertNotIn("credential_floor(caption", body)
        self.assertNotIn("credential_floor(str(caption", body)
        self.assertIn("Подпись к файлу на ПРЯМОМ пути", self.text("credential_floor"),
                      "подпись не читает никто — это обязано быть сказано")

    def test_the_owner_dm_exemption_is_named(self):
        body = self._direct_file_body()
        self.assertIn("if not _dest_is_owner:", body,
                      "исключение для личка Егора исчезло — рельс о нём всё ещё говорит")
        self.assertIn("к самому Егору в личку вложение не сканируется",
                      self.text("credential_floor"))

    def test_the_read_window_is_named_with_its_number(self):
        import core.secrets as secrets
        self.assertIn(f"{secrets.DOCUMENT_SCAN_CAP // 1024} КиБ",
                      self.text("credential_floor"),
                      "окно чтения вложения не названо: секрет глубже него проходит молча")

    def test_a_binary_document_is_only_recognised_by_name(self):
        """Осознанный остаток: держать каждый архив было бы забором по её работе."""
        import core.secrets as secrets
        self.assertIsNone(secrets._decode_text_head(b"\x00\x01binary"),
                          "двоичный заголовок вдруг стал текстом — остаток изменился")
        self.assertIn("двоичный документ по содержимому не читается",
                      self.text("credential_floor"))

    def test_the_holds_no_longer_says_files_are_the_same_floor(self):
        holds = str(self.rail("credential_floor")["holds"])
        self.assertNotIn("ни файлом", holds,
                         "у файла пол свой и уже — плоское «ни файлом» это скрывало")
        self.assertIn("твёрдое", holds)


class TestProtectedRootsAreNotGuessedFromHere(Base):
    """Список закрытых корней живёт в окружении ДЕМОНА — из контейнера его не видно.

    Значением стоял литерал «по умолчанию список пуст»: утверждение о чужом процессе,
    сделанное отсюда. Сегодня оно верно; задай Егор корни завтра — рельс продолжил бы
    говорить «пуст», а отказы брокера стали бы для неё необъяснимыми.
    """

    def test_the_rail_does_not_assert_an_empty_list(self):
        value = str(self.rail("host_scope")["value"])
        self.assertIn("НЕ ВИДНО", value)
        self.assertNotIn("список пуст", value)
        self.assertIn("admin.advisor", value, "не названо место, где ответ есть")

    def test_rails_never_reads_that_variable_from_its_own_environment(self):
        """Прочитать os.getenv отсюда — ответ про МЕНЯ, выданный за ответ про брокера."""
        self.assertNotRegex(
            _src("rails.py"), r'getenv\(\s*"PRAXIS_PROTECTED_ROOTS"',
            "манифест читает окружение своего процесса и выдаёт его за конфигурацию демона")

    def test_the_daemon_really_publishes_it(self):
        """Обещанное место обязано существовать, иначе рельс отправляет её в никуда."""
        advisor = _src("serverd/advisor.py")
        self.assertIn("def manifest()", advisor)
        self.assertIn('"protected_roots"', advisor)
        self.assertIn('"floor"', advisor)
        self.assertIn("admin.advisor", _src("serverd_client.py"),
                      "глагола admin.advisor у клиента нет — спросить нечем")


class _FakeAgentSource:
    """Подменяет `rails._source_text` для agent.py и глушит чтение живого модуля.

    Глушение `_live` — не украшение. Оба словаря вердиктов рельс сперва берёт из уже
    поднятого `agent` (там применены все правки процесса), и если модуль в этом прогоне
    импортирован соседним тестом, подменённый исходник будет молча проигнорирован — тест
    станет вакуумным и позеленеет на чём угодно. Порядок тестов такому решать нельзя.
    """

    def __init__(self, case, text: str):
        self.case, self.text = case, text

    def __enter__(self):
        self._src, self._live = rails._source_text, rails._live
        original = self._src
        rails._source_text = lambda rel: (self.text if rel == "agent.py" else original(rel))
        rails._live = lambda module, name: (None if module == "agent"
                                            else self._live(module, name))
        return self

    def __exit__(self, *exc):
        rails._source_text, rails._live = self._src, self._live
        return False


class TestJudgeAdviceIsNotAHold(Base):
    """27.07, решение Егора: судья в разговорах ослаблен — он СОВЕТУЕТ, а не держит.

    Почему это отдельный класс, а не строка в описи. Утренняя редакция рельса описывала
    судью, который придерживал её слово, и недоступность которого была таким же отказом.
    Вечером обе конструкции убраны из agent.py. Ошибка «оставить вчерашний текст поверх
    сегодняшнего кода» здесь дороже обычного: манифест, обещающий придержку там, где её
    нет, заставляет её молчать по собственной воле — то есть строит забор словами после
    того, как забор сняли кодом. Поэтому ни одно утверждение записи не держится на памяти:
    что стопорит, что советует, будит ли совет и держит ли молчание судьи — всё считано.
    """

    STOP = "PRIVACY_HOLD_CREDENTIAL"

    def test_exactly_one_code_still_stops_her_and_the_rail_names_it(self):
        import agent
        self.assertEqual(tuple(agent._PRIVATE_DM_PRIVACY_HOLDS), (self.STOP,),
                         "набор придерживающих кодов изменился — рельс evaluator_mirror и "
                         "STATE написаны под «держит только кред на картинке»")
        value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn(f"ОСТАНАВЛИВАЕТ 1 код(ов) судьи: {self.STOP}", value)

    def test_every_advice_code_is_named_as_advice(self):
        """Совет назван поимённо: «часть вердиктов советует» ей ничего не даёт."""
        import agent
        self.assertTrue(agent._PRIVATE_DM_PRIVACY_ADVICE,
                        "словарь советов пуст — тогда сверка ниже вакуумна")
        value = str(self.rail("evaluator_mirror")["value"])
        for code in agent._PRIVATE_DM_PRIVACY_ADVICE:
            self.assertIn(code, value, f"{code} — совет, а рельс его не называет")
            self.assertNotIn(code, str(self.rail("evaluator_mirror")["holds"]),
                             "код совета попал в текст про то, что держит")

    def test_the_codes_really_behave_as_the_rail_says(self):
        """Сверка со СМЫСЛОМ, а не с двумя словарями: спрашиваем сам разбор вердикта."""
        import agent
        self.assertEqual(agent._privacy_code_verdict(self.STOP)[0], "deny")
        for code in agent._PRIVATE_DM_PRIVACY_ADVICE:
            self.assertEqual(agent._privacy_code_verdict(code)[0], "advice",
                             f"{code} разбирается не как совет, а рельс обещает совет")

    def test_advice_reaches_her_and_does_not_wake_her(self):
        value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("совет доезжает дневником", value)
        self.assertIn("не будит", value)
        # Невакуумность: вернём пробуждение в ветку совета — рельс обязан сказать это сам.
        with _FakeAgentSource(self, _AGENT_ADVICE_WAKES):
            loud = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("БУДИТ меня", loud,
                      "пробуждение на каждое замечание вернулось, а манифест молчит")

    def test_a_silent_advice_channel_is_shouted(self):
        with _FakeAgentSource(self, _AGENT_ADVICE_SILENT):
            value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("НИКУДА не пишется", value,
                      "вердикт вынесен, следа у неё нет — и манифест это скрыл")

    def test_unavailability_no_longer_stops_her(self):
        """Оба места, где молчание чужой модели держало её слово, обязаны остаться снятыми."""
        code = re.sub(r"(?m)#.*$", "", _src("agent.py"))
        self.assertNotRegex(code, r'RunStopped\(\s*["\'][^"\']*advisor unavailable',
                            "возобновлённый ход снова останавливается из-за молчания судьи")
        self.assertNotRegex(code, r'advisor_verdict"\)\s*==\s*"unavailable"',
                            "расписка «судья не ответил» снова гоняет гард заново")
        value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("недоступность судьи не держит и не советует ничего", value)
        with _FakeAgentSource(self, _AGENT_UNAVAILABLE_STOPS):
            loud = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("СНОВА держит мой ход", loud,
                      "fail-closed по недоступности вернулся, а манифест обещает обратное")

    def test_moving_a_code_back_into_holds_is_said_out_loud(self):
        with _FakeAgentSource(self, _AGENT_CROSS_PERSON_HOLDS):
            value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("ОСТАНАВЛИВАЕТ 2 код(ов)", value)
        self.assertIn("это НЕ только кред на картинке", value,
                      "стопор добавили молча — ровно тот класс правки, который манифест "
                      "уже дважды за сутки не заметил")

    def test_removing_the_split_altogether_is_shouted(self):
        with _FakeAgentSource(self, _AGENT_NO_ADVICE_DICT):
            value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("разделения на стоп и совет в коде БОЛЬШЕ НЕТ", value)
        self.assertIn("вчерашний день", value)

    def test_unreadable_agent_says_i_do_not_know(self):
        with _FakeAgentSource(self, ""):
            value = str(self.rail("evaluator_mirror")["value"])
        self.assertIn("НЕ ВИДНО", value)
        self.assertNotIn("ОСТАНАВЛИВАЕТ 1", value)

    def test_the_rail_text_no_longer_promises_a_hold(self):
        """Утренняя формулировка не имеет права пережить вечерний код."""
        holds = str(self.rail("evaluator_mirror")["holds"])
        self.assertIn("НИЧЕГО НЕ ОСТАНАВЛИВАЕТ", holds)
        self.assertIn("СОВЕТ", holds)
        self.assertNotIn("типизированная проверка полномочия", holds,
                         "вернулась утренняя формулировка про проверку, которая держит")
        self.assertIn("не удаляется", holds,
                      "молчаливая потеря вложения за неоценимое медиа снята — а её и надо "
                      "назвать, иначе она продолжит считать, что рискует работой")

    def test_state_line_and_snapshot_speak_the_same_authority(self):
        """Два пересказа одного факта расходятся молча — источник обязан быть один."""
        line = capabilities.state_line()
        self.assertIn("СОВЕТУЕТ", line)
        self.assertIn("прямые тулы", line)
        self.assertIn("на голосе", line)
        self.assertIn(rails.outbound_judge_authority(),
                      capabilities.snapshot()["reflexes"]["evaluator"])

    def test_state_line_shouts_if_the_hold_comes_back(self):
        with _FakeAgentSource(self, _AGENT_CROSS_PERSON_HOLDS):
            line = capabilities._outbound_stop_word()
        self.assertIn("⚠", line)
        self.assertIn("PRIVACY_HOLD_CROSS_PERSON", line)


# Подложные исходники agent.py для проверок выше. Держатся рядом с классом намеренно:
# каждый отличается от соседа ровно одной чертой, ради которой написан.
_AGENT_DICTS = (
    '_PRIVATE_DM_PRIVACY_HOLDS = {\n'
    '    "PRIVACY_HOLD_CREDENTIAL": "privacy:credential",\n'
    '}\n'
    '_PRIVATE_DM_PRIVACY_ADVICE = {\n'
    '    "PRIVACY_HOLD_CROSS_PERSON": "privacy:cross-person-private",\n'
    '}\n'
)
_AGENT_JUDGE_CALL = (
    'def _guard_outbound(reply):\n'
    '    verdict, reason = evaluate_reply(reply)\n'
)
_AGENT_ADVICE_OK = (
    '    if verdict == "advice":\n'
    '        tool_journal("[совет] " + reason)\n'
    '    if verdict == "deny":\n'
    '        _held_self_wake(ctx, reason=reason, reply=reply)\n'
    '    return reply\n'
)
_AGENT_ADVICE_WAKES = _AGENT_DICTS + _AGENT_JUDGE_CALL + (
    '    if verdict == "advice":\n'
    '        tool_journal("[совет] " + reason)\n'
    '        _held_self_wake(ctx, reason=reason, reply=reply)\n'
    '    if verdict == "deny":\n'
    '        return ""\n'
    '    return reply\n'
)
_AGENT_ADVICE_SILENT = _AGENT_DICTS + _AGENT_JUDGE_CALL + (
    '    if verdict == "advice":\n'
    '        pass\n'
    '    if verdict == "deny":\n'
    '        return ""\n'
    '    return reply\n'
)
_AGENT_UNAVAILABLE_STOPS = _AGENT_DICTS + _AGENT_JUDGE_CALL + (
    '    if verdict == "unavailable":\n'
    '        raise RunStopped("privacy advisor unavailable; retry outbound guard")\n'
) + _AGENT_ADVICE_OK
_AGENT_CROSS_PERSON_HOLDS = (
    '_PRIVATE_DM_PRIVACY_HOLDS = {\n'
    '    "PRIVACY_HOLD_CREDENTIAL": "privacy:credential",\n'
    '    "PRIVACY_HOLD_CROSS_PERSON": "privacy:cross-person-private",\n'
    '}\n'
    '_PRIVATE_DM_PRIVACY_ADVICE = {\n'
    '    "PRIVACY_HOLD_CROSS_CHAT": "privacy:cross-chat-private",\n'
    '}\n'
) + _AGENT_JUDGE_CALL + _AGENT_ADVICE_OK
_AGENT_NO_ADVICE_DICT = (
    '_PRIVATE_DM_PRIVACY_HOLDS = {\n'
    '    "PRIVACY_HOLD_CREDENTIAL": "privacy:credential",\n'
    '    "PRIVACY_HOLD_CROSS_PERSON": "privacy:cross-person-private",\n'
    '}\n'
) + _AGENT_JUDGE_CALL + _AGENT_ADVICE_OK


class TestJudgeEvidenceCapsSplitNamedFromSilent(Base):
    """Рельс говорил «сам судья об усечении НЕ предупреждается» — а это уже неправда.

    У блоков STATE и ориентации владелец agent.py дописал к содержимому «cap N chars»,
    у остальных пяти видов улик срез так и остался немым. Разница не косметическая:
    молчаливо урезанная лента выглядит для судьи как вся лента, и вердикт о её слове
    выносится по куску, о котором судья не знает. Значит и здесь список «кто из них кто»
    обязан считаться, а не переписываться руками.
    """

    def test_named_caps_are_really_named_in_the_block(self):
        import agent
        code = re.sub(r"(?m)#.*$", "", _src("agent.py"))
        value = str(self.rail("evaluator_evidence_caps")["value"])
        for const in ("_GUARD_STATE_CHARS", "_GUARD_TOPIC_CHARS"):
            self.assertIn(f"cap {{{const}}}", code,
                          f"{const} перестал называться судье — значит он в НЕМОМ списке")
            self.assertIn(str(getattr(agent, const)), value)
        self.assertIn("срез НАЗВАН судье у:", value)

    def test_the_silent_ones_are_listed_as_silent(self):
        value = str(self.rail("evaluator_evidence_caps")["value"])
        head, _, tail = value.partition("НЕМОЙ срез у:")
        self.assertTrue(tail, "немого списка нет, хотя немые срезы в agent.py есть")
        for name in ("лента канала", "прожитые ходы", "рамка аудитории"):
            self.assertIn(name, tail, f"{name} режется молча, а рельс числит его названным")
            self.assertNotIn(name, head.partition("срез НАЗВАН судье у:")[2])

    def test_the_verdict_ceiling_is_named_with_what_it_now_means(self):
        """Восьмой предел контура — на ВЫХОДЕ судьи, и его смысл сменился вместе с волной."""
        import agent
        value = str(self.rail("evaluator_evidence_caps")["value"])
        self.assertIn(f"{agent._GUARD_VERDICT_MAX_TOKENS} токенов", value)
        self.assertIn("совета не будет вовсе", value,
                      "исчерпание потолка описано как придержка — это вчерашний смысл")

    def test_naming_a_cap_moves_it_out_of_the_silent_list(self):
        """Невакуумность: припишут «cap N» к ленте — рельс обязан перестать звать её немой."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: (
                original(rel) + '\nf"cap 6000 chars, cut on line boundaries"\n'
                if rel == "agent.py" else original(rel))
            value = str(self.rail("evaluator_evidence_caps")["value"])
        finally:
            rails._source_text = original
        named = value.partition("срез НАЗВАН судье у:")[2].partition("НЕМОЙ срез у:")[0]
        self.assertIn("лента канала", named,
                      "приписку к блоку добавили, а рельс всё ещё зовёт срез немым")


class TestNoteLimitsSurviveWithoutTheModule(Base):
    """Фолбэк рельса записки был сломан молча — и молчал ровно про то, ради чего написан.

    `_LOCK_WAIT_SEC` читался из исходника БЕЗ приведения к float, то есть `int("5.0")`
    падал, значение становилось None, и в любом процессе, где `notes` не поднят, весь
    рельс отвечал «пределы записки не прочитались». Тест ловит именно эту ветку: живой
    модуль глушится, читать остаётся только код.
    """

    def _value_without_module(self) -> str:
        original = rails._live
        try:
            rails._live = lambda module, name: (None if module == "notes"
                                                else original(module, name))
            return str(self.rail("scratch_note_lock")["value"])
        finally:
            rails._live = original

    def test_all_four_waits_are_read_from_the_source(self):
        import notes
        value = self._value_without_module()
        self.assertNotIn("не прочиталось", value)
        self.assertNotIn("⚠ пределы записки не прочитались", value)
        for const in ("_LOCK_WAIT_SEC", "_LOCK_WAIT_DEGRADED_SEC",
                      "_LOCK_DEGRADE_FOR_SEC", "_PROBE_WAIT_SEC"):
            number = f"{float(getattr(notes, const)):g}с"
            self.assertIn(number, value, f"{const}={number} не доехал до манифеста без "
                                         f"живого модуля — фолбэк снова молчит")

    def test_the_short_wait_is_named_as_a_replacement_of_the_full_one(self):
        """«до 5с» перестаёт быть правдой после первого истечения — это обязано быть сказано."""
        value = str(self.rail("scratch_note_lock")["value"])
        self.assertIn("после первого же ИСТЕЧЕНИЯ", value)
        self.assertIn("первый удавшийся захват снимает режим сразу", value)

    def test_the_trim_ceiling_is_still_a_product_of_two_live_numbers(self):
        import notes
        value = str(self.rail("scratch_note_lock")["value"])
        ceiling = int(notes.MAX_LINES * notes._UNLOCKED_TRIM_FACTOR)
        self.assertIn(f"{ceiling} строк", value)
        self.assertIn(f"{notes.MAX_LINES}×{int(notes._UNLOCKED_TRIM_FACTOR)}", value)


class TestDaemonLimitsIntroducedTonightAreNamed(Base):
    """Пределы демона, заведённые волной 27.07: списки, логи, сроки op.run и op.stop.

    Их родные файлы (`serverd/*`) не входят в discover и живут на ХОСТЕ, а не в её
    контейнере — тем важнее, чтобы числа приезжали к ней манифестом, а не только текстом
    отказа. Все читаются из исходника: откатят файл — рельс скажет «не прочиталось»,
    и это увидит сверка чисел, а не она своим удивлением.
    """

    def test_the_op_run_grace_says_whose_deadline_fires_first(self):
        value = str(self.rail("host_operation_caps")["value"])
        self.assertIn("первым срабатывает мой срок", value,
                      "надбавка названа, а смысл её — чей таймаут раньше — нет")

    def test_git_silence_is_not_reported_as_absence_of_git(self):
        value = str(self.rail("host_operation_caps")["value"])
        self.assertIn("«не ответил» — это НЕ «git тут нет»", value)

    def test_list_caps_name_the_one_that_feeds_her_own_decisions(self):
        value = str(self.rail("host_list_caps")["value"])
        self.assertIn("op.list", value)
        self.assertIn("op.poll", value)
        why = str(self.rail("host_list_caps")["why"])
        self.assertIn("закрываю собственные задачи", why,
                      "не сказано, ЧЕМ обрезанный список опасен именно ей")

    def test_stop_grace_is_read_from_hostproc_and_not_remembered(self):
        rel = "serverd/hostproc.py"
        for const in ("TERM_GRACE_SEC", "START_GRACE_SEC"):
            self.assertRegex(_src(rel), rf"(?m)^{const} = [\d.]+",
                             f"{const} исчез из {rel} — рельс host_operation_stop обещает "
                             f"число, которого больше нет в коде")
        value = str(self.rail("host_operation_stop")["value"])
        self.assertIn("SIGKILL", value)
        self.assertIn("не знаю, что останавливать", value)

    def test_the_rail_admits_it_when_hostproc_loses_the_constants(self):
        """Откатят файл — обязано выйти «не знаю», а не вчерашнее число."""
        original = rails._source_text
        try:
            rails._source_text = lambda rel: ("" if rel == "serverd/hostproc.py"
                                              else original(rel))
            value = str(self.rail("host_operation_stop")["value"])
        finally:
            rails._source_text = original
        self.assertIn("НЕ ВИДНО", value)
        self.assertNotIn("SIGKILL", value)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEveryReflexReachesHer(unittest.TestCase):
    """Ключ в словаре, который никто не печатает, — это молчаливо отнятая возможность.

    28.07: два ключа `reflexes` (`mine` и `hands`) лежали в snapshot() и НЕ попадали в
    describe() ни разу. Через `mine` до неё шёл единственный указатель на директиву
    `РЕЖИМ:` — механизм существовал, был описан в манифесте и применён ноль раз за всю
    историю, потому что она о нём не знала. Заметить это глазами нельзя: словарь и рендер
    лежат в трёхстах строках друг от друга. Пусть замечает тест.
    """

    def test_each_reflex_key_appears_in_what_she_reads(self):
        import capabilities
        snap = capabilities.snapshot()
        reflexes = snap.get("reflexes") or {}
        self.assertGreater(len(reflexes), 8, "рефлексов не нашлось — сверка вакуумна")
        text = capabilities.describe("owner")
        lost = []
        for key, value in reflexes.items():
            body = str(value or "").strip()
            if not body:
                continue
            # Сверяем по НАЧАЛУ строки: хвост может быть подрезан рендером, но если ключ
            # печатается вообще, его первые слова обязаны найтись.
            head = body[:40]
            if head not in text:
                lost.append(key)
        self.assertFalse(lost, f"эти рефлексы есть в snapshot(), но до неё не доходят: {lost} — "
                               f"возможность, о которой она не знает, это та же ложь, что "
                               f"молчаливый гейт")
