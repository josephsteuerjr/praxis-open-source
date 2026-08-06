"""След кадра: из каких отрезков физически состоит вход, уехавший в модель.

Прибор отвечает на один вопрос — «где какой кусок кадра начинается, сколько он занял и
что не поехало». Он НАБЛЮДАТЕЛЬ: `mark()` возвращает переданный текст ТЕМ ЖЕ ОБЪЕКТОМ,
поэтому ни один байт кадра не меняется от того, включён след или выключен.

Модуль намеренно самостоятельный: только stdlib, ни одного импорта из `agent`,
`run_manager`, `run_context`, `llm`. Обратная связь только через аргументы вызовов —
иначе прибор стал бы частью того, что он измеряет.

Что прибор обязан сказать о себе вслух (читатель следа не должен сделать неверный вывод):

1. Смещения — ТОЛЬКО внутри зоны. Сквозной оси кадра не существует: anthropic получает
   список блоков, openai — склейку через "\\n\\n" (llm.system_text), а PRAXIS_PROMPT_CACHE=0
   — склейку без шва. Отсюда «зона + смещение внутри зоны» и поле `system_form`.
2. `basis: "live"` — след описывает текст, уехавший В МОДЕЛЬ, а не текст расписки.
   Расписка проходит через `_scrub_critical_value`, и при account-critical секретах её
   длины отличаются. Факт возможного расхождения назван полем `receipt.scrubbed_possible`;
   пере-якорить смещения на подчищенный текст запрещено.
3. Бюджет PRAXIS_CONTEXT_BUDGET считает СИМВОЛЫ и НЕ считает: шапку evidence,
   extra_system, extra_evidence, обёртку <praxis_context_evidence>, схемы рук, сообщения
   (см. BUDGET_EXCLUDES). «Кадр не влез» = «не влезли тиры», и только они.
4. Один кадр = один полный след + N тонких ссылок ({frame_id, emit, reused}). Число
   расписок НЕ равно числу кадров: вторая и далее итерация тул-цикла шлёт тот же кадр.
5. Зона `evidence` живёт в `messages[-1]`, а не в системном промпте.
6. Длина секции `state.state_block` скачет от хода к ходу при неизменном коде: внутри
   `build_state_block` семнадцать независимых `try/except: pass`. Дифф длины ≠ правка кода.
7. Длина `state.owner_place` меняется на десятки символов между тремя ветками — сравнивать
   только с учётом поля `variant`.
8. Профиль комнаты читается с диска трижды за кадр — совпадение содержимого не
   гарантировано даже внутри одного кадра.
9. Токены здесь не оцениваются и не печатаются ни в каком виде. Единицы — символы Python
   (`len`) и байты UTF-8. Прибор печатает наблюдённое, а не производную.

⛔ ЗАПРЕТ, который проходит по ВИДУ события, а не по месту вызова. Метаданные этого следа
   законны ТОЛЬКО на расписке `event_kind="model_input"`. Тот же ключ на расписке
   `tool_result` даст `ResumeEvidenceError` на каждом прогоне с медиа: `run_resume`
   валидирует metadata строк `kind == "tool_result"` по схеме
   `praxis.tool-result.metadata.v1` и любую чужую форму считает поломкой. Рядом со
   `store_result` эта граница нигде не задокументирована.

⚠ Ретенции прогонов не существует: всё, что прибор допишет в events.jsonl, останется
   навсегда. Прибавка — порядка 5.5 КиБ на КАДР плюс ~110 Б на каждую следующую итерацию
   тул-цикла. Тонкие ссылки придуманы ровно ради второго слагаемого.

⚠ Метаданные обязаны быть ДЕТЕРМИНИРОВАНЫ: `run_manager.store_result(idempotent=True)`
   сверяет их на строгое равенство и при расхождении бросает `RunConflict`, а тот в
   `_model_call` уходит в `_stop_for_durability` — то есть ход убит. Поэтому здесь нет ни
   времени, ни uuid, ни чтения окружения после `start()`, ни обхода `set`, ни `float`.

Рычаг: PRAXIS_FRAME_TRACE = on | off | strict. ОТСУТСТВИЕ переменной означает "on" —
`_standenv.sanitize` снимает все PRAXIS_* вне ENV_KEEP/ENV_PIN, и любое умолчание "off"
означало бы, что на стенде прибор молча выключен, а гейт при этом говорит OK.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os

SCHEMA = "praxis.frame-trace.v1"
ROSTER = "v1"

# Две последние зоны зарезервированы под этап 2: разговор и схемы рук сегодня не размечены.
ZONES = ("persona", "dynamic", "evidence", "messages", "tools")
KINDS = ("md", "text", "json", "jsonl", "frame", "marker")
REASONS = ("context_budget", "empty", "branch", "cap")

# Закрытый реестр системных зон. Каждое имя в КАЖДОМ кадре обязано быть либо помечено
# (mark), либо объявлено отсутствующим (absent). Реестр зоны evidence ОТКРЫТ: тир
# опознаётся своим ярлыком (label), а не именем, и тиры, которые не собрались вовсе,
# записи сегодня не получают — это названная граница этапа 1, а не умолчание.
SYSTEM_ROSTER = (
    "persona.soul", "persona.voice", "persona.self_current",
    "contract.base", "state.owner_place", "contract.owner_tools",
    "contract.appetite", "state.state_block",
    "contract.family_audience", "contract.unknown_authority",
    "state.room_mode_enum", "state.channel_facts",
)

# Что бюджет PRAXIS_CONTEXT_BUDGET заведомо НЕ считает: он сложен из len(persona) и
# len(tail_text) ДО того, как появятся шапка evidence и дописывания вне билдера.
BUDGET_EXCLUDES = ("evidence_header", "extra_system", "extra_evidence",
                   "evidence_envelope", "omitted_marker", "tools", "messages")

# Что этап 1 измеряет и чего не измеряет. Публикуется в самом конверте: читатель, который
# сложит bytes трёх зон и назовёт это «её кадром», ошибётся — схемы рук и разговор весят
# своё, и молчание об этом было бы занижением, а не скромностью.
COVERS = ("persona", "dynamic", "evidence")
UNMEASURED = ("messages", "tools")

MAX_SECTIONS = 128              # сверх — публикуем первые и говорим об этом в caps
MAX_METADATA_BYTES = 64 * 1024  # свой потолок, ×16 запаса до 1 МиБ run_manager
MAX_EMIT_KEYS = 64              # сверх — поле emit не пишется вовсе (не врать)

ENV_LEVER = "PRAXIS_FRAME_TRACE"
MODES = ("on", "off", "strict")

# Внутренний пол на число записей: патологический цикл разметки не имеет права съесть
# память. Сверх него мы перестаём записывать — и склейка честно разойдётся (honesty.ok=false),
# вместо того чтобы тихо соврать геометрией.
_MAX_RECORDS = 4096

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("praxis_frame_trace", default=None)
_MODE_OVERRIDE: str | None = None


class FrameTraceMismatch(RuntimeError):
    """Склейка размеченных отрезков разошлась с текстом, уехавшим в модель."""


# --------------------------------------------------------------------------- режимы


def mode() -> str:
    """on | off | strict. Отсутствие переменной = on (см. докстринг модуля)."""
    if _MODE_OVERRIDE is not None:
        return _MODE_OVERRIDE
    raw = (os.getenv(ENV_LEVER) or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return "off"
    if raw == "strict":
        return "strict"
    return "on"


def set_mode(value: str | None) -> str | None:
    """Переключить режим В ОБХОД окружения; возвращает ПРЕЖНИЙ override (может быть None).

    Тесты обязаны ходить сюда, а не в os.environ: детектор утечек стенда краснит любую
    PRAXIS_*, оставшуюся после теста. Восстановление — `addCleanup(set_mode, prev)`.
    """
    global _MODE_OVERRIDE
    previous = _MODE_OVERRIDE
    if value is None:
        _MODE_OVERRIDE = None
    else:
        text = str(value).strip().lower()
        _MODE_OVERRIDE = text if text in MODES else None
    return previous


# ------------------------------------------------------------------------ утилиты


def _first_diff(a: str, b: str) -> int:
    """Индекс первого различающегося символа, либо min(len) если один — префикс другого."""
    n = min(len(a), len(b))
    if a[:n] == b[:n]:
        return n
    lo, hi = 0, n  # P(lo) — «первые lo символов совпали» — истинно, P(hi) ложно
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid
    return lo


def _digest_pair(first: str, second: str) -> str:
    return hashlib.sha256(
        (first + "\x00" + second).encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def _extract_id(system) -> str | None:
    """Отпечаток по ФАКТИЧЕСКОМУ объекту system — защита от просроченного ContextVar.

    Чужой кадр (путь возобновления, фоновая нить, вложенный прогон) следа не получит
    никогда: пересчёт делается на каждом обращении, а не один раз при опечатывании.
    """
    if isinstance(system, str):
        return _digest_pair(system, "")
    if isinstance(system, list):
        def _text(index: int) -> str:
            if index >= len(system):
                return ""
            block = system[index]
            if isinstance(block, dict):
                value = block.get("text")
                return value if isinstance(value, str) else ""
            return ""
        return _digest_pair(_text(0), _text(1))
    return None


def _encode(payload: dict) -> bytes:
    """Ровно те флаги, которыми нормализует run_manager.store_result."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


