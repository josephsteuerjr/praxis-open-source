"""Куда стенд ходит по сети. ФАЗА 1: только журнал, зубов нет.

03.08.2026. За день нашлись три молчаливых выхода наружу, и каждый — отдельным аудитом:
живой AF_UNIX-RPC к root-демону хоста (~300 вызовов за прогон по 8с срока), два HTTPS к
провайдерам моделей из `panel.brain_catalog()`, и её боевая сессия Telegram, открывавшаяся
тест-процессом на импорте модуля. Ни один не был виден: вердикт двигало ВРЕМЯ, а гейт
краснел как «тесты не уложились», не называя причины.

Разовый аудит так и остаётся разовым. Журнал делает его постоянным.

Почему зубы стоят (а сейчас — не стоят) именно на `connect`/`connect_ex`/`sendto`: это
единственная воронка, в которую сходится ВЕСЬ исходящий трафик дерева — urllib, httpx,
aiohttp через asyncio, smtplib/imaplib, telethon и голые AF_UNIX-сокеты. Слои выше
(`urlopen`, `http.client`, `create_connection`) производны и промахиваются мимо половины
из них, а `getaddrinfo` мимо числовых адресов — и его к тому же глобально патчат шесть
мест в `test_webtool`.

⚠ Чего этот забор не достаёт, и это надо знать, а не выяснять:
  • подпроцессы (`hands`, `git`, форж) — у них своё пространство процессов;
  • Windows/Proactor соединяется через `_overlapped.ConnectEx` мимо `socket.socket`;
  • всё ниже питона: resolv.conf, iptables, netns.
Закрыта питоновская дверь одного процесса — ровно как у файлового забора рядом.

⚠ И чего он не сделает никогда: он НЕ находит ложную зелень. Он делает её опровержимой —
позволяет написать положительный контроль на швы вида `if PRAXIS_TEST: return []`, — но
сам её не ищет.
"""
from __future__ import annotations

import atexit
import os
import socket
import sys
import tempfile
import traceback
from collections import Counter

class StandWentOutside(BaseException):
    """Стенд попытался выйти наружу. Наследник BaseException — НАМЕРЕННО.

    Сделай его OSError — и `except (URLError, OSError, ...)` в hostview.py:52 его
    проглотит, вернёт None, тест позеленеет: забор своими руками изготовит ложную
    зелень ровно той формы, ради которой он и ставился. Значение отказа не должно
    совпадать со значением ветки отказа.

    Прогон это переживает: unittest ловит голым `except:` и кладёт ошибку на
    конкретный тест, а не рвёт весь прогон.
    """


#: Зубы. Выключаются ручкой стенда — она переживает санитайз (_standenv).
TEETH = (os.environ.get("PRAXIS_TEST_NET_TEETH", "1") or "").strip().lower() not in (
    "0", "off", "no", "false")

#: ⚠ loopback НЕ безопасен сам по себе: за этими портами живут настоящие выходы.
LOOPBACK_DENY = {5011: "реле мозга", 5012: "реле через host-Caddy", 9473: "мост тела"}

#: Все попытки соединения за прогон.
ATTEMPTS: list[dict] = []

#: Имя идущего теста. Заполняет `_standenv` на каждом тесте; пусто — вне теста (импорт).
CURRENT_TEST = ""

_STDLIB = os.path.dirname(os.path.abspath(os.__file__))
_SELF_FILE = os.path.abspath(__file__)

_VERBS = ("connect", "connect_ex", "sendto")


def _throwaway_roots() -> tuple[str, ...]:
    return tuple(p for p in (os.environ.get("PRAXIS_BASE") or "", tempfile.gettempdir()) if p)


def classify(family, address) -> str:
    """«песочница» / «loopback» / «НАРУЖУ» — и loopback НЕ синоним безопасного.

    На loopback в этом коде висят настоящие выходы: мост тела (127.0.0.1:9473) и реле
    мозга (127.0.0.1:5011). Фаза 1 их только называет; разделять по портам — работа
    фазы 2, и делать её надо по фактическому журналу, а не по догадке.
    """
    try:
        if family is not None and int(family) == int(getattr(socket, "AF_UNIX", -1)):
            path = os.path.abspath(str(address))
            roots = _throwaway_roots()
            return "песочница" if any(path.startswith(r) for r in roots) else "НАРУЖУ"
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        text = str(host or "")
        if (text.startswith("127.") or text.startswith("::ffff:127.")
                or text in ("::1", "localhost", "")):
            return "loopback"
        return "НАРУЖУ"
    except Exception:
        return "НАРУЖУ"


def _caller() -> str:
    """Первый кадр вне stdlib и вне этого модуля — то есть чей это выход."""
    for frame in reversed(traceback.extract_stack()[:-2]):
        path = os.path.abspath(frame.filename)
        if path == _SELF_FILE or os.path.dirname(path).startswith(_STDLIB):
            continue
        return f"{os.path.basename(path)}:{frame.lineno} {frame.name}"
    return "?"


