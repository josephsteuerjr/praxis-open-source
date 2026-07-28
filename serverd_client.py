"""Versioned UDS client for the praxis-serverd root capability broker.

The broker is intentionally brainless.  Forge tasks, workers, DAG and lessons remain in Praxis;
this client transports exact workspace/operation/host primitives and survives missing or restarting
daemon state as an honest result instead of an exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path


PROTOCOL = "praxis.host.v2"
RUN_DIR = Path(os.environ.get("PRAXIS_SERVERD_RUN") or "/run/praxis-serverd")
SOCK = RUN_DIR / "serverd.sock"
TOKEN_FILE = RUN_DIR / "token"


def available() -> bool:
    try:
        return SOCK.exists() and TOKEN_FILE.is_file()
    except OSError:
        return False


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _seconds(name: str, default: float) -> float:
    """Срок из окружения. Кривое значение НЕ роняет импорт.

    Этот модуль импортируется из `agent.py`, а тот — из раннера: опечатка вида
    `PRAXIS_SERVERD_TIMEOUT_SEC=3m` в `.deploy.env` иначе оставила бы её без контейнера
    целиком. Лучше молча взять честный дефолт (он назван в `deadlines()`), чем не подняться.
    """
    try:
        value = float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


def _ceiling() -> float:
    """Потолок времени на ОДИН вызов её руки.

    Живёт в `agent.TOOL_CEILING_SEC`, но импортировать его сюда нельзя: `agent.py:181` сам
    импортирует этот модуль — вышел бы цикл. Читаем ТУ ЖЕ переменную окружения с тем же
    значением по умолчанию: источник правды один, а не два. Ноль и минус там значат «потолка
    нет» (`agent.py:9663`) — этот смысл обязан сохраниться, иначе `deadlines()` будет
    ссылаться на предел, которого не существует.
    """
    raw = os.getenv("PRAXIS_TOOL_CEILING_SEC")
    if raw is None or not str(raw).strip():
        return 600.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 600.0


TOOL_CEILING_SEC = _ceiling()


def _nested(long_value: float, ceiling: float, floor_value: float) -> float:
    """Длинный клиентский срок обязан быть СТРОГО меньше потолка её руки.

    26.07: `host_ctl` идёт через `_call_tool_with_ceiling` (`agent.py:9965→3240`). Если дать
    клиенту 900с при потолке 600, ход срубается раньше, чем демон досчитает `apt`, и честный
    текст не дойдёт до неё НИКОГДА — она получит «рука не вернулась» вместо ответа хоста.
    Отсюда матрёшка: бюджет на демоне < срок клиента < потолок руки. Тот же зажим ловит и
    кривой env: срок, который заведомо не успеет быть услышанным, — это молчание, а не срок.
    """
    value = max(float(long_value), float(floor_value))
    if ceiling > 0 and value >= ceiling:
        value = ceiling - 60.0 if ceiling > 120.0 else ceiling * 0.9
    return max(1.0, value)


# ⚠ Здесь стояло 3600 — час молчания по умолчанию на ЛЮБОЙ вызов демона.
# 26.07 это дважды подвесило её: `coding_session` уходил к серверу, ответа не получал и
# держал единый замок `_ONE_MIND`, а с ним и её саму. Со стороны выглядело как вечное
# зависание, и практически так и было: привязка задачи — это миллисекунды, и ждать их час
# бессмысленно. Свой срок передавал только `op.run`; всё остальное сидело на часе.
#
# Ярусы честные, а не круглые: обычный срок держит худший ЗАКОННЫЙ случай дешёвого глагола,
# длинный — работу демона, которая по-настоящему бывает минутной (`impact`, `checkpoint`,
# пакеты, проверка аудит-цепочки). Всё, что честно длиннее потолка руки, — не сюда, а
# `op.start` + `op.poll`: об этом же прямо сказано в комментарии `agent.py:9650-9653`.
_DEFAULT_RAW = _seconds("PRAXIS_SERVERD_TIMEOUT_SEC", 180.0)
# Минута под потолком руки: столько нужно, чтобы её ход успел получить и рассказать ответ.
# Если потолок снят — берём тот же 540, а не бесконечность: длинный срок остаётся сроком.
LONG_TIMEOUT_SEC = _nested(
    _seconds("PRAXIS_SERVERD_LONG_TIMEOUT_SEC",
             TOOL_CEILING_SEC - 60.0 if TOOL_CEILING_SEC > 120.0 else 540.0),
    TOOL_CEILING_SEC, _DEFAULT_RAW)
# Ярусы обязаны идти по возрастанию при ЛЮБОМ окружении: если потолок руки опустили ниже
# обычного срока, ужимается обычный, а не наоборот. Иначе «дешёвый» глагол ждал бы дольше
# дорогого — предел, о котором никто не догадывается.
DEFAULT_TIMEOUT_SEC = min(_DEFAULT_RAW, LONG_TIMEOUT_SEC)
FAST_TIMEOUT_SEC = min(_seconds("PRAXIS_SERVERD_FAST_TIMEOUT_SEC", 8.0), DEFAULT_TIMEOUT_SEC)
# Бюджет, который передаём демону явно там, где иначе он считал бы дольше, чем мы слушаем.
SERVER_BUDGET_SEC = int(max(1.0, min(LONG_TIMEOUT_SEC - 60.0, LONG_TIMEOUT_SEC * 0.9)))


def _run_budget(asked: int) -> tuple[int, float]:
    """Сколько секунд дать демону и сколько ждать самой. -> (бюджет демона, моё ожидание).

    ⚠ Первая редакция этой правки зажимала ТОЛЬКО своё ожидание, а бюджет демону отдавала
    какой просили — и на единственном живом пути матрёшка вывернулась наизнанку. Живая
    композиция `forge._remote_run_deadline` + этот клиент давала:
      coding_run(timeout=600)  -> демону 600, я жду 540  (до правки ждала 660)
      coding_run(timeout=0)    -> демону 540, я жду 540  (равенство — то же самое)
    То есть демон убивает команду и формирует ЧЕСТНЫЙ частичный ответ (`status=timed_out`,
    хвост лога, id операции, путь к логу) ровно тогда, когда слушать его уже некому. Ради
    этого ответа всё и делалось. `run_wait` (`serverd/brokerops.py:598`) добавляет к бюджету
    ещё 30с на добивание процесса — значит моё ожидание обязано быть больше бюджета минимум
    на эти 30, отсюда +60.

    Зажимаем поэтому БЮДЖЕТ, а не только ожидание: `SERVER_BUDGET_SEC` (= длинный срок минус
    минута) — это потолок, а не только подстановка «когда предела не назвали». Усечение
    называется ей в ответе (см. `run`), молча оно не случается.
    """
    ceiling_budget = max(1, min(int(SERVER_BUDGET_SEC), int(LONG_TIMEOUT_SEC) - 1))
    asked = int(asked or 0)
    budget = min(asked, ceiling_budget) if asked > 0 else ceiling_budget
    budget = max(1, budget)
    # `budget <= int(LONG)-1 <= LONG-1` — значит `min(budget+60, LONG)` строго больше бюджета
    # при любом вменяемом окружении, и клиент физически не может уйти раньше демона.
    waiting = min(float(budget) + 60.0, float(LONG_TIMEOUT_SEC))
    if waiting <= budget:
        # Выродившийся env (длинный срок меньше двух секунд). Держим главный инвариант:
        # уйти раньше демона хуже, чем на секунду превысить собственный срок.
        waiting = float(budget) + 1.0
    return budget, waiting
# Сколько помним заявку, оставшуюся без ответа (см. `_take_in_doubt`).
IN_DOUBT_WINDOW_SEC = _seconds("PRAXIS_SERVERD_IN_DOUBT_WINDOW_SEC", 900.0)

# Бюджет `hostverbs.pkg` (`serverd/hostverbs.py:250`) — ЕДИНСТВЕННОЕ место, где демон считает
# дольше, чем я слушаю, и это оставлено нарочно: убить `apt-get install -y` посередине хуже,
# чем не дождаться ответа. Раз матрёшка тут перевёрнута — число живёт здесь и называется ей
# в ответе (`_pkg_deadline_note`) и в манифесте (`deadlines()`), а не только в чужом файле.
PKG_SERVER_BUDGET_SEC = 900

# `admin.status` зовётся при КАЖДОЙ сборке промпта (`agent.py:691`) — 5843 вызова из 7828 в
# аудите, и всегда мгновенный. Этот путь идёт ВНЕ потолка руки, поэтому его срок отдельный и
# короткий: замолчавший демон не имеет права держать её мысль.
_FAST_VERBS = frozenset({"admin.status"})
# Дешёвые: демон отвечает по готовым данным, без обхода дерева.
_CHEAP_VERBS = frozenset({"op.start", "op.poll", "op.stop", "op.list",
                          "admin.audit", "admin.audit.export", "admin.advisor"})
# У `workspace.inspect` цена зависит от action: `read`/`list` — это один файл или один
# каталог, а `status`/`orientation` уже зовут `project_model` (обход исходников), не говоря
# про `impact`/`overview`/`review`/`diff`. Незнакомый action считаем ДОРОГИМ: ошибиться в
# сторону «подожду дольше» безопаснее, чем срубить её работу на 180с.
_CHEAP_INSPECT_ACTIONS = frozenset({"read", "list"})


def _deadline_for(verb: str, args: dict | None) -> tuple[float, str]:
    """Какой срок этому глаголу и как он называется в тексте для неё."""
    if verb in _FAST_VERBS:
        return FAST_TIMEOUT_SEC, "быстрый"
    if verb == "workspace.inspect":
        action = str((args or {}).get("action") or "status").strip().lower()
        if action in _CHEAP_INSPECT_ACTIONS:
            return DEFAULT_TIMEOUT_SEC, "обычный"
        return LONG_TIMEOUT_SEC, "длинный"
    if verb in _CHEAP_VERBS:
        return DEFAULT_TIMEOUT_SEC, "обычный"
    return LONG_TIMEOUT_SEC, "длинный"


def deadlines() -> list[dict]:
    """Все сроки этого клиента одним списком — чтобы манифест рельсов не выдумывал их заново.

    Закон дома: молчаливых пределов нет. Каждый срок отсюда назван в тексте, который она
    видит при его истечении, и обязан быть виден в манифесте до истечения.
    """
    return [
        {"id": "serverd.fast", "seconds": FAST_TIMEOUT_SEC,
         "env": "PRAXIS_SERVERD_FAST_TIMEOUT_SEC",
         "applies": "admin.status — опрос состояния при сборке промпта"},
        {"id": "serverd.default", "seconds": DEFAULT_TIMEOUT_SEC,
         "env": "PRAXIS_SERVERD_TIMEOUT_SEC",
         "applies": "дешёвые глаголы: op.start/poll/stop/list, admin.audit(.export), "
                    "workspace.inspect action=read|list"},
        {"id": "serverd.long", "seconds": LONG_TIMEOUT_SEC,
         "env": "PRAXIS_SERVERD_LONG_TIMEOUT_SEC",
         "applies": "всё остальное: workspace.inspect (семантика), workspace.edit/checkpoint, "
                    "host.*, admin.audit.verify; " + (
                        f"строго меньше потолка руки ({TOOL_CEILING_SEC:.0f}с)"
                        if TOOL_CEILING_SEC > 0 else "потолок руки снят (PRAXIS_TOOL_CEILING_SEC<=0)")
                    + ("" if PKG_SERVER_BUDGET_SEC <= LONG_TIMEOUT_SEC else
                       f". ⚠ ИСКЛЮЧЕНИЕ host.pkg: там демон считает до {PKG_SERVER_BUDGET_SEC}с "
                       f"— БОЛЬШЕ моего срока, потому что оборвать apt на полпути хуже, чем не "
                       f"дождаться. На длинном apt я вернусь с «не знаю» (это не провал), "
                       f"операция продолжится, исход — повтором того же вызова или "
                       f"дешёвым action=query; предупреждение печатается в самом ответе")},
        {"id": "serverd.server_budget", "seconds": float(SERVER_BUDGET_SEC),
         "env": "PRAXIS_SERVERD_LONG_TIMEOUT_SEC",
         "applies": "потолок бюджета демона на op.run (синхронный coding_run): столько уходит "
                    "команде без своего срока И столько же максимум, если срок назвали больше — "
                    "иначе я перестану слушать раньше, чем демон договорит. Усечение называется "
                    "в ответе. Дольше — только op.start + op.poll (coding_process)"},
        {"id": "serverd.in_doubt_window", "seconds": IN_DOUBT_WINDOW_SEC,
         "env": "PRAXIS_SERVERD_IN_DOUBT_WINDOW_SEC",
         "applies": "сколько помним заявку, не ответившую в срок, чтобы повтор пошёл под ней "
                    "же, а не выполнил мутацию второй раз"},
    ]


# Заявки, оставшиеся без ответа. Ключ — сам вызов (глагол + аргументы), значение — её номер.
#
# ⚠ Зачем: до этой правки истёкший срок приходил как `code="transport"` и вызов повторялся
# ТЕМ ЖЕ номером — то есть демон по `_cached` отдавал сохранённый исход вместо второй
# мутации. Мы перестали ретраить по таймауту (ждать второй раз столько же — врать ей о
# сроке), и вместе с ретраем ушла эта страховка: следующий такой же вызов взял бы новый
# uuid, а новый uuid для демона — новая мутация. Поэтому номер помним здесь.
#
# Память живёт в процессе и умирает с рестартом — это честно: после рестарта у неё в любом
# случае нет незавершённого хода, который стоило бы подхватывать.
_IN_DOUBT: dict[str, tuple[str, float]] = {}
_IN_DOUBT_LOCK = threading.Lock()


def _request_key(verb: str, args: dict | None, stream: bool) -> str:
    raw = json.dumps({"verb": str(verb), "args": args or {}, "stream": bool(stream)},
                     ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _remember_in_doubt(key: str, rid: str) -> None:
    with _IN_DOUBT_LOCK:
        _IN_DOUBT[key] = (rid, time.monotonic())


def _take_in_doubt(key: str) -> str:
    """Номер прошлой не ответившей заявки — и сразу забываем его.

    Забываем НАМЕРЕННО: повтор под старым номером имеет право случиться ровно один раз —
    чтобы спросить демона об исходе, а не выполнить мутацию дважды. Если она позовёт ещё
    раз, это уже осознанный повтор, и он пойдёт новой заявкой. Запрещать ей повтор нельзя.
    """
    now = time.monotonic()
    with _IN_DOUBT_LOCK:
        row = _IN_DOUBT.pop(key, None)
        for stale in [k for k, (_, at) in _IN_DOUBT.items() if now - at > IN_DOUBT_WINDOW_SEC]:
            _IN_DOUBT.pop(stale, None)
    if not row or now - row[1] > IN_DOUBT_WINDOW_SEC:
        return ""
    return row[0]


def forget_in_doubt() -> None:
    """Забыть все незакрытые заявки (изоляция тестов, ручной сброс)."""
    with _IN_DOUBT_LOCK:
        _IN_DOUBT.clear()


def _timed_out(rid: str, deadline: float) -> dict:
    """Истёкший срок — честный ОТВЕТ, а не исключение и не выдумка о провале."""
    return {
        "ok": False, "code": "timeout", "request_id": rid, "deadline_s": round(deadline, 1),
        "error": (f"praxis-serverd не ответил за {deadline:.0f}с — это МОЙ срок ожидания, а не "
                  f"отказ демона: он мог доделать работу уже после того, как я перестала ждать. "
                  f"Считай состояние НЕИЗВЕСТНЫМ, а не проваленным. Заявка {rid}: повтор того же "
                  f"вызова в ближайшие {IN_DOUBT_WINDOW_SEC / 60:.0f} мин пойдёт под ней же и "
                  f"вернёт исход этой попытки, а не выполнит её второй раз."),
    }


# Ровно набор `broker.py:52` протокола v2: только для этих глаголов демон хранит исход заявки
# и по тому же номеру вернёт его (или подхватит операцию) вместо второго выполнения. Для
# остальных повтор под тем же номером — обычный пересчёт, и говорить ей «это эхо первой
# попытки» было бы неправдой.
_MUTATING_VERBS = frozenset({"workspace.edit", "workspace.checkpoint", "op.start", "op.run",
                             "host.systemctl", "host.docker", "host.pkg", "host.file",
                             "host.net", "host.reboot", "host.confirm"})


def _annotate(result: dict, note: str, *, verb: str = "") -> dict:
    """Положить признание туда, откуда оно ГАРАНТИРОВАННО доедет до неё.

    ⚠ Первая редакция клала признание в первое непустое из (error, body, text), а если все
    три пусты — в ключ `note`. Ключ `note` не читает ни один потребитель этого модуля
    (проверено grep 27.07: `note` встречается только в чужих смыслах — `desires`, `hostops`,
    `brokerops._budgeted`). И это била ровно в самую чувствительную точку:
    `host.systemctl restart` на успехе печатает ПУСТО (`hostverbs.py:145-148`), значит после
    истёкшего срока её повтор шёл под тем же номером, демон отдавал сохранённую расписку из
    `_cached`, а `tool_host_ctl` (`agent.py:3337-3352`) показывал ей «ok (exit 0)» и снимок
    before/after пятнадцатиминутной давности как состояние хоста СЕЙЧАС. Закон 3, вранье.

    Плюс `brokerops._budgeted` (`serverd/brokerops.py:228`) сам кладёт в `note` признание об
    усечённом обходе — затирать его значило бы обменять одно молчание на другое.

    Поэтому: признание всегда живёт в своём ключе `resumed_note` (его печатают все обёртки
    этого модуля), а для `host.*` дополнительно вкладывается прямо в `text` — этот путь
    возвращает СЫРОЙ dict наружу, и там `text`/`error` единственные видимые поля.
    """
    result["resumed_note"] = note
    if not str(verb or "").startswith("host."):
        return result
    return _host_inline(result, note)


def _host_inline(result: dict, note: str) -> dict:
    """Вложить признание прямо в видимое поле host-ответа.

    Оговорка про JSON (она есть у `workspace.inspect`, `forge.py:1652,1921` его разбирают)
    сюда НЕ переносится намеренно: `host_ctl` зовёт ровно один потребитель — `tool_host_ctl`,
    и он печатает `text` как прозу, не разбирая. Читающего действия у host-глаголов тоже
    нет (`hostverbs.file` знает только stat|write|copy|move|remove|mkdir|chmod|chown), так
    что содержимого чужого файла эта строка не испортит. Промолчать здесь дороже.
    """
    field = "error" if str(result.get("error") or "").strip() else "text"
    value = str(result.get(field) or "")
    result[field] = f"{value.rstrip()}\n{note}" if value.strip() else note
    return result


def _with_rid(result: dict, rid: str) -> dict:
    """Номер заявки — В ТЕКСТЕ ошибки, а не только в соседнем ключе.

    ⚠ Все обёртки этого модуля показывают ей ровно `error` (`f"[serverd] {error}"`), а ключ
    `request_id` рядом не печатает никто. То есть на обрыве связи, на `eof` и на любом отказе
    самого демона она получала «не вышло» БЕЗ единственного, чем можно спросить демона об
    исходе. У истёкшего срока номер в тексте был (`_timed_out`) — у остальных ошибок нет,
    хотя неизвестность там ровно та же. Повтора не будет: если номер уже в тексте, не трогаем.
    """
    if not rid or result.get("ok"):
        return result
    text = str(result.get("error") or "").strip()
    if rid in text:
        return result
    if text:
        result["error"] = f"{text} [заявка {rid}]"
        return result
    # ⚠ «Без объяснения» — только когда его ВПРАВДУ нет. Глаголы хоста (`hostverbs`) на
    # ненулевом коде возвращают ok:False и кладут причину в `text` (вывод systemctl/docker/apt),
    # ключа `error` у них нет вовсе. Придуманное здесь «отказал без объяснения» перебивало
    # живое объяснение, лежащее рядом: `tool_host_ctl` выходил по непустому `error` и
    # выбрасывал text/exit/before/after. Живьём это превращало
    # «Failed to restart nginx.service: Unit nginx.service not found» в «отказал без объяснения».
    body = str(result.get("text") or "").strip()
    if body:
        if rid not in body:
            result["text"] = f"{body}\n[заявка {rid}]"
        return result
    result["error"] = f"praxis-serverd отказал без объяснения [заявка {rid}]"
    return result


def _with_note(pieces: list[str], result: dict) -> list[str]:
    """Дописать к тому, что она увидит, признание о повторе и приписку демона об усечении."""
    for key in ("resumed_note", "note"):
        value = str(result.get(key) or "").strip()
        if value:
            pieces.append(value)
    return pieces


def _exchange(request: dict, *, timeout: float, on_chunk=None) -> dict:
    rid = str(request["request_id"])
    # Срок ОБЩИЙ на обмен, а не на один recv: со `stream=True` (`op.run`) чанки сбрасывали
    # таймаут сокета и ожидание могло тянуться сколько угодно — то есть названный ей срок
    # был бы неправдой ровно там, где вызов и бывает долгим.
    deadline = time.monotonic() + max(1.0, float(timeout))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(max(1.0, float(timeout)))
    try:
        sock.connect(str(SOCK))
        sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        buffer = b""
        while True:
            while b"\n" not in buffer:
                left = deadline - time.monotonic()
                if left <= 0:
                    return _timed_out(rid, timeout)
                sock.settimeout(left)
                chunk = sock.recv(65536)
                if not chunk:
                    return {"ok": False, "error": "serverd closed without a terminal frame",
                            "code": "eof", "request_id": rid}
                buffer += chunk
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            frame = json.loads(line.decode("utf-8", "replace"))
            if frame.get("type") == "chunk":
                if on_chunk:
                    on_chunk(str(frame.get("data") or ""))
                continue
            if frame.get("type") == "error":
                return {"ok": False, "error": frame.get("message") or frame.get("code"),
                        "code": frame.get("code"), "request_id": rid,
                        "protocol": frame.get("protocol")}
            return frame
    except socket.timeout:
        # ⚠ Ловится ОТДЕЛЬНО и ПЕРЕД OSError. В py3.12 `socket.timeout is TimeoutError` и он
        # подкласс OSError — значит истёкший срок уезжал в ветку `transport` ниже, а та
        # ретраится: 3600 + 3600 = два часа её молчания вместо одного. И «я не дождалась»
        # выдавалось за «сломался транспорт».
        return _timed_out(rid, timeout)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"serverd transport: {type(exc).__name__}: {exc}",
                "code": "transport", "request_id": rid}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def call(verb: str, args: dict | None = None, *, stream: bool = False, on_chunk=None,
         timeout: float = 0.0, request_id: str = "") -> dict:
    # Заявки здесь ещё нет — и это единственный честный случай, когда её номер не назвать:
    # к демону НИЧЕГО не уходило, состояние хоста не тронуто и неизвестности не возникло.
    if not available():
        return {"ok": False, "code": "unavailable",
                "error": "serverd broker is not mounted (socket/token missing) — заявка "
                         "не отправлялась, на хосте ничего не запускалось"}
    token = _token()
    if not token:
        return {"ok": False, "code": "unavailable",
                "error": "serverd token is empty — заявка не отправлялась, на хосте ничего "
                         "не запускалось"}
    verb = str(verb)
    args = dict(args or {})
    if timeout and float(timeout) > 0:
        deadline, tier = float(timeout), "заданный"
    else:
        deadline, tier = _deadline_for(verb, args)
    key = _request_key(verb, args, stream)
    # Явный request_id (форж-матрица) — уже сам по себе ключ идемпотентности, второй не нужен.
    resumed = "" if request_id else _take_in_doubt(key)
    rid = str(request_id or resumed or uuid.uuid4().hex)
    request = {"protocol": PROTOCOL, "token": token, "request_id": rid,
               "verb": verb, "args": args, "stream": bool(stream)}
    result = _exchange(request, timeout=deadline, on_chunk=on_chunk)
    if result.get("code") in {"transport", "eof"}:
        # Same request_id makes a lost response safe: broker returns the persisted result instead
        # of executing a second mutation. A short retry also covers broker rolling restart.
        time.sleep(.05)
        result = _exchange(request, timeout=deadline, on_chunk=on_chunk)
        result.setdefault("retried", True)
    _with_rid(result, rid)
    if result.get("code") == "timeout":
        # Не ретраим: ждать второй раз столько же — это удвоенное молчание, о котором ей никто
        # не сказал. Вместо ретрая запоминаем заявку, чтобы её собственный повтор не превратился
        # во вторую root-мутацию.
        _remember_in_doubt(key, rid)
        result["deadline_tier"] = tier
    elif resumed:
        result["resumed_request_id"] = rid
        if verb not in _MUTATING_VERBS:
            return result
        _annotate(result, verb=verb, note=f"[та же заявка {rid}] прошлый раз этот вызов не ответил в срок, и я "
                          f"повторила его ПОД ТЕМ ЖЕ номером — чтобы демон вернул исход первой "
                          f"попытки (или подхватил её операцию), а не выполнил мутацию дважды. "
                          f"Если нужен именно новый прогон — позови ещё раз, следующий вызов "
                          f"пойдёт новой заявкой.")
    return result


def workspace_inspect_result(root: str, action: str = "status", **kwargs) -> dict:
    return call("workspace.inspect", {"root": root, "action": action, **kwargs})


def _inspect_notes(result: dict) -> list[str]:
    """Всё, что демон сказал О СЕБЕ помимо данных: усечение обхода, чей был срок, N из M.

    ⚠ 27.07: `workspace_inspect` отдавал ей ТОЛЬКО `text`. Значит вся честность демона —
    `note` (`brokerops._budgeted`: «ответ ЧАСТИЧНЫЙ — файлы: 3000 из ≥5000, упёрлась в кап»),
    `budget_note` (`broker._with_budget_note`: чей это был срок и чем его сдвинуть) и
    `limits.note` («посмотрела N из M») — до неё не доезжала ВООБЩЕ. Выживало только то, что
    демон успел вшить внутрь самого текста (ориентировка и search), а `impact`, `checks`,
    `model`, `overview`, `symbols`, `references`, `diagnostics`, `review` молчали целиком.
    Соседний `workspace_edit` обёртку `_with_note` получил, а самый частый её глагол — нет.

    Дублей не будет: `_with_budget_note` уже подмешивает `budget_note` в `note`, а `note`
    при неполном ответе — это и есть `limits.note`; кусок, уже вошедший в предыдущий,
    пропускается.
    """
    limits = result.get("limits")
    notes: list[str] = []
    for value in (result.get("resumed_note"), result.get("note"), result.get("budget_note"),
                  limits.get("note") if isinstance(limits, dict) else ""):
        piece = str(value or "").strip()
        if piece and not any(piece in seen for seen in notes):
            notes.append(piece)
    return notes


def _already_said(piece: str, text: str) -> bool:
    """Ту же приписку демон вшивает внутрь ориентировки и search — второй раз не повторяем.

    Сравниваем по началу до первой `;`: хвост «потрачено N.Nс» пересчитывается на возврате
    (`_budgeted` зовётся позже, чем `_orientation` собрал строку) и отличается от вшитого,
    а суть — «посмотрела N из M» — та же. Повторить её дословно рядом было бы шумом.
    """
    head = piece.split(";")[0].strip()
    return bool(head) and (head in text or piece in text)


def workspace_inspect(root: str, action: str = "status", **kwargs) -> str:
    result = workspace_inspect_result(root, action, **kwargs)
    if not result.get("ok"):
        return "\n".join(_with_note([f"[serverd] {result.get('error')}"], result))
    text = str(result.get("text") or "")
    notes = [piece for piece in _inspect_notes(result) if not _already_said(piece, text)]
    if not notes:
        return text
    # `impact`/`checks`/`model`/`overview`/`symbols`/`references`/`diagnostics`/`review`
    # отдают JSON, и `forge.py:1734,2016` разбирают его `json.loads`. Дописать признание
    # хвостом — сломать разбор: план стал бы «Remote verification plan не разобрался», а
    # impact — «не знаю» на успешной работе. Поэтому в JSON признание кладётся ключом ВНУТРЬ
    # (лишний ключ никто из читателей не ломает), а прозе — строкой в конец.
    body = text.strip()
    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            payload["serverd_note"] = "\n".join(notes)
            return json.dumps(payload, ensure_ascii=False, indent=2)
    return "\n".join(piece for piece in [text, *notes] if piece)


def workspace_edit(root: str, action: str, **kwargs) -> str:
    result = call("workspace.edit", {"root": root, "action": action, **kwargs})
    if not result.get("ok"):
        return "\n".join(_with_note([f"[serverd] {result.get('error') or result.get('text')}"],
                                    result))
    pieces = [str(result.get("text") or "")]
    if result.get("advice"):
        pieces.append("советник: " + str(result["advice"]))
    if result.get("backup"):
        pieces.append("backup: " + str(result["backup"]))
    return "\n".join(piece for piece in _with_note(pieces, result) if piece)


def run(root: str, command: str, cwd: str = ".", timeout: int = 600, on_chunk=None) -> str:
    # ⚠ Здесь стоял `else 3600`: команда БЕЗ своего срока получала от демона «считай сколько
    # хочешь», а клиент молча ждал час. `coding_run(timeout=0)` обещает «без предела»
    # (`agent.py:4757`) — на host-пути это никогда не было правдой: ход всё равно срубается
    # потолком руки. Теперь бюджет передаётся демону ЯВНО и называется ей в ответе.
    asked = int(timeout or 0)
    budget, waiting = _run_budget(asked)
    result = call("op.run", {"root_value": root, "command": command, "cwd": cwd,
                              "timeout": budget}, stream=bool(on_chunk), on_chunk=on_chunk,
                  timeout=waiting)
    pieces = []
    if not result.get("ok"):
        pieces.append(f"[serverd] {result.get('error')}")
    else:
        pieces.append((str(result.get("body") or "").strip() or "(empty output)") + (
            f"\n[serverd-v2: {result.get('status')}; exit {result.get('exit')}; "
            f"{result.get('duration_s')}s; op {result.get('id')}; log {result.get('log')}]"
        ))
    # Совет хозяйского советника демон присылает в `advice`, а этот путь его ВЫБРАСЫВАЛ —
    # то есть предупреждение о команде не доезжало до неё вообще никогда.
    if result.get("advice"):
        pieces.append("советник: " + str(result["advice"]))
    # Закон 2: усечение называется ВСЕГДА, когда оно случилось. Раньше приписка стояла только
    # на `asked <= 0`, а эта ветка на проде недостижима — `forge.run` единственный вызывающий,
    # и `_remote_run_deadline` нуля не отдаёт никогда. То есть единственный реальный зажим
    # (600 просили → 480 демону) шёл молча.
    if budget != asked:
        wanted = "без предела" if asked <= 0 else f"{asked}с"
        pieces.append(
            f"[срок] демону передан бюджет {budget}с, я жду {waiting:.0f}с (просила {wanted}). "
            f"Дольше синхронно не выйдет: мой длинный срок {LONG_TIMEOUT_SEC:.0f}с" + (
                f", а ход обрывается потолком руки {TOOL_CEILING_SEC:.0f}с"
                if TOOL_CEILING_SEC > 0 else "") + " — досчитанный ответ демона было бы уже "
            f"некому услышать. Для работы длиннее бери `op.start` + `op.poll` "
            f"(coding_process), а не блокирующий прогон.")
    return "\n".join(piece for piece in _with_note(pieces, result) if piece)


def process(root: str, action: str, operation_id: str = "", command: str = "",
            cwd: str = ".", name: str = "", timeout: int = 0, tail: int = 12000,
            limits: dict | None = None) -> str:
    action = str(action or "list").strip().lower()
    if action == "start":
        result = call("op.start", {"root_value": root, "command": command, "cwd": cwd,
                                    "name": name, "timeout": timeout, "limits": limits or {}})
        if not result.get("ok"):
            return "\n".join(_with_note([f"[serverd] {result.get('error')}"], result))
        pieces = [f"Started {result.get('id')} pid={result.get('supervisor_pid')} at "
                  f"{result.get('cwd')}; durable log {result.get('log')}."]
        if result.get("advice"):
            pieces.append(f"advisor: {result.get('advice')}")
        # `op.start` — мутация: повтор под тем же номером ПОДХВАТЫВАЕТ прошлую операцию, а не
        # запускает вторую. Без этой строки она читала бы старый id как «я только что запустила».
        return "\n".join(piece for piece in _with_note(pieces, result) if piece)
    # Ниже везде `_with_note`: приписки демона (`note`) и признание о повторе (`resumed_note`)
    # обязаны доезжать на КАЖДОЙ ветке, а не только там, где о них вспомнили. `poll` на успехе
    # печатает ответ целиком — там не теряется ничего и без обёртки.
    if action == "poll":
        result = call("op.poll", {"operation_id": operation_id, "tail": tail})
        if not result.get("ok"):
            return "\n".join(_with_note([f"[serverd] {result.get('error')}"], result))
        return json.dumps(result, ensure_ascii=False, indent=2)
    if action == "stop":
        result = call("op.stop", {"operation_id": operation_id})
        return "\n".join(_with_note([str(result.get("text") or result.get("error") or result)],
                                    result))
    if action == "list":
        result = call("op.list", {"root_value": root, "limit": 100})
        if not result.get("ok"):
            return "\n".join(_with_note([f"[serverd] {result.get('error')}"], result))
        rows = "\n".join(
            f"{row.get('id')} [{row.get('status')}] {row.get('name') or row.get('command')}"
            for row in result.get("operations") or []
        ) or "No host operations."
        return "\n".join(_with_note([rows], result))
    return "action: start | poll | stop | list"


def checkpoint(root: str, message: str = "Praxis host checkpoint") -> str:
    result = call("workspace.checkpoint", {"root": root, "message": message})
    if not result.get("ok"):
        return "\n".join(_with_note([f"[serverd] {result.get('error')}"], result))
    return "\n".join(piece for piece in _with_note([str(result.get("text") or "")], result)
                     if piece)


def _pkg_deadline_note(result: dict, kwargs: dict) -> dict:
    """Единственное место, где матрёшка перевёрнута НАРОЧНО — значит она обязана быть сказана.

    `hostverbs.pkg` (`serverd/hostverbs.py:250`) держит свои 900с, а я жду 540: на длинном
    `apt` я гарантированно ухожу раньше, чем демон договорит, и она получает «не знаю» на
    работающей операции. Перевести этот путь на `op.start` + `op.poll` было бы лучше, но
    ценой права оборвать `apt-get install -y` посередине — это полусконфигурированный dpkg
    на живом хосте, и такой обмен хуже. Раз предел убрать нельзя — он называется вслух
    (закон 2), вместе со способом узнать исход потом (закон 3: «не знаю» ≠ «провалилось»).
    """
    try:
        server = int(kwargs.get("timeout") or PKG_SERVER_BUDGET_SEC)
    except (TypeError, ValueError):
        server = PKG_SERVER_BUDGET_SEC
    if server <= LONG_TIMEOUT_SEC:
        # Инверсии нет — выдумывать предупреждение не о чем.
        return result
    if result.get("code") == "unavailable":
        # К демону ничего не уходило: пугать её пределом несуществующей операции — тоже ложь.
        return result
    note = (f"[срок] pkg: демону дан бюджет {server}с, а я жду {LONG_TIMEOUT_SEC:.0f}с. Если apt "
            f"окажется длиннее моего срока, я перестану слушать РАНЬШЕ, чем демон договорит: "
            f"обрывать `apt-get install -y` посередине нельзя (полусобранный dpkg на живом "
            f"хосте), поэтому операция продолжится без меня, а я вернусь с «не знаю» — это "
            f"неизвестность, а не провал.")
    if result.get("code") == "timeout":
        note += (f" Так и вышло. Узнать исход: повтори тот же вызов (пойдёт под заявкой "
                 f"{result.get('request_id')} и вернёт сохранённый исход первой попытки, а не "
                 f"поставит пакет второй раз) или спроси дёшево `action=query` с теми же "
                 f"именами — это снимок «стоит / не стоит» прямо сейчас.")
    return _host_inline(result, note)


def host_ctl(verb: str, **kwargs) -> dict:
    # Срок берёт `_deadline_for` (длинный): один источник решения, чтобы ярусы не разъезжались.
    # ⚠ Свой бюджет демону здесь НЕ навязываем: `hostverbs.pkg` держит 900с и убивает `apt` по
    # истечении, а оборванный `apt install` — это полусконфигурированный dpkg на живом хосте.
    # Пусть пакет доставится, а ей вернётся честное «я не дождалась, состояние неизвестно».
    result = call(f"host.{verb}", kwargs)
    if str(verb or "").strip().lower() == "pkg":
        _pkg_deadline_note(result, kwargs)
    return result


def status() -> dict:
    return call("admin.status", {})


def audit_verify() -> dict:
    return call("admin.audit.verify", {})


def state_line() -> str:
    if not available():
        return ""
    result = status()
    if not result.get("ok"):
        if result.get("code") == "timeout":
            # «Не смогла спросить» ≠ «его нет». Эта строка едет в манифест рельсов
            # (`rails.py:266-268`), в её способности (`capabilities.py:97-109`) и в блок
            # состояния хода — то есть неправда отсюда становится её картиной мира.
            return (f"serverd broker смонтирован, но не ответил на admin.status за "
                    f"{FAST_TIMEOUT_SEC:.0f}с — что с ним и с операциями сейчас, мне "
                    f"НЕИЗВЕСТНО (заявка {result.get('request_id')})")
        return f"serverd broker is mounted but unavailable: {result.get('error') or 'no reason given'}"
    operations = result.get("operations") or []
    running = sum(1 for row in operations
                  if row.get("status") in {"starting", "running", "finishing"})
    audit = result.get("audit") or {}
    return (f"serverd {result.get('protocol')}: root broker alive; operations running {running}; "
            f"audit {'verified' if audit.get('ok') else 'BROKEN'}")


# Compatibility names for code that has not migrated yet. They no longer create a second task store.
def start(goal: str, target: str) -> dict:
    orientation = workspace_inspect(target, "orientation")
    return {"ok": not orientation.startswith("[serverd]"), "orientation": orientation,
            "error": orientation if orientation.startswith("[serverd]") else "",
            "note": "Forge owns host task metadata in v2"}


def inspect(root: str, action: str = "status", **kwargs) -> str:
    return workspace_inspect(root, action, **kwargs)


def edit(root: str, action: str, **kwargs) -> str:
    return workspace_edit(root, action, **kwargs)