# -------------------------------------------------------------------------- записи


class _Rec:
    __slots__ = ("name", "zone", "kind", "label", "variant",
                 "included", "reason", "text", "offset", "chars", "nbytes")

    def __init__(self, name: str, zone: str, kind: str, *, label: str, variant: str,
                 included: bool, reason: str, text, chars: int) -> None:
        self.name = name
        self.zone = zone
        self.kind = kind
        self.label = label
        self.variant = variant
        self.included = included
        self.reason = reason
        self.text = text
        self.offset = -1
        self.chars = chars
        self.nbytes = -1


class Trace:
    """Один кадр. После `seal` держит только числа и хэши — тексты сброшены."""

    __slots__ = ("mode", "usable", "sealed", "overflow",
                 "_by_zone", "_count", "budget", "counts", "embed",
                 "zones", "honesty", "frame_id", "verify_id",
                 "system_form", "dynamic_offset", "emits", "emit_seq")

    def __init__(self, trace_mode: str) -> None:
        self.mode = trace_mode
        self.usable = True
        self.sealed = False
        self.overflow = False
        self._by_zone: dict[str, list[_Rec]] = {}
        self._count = 0
        self.budget: dict | None = None
        self.counts: dict = {}
        self.embed: dict[str, dict] = {}
        self.zones: list[dict] = []
        self.honesty: dict = {"ok": True}
        self.frame_id = ""
        self.verify_id: str | None = None
        self.system_form = ""
        self.dynamic_offset: int | None = None
        self.emits: dict[str, int] = {}
        self.emit_seq = 0

    # --- разметка ---

    def _add(self, rec: _Rec, *, first: bool) -> None:
        if self._count >= _MAX_RECORDS:
            self.overflow = True
            return
        bucket = self._by_zone.setdefault(rec.zone, [])
        if first:
            bucket.insert(0, rec)
        else:
            bucket.append(rec)
        self._count += 1

    def _drop_texts(self) -> None:
        for bucket in self._by_zone.values():
            for rec in bucket:
                rec.text = None

    # --- выдача ---

    def _zone_names(self) -> list[str]:
        extra = [name for name in self._by_zone if name not in ZONES]
        return [name for name in ZONES] + sorted(extra)

    def section_rows(self) -> list[dict]:
        out: list[dict] = []
        for zone in self._zone_names():
            for rec in self._by_zone.get(zone, ()):
                row: dict = {"name": rec.name, "zone": rec.zone, "kind": rec.kind,
                             "included": bool(rec.included)}
                if rec.included:
                    if rec.offset >= 0:
                        row["offset"] = int(rec.offset)
                    row["chars"] = int(rec.chars)
                    if rec.nbytes >= 0:
                        row["bytes"] = int(rec.nbytes)
                else:
                    row["reason"] = rec.reason
                    if rec.chars:
                        row["chars"] = int(rec.chars)
                if rec.label:
                    row["label"] = rec.label
                if rec.variant:
                    row["variant"] = rec.variant
                out.append(row)
        return out


