"""
hostview — глаза Праксис на дом, в котором она живёт (PASS 17.A, «хозяйка сервера»).

Первая ступень лестницы доверия: ТОЛЬКО СМОТРЕТЬ. Ни одной новой привилегии у её
контейнера — ни docker.sock, ни /proc хоста. Она спрашивает обсерваторию «Атланту»
(praxis-serverapp) по внутренней сети; та read-only по построению: докер видит через
GET-проксик, хост — примонтированным ro. Отдельная дверь (X-Praxis-Agent), отдельный
allowlist полей: без пользователей, ssh-конфига, истории логинов и командных строк.

Ступени B (перезапуск своих сервисов) и C (правки хоста) сюда НЕ входят и потребуют
другого механизма — исполнителя на хосте. Наблюдатель, который не умеет действовать,
ценен именно этим.

Ход не роняется никогда: нет токена, нет связи, обсерватория молчит → честная строка
«глаза закрыты, потому что …», а не исключение.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("praxis-hostview")

URL = (os.getenv("PRAXIS_SERVERAPP_URL") or "http://serverapp:8093").rstrip("/")
TIMEOUT = float(os.getenv("PRAXIS_HOSTVIEW_TIMEOUT", "8") or 8)

SECTIONS = ("overview", "containers", "ports", "all")


def _token() -> str:
    """Читается на каждый вызов: .env перечитывается при рестарте (load_dotenv override)."""
    return (os.getenv("PRAXIS_AGENT_TOKEN") or "").strip()


def available() -> bool:
    return bool(_token())


def _get(path: str) -> dict | None:
    """GET к обсерватории с её токеном. None = «глаз нет», не ошибка хода."""
    tok = _token()
    if not tok:
        return None
    req = urllib.request.Request(f"{URL}{path}", headers={"X-Praxis-Agent": tok})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        log.warning("hostview: обсерватория недоступна (%s)", path, exc_info=True)
        return None


def fetch() -> dict | None:
    """Сводка о хосте или None."""
    return _get("/api/agent/status")


def logs(unit: str, tail: int = 60) -> str:
    """PASS 17.B: логи её сервиса — read-only, секреты замаскированы обсерваторией.

    Список разрешённых имён живёт в services.py (один источник правды); честный отказ
    для чужих контейнеров формулируется здесь, чтобы не гонять запрос впустую."""
    import services
    unit = (unit or "").strip()
    if unit not in services.LOG_ALLOWED:
        return (f"Логи «{unit}» не в моей зоне. Могу смотреть: {', '.join(services.LOG_ALLOWED)} "
                "— чужие проекты Егора на этой машине не моё дело.")
    if not available():
        return "Глаза закрыты: PRAXIS_AGENT_TOKEN не задан (рычаг Егора в .env)."
    tail = max(1, min(200, int(tail or 60)))
    d = _get(f"/api/agent/logs?unit={urllib.parse.quote(unit)}&tail={tail}")
    if d is None:
        return "Обсерватория не ответила — логи сейчас недоступны."
    if d.get("error"):
        return f"Логи {unit}: {str(d['error'])[:200]}"
    text = (d.get("text") or "").strip()
    return f"{unit}, последние {d.get('tail', tail)} строк:\n{text}" if text \
        else f"{unit}: лог пуст (контейнер молчит или только что поднялся)."


# --------------------------------------------------------------------------- #
#  Человеческие единицы: она читает текст, а не JSON
# --------------------------------------------------------------------------- #

def human_bytes(n) -> str:
    if n is None:
        return "?"          # «не знаю» и «ноль» — разные факты
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit, step in (("ТБ", 1024 ** 4), ("ГБ", 1024 ** 3), ("МБ", 1024 ** 2), ("КБ", 1024)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{int(n)} Б"


def human_uptime(seconds) -> str:
    if seconds is None:
        return "?"
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m}м"
    return f"{m}м"


def _overview_lines(d: dict) -> list[str]:
    host = d.get("host") or {}
    cpu = d.get("cpu") or {}
    mem = d.get("mem") or {}
    root = d.get("disk_root") or {}
    load = cpu.get("load") or {}
    dock = d.get("docker") or {}
    px = d.get("praxis") or {}
    virt = (d.get("virt") or {}).get("kindname") or "?"
    lines = [
        f"Хост {host.get('hostname') or '?'} · {host.get('os') or '?'} · ядро "
        f"{host.get('kernel') or '?'} · аптайм {human_uptime(host.get('uptime'))} · {virt}",
        f"CPU {cpu.get('total', 0)}% (load {load.get('m1', 0)}/{load.get('m5', 0)}/"
        f"{load.get('m15', 0)}) · память {mem.get('used_pct', 0)}% "
        f"({human_bytes(mem.get('used'))} из {human_bytes(mem.get('total'))})",
    ]
    if root:
        lines.append(f"Диск / — занято {root.get('used_pct', 0)}% "
                     f"(свободно {human_bytes(root.get('free'))} из {human_bytes(root.get('total'))})")
    if dock:
        lines.append(f"Контейнеры: {dock.get('running', 0)} живых из {dock.get('total', 0)}")
    if px.get("head"):
        lines.append(f"Я живу на коде {px['head']}" + (f" — «{px.get('subject')}»" if px.get("subject") else ""))
    return lines


def _containers_lines(d: dict) -> list[str]:
    rows = d.get("containers") or []
    if not rows:
        return ["Контейнеров не видно (обсерватория не ответила про докер)."]
    out = ["Контейнеры:"]
    for c in rows:
        alive = c.get("state") == "running"
        mark = "●" if alive else "○"
        eat = (f"cpu {c.get('cpu_pct', 0)}% · {human_bytes(c.get('mem_used'))}"
               if alive and (c.get("cpu_pct") or c.get("mem_used")) else "")
        lvl = c.get("level") or ""
        risk = " ⚠ доступ к докеру/хосту" if lvl == "host" else (" · изоляция ослаблена" if lvl == "loose" else "")
        out.append(f"  {mark} {c.get('name', '?'):<20} {c.get('status') or c.get('state') or '?'}"
                   + (f"  · {eat}" if eat else "") + risk)
    return out


def _ports_lines(d: dict) -> list[str]:
    p = d.get("ports") or {}
    pub = p.get("public") or []
    out = [f"Порты наружу ({len(pub)}):"]
    if not pub:
        out.append("  (ни одного — всё слушает только loopback)")
    for row in pub:
        who = row.get("comm") or "?"
        tgt = f" → {row['target']}" if row.get("target") else ""
        out.append(f"  {row.get('port')}/{row.get('proto')} · {who}{tgt}")
    if p.get("local_only"):
        out.append(f"  + {p['local_only']} сокет(ов) только на loopback (наружу не торчат)")
    return out


def describe(section: str = "overview") -> str:
    """Текст для её тула. Никогда не бросает: нет глаз — честно объясняет, почему."""
    section = (section or "overview").strip().lower()
    if section not in SECTIONS:
        return f"Не знаю раздел «{section}». Есть: {', '.join(SECTIONS)}."
    if not available():
        return ("Глаза закрыты: PRAXIS_AGENT_TOKEN не задан (это рычаг Егора в .env). "
                "Пока его нет, обсерватория меня не пускает — и правильно делает.")
    d = fetch()
    if d is None:
        return ("Глаза закрыты: обсерватория (praxis-serverapp) не ответила. "
                "Она могла упасть или перезапускаться — попробуй позже или посмотри read_log.")
    if d.get("error"):
        return f"Обсерватория ответила ошибкой: {str(d['error'])[:200]}"
    parts: list[str] = []
    if section in ("overview", "all"):
        parts += _overview_lines(d)
    if section in ("containers", "all"):
        parts += ([""] if parts else []) + _containers_lines(d)
    if section in ("ports", "all"):
        parts += ([""] if parts else []) + _ports_lines(d)
    return "\n".join(parts)


def state_line() -> str:
    """Строка для my_capabilities: открыты ли глаза и насколько далеко они видят.

    Про руки врать нельзя: перезапуск своих сервисов у неё есть (17.B, manage_service),
    а вот править сам хост — нет. Формулировка ровно по факту."""
    if not available():
        return "глаза на сервер: закрыты (нет PRAXIS_AGENT_TOKEN)"
    return ("глаза на сервер: открыты, только чтение (здоровье хоста, контейнеры, порты, "
            "логи своих сервисов); сам хост править не умею")
