#!/usr/bin/env python3
"""praxis-serverd v2 — privileged capability broker for the one canonical Praxis Forge.

No model, prompt, agent, goal or second task store lives here.  Praxis owns cognition and task
coordination; this host daemon executes versioned root capabilities, supervises durable operations
and returns exact evidence through a cgroup-pinned Unix socket.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import advisor
import auditlog
import brokerops
import hostverbs


PROTOCOL = "praxis.host.v2"
HOME = Path(os.environ.get("PRAXIS_SERVERD_HOME") or "/opt/praxis-serverd")
STATE = Path(os.environ.get("PRAXIS_SERVERD_STATE") or str(HOME / "state"))
RUN_DIR = Path(os.environ.get("PRAXIS_SERVERD_RUN") or str(HOME / "run"))
SOCK = RUN_DIR / "serverd.sock"
TOKEN_FILE = RUN_DIR / "token"
AUDIT_FILE = STATE / "audit.jsonl"
REQUESTS = STATE / "requests"
EXPORTS = STATE / "audit-exports"
PRAXIS_CONTAINER = os.environ.get("PRAXIS_SERVERD_PIN_CONTAINER", "praxis")
PIN_CGROUP = os.environ.get("PRAXIS_SERVERD_PIN_CGROUP", "1") == "1"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
CAPABILITIES = {
    "workspace": ["inspect", "edit", "checkpoint"],
    "operation": ["start", "run", "poll", "stop", "list"],
    "host": ["systemctl", "docker", "pkg", "file", "net", "reboot", "confirm"],
    "admin": ["status", "audit", "audit.verify", "audit.export", "advisor"],
}
MUTATING = {"workspace.edit", "workspace.checkpoint", "op.start", "op.run",
            "host.systemctl", "host.docker", "host.pkg", "host.file", "host.net",
            "host.reboot", "host.confirm"}


def _env_number(name: str, default: float) -> float:
    """Число из среды не имеет права уронить демон при старте."""
    try:
        raw = (os.environ.get(name) or "").strip()
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


# Срок счёта на СТОРОНЕ ДЕМОНА. Раньше dispatch звался без всякого срока (`conn.settimeout(3600)`
# сторожил только recv/send), и 26.07 один `impact('/tmp')` держал ядро 793с, пока клиент уже
# умер. Число ниже клиентского потолка руки — чтобы честный частичный ответ успевал доехать.
WORKSPACE_BUDGET_SEC = _env_number("PRAXIS_SERVERD_WORKSPACE_BUDGET_SEC", 420.0)
# Поток на соединение без счётчика: три параллельных обхода на 4 ядрах = час голодания.
_default_slots = max(1, min(3, (os.cpu_count() or 2) - 1))
WORKSPACE_SLOTS = max(1, int(_env_number("PRAXIS_SERVERD_WORKSPACE_SLOTS", _default_slots)))
# Сколько ждать освободившийся слот прежде чем ответить «занято» — это очередь, не отказ.
WORKSPACE_WAIT_SEC = _env_number("PRAXIS_SERVERD_WORKSPACE_WAIT_SEC", 60.0)
# Сколько демон держит соединение. Число было вписано трижды (settimeout, манифест, текст) —
# одно имя, чтобы «сколько я живу» нельзя было рассинхронить и тем самым соврать.
CONNECTION_TIMEOUT_SEC = _env_number("PRAXIS_SERVERD_CONNECTION_TIMEOUT_SEC", 3600.0)
# Потолок ЕЁ собственного срока (`budget_seconds`, см. workspace_budget). Не физика и не
# приговор: столько же, сколько живёт соединение — считать дольше, чем живёт канал, значит
# считать некому. Сдвигается PRAXIS_SERVERD_WORKSPACE_BUDGET_MAX_SEC.
WORKSPACE_BUDGET_MAX_SEC = _env_number("PRAXIS_SERVERD_WORKSPACE_BUDGET_MAX_SEC",
                                       CONNECTION_TIMEOUT_SEC)
# Тяжело только семантическое чтение; read/list/diff/status не считают дерево и не ждут слота.
HEAVY_INSPECT_ACTIONS = {"overview", "review", "model", "impact", "checks",
                         "symbols", "references", "diagnostics", "search"}

_container_cache: dict[str, tuple[str, float]] = {}
# Неудача выяснения контейнера кэшируется ОТДЕЛЬНО от ответа и на порядок короче: сама
# неудача — не факт о заявителе, но и платить по 10с таймаута docker на каждой заявке, пока
# он лежит, незачем. Хранится ПРИЧИНА, а не вердикт.
CONTAINER_ERROR_TTL_SEC = _env_number("PRAXIS_SERVERD_CONTAINER_ERROR_TTL_SEC", 10.0)
_container_error_cache: dict[str, tuple[str, float]] = {}
_request_lock = threading.Lock()
_request_locks: dict[str, threading.Lock] = {}
_workspace_slots = threading.BoundedSemaphore(WORKSPACE_SLOTS)
_workspace_lock = threading.Lock()
_workspace_active: dict[str, dict] = {}
# Сколько заявок реально привезли `budget_seconds` с запуска демона. Код её инструмента
# отсюда не прочитать, но «приезжала ли ручка хоть раз» — факт, который демон знает точно, и
# он решает спор: 27.07 `grep -rn budget_seconds --include=*.py` вне serverd/ был ПУСТ, то
# есть ручка объявлялась рычагом, не существуя в клиенте. Обещать рычаг, которого нет, —
# та же ложь, что молчаливый предел, только наоборот.
_budget_handle_seen = 0
AUDIT: auditlog.AuditLog | None = None


def limits() -> dict:
    """Пределы демона одним читаемым куском — чтобы она узнавала их не из текста отказа."""
    row = dict(brokerops.limits_manifest())
    row.update({
        "workspace_budget_s": WORKSPACE_BUDGET_SEC,
        "workspace_budget_handle": "budget_seconds",
        "workspace_budget_max_s": WORKSPACE_BUDGET_MAX_SEC,
        "workspace_slots": WORKSPACE_SLOTS,
        "workspace_wait_s": WORKSPACE_WAIT_SEC,
        "heavy_actions": sorted(HEAVY_INSPECT_ACTIONS),
        "connection_socket_timeout_s": CONNECTION_TIMEOUT_SEC,
        "container_error_ttl_s": CONTAINER_ERROR_TTL_SEC,
        "max_request_bytes": MAX_REQUEST_BYTES,
        "recovery_default_delay_s": 120,
        "workspace_budget_handle_seen": _budget_handle_seen,
    })
    # «Довозит ли аргумент твой инструмент, отсюда не видно» было честно про исходники и всё
    # же оставляло рычаг обещанным. Приезжала ли ручка ХОТЬ РАЗ — демон знает наверняка,
    # и молчать об этом нельзя: несдвигаемый предел, названный сдвигаемым, хуже молчаливого.
    if _budget_handle_seen:
        handle_state = (f"Твой инструмент её уже привозил ({_budget_handle_seen} раз с моего "
                        f"запуска) — ручка живая.")
    else:
        handle_state = ("⚠ Но с моего запуска ЭТУ ручку не привезла ни одна заявка: похоже, твой "
                        "инструмент её не передаёт, и тогда мои "
                        f"{WORKSPACE_BUDGET_SEC:.0f}с для тебя НЕСДВИГАЕМЫ. Это предел "
                        "инструмента (serverd_client), а не мой, и чинится он в нём.")
    row["note"] = (
        f"тяжёлое чтение ({', '.join(sorted(HEAVY_INSPECT_ACTIONS))}) считает не дольше "
        f"{WORKSPACE_BUDGET_SEC:.0f}с и отдаёт честно усечённый ответ, а не обрыв; одновременно "
        f"таких обходов не больше {WORKSPACE_SLOTS} (ждём слот до {WORKSPACE_WAIT_SEC:.0f}с); "
        f"сохранение (workspace.checkpoint) слот НЕ занимает и в очереди не стоит. "
        f"Этот срок МОЙ и он сдвигается твоим: аргумент `budget_seconds` в самой заявке "
        f"workspace.inspect сильнее моих {WORKSPACE_BUDGET_SEC:.0f}с, потолок "
        f"{WORKSPACE_BUDGET_MAX_SEC:.0f}с (PRAXIS_SERVERD_WORKSPACE_BUDGET_MAX_SEC), больше — "
        f"обрежу до потолка и скажу об этом. " + handle_state + " "
        + row["note"]
    )
    return row


def _fallback_phrase(server_deadline: float | None) -> str:
    """Чей срок считает, когда её ручки в заявке нет. Ответ разный, и соврать тут легко:
    мой срок стоит ТОЛЬКО на тяжёлом чтении (`_heavy`), у остальных действий его нет вовсе
    и обход идёт по обычному бюджету forge_intelligence (файлы/секунды из того же манифеста)."""
    if server_deadline is None:
        return ("это чтение не из тяжёлых, моего срока на нём нет — обход шёл по обычному "
                "бюджету (файлы/секунды названы в манифесте пределов)")
    return f"считала по своему сроку {WORKSPACE_BUDGET_SEC:.0f}с"


def workspace_budget(asked, server_deadline: float | None = None) -> tuple[float | None, str]:
    """Её `budget_seconds` против срока демона. -> (секунды или None, признание или '').

    ⚠ 27.07. Здесь (в комментарии у workspace.inspect) стояло: «её собственная ручка —
    `budget_seconds`, и она сильнее этого срока (совет, не забор)». На стороне демона это
    правда — `brokerops.inspect` действительно берёт её число вместо моего срока. Но из её
    рук этот аргумент не передавался НИОТКУДА: `grep -rn budget_seconds --include=*.py` вне
    `serverd/` — пусто. То есть 420с были несдвигаемым пределом, который код описывал как
    советуемый: молчаливое ограничение плюс ложь про рычаг.

    Провести ручку до её тулов целиком отсюда нельзя (это `serverd_client.py` и `agent.py`),
    поэтому здесь делается всё, что принадлежит демону: ручка честно принимается и
    ограничивается только названным потолком, мусор в ней не молчит, а её отсутствие
    признаётся в ответе, когда именно мой срок и обрезал обход.

    Ничего не запрещаем: непонятное число не отменяет вызов, а откатывает его на мой срок
    с прямым текстом, что произошло.
    """
    if asked is None or (isinstance(asked, str) and not asked.strip()):
        return None, ""
    fallback = _fallback_phrase(server_deadline)
    try:
        seconds = float(asked)
    except (TypeError, ValueError):
        return None, (f"budget_seconds={asked!r} — это не число секунд, поэтому {fallback}. "
                      f"Ручку не отбираю: пришли число, и оно будет сильнее моего срока.")
    if seconds <= 0:
        return None, (f"budget_seconds={seconds:.0f} — не срок, а его отсутствие, и «считай "
                      f"сколько хочешь» я обещать не могу: соединение живёт "
                      f"{CONNECTION_TIMEOUT_SEC:.0f}с. Поэтому {fallback}; чтобы считать "
                      f"дольше, назови число до {WORKSPACE_BUDGET_MAX_SEC:.0f}с.")
    if seconds > WORKSPACE_BUDGET_MAX_SEC:
        return WORKSPACE_BUDGET_MAX_SEC, (
            f"просила {seconds:.0f}с, считала {WORKSPACE_BUDGET_MAX_SEC:.0f}с — это мой "
            f"потолок на твой срок (PRAXIS_SERVERD_WORKSPACE_BUDGET_MAX_SEC), столько же, "
            f"сколько я держу соединение. Ответ мог остаться неполным именно поэтому.")
    return seconds, f"считала по ТВОЕМУ сроку {seconds:.0f}с, а не по своему ({fallback})."


def _with_budget_note(result: dict, budget: float | None, note: str,
                      server_deadline: float | None) -> dict:
    """Признание про срок кладётся В ОТВЕТ, а не в комментарий к коду: предел, который она
    не может ни назвать, ни сдвинуть, — молчаливый предел, даже если он разумный.

    Причину усечения себе НЕ приписываем: обход связывает и время, и число файлов, и что
    именно кончилось первым — сказано в `limits.note` самим бюджетом. Здесь говорится
    только то, что демон знает точно: чей это был срок и чем его сдвинуть.
    """
    if not isinstance(result, dict):
        return result
    limits_row = result.get("limits")
    incomplete = bool(result.get("truncated")) or (
        isinstance(limits_row, dict) and limits_row.get("complete") is False)
    if budget is None and incomplete and server_deadline is not None and not note:
        note = (f"ответ неполон, и в заявке не было `budget_seconds`: если обход связало "
                f"ВРЕМЯ, это был МОЙ срок {WORKSPACE_BUDGET_SEC:.0f}с (что именно кончилось "
                f"— сказано в limits). Эта ручка сильнее моего срока (потолок "
                f"{WORKSPACE_BUDGET_MAX_SEC:.0f}с); если твой инструмент её не передаёт, "
                f"мой срок для тебя несдвигаем, и это предел инструмента, а не демона.")
    if not note:
        return result
    result["budget_note"] = note
    result["note"] = "\n".join(piece for piece in
                              (str(result.get("note") or "").rstrip(), note) if piece)
    return result


# `admin.status.limits` до неё не доезжает: её единственный читатель `serverd_client.state_line`
# берёт из ответа protocol/operations/audit и выбрасывает остальное, а тулы на `admin.*` у неё
# нет вовсе. Поэтому те же числа кладутся в ориентировку по корню — текст, который она читает
# первым и целиком (`brokerops._orientation`). Регистрация, а не импорт: broker импортирует
# brokerops, обратный импорт был бы циклом.
brokerops.register_daemon_limits(limits)


def _heavy(verb: str, args: dict) -> bool:
    """Тяжёлое = семантический ОБХОД дерева. Ничего больше слот занимать не имеет права.

    ⚠ 27.07. Здесь стоял ещё и `workspace.checkpoint` — единственный новый отказ этой волны
    на пути СОХРАНЕНИЯ её работы: чужой `impact('/opt')` держит слот до 420с, а её
    `coding_checkpoint` в это время получал «занято». Между тем checkpoint дерево не обходит:
    это `git status --porcelain` + `git add -A` + `git commit`, работа по индексу git, без
    чтения и разбора файлов. Вдобавок он молчал — `limits()["heavy_actions"]` перечисляет
    ровно HEAVY_INSPECT_ACTIONS, то есть слот отнимало действие, которого в объявленном
    списке тяжёлых не было вовсе. Состав HEAVY_INSPECT_ACTIONS перепроверен тем же вопросом:
    все девять зовут source_files()/impact() и читают дерево. Оговорка: symbols/references/
    diagnostics с `path` на КОНКРЕТНЫЙ файл дерево не обходят и слот берут зря — но проверка
    «файл это или каталог» лезет в ФС из классификатора, а держат они слот доли секунды;
    трогать это перед деплоем не стал, оставил в отчёте.
    """
    if verb != "workspace.inspect":
        return False
    return str((args or {}).get("action") or "status").strip().lower() in HEAVY_INSPECT_ACTIONS


def _busy_answer(waited: float) -> dict:
    with _workspace_lock:
        rows = [{"request_id": row.get("request_id", ""), "verb": row["verb"],
                 "action": row["action"], "root": row["root"],
                 "seconds": round(time.monotonic() - row["started"], 1)}
                for row in _workspace_active.values()]
    # Раз мы здесь — свободных разрешений НЕТ ни одного, значит занято ровно WORKSPACE_SLOTS.
    # Всё, что не подтверждено живым обходом, — утёкшее разрешение. Признак стоял только на
    # ПОЛНОЙ утечке (`not rows`), а инцидент 27.07 шёл ступенями 3 → 2 → 1: на первых двух
    # ступенях ответ говорил `slots_unaccounted: false` при уже потерянном слоте, то есть врал.
    unaccounted = max(0, WORKSPACE_SLOTS - len(rows))
    listing = ("Сейчас считают: "
               + "; ".join(f"{row['verb']}/{row['action']} {row['root']} "
                           f"{row['seconds']:.0f}с" for row in rows) + ".") if rows else ""
    # Расхождение бывает по двум причинам, и обе надо назвать вслух: печатать «Сейчас
    # считают: —» значило бы в одной строке заявить «занято» и тут же признать «ничем не занято».
    drift = ("либо столько обходов закончилось ровно в эту секунду, либо счётчик слотов "
             "разошёлся с явью — во втором случае сам он не починится и тяжёлое чтение "
             "будет отказывать до перезапуска praxis-serverd.")
    if not rows:
        running = "Но НИ ОДНОГО живого обхода сейчас не числится: " + drift
    elif unaccounted:
        running = (listing + f" Но живых обходов всего {len(rows)} из {WORKSPACE_SLOTS} "
                   f"слотов: остальные {unaccounted} заняты без обхода — " + drift)
    else:
        running = listing
    return {"ok": False, "code": "busy", "active": rows, "limits": limits(),
            "retry_after_s": round(WORKSPACE_WAIT_SEC, 1), "slots_unaccounted": unaccounted > 0,
            "slots_unaccounted_count": unaccounted,
            "error": (f"занято: все {WORKSPACE_SLOTS} обработчика тяжёлого чтения заняты, ждала "
                      f"{waited:.0f}с и не дождалась. " + running
                      + " Ничего не запускалось и не менялось — повтори позже или сузь корень.")}


def workspace_slot(request_id: str, verb: str, args: dict):
    """Занять место под тяжёлое чтение.

    Возвращает `(deadline, busy, slot)`. `busy` не None — значит слот не освободился за
    WORKSPACE_WAIT_SEC и НИЧЕГО не выполнялось; это факт о занятости, а не запрет.
    Освобождать — `workspace_release(slot)` ТЕМ ЖЕ токеном, что вернул этот вызов.
    """
    if not _heavy(verb, args):
        return None, None, ""
    started = time.monotonic()
    if not _workspace_slots.acquire(timeout=WORKSPACE_WAIT_SEC):
        return None, _busy_answer(time.monotonic() - started), ""
    # ⚠ Ключ занятости — НЕ request_id. `workspace.inspect` не входит в MUTATING, значит
    # handle даёт ему одноразовый замок и два хода с ОДНИМ номером идут параллельно; а такие
    # ходы клиент делает штатно (`serverd_client.py:394` повторяет transport/eof тем же
    # номером, `_take_in_doubt` переиспользует его после её таймаута, пока демон ещё считает).
    # С ключом по номеру второй захват затирал запись первого: первый release отдавал одно
    # разрешение на двоих, второй не отдавал ничего. Репро 27.07 в linux-образе: 3 → 2 → 1
    # свободных слота, и дальше вечное «занято» при пустом списке считающих.
    #
    # Разрешение уже взято, а записи о нём ещё нет: любой сбой в этом окне (в т.ч. падение
    # самого сбора полей заявки) унёс бы слот молча — ровно тот класс утечки, что 27.07 дал
    # 3 → 2 → 1. Отдаём назад и падаем дальше: до `finally` в handle этот слот не доживает.
    slot = ""
    try:
        slot = f"{request_id}:{uuid.uuid4().hex}"
        with _workspace_lock:
            _workspace_active[slot] = {
                "request_id": request_id,
                "verb": verb, "action": str((args or {}).get("action") or ""),
                "root": str((args or {}).get("root") or (args or {}).get("root_value") or ""),
                "started": time.monotonic()}
    except BaseException:
        _workspace_slots.release()
        raise
    return time.monotonic() + WORKSPACE_BUDGET_SEC, None, slot


def workspace_release(slot: str) -> None:
    if not slot:
        return
    with _workspace_lock:
        present = _workspace_active.pop(slot, None)
    if present is not None:
        _workspace_slots.release()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"{_now()} serverd-v2: {message}", flush=True)


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _peer_cred(conn: socket.socket) -> dict:
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return {"pid": pid, "uid": uid, "gid": gid}
    except OSError:
        return {"pid": 0, "uid": -1, "gid": -1}


def _peer_container(pid: int) -> tuple[str, str]:
    """Имя контейнера заявителя и ПРИЧИНА, если выяснить не вышло. -> (имя или '', причина).

    ⚠ 27.07. Пустая строка значила сразу три разные вещи: «заявитель не в докере», «/proc не
    прочитался» и «docker inspect не ответил». Все три уезжали в один отказ «peer container ?
    is not praxis» — то есть на неудавшийся вопрос она получала утверждение о заявителе.
    Хуже: неудача КЭШИРОВАЛАСЬ на 60с наравне с ответом, и один таймаут docker закрывал ей
    доступ к демону на минуту с текстом, отрицающим её же контейнер. Кэшируем только ответы.
    """
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError as exc:
        return "", f"/proc/{pid}/cgroup: {type(exc).__name__}: {exc}"
    docker_id = ""
    for token in cgroup.replace("/", " ").replace(".scope", " ").split():
        if token.startswith("docker-") and len(token) > 20:
            docker_id = token[len("docker-"):]
            break
    if not docker_id:
        return "", ""   # честный ответ: заявитель не в докер-контейнере
    cached = _container_cache.get(docker_id)
    if cached and time.monotonic() - cached[1] < 60:
        return cached[0], ""
    failed = _container_error_cache.get(docker_id)
    if failed and time.monotonic() - failed[1] < CONTAINER_ERROR_TTL_SEC:
        # Кэшируем ПРИЧИНУ, а не вердикт: пока docker молчит, каждая заявка платила бы по 10с
        # своего таймаута, но выдавать это молчание за ответ «контейнер не тот» всё равно нельзя.
        return "", failed[0] + f" (тот же ответ ≤{CONTAINER_ERROR_TTL_SEC:.0f}с)"
    try:
        result = subprocess.run(["docker", "inspect", "-f", "{{.Name}}", docker_id],
                                capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        reason = f"docker inspect {docker_id[:12]}: {type(exc).__name__}: {exc}"
        _container_error_cache[docker_id] = (reason, time.monotonic())
        return "", reason
    if result.returncode != 0:
        reason = (f"docker inspect {docker_id[:12]} завершился с кодом {result.returncode}: "
                  f"{(result.stderr or '').strip()[:200] or 'без вывода'}")
        _container_error_cache[docker_id] = (reason, time.monotonic())
        return "", reason
    name = result.stdout.strip().lstrip("/")
    _container_cache[docker_id] = (name, time.monotonic())
    _container_error_cache.pop(docker_id, None)
    return name, ""


def _token_ok(supplied: str) -> tuple[bool, str]:
    """Сверка токена и ПРИЧИНА отказа. -> (сошёлся, причина).

    ⚠ 27.07. Пропавший, пустой или нечитаемый файл токена давал ей ровно тот же ответ, что
    и подложный токен: «token mismatch». То есть на «мне нечем сверять» она читала «ты
    прислала не то» — и чинила бы не то, что сломано.
    """
    try:
        expected = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False, f"файла токена {TOKEN_FILE} нет — сверять не с чем; это сбой демона, не твой"
    except OSError as exc:
        return False, (f"файл токена {TOKEN_FILE} не прочитан ({type(exc).__name__}: {exc}) — "
                       f"сверять не с чем; это сбой демона, не твой")
    if not expected:
        return False, f"файл токена {TOKEN_FILE} пуст — сверять не с чем; это сбой демона, не твой"
    if not hmac.compare_digest(expected, str(supplied or "")):
        return False, "token mismatch"
    return True, ""


def authorize(conn: socket.socket, request: dict) -> tuple[dict | None, str]:
    peer = _peer_cred(conn)
    container, container_error = _peer_container(peer["pid"]) if peer["pid"] else ("", "")
    peer["container"] = container
    if container_error:
        peer["container_error"] = container_error
    accepted, token_error = _token_ok(request.get("token", ""))
    if not accepted:
        return None, token_error
    if PIN_CGROUP and container != PRAXIS_CONTAINER:
        if container_error:
            return None, (f"чей это контейнер — выяснить не вышло ({container_error}), поэтому "
                          f"привязку к {PRAXIS_CONTAINER} подтвердить нечем. Это НЕ значит, "
                          f"что заявка пришла не из {PRAXIS_CONTAINER}; заявка не выполнялась")
        return None, f"peer container {container or '?'} is not {PRAXIS_CONTAINER}"
    return peer, ""


def _request_path(request_id: str) -> Path:
    safe = "".join(ch for ch in request_id if ch.isalnum() or ch in "-_")[:96]
    return REQUESTS / f"{safe}.json"


def _cached(request_id: str, verb: str, args_digest: str) -> dict | None:
    if not request_id or verb not in MUTATING:
        return None
    try:
        row = json.loads(_request_path(request_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None   # расписки нет — этой заявки я вправду не видел
    except (OSError, ValueError) as exc:
        # ⚠ 27.07. Здесь стоял тот же `return None`, что и на «файла нет»: битая или
        # нечитаемая расписка означала «попытки не было» и МУТАЦИЯ ПОВТОРЯЛАСЬ ВСЛЕПУЮ.
        # Но файл существует — значит попытка была, а чем кончилась, я не знаю. Это ровно
        # тот случай, под который тут уже заведён честный in_doubt.
        return {"ok": False, "code": "in_doubt", "in_doubt": True, "request_id": request_id,
                "error": (f"расписка по заявке {request_id} существует, но прочитать её не "
                          f"вышло ({type(exc).__name__}: {exc}) — чем кончилась прошлая "
                          f"попытка, я НЕ ЗНАЮ. Повторять вслепую не стал: посмотри "
                          f"состояние хоста, а для нового действия возьми новый номер.")}
    if not isinstance(row, dict):
        return {"ok": False, "code": "in_doubt", "in_doubt": True, "request_id": request_id,
                "error": (f"расписка по заявке {request_id} есть, но это не объект JSON "
                          f"({type(row).__name__}) — чем кончилась прошлая попытка, не знаю.")}
    if row.get("verb") != verb or row.get("args_digest") != args_digest:
        return {"ok": False, "error": "idempotency key reused with different request", "code": "id_conflict"}
    result = row.get("result")
    if isinstance(result, dict):
        return result
    if row.get("state") == "started":
        # Durable operations have deterministic ids and can safely be rejoined.  Other host
        # mutations may already have happened; blindly repeating them would be worse than an
        # explicit in-doubt receipt that asks Praxis to observe the after-state.
        if verb in {"op.start", "op.run"}:
            return None
        return {"ok": False, "error": "previous attempt has no terminal receipt; observe after-state",
                "code": "in_doubt", "in_doubt": True, "request_id": request_id}
    return None


def _store_intent(request_id: str, verb: str, args_digest: str) -> str:
    """Записать «попытка началась». -> причина сбоя или ''.

    Сбой записи раньше летел исключением ДО вызова и превращался в «internal error»: действие
    не выполнялось, а причина отказа выглядела как поломка самого действия.
    """
    if not request_id or verb not in MUTATING:
        return ""
    path = _request_path(request_id)
    row = {"request_id": request_id, "verb": verb, "args_digest": args_digest,
           "stored": _now(), "state": "started"}
    try:
        REQUESTS.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return ""
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return ""
    except OSError as exc:
        _log(f"intent {request_id} not stored: {type(exc).__name__}: {exc}")
        return f"{type(exc).__name__}: {exc}"


def _store_request(request_id: str, verb: str, args_digest: str, result: dict) -> str:
    """Записать расписку об исходе. -> причина сбоя или ''.

    ⚠ 27.07. Сбой записи здесь летел исключением ПОСЛЕ выполненного действия: `handle` ловил
    его общим `except` и отвечал ей «internal: OSError …» — то есть выполненную мутацию хоста
    объявлял несостоявшейся. Тот же класс, что `agent.py:2306`, где ошибка леджера
    становилась вердиктом о доставке. Расписка важна, но истина — в том, что уже сделано.
    """
    if not request_id or verb not in MUTATING:
        return ""
    path = _request_path(request_id)
    row = {"request_id": request_id, "verb": verb, "args_digest": args_digest,
           "stored": _now(), "state": "finished", "result": result}
    try:
        REQUESTS.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return ""
    except (OSError, ValueError) as exc:
        _log(f"receipt {request_id} not stored: {type(exc).__name__}: {exc}")
        return f"{type(exc).__name__}: {exc}"


def _with_receipt_warning(result: dict, intent_error: str, receipt_error: str,
                          request_id: str) -> dict:
    """Сказать вслух, что расписки об этом действии нет.

    Без расписки повтор под тем же номером НЕ подхватит результат, а выполнит мутацию хоста
    ВТОРОЙ раз — и клиент повторяет номер штатно (`serverd_client` после transport/eof).
    Молчать об этом значит зарядить второй `apt`/`systemctl`/`op.start` под видом идемпотентности.
    """
    if not isinstance(result, dict) or not (intent_error or receipt_error):
        return result
    if receipt_error:
        line = (f"⚠ действие ВЫПОЛНЕНО, но расписку о нём записать не вышло ({receipt_error}). "
                f"Повтор под тем же номером заявки {request_id} этот результат НЕ подхватит — "
                f"он выполнит действие второй раз. Если надо повторять, сперва посмотри "
                f"состояние хоста.")
    else:
        line = (f"⚠ отметку о начале заявки {request_id} записать не вышло ({intent_error}); "
                f"действие выполнено. Страховка от двойного выполнения по этому номеру не "
                f"работает — состояние хоста надёжнее номера.")
    row = dict(result)
    row["receipt_error"] = receipt_error or intent_error
    row["note"] = "\n".join(piece for piece in (str(row.get("note") or ""), line) if piece)
    return row


def _idempotency_lock(request_id: str) -> threading.Lock:
    with _request_lock:
        lock = _request_locks.get(request_id)
        if lock is None:
            lock = threading.Lock()
            _request_locks[request_id] = lock
        return lock


def _legacy_root(task_id: str) -> tuple[str, str]:
    """One-way compatibility for hcode tasks created before broker v2; never writes old state.

    Возвращает (корень, причина). Пустой корень раньше молча ронял вызов в самый низ dispatch,
    и она получала «unknown broker verb forge.inspect» — неверную причину: глагол известен,
    неизвестна задача.
    """
    path = STATE / "tasks" / task_id / "task.json"
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "", f"записи о задаче {task_id} в {path.parent} нет"
    except (OSError, ValueError) as exc:
        return "", f"{path}: {type(exc).__name__}: {exc}"
    if not isinstance(row, dict) or not str(row.get("root") or ""):
        return "", f"в {path} не назван корень задачи"
    return str(row["root"]), ""


def dispatch(verb: str, args: dict, send_chunk=lambda _text: None, request_id: str = "",
             deadline: float | None = None) -> dict:
    values = {key: value for key, value in (args or {}).items() if value is not None}
    if verb == "admin.status":
        assert AUDIT is not None
        # `limit=40` резал список молча: блок состояния показывал «вот все операции» на
        # усечённом наборе. Числа приезжают вместе со списком, а не остаются в срезе.
        ops = brokerops.op_list(limit=40)
        answer = {"ok": True, "protocol": PROTOCOL, "capabilities": CAPABILITIES,
                  "operations": ops.get("operations", []),
                  "operations_total": ops.get("total", 0),
                  "audit": AUDIT.status(), "advisor": advisor.manifest(),
                  "limits": limits(),
                  "recovery": hostverbs.armed_recoveries()}
        if not ops.get("ok", True):
            # ⚠ 27.07. `operations: []` + `operations_total: 0` — это блок состояния, который она
            # читает как «на хосте сейчас ничего не крутится». Когда список не состоялся вовсе
            # (каталог операций не прочитан), пустота обязана приехать с признанием, а не молча.
            answer["operations_unknown"] = True
            answer["operations_error"] = str(ops.get("error") or "")
            answer["operations_total"] = None
        if ops.get("note") or ops.get("error"):
            answer["operations_note"] = str(ops.get("note") or ops.get("error"))
        return answer
    if verb == "admin.audit":
        assert AUDIT is not None
        return {"ok": True, "rows": AUDIT.tail(int(values.get("tail") or 40))}
    if verb == "admin.audit.verify":
        assert AUDIT is not None
        verdict = AUDIT.verify()
        AUDIT.startup_verify = verdict
        return {"ok": True, "verify": verdict}
    if verb == "admin.audit.export":
        assert AUDIT is not None
        return AUDIT.export(EXPORTS)
    if verb in {"admin.advisor", "admin.deadman"}:
        return {"ok": True, **advisor.manifest()}
    if verb == "workspace.inspect":
        # Срок приезжает от handle: тяжёлое чтение обязано вернуть частичный ответ вовремя, а не
        # считать до потери клиента. `deadline` — монотонные часы демона, снаружи их задать нельзя.
        # Её ручка поверх него — `budget_seconds` в самой заявке; сильнее моего срока она только
        # после `workspace_budget`, и только там же становится СКАЗАННОЙ (см. R7 в его докстринге).
        values["deadline"] = deadline
        asked = values.pop("budget_seconds", None)
        if asked is not None:
            # Считаем ФАКТ приезда, а не удачных чисел: даже мусор в ручке доказывает, что
            # инструмент её передаёт, — а именно это и спорно (см. _budget_handle_seen).
            global _budget_handle_seen
            _budget_handle_seen += 1
        budget, budget_note = workspace_budget(asked, deadline)
        if budget is not None:
            values["budget_seconds"] = budget
        return _with_budget_note(brokerops.inspect(values.pop("root", ""), **values),
                                 budget, budget_note, deadline)
    if verb == "workspace.edit":
        return brokerops.edit(values.pop("root", ""), **values)
    if verb == "workspace.checkpoint":
        return brokerops.checkpoint(values.get("root", ""), values.get("message", "Praxis host checkpoint"))
    if verb == "op.start":
        values.setdefault("operation_id", "op-" + hashlib.sha256(request_id.encode()).hexdigest()[:16])
        return brokerops.op_start(**values)
    if verb == "op.run":
        values.setdefault("operation_id", "op-" + hashlib.sha256(request_id.encode()).hexdigest()[:16])
        return brokerops.run_wait(send_chunk=send_chunk, **values)
    if verb == "op.poll":
        return brokerops.op_poll(values.get("operation_id", ""), int(values.get("tail") or 12000))
    if verb == "op.stop":
        return brokerops.op_stop(values.get("operation_id", ""))
    if verb == "op.list":
        return brokerops.op_list(values.get("root_value", ""), int(values.get("limit") or 100))
    if verb.startswith("host."):
        return hostverbs.dispatch(verb, values)

    # Rolling-upgrade bridge for old container code. The next container stores tasks only in Forge.
    task_id = str(values.pop("task", "") or values.pop("task_id", ""))
    root, root_error = _legacy_root(task_id) if task_id else ("", "")
    if verb.startswith("forge.") and not root:
        return {"ok": False, "code": "legacy_task_unknown",
                "error": (f"старый глагол {verb}: корень задачи взять неоткуда — "
                          + (root_error if root_error else
                             "номер задачи в заявке не назван вовсе")
                          + ". Ничего не выполнялось. Новые глаголы — workspace.*/op.*")}
    if verb == "forge.inspect" and root:
        return brokerops.inspect(root, **values)
    if verb == "forge.edit" and root:
        return brokerops.edit(root, **values)
    if verb == "forge.run" and root:
        return brokerops.run_wait(root, values.get("command", ""), values.get("cwd", "."),
                                  int(values.get("timeout") or 600), send_chunk)
    if verb == "forge.checkpoint" and root:
        return brokerops.checkpoint(root, values.get("message", "legacy host checkpoint"))
    return {"ok": False, "error": f"unknown broker verb {verb}", "code": "unknown_verb"}


def _audit(peer: dict, request_id: str, verb: str, task: str, args_digest: str,
           result: dict, cached: bool = False) -> None:
    if AUDIT is None:
        return
    row = {"at": _now(), "kind": "rpc", "request_id": request_id,
           "peer_pid": peer.get("pid"), "peer_uid": peer.get("uid"),
           "peer_cgroup": peer.get("container"), "verb": verb, "task": task,
           "args_digest": args_digest, "result_digest": _digest(result),
           "status": "ok" if result.get("ok", True) else "failed", "cached": bool(cached)}
    if result.get("advice"):
        row["advice"] = str(result.get("advice"))[:1000]
    if result.get("id") and str(result.get("id")).startswith("op-"):
        row["operation_id"] = result.get("id")
    AUDIT.append(row)


def handle(conn: socket.socket) -> None:
    peer: dict | None = None
    request_id = ""
    verb = "?"
    try:
        conn.settimeout(CONNECTION_TIMEOUT_SEC)
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buffer += chunk
            if len(buffer) > MAX_REQUEST_BYTES:
                conn.sendall(json.dumps({"type": "error", "protocol": PROTOCOL,
                                         "code": "too_big"}).encode() + b"\n")
                return
        line, _ = buffer.split(b"\n", 1)
        request = json.loads(line.decode("utf-8", "replace"))
        request_id = str(request.get("request_id") or request.get("id") or uuid.uuid4().hex)
        verb = str(request.get("verb") or "?")
        peer, error = authorize(conn, request)
        if error:
            if AUDIT:
                AUDIT.append({"at": _now(), "kind": "rpc", "request_id": request_id,
                              "peer_pid": _peer_cred(conn).get("pid"), "verb": verb,
                              "status": "unauth", "error": error})
            conn.sendall(json.dumps({"type": "error", "protocol": PROTOCOL,
                                     "request_id": request_id, "code": "unauth",
                                     "message": error}).encode() + b"\n")
            return
        supplied_protocol = str(request.get("protocol") or "")
        if supplied_protocol and supplied_protocol != PROTOCOL:
            conn.sendall(json.dumps({"type": "error", "protocol": PROTOCOL,
                                     "request_id": request_id, "code": "protocol",
                                     "message": f"need {PROTOCOL}, got {supplied_protocol}"}).encode() + b"\n")
            return
        args = request.get("args") or {}
        args_digest = _digest(args)
        task = str(args.get("task") or args.get("task_id") or "")
        def send_chunk(text: str) -> None:
            try:
                conn.sendall(json.dumps({"type": "chunk", "protocol": PROTOCOL,
                                         "request_id": request_id, "data": text},
                                        ensure_ascii=False).encode("utf-8") + b"\n")
            except OSError:
                pass

        lock = _idempotency_lock(request_id) if verb in MUTATING else threading.Lock()
        with lock:
            result = _cached(request_id, verb, args_digest)
            was_cached = result is not None
            if result is None:
                deadline, busy, slot = workspace_slot(request_id, verb, args)
                if busy is not None:
                    result = busy
                else:
                    try:
                        intent_error = _store_intent(request_id, verb, args_digest)
                        result = dispatch(
                            verb, args,
                            send_chunk if request.get("stream") else (lambda _text: None),
                            request_id=request_id, deadline=deadline)
                        receipt_error = _store_request(request_id, verb, args_digest, result)
                        result = _with_receipt_warning(result, intent_error, receipt_error,
                                                       request_id)
                    finally:
                        # Токен этого захвата, а не номер заявки: под одним номером может
                        # считать второй такой же ход, и его слот не наш, чтобы его отдавать.
                        workspace_release(slot)
        if verb in MUTATING:
            with _request_lock:
                if _request_locks.get(request_id) is lock:
                    _request_locks.pop(request_id, None)
        _audit(peer or {}, request_id, verb, task, args_digest, result, was_cached)
        frame = {"type": "result", "protocol": PROTOCOL, "request_id": request_id, **result}
        try:
            conn.sendall(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError as exc:
            # ⚠ 26.07: три `handle workspace.inspect failed: BrokenPipeError` при `status=ok`
            # в аудите — демон досчитал, а клиент уже умер. В аудите вызов выглядел удавшимся,
            # и факт «ответ никто не получил» не хранился НИГДЕ: восстанавливать его пришлось
            # сверкой journalctl с audit.jsonl. Выполнение и доставка — два разных факта.
            _log(f"result {verb} [{request_id}] not delivered: {type(exc).__name__}: {exc}")
            if AUDIT:
                AUDIT.append({"at": _now(), "kind": "rpc", "request_id": request_id,
                              "peer_pid": (peer or {}).get("pid"), "verb": verb, "task": task,
                              "args_digest": args_digest, "status": "undelivered",
                              "error": f"{type(exc).__name__}: {exc}"})
    except (ValueError, json.JSONDecodeError) as exc:
        try:
            conn.sendall(json.dumps({"type": "error", "protocol": PROTOCOL,
                                     "request_id": request_id, "code": "bad_request",
                                     "message": str(exc)}).encode() + b"\n")
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        _log(f"handle {verb} failed: {type(exc).__name__}: {exc}")
        try:
            conn.sendall(json.dumps({"type": "error", "protocol": PROTOCOL,
                                     "request_id": request_id, "code": "internal",
                                     "message": f"{type(exc).__name__}: {exc}"}).encode() + b"\n")
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve() -> None:
    global AUDIT
    STATE.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS.mkdir(parents=True, exist_ok=True)
    brokerops.configure(STATE)
    hostverbs.configure(STATE)
    AUDIT = auditlog.AuditLog(AUDIT_FILE)
    reconciled = brokerops.reconcile()
    reboot_state = hostverbs.reboot("status")
    verdict = AUDIT.verify()
    _log(f"reconcile={reconciled}; reboot={reboot_state}; audit={verdict}")
    try:
        SOCK.unlink()
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCK))
    try:
        os.chmod(SOCK, 0o660)
        import grp
        os.chown(SOCK, 0, grp.getgrnam("praxis").gr_gid)
    except (OSError, KeyError) as exc:
        _log(f"socket ownership: {exc}; cgroup pin remains authoritative")
    server.listen(64)
    _log(f"listening {SOCK}; protocol={PROTOCOL}; cgroup_pin={PIN_CGROUP}:{PRAXIS_CONTAINER}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    serve()