# ------------------------------------------------------------------ жизненный цикл


def start():
    """Привязать свежий след к текущему контексту. None, если режим off."""
    trace_mode = mode()
    if trace_mode == "off":
        return None
    try:
        return _CURRENT.set(Trace(trace_mode))
    except Exception:
        return None


def finish(token) -> None:
    """ОБЯЗАТЕЛЕН в finally: восстанавливает внешний след (вложенные прогоны законны)."""
    if token is None:
        return
    try:
        _CURRENT.reset(token)
    except Exception:
        pass


def clear() -> None:
    """Явно снять след: «этот кадр собран не здесь» (путь возобновления)."""
    try:
        _CURRENT.set(None)
    except Exception:
        pass


def current() -> Trace | None:
    try:
        return _CURRENT.get()
    except Exception:
        return None


@contextlib.contextmanager
def capture():
    """Для тестов: start + finish вокруг блока."""
    token = start()
    try:
        yield current()
    finally:
        finish(token)


# ----------------------------------------------------------------------- разметка


def mark(name: str, zone: str, kind: str, text: str, *,
         label: str = "", variant: str = "", first: bool = False) -> str:
    """Вернуть `text` ТЕМ ЖЕ ОБЪЕКТОМ, попутно записав отрезок. Никогда не бросает.

    Возврат того же объекта — не косметика: он позволяет обернуть метку прямо вокруг
    существующего выражения (`tail.append(mark(...) + mark(...))`), не переписывая ни
    одного литерала. Отсюда же железное требование среза: кадр байт-в-байт одинаков с
    прибором и без него.

    `first=True` вставляет запись в НАЧАЛО своей зоны — нужно ровно один раз, потому что
    шапка evidence вычисляется последней, а в кадре стоит первой.
    """
    try:
        trace = _CURRENT.get()
        if trace is None or trace.sealed or not isinstance(text, str):
            return text
        trace._add(_Rec(str(name), str(zone), str(kind), label=str(label),
                        variant=str(variant), included=True, reason="",
                        text=text, chars=len(text)), first=bool(first))
    except Exception:
        pass
    return text


