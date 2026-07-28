"""
services — её руки на СВОИХ сервисах (PASS 17.B, ступень B «хозяйки сервера»).

Ступень A дала глаза (hostview). Здесь появляется первое ДЕЙСТВИЕ на уровне машины —
и оно сделано так, чтобы новых привилегий у контейнера снова не появилось.

Механика — не docker.sock, а файл-сигнал (прецедент PASS 12.x, tool_restart_mailbot):
она пишет флаг, сам сервис его замечает на своём тике и выходит, `restart: unless-stopped`
поднимает его заново. Права остаются там, где были: она может попросить выйти только тех,
кто согласился слушать её сигнал. Чужой контейнер (relay) так не перезапустить — и это
честный отказ, а не обход.

Два диалекта сноса флага, потому что дома разные:
  * mailbot монтирует код rw — он сносит флаг сам (как и раньше);
  * serverapp монтирует `./:/app:ro` — снести не может, поэтому решение принимается
    по МЕТКЕ ВРЕМЕНИ: флаг новее старта процесса → выходим. После подъёма старт новее
    флага → тишина. Иначе был бы вечный цикл рестартов.

Рельсы: allowlist сервисов (реестр ниже), стоп-кран (.panic гасит и эти руки),
квота на юнит в час (анти-петля), расписка о каждой попытке. Модель здесь не зовётся.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("praxis-services")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
STATE_DIR = MEM_DIR / ".state"
RECEIPTS = STATE_DIR / "services.jsonl"
PANIC_SENTINEL = MEM_DIR / ".panic"       # тот же sentinel, что у agent.panic (путь, не импорт)

RESTARTS_PER_HOUR = 3                      # на юнит: больше — это петля, а не починка


class Service:
    """Один сервис: как его зовут, как он умирает, и умеет ли он вообще слушать её.

    kind='signal' — читает флаг и выходит сам (её код: mailbot, serverapp);
    kind='self'   — это она сама (у неё для этого restart_self, не эти руки);
    kind='foreign'— чужой процесс (relay): сигнала не слушает, руками её не трогается.
    """

    def __init__(self, name: str, kind: str, container: str, flag: Path | None = None,
                 what: str = ""):
        self.name, self.kind, self.container, self.flag, self.what = name, kind, container, flag, what


# Реестр — единственный источник правды о том, что ей позволено трогать и смотреть.
SERVICES: dict[str, Service] = {
    "mailbot": Service("mailbot", "signal", "praxis-mailbot",
                       MEM_DIR / ".restart_mailbot", "почта, мини-апп и карточки предложений"),
    "serverapp": Service("serverapp", "signal", "praxis-serverapp",
                         MEM_DIR / ".restart_serverapp", "обсерватория Атланта — мои глаза на сервер"),
    "praxis": Service("praxis", "self", "praxis", None, "я сама"),
    "relay": Service("relay", "foreign", "relay", None, "запасной мозг (чужой процесс)"),
}

# Чьи логи ей можно читать: свои сервисы + запасной мозг. Чужие проекты Егора
# (dasha, vroom, hardbot…) в этот список НЕ входят — их логи не её дело.
LOG_ALLOWED = ("praxis", "praxis-mailbot", "praxis-serverapp", "relay")


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def panicked() -> bool:
    """Стоп-кран гасит и руки к сервисам: остановка должна останавливать всё."""
    try:
        return PANIC_SENTINEL.exists()
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  Расписки и квота
# --------------------------------------------------------------------------- #

def receipt(op: str, unit: str, ok: bool, note: str = "") -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": int(time.time()), "at": _now(), "op": op, "unit": unit,
                           "ok": bool(ok), "note": note[:200]}, ensure_ascii=False)
        with RECEIPTS.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        log.warning("расписка services не записалась", exc_info=True)


def _recent_restarts(unit: str, window_s: int = 3600) -> int:
    """Сколько УСПЕШНЫХ рестартов этого юнита за окно (по расписке — она переживает рестарт)."""
    if not RECEIPTS.exists():
        return 0
    edge = time.time() - window_s
    n = 0
    try:
        for line in RECEIPTS.read_text(encoding="utf-8", errors="ignore").splitlines()[-500:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("op") == "restart" and r.get("unit") == unit and r.get("ok") \
                    and float(r.get("ts", 0)) >= edge:
                n += 1
    except OSError:
        return 0
    return n


# --------------------------------------------------------------------------- #
#  Действие: попросить сервис перезапуститься
# --------------------------------------------------------------------------- #

def request_restart(unit: str, reason: str = "") -> tuple[bool, str]:
    """Записать флаг-сигнал. -> (ок, что сказать ей). Никаких исключений наружу."""
    unit = (unit or "").strip().lower()
    svc = SERVICES.get(unit)
    if svc is None:
        known = ", ".join(s for s, v in SERVICES.items() if v.kind == "signal")
        return False, (f"Не знаю сервис «{unit}». Мои руки достают до: {known} "
                       f"(себя перезапускаю через restart_self).")
    if svc.kind == "self":
        return False, "Себя перезапускаю через restart_self — это другой рычаг, осознанный."
    if svc.kind == "foreign":
        return False, (f"{svc.name} — чужой процесс ({svc.what}); он мой файл-сигнал не читает. "
                       "Перезапустить его может только Егор с хоста. Могу посмотреть его логи "
                       "(server_logs) и сказать, что там.")
    if panicked():
        return False, "Стоп-кран поднят (.panic) — руками к сервисам не тянусь."
    used = _recent_restarts(unit)
    if used >= RESTARTS_PER_HOUR:
        receipt("restart", unit, False, f"квота {used}/{RESTARTS_PER_HOUR}")
        return False, (f"Уже перезапускала {unit} {used} раза за час — это петля, а не починка. "
                       "Разберись в причине (server_logs) или позови Егора.")
    try:
        svc.flag.parent.mkdir(parents=True, exist_ok=True)
        svc.flag.write_text(f"{_now()} — {(reason or 'без причины').strip()}", encoding="utf-8")
    except OSError as e:
        receipt("restart", unit, False, f"флаг не записался: {e}")
        return False, f"Не получилось попросить {unit} перезапуститься: флаг не записался."
    receipt("restart", unit, True, (reason or "")[:200])
    log.warning("RESTART SIGNAL %s: %s", unit, (reason or "без причины")[:120])
    return True, (f"Попросила {unit} перезапуститься ({svc.what}) — заметит в течение "
                  f"нескольких секунд и поднимется сам.")


# --------------------------------------------------------------------------- #
#  Сторона сервиса: «а меня просили выйти?»
# --------------------------------------------------------------------------- #

def restart_requested(unit: str, started_at: float) -> str:
    """Для сервиса, который НЕ МОЖЕТ снести флаг (код примонтирован ro).

    Просьба считается свежей, только если флаг новее старта процесса. После подъёма
    тот же флаг уже старше нового старта — и вечного цикла рестартов не выходит.
    -> причина (истина) или '' (не просили)."""
    svc = SERVICES.get((unit or "").strip().lower())
    if svc is None or svc.flag is None:
        return ""
    try:
        if not svc.flag.exists() or svc.flag.stat().st_mtime <= started_at:
            return ""
        return svc.flag.read_text(encoding="utf-8", errors="replace").strip() or "без причины"
    except OSError:
        return ""


def clear_flag(unit: str) -> None:
    """Для сервиса, который МОЖЕТ снести флаг (mailbot, rw-монтирование)."""
    svc = SERVICES.get((unit or "").strip().lower())
    if svc and svc.flag:
        try:
            svc.flag.unlink(missing_ok=True)
        except OSError:
            log.debug("флаг %s не снёсся", unit, exc_info=True)


def describe() -> str:
    """Что она может сказать о своих сервисах, не спрашивая никого."""
    rows = []
    for s in SERVICES.values():
        can = {"signal": "могу попросить перезапуститься", "self": "restart_self",
               "foreign": "перезапуск — только Егор с хоста"}[s.kind]
        used = _recent_restarts(s.name) if s.kind == "signal" else 0
        quota = f" (за час: {used}/{RESTARTS_PER_HOUR})" if s.kind == "signal" else ""
        rows.append(f"- {s.name} — {s.what}: {can}{quota}")
    return "Мои сервисы:\n" + "\n".join(rows) + \
           "\nЛоги читаю у: " + ", ".join(LOG_ALLOWED) + " (чужие проекты Егора — не моё дело)."