def _target(family, address) -> str:
    if family is not None and int(family or 0) == int(getattr(socket, "AF_UNIX", -1)):
        return str(address)
    if isinstance(address, (tuple, list)) and address:
        return ":".join(str(part) for part in address[:2])
    return str(address)


def _refusal(kind: str, address) -> str:
    """Почему этот адрес запрещён; пусто — разрешён."""
    if kind == "НАРУЖУ":
        return "адрес вне песочницы"
    if kind == "loopback" and isinstance(address, (tuple, list)) and len(address) > 1:
        try:
            why = LOOPBACK_DENY.get(int(address[1]))
        except (TypeError, ValueError):
            why = None
        if why:
            return f"порт {address[1]} на loopback — это {why}"
    return ""


def _note(verb: str, sock, address) -> None:
    row = None
    try:
        family = getattr(sock, "family", None)
        row = {"verb": verb, "target": _target(family, address),
               "kind": classify(family, address),
               "test": CURRENT_TEST, "caller": _caller()}
        ATTEMPTS.append(row)
    except Exception:  # журнал не имеет права ронять прогон
        return
    if not TEETH:
        return
    reason = _refusal(row["kind"], address)
    if not reason:
        return
    raise StandWentOutside(chr(10).join([
        "СТЕНД ПОШЁЛ НАРУЖУ: " + verb + " -> " + row["target"],
        "  причина: " + reason,
        "  тест:  " + (row["test"] or "(вне теста)"),
        "  звал:  " + row["caller"],
        "Это забор для СТЕНДА, а не для неё: под тестом сеть — это мок, а не срок.",
        "Тест ПРОВЕРЯЕТ отказ сети? Не глуши забор — подмени шов у себя",
        "(mock.patch.object(<модуль>, <клиент>), как в test_hostview.py:239).",
        "Свой сервер на 127.0.0.1 и свой AF_UNIX в песочнице уже разрешены.",
    ]))


def install() -> bool:
    """Встать один раз. False — уже стоял: двойная обёртка удвоила бы журнал."""
    if getattr(socket.socket.connect, "__praxis_net_watch__", False):
        return False
    originals = {name: getattr(socket.socket, name) for name in _VERBS}

    def make(name: str):
        base = originals[name]

        def wrapper(self, *args, **kwargs):
            address = args[-1] if (name == "sendto" and args) else (args[0] if args else None)
            _note(name, self, address)
            return base(self, *args, **kwargs)

        wrapper.__praxis_net_watch__ = True
        wrapper.__wrapped__ = base
        wrapper.__name__ = name
        return wrapper

    for name in _VERBS:
        setattr(socket.socket, name, make(name))
    atexit.register(report)
    return True


def summary() -> dict:
    counts = Counter(row["kind"] for row in ATTEMPTS)
    outward = [row for row in ATTEMPTS if row["kind"] == "НАРУЖУ"]
    by_target = Counter(row["target"] for row in outward)
    tests = {}
    for row in outward:
        tests.setdefault(row["target"], set()).add(row["test"] or "(вне теста)")
    # ⚠ loopback НЕ синоним безопасного: на 127.0.0.1 в этом коде висят мост тела
    # (:9473) и реле мозга (:5011). Поэтому сводка называет и их адресатов —
    # разделять «свой сервер в своём процессе» от «выход через loopback» будет
    # фаза 2, и делать это надо по этому журналу, а не по догадке.
    loop = Counter(row["target"] for row in ATTEMPTS if row["kind"] == "loopback")
    return {"total": len(ATTEMPTS), "by_kind": dict(counts),
            "outward": len(outward), "by_target": dict(by_target),
            "loopback_targets": dict(loop),
            "tests": {k: sorted(v) for k, v in tests.items()}}


def report(stream=None) -> str:
    """Печатается всегда — «ноль попыток наружу» это тоже результат, а не молчание."""
    stream = stream if stream is not None else sys.stderr
    data = summary()
    lines = [f"praxis_test: сеть — попыток наружу {data['outward']} "
             f"(всего соединений {data['total']}: {data['by_kind'] or 'нет'})"]
    for target, count in sorted(data["by_target"].items(), key=lambda kv: -kv[1]):
        who = ", ".join(data["tests"][target][:3])
        more = "" if len(data["tests"][target]) <= 3 else f" (+{len(data['tests'][target]) - 3})"
        lines.append(f"   {target}  ×{count}   {who}{more}")
    text = "\n".join(lines)
    for target, count in sorted((data.get("loopback_targets") or {}).items(),
                                key=lambda kv: -kv[1])[:8]:
        lines.append(f"   loopback {target}  x{count}")
    text = chr(10).join(lines)
    print(text, file=stream)
    return text