def absent(name: str, zone: str, kind: str, reason: str, *,
           label: str = "", chars: int = 0) -> None:
    """Объявить «секции нет и вот почему». Никогда не бросает.

    Три разных нуля — это три РАЗНЫЕ записи: «ветка не выбрана» (branch), «источник пуст»
    (empty), «не влезло в бюджет» (context_budget, с длиной того, что не влезло). Четвёртый
    случай — «до решения вообще не дошли» — записи не получает вовсе и ловится сверкой с
    SYSTEM_ROSTER.
    """
    try:
        trace = _CURRENT.get()
        if trace is None or trace.sealed:
            return
        trace._add(_Rec(str(name), str(zone), str(kind), label=str(label),
                        variant="", included=False, reason=str(reason),
                        text=None, chars=int(chars or 0)), first=False)
    except Exception:
        pass


def note_budget(*, limit: int, used_start: int, used_final: int,
                offered: int, included: int, dropped: int) -> None:
    """Наблюдение без текста. `used_start` обязан быть ТЕМ ЖЕ выражением, что в коде."""
    try:
        trace = _CURRENT.get()
        if trace is None or trace.sealed:
            return
        trace.budget = {
            "limit": int(limit), "used_start": int(used_start),
            "used_final": int(used_final), "unit": "chars",
            "excludes": list(BUDGET_EXCLUDES),
        }
        trace.counts = {
            "tiers_offered": int(offered),
            "tiers_included": int(included),
            "tiers_dropped": int(dropped),
        }
    except Exception:
        pass


