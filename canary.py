"""
Канарейки: три дешёвых показателя, по которым видно, что перекладка ключа не сломала
ей жизнь.

Перекладка ключа разговора (пункт 5) меняет то, что она видит и что считает одним
местом. Такие изменения не падают с ошибкой — они тихо меняют поведение: пасс уходит
чаще, ход не закрывается, обращение теряется. Поэтому у каждого изменения должен быть
показатель, у показателя — база ДО изменения, и всё это — до того, как её включат.

Три канарейки — по одной на каждый способ сломаться незаметно:

  1. `stuck_runs` — ход, который начался и не кончился. Незакрытый ход не отдаёт ни
     результата, ни отказа: снаружи это просто тишина. База на 24.07: 1 незакрытый на
     1090 ходов (`in_doubt` от 22.07, smoke narrate).
  2. `dropped_addresses` — обращение, вытесненное другим обращением до прохода.
     Пробуждение на комнату одно; пока держится кулдаун, второе обращение
     перезаписывает первое. Само сообщение остаётся в контексте, но перестаёт быть
     обращением — вместе с ним уходят `speaker`, `kind` и `owner`.
  3. `pass_rate` — сколько проходов в час она делает по комнате. Склейка ключей на
     чтении даёт ей больше контекста; если она начнёт от этого просыпаться чаще, это
     видно здесь, и видно в сравнении с тем, как было ДО.

## База, снятая на живых данных 24.07 (до перекладки)

    комната                 ключей   проходов   рабочих часов   в час   пик
    AbstractDL              79       403        83              4.86    21
    Егор, ЛС                1        183        79              2.32    9
    Грибница                19       122        33              3.70    15

79 ключей на одну комнату — это и есть расщепление, ради которого делался пункт 5.
Цифры взяты из `memory/runs` и живут ровно столько, сколько живут манифесты; здесь они
записаны, чтобы «как было» пережило любую чистку.

Модуль только ЧИТАЕТ. Он не берёт локов ходов, ничего не пишет и ничего не решает —
у канарейки нет права голоса. Всё, на что он смотрит, она видит и сама: журнал
пропусков лежит в её `manage_perception`, манифесты ходов — в её же `memory/runs`.

Запуск:  python canary.py [--base /app] [--hours 24]
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import statistics
from pathlib import Path

import telegram_routes

TERMINAL = frozenset({"done", "cancelled", "failed"})


def _base(base=None) -> Path:
    return Path(base or os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)


def _iso_ts(value) -> float | None:
    """ISO-8601 из манифеста в epoch. Всё, что не разбирается, — None, а не 0."""
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        stamp = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return stamp.timestamp()


def _manifests(base=None):
    """Манифесты ходов как они лежат на диске.

    Читаем файл, а не через RunManager: менеджер на чтении берёт лок хода и
    доигрывает хвост после падения — то есть ПИШЕТ. Канарейка не имеет права менять
    то, что измеряет. Цена — мы видим манифест таким, каким он записан; для счёта
    незакрытых ходов этого ровно достаточно.
    """
    root = _base(base) / "memory" / "runs"
    if not root.exists():
        return
    for path in sorted(root.glob("*/*/manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield path, data


# --------------------------------------------------------------------------- #
#  1. незакрытые ходы
# --------------------------------------------------------------------------- #
def stuck_runs(base=None, *, older_than_min: float = 30.0, now: float | None = None) -> dict:
    """Ходы, не дошедшие до терминального статуса, и их возраст.

    `in_doubt` считается отдельно: это её честное «не знаю, дошло ли», законное
    состояние. Незаконно другое — когда таких накапливается.
    """
    moment = float(now if now is not None else _dt.datetime.now(_dt.timezone.utc).timestamp())
    total = 0
    rows: list[dict] = []
    for _path, manifest in _manifests(base):
        total += 1
        status = str(manifest.get("status") or "")
        if status in TERMINAL:
            continue
        context = manifest.get("context") or {}
        started = _iso_ts(manifest.get("updated_at")) or _iso_ts(manifest.get("created_at"))
        rows.append({
            "run_id": str(manifest.get("run_id") or (context.get("run_id") or "")),
            "status": status,
            "created_at": str(manifest.get("created_at") or ""),
            "updated_at": str(manifest.get("updated_at") or ""),
            "age_min": round((moment - started) / 60.0, 1) if started else None,
            "kind": str(context.get("kind") or ""),
            "chat": str(context.get("delivery_chat_id") or context.get("origin_chat_id") or ""),
            "goal": str(context.get("goal") or "")[:80],
        })
    rows.sort(key=lambda r: (r["age_min"] is None, -(r["age_min"] or 0.0)))
    stale = [r for r in rows if (r["age_min"] or 0.0) >= older_than_min]
    return {
        "total_runs": total,
        "nonterminal": len(rows),
        "stale": len(stale),
        "older_than_min": older_than_min,
        "by_status": _counts(r["status"] for r in rows),
        "rows": rows[:20],
    }


# --------------------------------------------------------------------------- #
#  2. вытесненные обращения
# --------------------------------------------------------------------------- #
def dropped_addresses(base=None, *, hours: float = 24.0, now: float | None = None) -> dict:
    """Обращения, вытесненные более новым обращением до прохода.

    Источник — её собственный журнал пропусков (кольцо на `SKIPS_KEEP` записей).
    Если кольцо короче запрошенного окна, честно возвращаем реальный охват в
    `span_hours`: «ноль за сутки» и «ноль за час, дальше не видно» — разные ответы.

    ⚠ Третий вид нуля: записи появляются только с того момента, как раннер начал их
    писать (`_note_address_overwritten`). Ноль за окно, которое старше этого момента,
    означает «не измерялось», а не «не случалось».
    """
    moment = float(now if now is not None else _dt.datetime.now(_dt.timezone.utc).timestamp())
    path = _base(base) / "memory" / ".state" / "perception_skips.jsonl"
    cut = moment - hours * 3600.0
    records: list[dict] = []
    oldest: float | None = None
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = float(rec.get("ts") or 0.0)
            if oldest is None or (ts and ts < oldest):
                oldest = ts
            if ts < cut:
                continue
            if str(rec.get("stage")) != "group_wake":
                continue
            if str((rec.get("meta") or {}).get("dropped")) != "addressed":
                continue
            records.append(rec)
    except OSError:
        pass
    drops = sum(1 + int(r.get("prev_n") or 0) for r in records)
    owner_lost = sum(1 + int(r.get("prev_n") or 0) for r in records
                     if (r.get("meta") or {}).get("owner_lost"))
    return {
        "window_hours": hours,
        "span_hours": round((moment - oldest) / 3600.0, 1) if oldest else 0.0,
        "drops": drops,
        "owner_lost": owner_lost,
        "by_room": _counts(telegram_routes.room_of(r.get("chat")) for r in records),
        "rows": [{"ts": r.get("ts"), "chat": r.get("chat"), "detail": r.get("detail")}
                 for r in records[-10:]],
    }


# --------------------------------------------------------------------------- #
#  3. темп проходов по комнате
# --------------------------------------------------------------------------- #
def _flip_at(base, peer: str) -> float | None:
    """Когда мы УЗНАЛИ, что комната — не форум. До этого момента слой B выключен.

    Читаем ту же ленту эпох, что и реестр, но своим путём: канарейке нужен `base`
    параметром (её гоняют по копии), а `telegram_routes.DIR` фиксируется на импорте.
    Согласие двух чтений закреплено тестом, а не обещанием.
    """
    path = (_base(base) / "memory" / ".state" / "group_context"
            / f"{telegram_routes._slug(peer)}.route.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for epoch in (data.get("epochs") or []):
        if str(epoch.get("forum_status")) == telegram_routes.FALSE:
            return _iso_ts(epoch.get("since"))
    return None


def _segment(stamps: list[float]) -> dict:
    """Интенсивность отрезка: проходов в тот час, когда она вообще работала.

    Делить на календарный промежуток нельзя — она спит, её останавливают, и любой
    простой развёл бы темп вниз до бессмысленности. Считаем по часам, в которых был
    хотя бы один проход: это и есть темп, когда она в разговоре.
    """
    if not stamps:
        return {"passes": 0, "active_hours": 0, "per_active_hour": 0.0, "busiest": 0,
                "median": 0.0, "first": "", "last": ""}
    buckets = _counts(int(ts // 3600) for ts in stamps)
    per_hour = sorted(buckets.values())
    return {
        "passes": len(stamps),
        "active_hours": len(buckets),
        "per_active_hour": round(len(stamps) / len(buckets), 2),
        "busiest": per_hour[-1],
        "median": round(statistics.median(per_hour), 2),
        "first": _stamp(min(stamps)),
        "last": _stamp(max(stamps)),
    }


def pass_rate(base=None, *, room: str | None = None) -> dict:
    """Проходы по комнатам: сколько, как часто и как это делится вокруг перекладки.

    Ключи одной комнаты (`<peer>` и `<peer>__topic__N`) сводятся в комнату — иначе
    сравнивать «до» и «после» не с чем: до перекладки её проходы были размазаны по
    десяткам ключей, и каждый ключ поодиночке выглядел бы редким.
    """
    rooms: dict[str, dict] = {}
    for _path, manifest in _manifests(base):
        context = manifest.get("context") or {}
        key = str(context.get("delivery_chat_id") or context.get("origin_chat_id") or "")
        if not key or key == "None":
            continue
        ts = _iso_ts(manifest.get("created_at")) or _iso_ts(context.get("created_at"))
        if ts is None:
            continue
        peer = telegram_routes.room_of(key)
        if room and peer != telegram_routes.room_of(room):
            continue
        slot = rooms.setdefault(peer, {"keys": set(), "stamps": []})
        slot["keys"].add(key)
        slot["stamps"].append(ts)

    out: dict[str, dict] = {}
    for peer, slot in rooms.items():
        flip = _flip_at(base, peer)
        stamps = sorted(slot["stamps"])
        before = [t for t in stamps if flip is None or t < flip]
        after = [t for t in stamps if flip is not None and t >= flip]
        row = {
            "keys": len(slot["keys"]),
            "flip_at": _stamp(flip) if flip else "",
            "known": telegram_routes.describe(peer) if flip else "",
            "before": _segment(before),
            "after": _segment(after),
        }
        base_rate = row["before"]["per_active_hour"]
        row["ratio"] = (round(row["after"]["per_active_hour"] / base_rate, 2)
                        if base_rate and after else None)
        out[peer] = row
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["before"]["passes"]))


# --------------------------------------------------------------------------- #
#  отчёт
# --------------------------------------------------------------------------- #
def _counts(values) -> dict:
    acc: dict = {}
    for value in values:
        acc[value] = acc.get(value, 0) + 1
    return dict(sorted(acc.items(), key=lambda kv: -kv[1]))


def _stamp(ts) -> str:
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime("%d.%m %H:%M")


def report(base=None, *, hours: float = 24.0, older_than_min: float = 30.0) -> str:
    lines: list[str] = []

    runs = stuck_runs(base, older_than_min=older_than_min)
    lines.append(f"1. Незакрытые ходы: {runs['nonterminal']} из {runs['total_runs']}"
                 f" (старше {older_than_min:g} мин: {runs['stale']})")
    if runs["by_status"]:
        lines.append("   по статусам: " + ", ".join(f"{k} {v}" for k, v in runs["by_status"].items()))
    for row in runs["rows"][:5]:
        age = f"{row['age_min']} мин" if row["age_min"] is not None else "возраст неизвестен"
        lines.append(f"   · {row['run_id'] or '?'} [{row['status']}] {age} — {row['goal']}")

    drops = dropped_addresses(base, hours=hours)
    lines.append(f"2. Вытесненные обращения за {hours:g} ч: {drops['drops']}"
                 f" (из них с потерей полномочий Егора: {drops['owner_lost']});"
                 f" журнал охватывает {drops['span_hours']} ч")
    for peer, n in list(drops["by_room"].items())[:5]:
        lines.append(f"   · {peer}: {n}")

    lines.append("3. Темп проходов по комнатам (проходов в час, когда она работала):")
    for peer, row in list(pass_rate(base).items())[:8]:
        head = (f"   · {peer}: ключей {row['keys']}, до перекладки "
                f"{row['before']['passes']} проходов за {row['before']['active_hours']} ч "
                f"= {row['before']['per_active_hour']}/ч (пик {row['before']['busiest']})")
        lines.append(head)
        if row["flip_at"]:
            lines.append(f"     перекладка {row['flip_at']} → после: "
                         f"{row['after']['passes']} за {row['after']['active_hours']} ч "
                         f"= {row['after']['per_active_hour']}/ч"
                         + (f", ×{row['ratio']} к базе" if row["ratio"] else ""))
        else:
            lines.append("     перекладки ещё не было — это чистая база")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Канарейки перекладки ключа")
    parser.add_argument("--base", default=None)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--stale-min", type=float, default=30.0)
    args = parser.parse_args()
    print(report(args.base, hours=args.hours, older_than_min=args.stale_min))
