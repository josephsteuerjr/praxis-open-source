#!/usr/bin/env python3
"""
hostagent — root-исполнитель правок хоста (PASS 17.C, вершина «хозяйки сервера»).

ЖИВЁТ ВНЕ ЕЁ РЕПО (`/opt/praxis-hostagent/`), запускается Егором на хосте. Её контейнер
этот файл не видит и не правит: рельсы здесь — не самоизменяемый питон, а зашитый пол.

Модель доверия — та же, что снизу вверх по всей лестнице: НИ ОДНОЙ правки хоста без двух
независимых гейтов.
  1. Апрув Егора. Он неформжируем её контейнером: применение происходит ТОЛЬКО когда Егор
     сам набирает `hostagent apply <id>` на хосте. mailbot (это тоже её код) не может это
     подделать — у контейнера нет хост-шелла. Она умеет лишь СТЕЙДЖИТЬ заявку.
  2. Рельсы агента. Даже одобренную правку агент кладёт, только если путь в allowlist,
     не секрет, не усечение; перед записью — бэкап, после — расписка ей в память.

Она стейджит заявку (`memory/.host_ops/<id>.json` + `<id>.content`) → карточка Егору
(mailbot, информационная) → Егор смотрит дифф и применяет/отклоняет командой на хосте.

Команды:
  hostagent list                — заявки, ждущие решения
  hostagent show <id>           — заявка целиком + дифф против текущего файла хоста
  hostagent apply <id>          — применить (бэкап → запись → опц. reload → расписка)
  hostagent reject <id> [почему]— отклонить (расписка ей)
  hostagent history [N]         — последние решения

Stdlib only. Ничего не слушает (не демон): нет входящей поверхности — только явные команды.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Пути. STAGE_DIR — её память (она туда пишет заявки). BACKUPS/пол — наши, root.
# --------------------------------------------------------------------------- #

PRAXIS_HOME = Path(os.environ.get("PRAXIS_HOME") or "/opt/praxis")
STAGE_DIR = PRAXIS_HOME / "memory" / ".host_ops"                # её reach: сюда она стейджит
RECEIPTS = PRAXIS_HOME / "memory" / ".state" / "hostops.jsonl"  # её reach: сюда она видит исход
AGENT_HOME = Path(os.environ.get("HOSTAGENT_HOME") or "/opt/praxis-hostagent")
BACKUPS = AGENT_HOME / "backups"                                # наши: снимок до каждой правки

# --------------------------------------------------------------------------- #
#  РЕЛЬСЫ — зашиты здесь, потому что этот файл вне её самоизменяемого дерева.
# --------------------------------------------------------------------------- #

# Что ей разрешено предлагать к правке. Узко и осознанно: каждый путь Егор добавляет
# сюда сам, руками, в этом файле на хосте. Пустой список = она не может ничего (fail-closed).
HOST_ALLOW: tuple[str, ...] = (
    "/etc/praxis-hostagent/test.conf",   # безобидная мишень для смоука/приёмки
    # "/etc/caddy/Caddyfile",            # реальная цель — раскомментировать осознанно
)

# Optional explicit owner policy for this legacy staged route.  Empty by default;
# project names never create authority on their own.
PROTECTED_ROOTS: tuple[str, ...] = tuple(
    os.path.normpath(item.strip())
    for item in (os.environ.get("PRAXIS_PROTECTED_ROOTS") or "").split(os.pathsep)
    if item.strip()
)

# После применения — опциональный reload по пути (перезагрузить сервис, читающий конфиг).
# Тоже allowlist: агент не запускает произвольное, только вшитую строку для конкретного пути.
POST_APPLY: dict[str, list[str]] = {
    # "/etc/caddy/Caddyfile": ["systemctl", "reload", "caddy"],
}

SHRINK_RATIO = 0.5          # новый текст < 50% старого → отказ без --force (анти-усечение)
MAX_CONTENT_BYTES = 256 * 1024


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _norm(path: str) -> str:
    """Лексическая нормализация без разыменования симлинков (предсказуемость решения)."""
    return os.path.normpath(str(path or "").strip())


def rails_check(path: str) -> str:
    """'' если путь можно править; иначе короткая причина отказа. Порядок: deny сильнее allow."""
    p = _norm(path)
    if not p or not os.path.isabs(p):
        return "путь должен быть абсолютным"
    if ".." in p.split(os.sep):
        return "путь с .. не принимаю"
    if any(p == root or p.startswith(root + os.sep) for root in PROTECTED_ROOTS):
        return "путь явно закрыт владельцем через PRAXIS_PROTECTED_ROOTS"
    if p not in HOST_ALLOW:
        return (f"{p} не в allowlist хоста. Разрешённое правит только Егор, дописав путь "
                "в HOST_ALLOW самого hostagent на хосте.")
    return ""


# --------------------------------------------------------------------------- #
#  Заявки и расписки
# --------------------------------------------------------------------------- #

def _receipt(op: str, oid: str, path: str, ok: bool, note: str = "") -> None:
    try:
        RECEIPTS.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": int(_dt.datetime.now().timestamp()), "at": _now(),
                           "op": op, "id": oid, "path": path, "ok": bool(ok),
                           "note": note[:300]}, ensure_ascii=False)
        with RECEIPTS.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as e:
        print(f"[warn] расписка не записалась: {e}", file=sys.stderr)


def _load_op(oid: str) -> dict | None:
    meta = STAGE_DIR / f"{oid}.json"
    if not meta.is_file():
        return None
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("id") != oid:
        return None
    return d


def _content_of(oid: str) -> str | None:
    cf = STAGE_DIR / f"{oid}.content"
    try:
        return cf.read_text(encoding="utf-8")
    except OSError:
        return None


def _pending() -> list[dict]:
    if not STAGE_DIR.is_dir():
        return []
    out = []
    for meta in sorted(STAGE_DIR.glob("*.json")):
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("status") == "pending":
            out.append(d)
    return out


def _close_op(oid: str) -> None:
    """Убрать заявку из очереди после решения (расписка уже в hostops.jsonl)."""
    for suffix in (".json", ".content"):
        try:
            (STAGE_DIR / f"{oid}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  Команды
# --------------------------------------------------------------------------- #

def cmd_list() -> int:
    ops = _pending()
    if not ops:
        print("Заявок на правку хоста нет.")
        return 0
    print(f"Заявки, ждущие твоего решения ({len(ops)}):\n")
    for d in ops:
        print(f"  {d['id']}  {d.get('path','?')}")
        print(f"     зачем: {(d.get('reason') or '—')[:100]}")
        print(f"     когда: {d.get('created','?')}\n")
    print("Посмотреть дифф:  hostagent show <id>")
    print("Применить:        hostagent apply <id>    Отклонить: hostagent reject <id> [почему]")
    return 0


def _diff(path: str, new: str) -> str:
    try:
        old = Path(path).read_text(encoding="utf-8") if Path(path).is_file() else ""
    except OSError:
        old = ""
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                      fromfile=f"{path} (сейчас)", tofile=f"{path} (заявка)",
                                      lineterm=""))
    return "\n".join(lines) if lines else "(файлы идентичны — правка ничего не меняет)"


def cmd_show(oid: str) -> int:
    d = _load_op(oid)
    content = _content_of(oid)
    if d is None or content is None:
        print(f"Нет заявки {oid}.", file=sys.stderr)
        return 1
    print(f"Заявка {oid}")
    print(f"  путь:    {d.get('path')}")
    print(f"  зачем:   {d.get('reason') or '—'}")
    print(f"  создана: {d.get('created')}")
    print(f"  рельсы:  {rails_check(d.get('path','')) or 'ок'}")
    print("\n--- дифф против текущего файла хоста ---")
    print(_diff(d.get("path", ""), content))
    return 0


def cmd_apply(oid: str, force: bool = False) -> int:
    d = _load_op(oid)
    content = _content_of(oid)
    if d is None or content is None:
        print(f"Нет заявки {oid}.", file=sys.stderr)
        return 1
    path = _norm(d.get("path", ""))
    reason = rails_check(path)
    if reason:
        print(f"ОТКАЗ рельсов: {reason}", file=sys.stderr)
        _receipt("apply", oid, path, False, f"рельсы: {reason}")
        _close_op(oid)
        return 2
    if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
        print("ОТКАЗ: контент больше лимита.", file=sys.stderr)
        _receipt("apply", oid, path, False, "контент > лимита")
        return 2
    target = Path(path)
    if target.is_file() and not force:
        old = target.read_text(encoding="utf-8", errors="replace")
        if old and len(content) < len(old) * SHRINK_RATIO:
            pct = round(len(content) / len(old) * 100)
            print(f"ОТКАЗ: новый текст {pct}% от старого — похоже на усечение. "
                  f"Осознанно — `hostagent apply {oid} --force`.", file=sys.stderr)
            _receipt("apply", oid, path, False, f"shrink-guard {pct}%")
            return 2
    # бэкап текущего состояния хоста
    backup = ""
    try:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            slug = re.sub(r"[^\w.-]+", "_", path).strip("_") or "file"  # портируемо: без / \ :
            backup = str(BACKUPS / f"{slug}.{int(_dt.datetime.now().timestamp())}.bak")
            shutil.copy2(target, backup)
    except OSError as e:
        print(f"ОТКАЗ: бэкап не сделался ({e}) — правку не применяю.", file=sys.stderr)
        _receipt("apply", oid, path, False, f"бэкап не сделался: {e}")
        return 2
    # запись
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"ОТКАЗ: запись не удалась: {e}", file=sys.stderr)
        _receipt("apply", oid, path, False, f"запись: {e}")
        return 2
    # опциональный reload
    reload_note = ""
    cmd = POST_APPLY.get(path)
    if cmd:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            reload_note = f"; reload rc={r.returncode}"
        except Exception as e:  # noqa: BLE001 — reload не должен рушить уже применённую правку
            reload_note = f"; reload упал: {e}"
    note = f"применено Егором; бэкап {backup or '(нового файла не было)'}{reload_note}"
    _receipt("apply", oid, path, True, note)
    _close_op(oid)
    print(f"Применено: {path}")
    if backup:
        print(f"Бэкап:     {backup}")
    if reload_note:
        print(f"Reload:   {reload_note.lstrip('; ')}")
    return 0


def cmd_reject(oid: str, why: str = "") -> int:
    d = _load_op(oid)
    if d is None:
        print(f"Нет заявки {oid}.", file=sys.stderr)
        return 1
    _receipt("reject", oid, d.get("path", ""), False, (why or "без причины")[:300])
    _close_op(oid)
    print(f"Отклонено: {oid}" + (f" — {why}" if why else ""))
    return 0


def cmd_history(n: int = 20) -> int:
    if not RECEIPTS.is_file():
        print("Истории пока нет.")
        return 0
    lines = RECEIPTS.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        mark = "✓" if r.get("ok") else "✗"
        print(f"  {mark} {r.get('at','?')} {r.get('op','?')} {r.get('path','?')} — {r.get('note','')}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        return cmd_list()
    if cmd == "show" and rest:
        return cmd_show(rest[0])
    if cmd == "apply" and rest:
        return cmd_apply(rest[0], force="--force" in rest)
    if cmd == "reject" and rest:
        return cmd_reject(rest[0], " ".join(a for a in rest[1:] if a != "--force"))
    if cmd == "history":
        return cmd_history(int(rest[0]) if rest and rest[0].isdigit() else 20)
    print(f"Не знаю команду «{cmd}». См.:\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