def note_embed(zone: str, **counts: int) -> None:
    """Как зона легла в ЧУЖОЙ контейнер: обрезка, соседи, обёртка. Никогда не бросает."""
    try:
        trace = _CURRENT.get()
        if trace is None or trace.sealed:
            return
        bucket = trace.embed.setdefault(str(zone), {})
        for key, value in counts.items():
            bucket[str(key)] = int(value)
    except Exception:
        pass


# ------------------------------------------------------------------- опечатывание


def _containers(system, evidence: str) -> dict:
    if isinstance(system, list):
        persona_container = "system[0].text"
        dynamic_container = "system[1].text" if len(system) > 1 else "(absent)"
    else:
        persona_container = "system"
        dynamic_container = "system"
    return {
        "persona": persona_container,
        "dynamic": dynamic_container,
        "evidence": "messages[-1]" if str(evidence or "") else "(absent)",
    }


def _seal_impl(trace: Trace, persona: str, dynamic: str, evidence: str, system) -> None:
    actual = {"persona": persona, "dynamic": dynamic, "evidence": evidence}
    containers = _containers(system, evidence)
    zones_out: list[dict] = []
    honesty: dict = {"ok": True}
    for zone in ("persona", "dynamic", "evidence"):
        included = [rec for rec in trace._by_zone.get(zone, ()) if rec.included]
        marked = "".join(rec.text if isinstance(rec.text, str) else "" for rec in included)
        real = actual[zone] if isinstance(actual[zone], str) else ""
        offset = 0
        for rec in included:
            body = rec.text if isinstance(rec.text, str) else ""
            rec.offset = offset
            rec.chars = len(body)
            rec.nbytes = len(body.encode("utf-8"))
            offset += rec.chars
        row: dict = {
            "zone": zone,
            "container": containers[zone],
            "chars": len(real),
            "bytes": len(real.encode("utf-8")),
            "sha256": hashlib.sha256(real.encode("utf-8")).hexdigest(),
            "honest": marked == real,
        }
        embedded = trace.embed.get(zone)
        if embedded:
            row["embed"] = {key: int(value) for key, value in sorted(embedded.items())}
        zones_out.append(row)
        if marked != real and honesty.get("ok"):
            # Смещения ВСЁ РАВНО публикуются: они описывают то, что было размечено.
            honesty = {"ok": False, "zone": zone, "at": _first_diff(marked, real),
                       "marked_chars": len(marked), "actual_chars": len(real)}
    trace.zones = zones_out
    trace.honesty = honesty
    trace.frame_id = _digest_pair(persona, dynamic)
    trace.verify_id = _extract_id(system)
    trace.system_form = "blocks" if isinstance(system, list) else "string"
    trace.dynamic_offset = len(persona) if trace.system_form == "string" else None
    if trace.verify_id is None:
        trace.usable = False


def seal(*, persona: str, dynamic: str, evidence: str, system) -> None:
    """Опечатать след: сверить склейку, посчитать смещения и СБРОСИТЬ ссылки на тексты.

    Вызывается ровно один раз, на шве сборки кадра, ПОСЛЕ `system = _system(...)`. Точка
    правды именно здесь, а не на возврате билдера: между ними хвост дописывается
    (extra_system, подсказка компактирования), и съём раньше был бы ложью.
    """
    trace = _CURRENT.get()
    if trace is None or trace.sealed:
        return
    try:
        _seal_impl(trace, persona if isinstance(persona, str) else "",
                   dynamic if isinstance(dynamic, str) else "",
                   evidence if isinstance(evidence, str) else "", system)
    except Exception:
        # Прибор сломался — он умолкает, а не роняет её ход.
        trace.usable = False
    finally:
        trace.sealed = True
        trace._drop_texts()


def assert_honest() -> None:
    """Покраснеть, если след разошёлся с кадром или прибор не смог опечатать. Зовут ТЕСТЫ.

    ⚠ Раньше это жило внутри `seal` и в режиме `strict` бросало прямо со шва сборки кадра.
    Так наблюдатель получал право убивать наблюдаемое: `PRAXIS_FRAME_TRACE=strict` можно
    поставить из пульта (`panel.env_apply` принимает любой неизвестный `PRAXIS_*`), и тогда
    первая же неразмеченная дописка в хвост стоила бы ей КАЖДОГО хода, пока рычаг не снимут.
    Краснота — дело гейта, а не её разговора; поэтому единственный бросок живёт здесь, а
    `seal` молчит всегда. Второе отличие от прежнего: падение самого прибора (`usable=False`)
    теперь тоже краснит. Прежнее условие смотрело только на `honesty`, которая при упавшем
    `_seal_impl` оставалась начальным `{"ok": True}` — прогон говорил OK при исчезнувшем
    наблюдении, ровно тот класс, который в этом проекте уже ловили 03.08.
    """
    trace = _CURRENT.get()
    if trace is None or trace.mode != "strict":
        return
    if not trace.sealed:
        return
    if not trace.usable:
        raise FrameTraceMismatch("прибор не смог опечатать кадр: след не выдаётся")
    if not trace.honesty.get("ok", True):
        raise FrameTraceMismatch(
            f"зона {trace.honesty.get('zone')}: склейка разошлась с кадром на смещении "
            f"{trace.honesty.get('at')} (размечено {trace.honesty.get('marked_chars')}, "
            f"в кадре {trace.honesty.get('actual_chars')})"
        )


def sections() -> list[dict]:
    """Копия записей — работает и до, и после опечатывания."""
    trace = _CURRENT.get()
    if trace is None:
        return []
    try:
        return trace.section_rows()
    except Exception:
        return []


def join(zone: str) -> str:
    """Склейка меток зоны. ТОЛЬКО до seal: после него ссылки на тексты сброшены."""
    trace = _CURRENT.get()
    if trace is None:
        raise ValueError("след не привязан к контексту")
    if trace.sealed:
        raise ValueError("след опечатан: тексты сброшены, склеивать нечего")
    return "".join(rec.text for rec in trace._by_zone.get(str(zone), ())
                   if rec.included and isinstance(rec.text, str))


# ---------------------------------------------------------------- выдача метаданных


def _thin(trace: Trace, ordinal: int | None, *, receipt_scrubbed: bool) -> dict:
    # `receipt` едет и в тонкой ссылке (~40 байт). Иначе читатель архива, дойдя по frame_id
    # до полного конверта первой итерации, прочитал бы «расписка не подчищена» — а секреты
    # появляются в тул-цикле ПОЗЖЕ первой итерации, и именно со второй длины расписки
    # расходятся с живым текстом. Флаг здесь per-receipt и не липкий: липкий сделал бы
    # повтор с тем же call_id неравным прежнему и дал бы RunConflict на идемпотентной сверке.
    payload = {"schema": SCHEMA, "roster": ROSTER, "frame_id": trace.frame_id,
               "basis": "live", "reused": True,
               "receipt": {"scrubbed_possible": bool(receipt_scrubbed)}}
    if ordinal is not None:
        payload["emit"] = int(ordinal)
    return payload


def _full(trace: Trace, *, receipt_scrubbed: bool) -> dict | None:
    # records_dropped отличает «прибор перестал записывать на потолке» от «разметка кадра
    # действительно разъехалась»: без него оба случая выглядят одинаково — honesty.ok=false.
    caps = {"sections_dropped": 0, "labels_dropped": 0,
            "records_dropped": bool(trace.overflow)}
    rows = trace.section_rows()
    if len(rows) > MAX_SECTIONS:
        caps["sections_dropped"] = len(rows) - MAX_SECTIONS
        rows = rows[:MAX_SECTIONS]
    zones = []
    for row in trace.zones:
        copied = dict(row)
        if isinstance(copied.get("embed"), dict):
            copied["embed"] = dict(copied["embed"])
        zones.append(copied)
    payload: dict = {
        "schema": SCHEMA,
        "roster": ROSTER,
        "frame_id": trace.frame_id,
        "basis": "live",
        "emit": 1,
        "mode": trace.mode,
        "system_form": trace.system_form,
        # Что измерено и что НЕТ — в самом конверте, а не в докстринге. Сумма bytes трёх
        # зон не есть её кадр: схемы рук и разговор весят своё и здесь не считаны.
        "covers": list(COVERS),
        "unmeasured": list(UNMEASURED),
        "zones": zones,
        "sections": rows,
        "honesty": dict(trace.honesty),
        "receipt": {"scrubbed_possible": bool(receipt_scrubbed)},
        "caps": caps,
    }
    if trace.dynamic_offset is not None:
        payload["dynamic_offset"] = int(trace.dynamic_offset)
    if trace.budget is not None:
        budget = dict(trace.budget)
        budget["excludes"] = list(budget.get("excludes") or ())
        payload["budget"] = budget
        payload["counts"] = dict(trace.counts)
    if len(_encode(payload)) <= MAX_METADATA_BYTES:
        return payload
    # Ступень 1: ярлыки тиров — самое тяжёлое и самое необязательное.
    dropped_labels = 0
    for row in payload["sections"]:
        if row.pop("label", None) is not None:
            dropped_labels += 1
    caps["labels_dropped"] = dropped_labels
    if len(_encode(payload)) <= MAX_METADATA_BYTES:
        return payload
    # Ступень 2: записи «секции нет» — они описывают отсутствующее.
    kept = [row for row in payload["sections"] if row.get("included")]
    caps["sections_dropped"] += len(payload["sections"]) - len(kept)
    payload["sections"] = kept
    if len(_encode(payload)) <= MAX_METADATA_BYTES:
        return payload
    # Ступень 3: секции целиком. Зоны, бюджет и честность остаются.
    caps["sections_dropped"] += len(payload["sections"])
    payload["sections"] = []
    if len(_encode(payload)) <= MAX_METADATA_BYTES:
        return payload
    return None


def metadata_for(system, *, call_id: str, receipt_scrubbed: bool = False) -> dict | None:
    """Конверт следа для расписки model-input, либо None. НИКОГДА не бросает.

    Чистая функция от (system, call_id): повтор с тем же call_id возвращает РАВНЫЙ
    словарь — иначе идемпотентная сверка `store_result` дала бы `RunConflict`.
    Первая выдача — полный конверт, вторая и далее — тонкая ссылка «переиспользован».
    """
    try:
        trace = _CURRENT.get()
        if trace is None or not trace.sealed or not trace.usable:
            return None
        if trace.mode == "off":
            return None
        probe = _extract_id(system)
        if probe is None or probe != trace.verify_id:
            return None
        key = str(call_id or "")
        ordinal = trace.emits.get(key)
        if ordinal is None:
            if len(trace.emits) >= MAX_EMIT_KEYS:
                # Порядковый номер честно неизвестен — поле emit не пишем вовсе.
                return _thin(trace, None, receipt_scrubbed=bool(receipt_scrubbed))
            trace.emit_seq += 1
            ordinal = trace.emit_seq
            trace.emits[key] = ordinal
        if ordinal > 1:
            return _thin(trace, ordinal, receipt_scrubbed=bool(receipt_scrubbed))
        return _full(trace, receipt_scrubbed=bool(receipt_scrubbed))
    except Exception:
        return None
