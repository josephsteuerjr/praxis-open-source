"""
Догнать свёртку после миграции ключа: свести накопленное горячее в её сводки.

После сведения ключей у комнаты оказывается всё несвёрнутое, что копилось по веткам
поодиночке и никогда не доходило до порога. Свёртка штатно идёт фоном вне пути ответа,
но разгребать это по одному сворачиванию за ход — дни. Здесь то же самое, только сразу
и под присмотром.

Ничего своего не изобретает: зовёт её собственный `memory_life.compact_if_due` ровно
так, как зовёт раннер, — с границами эпизодов, а не с `force`.

ПРЕДОХРАНИТЕЛЬ. Если модель недоступна, компактор молча пишет механическую выжимку с
меткой degraded. На одном компакте это честная деградация, на сорока — подмена её
памяти мусором. Поэтому: живая проверка модели до старта и остановка на ПЕРВОМ же
degraded, с именем файла, чтобы его можно было убрать.

Запуск:  python compact_places.py [--limit 200] [--place <ключ>] [--dry-run]
"""

from __future__ import annotations

import argparse
import time

import llm
import memory_life as ml


def places() -> list[str]:
    return sorted(p.stem for p in ml.STATE_DIR.glob("*.json"))


def ping() -> bool:
    if not llm.configured("evaluator"):
        return False
    try:
        out = llm.chat("evaluator", system="Отвечай одним словом.",
                       messages=[{"role": "user", "content": "Скажи: живой"}], max_tokens=20)
        return bool((out.text or "").strip())
    except Exception:
        return False


def drain(place: str, *, limit: int) -> dict:
    folded_total = compacts = 0
    started = time.time()
    for _ in range(limit):
        out = ml.compact_if_due(place)
        folded = int(out.get("folded") or 0)
        if not folded:
            return {"place": place, "folded": folded_total, "compacts": compacts,
                    "stopped": (out.get("plan") or {}).get("reason") or "нечего сворачивать",
                    "hot": out.get("hot"), "sec": round(time.time() - started, 1)}
        if out.get("degraded"):
            return {"place": place, "folded": folded_total, "compacts": compacts,
                    "stopped": "DEGRADED", "degraded_id": out.get("compact_id"),
                    "hot": out.get("hot"), "sec": round(time.time() - started, 1)}
        folded_total += folded
        compacts += 1
        print(f"    {place}: +{folded} событий → {out.get('compact_id')} "
              f"(осталось горячих {out.get('hot')})", flush=True)
    return {"place": place, "folded": folded_total, "compacts": compacts,
            "stopped": "предел итераций", "sec": round(time.time() - started, 1)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Догнать свёртку после миграции")
    parser.add_argument("--limit", type=int, default=200, help="потолок свёрток на место")
    parser.add_argument("--place", default="", help="только одно место")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    targets = [args.place] if args.place else places()
    rows = []
    for place in targets:
        state = ml._load_state(place, rebuild=False)
        hot = len(state.get("hot") or [])
        plan = ml.plan_hot_fold(state.get("hot") or [])
        rows.append((place, hot, plan.get("due"), plan.get("reason")))
    print("места и их горячее:")
    for place, hot, due, reason in sorted(rows, key=lambda r: -r[1]):
        if hot:
            print(f"  {place}: горячих {hot}, свёртка {'нужна' if due else 'не нужна'} ({reason})")
    total_hot = sum(r[1] for r in rows if r[2])
    print(f"\nк сворачиванию событий: {total_hot}")

    if args.dry_run:
        print("(сухой прогон)")
        return 0

    print("\nпроверяю вспомогательную модель…", flush=True)
    if not ping():
        print("модель недоступна — компактор писал бы механические выжимки. Стоп.")
        return 2
    print("модель отвечает.\n")

    bad = 0
    for place, hot, due, _reason in sorted(rows, key=lambda r: -r[1]):
        if not due:
            continue
        print(f"  {place} (горячих {hot}):", flush=True)
        out = drain(place, limit=args.limit)
        print(f"  → свёрнуто {out['folded']} событий в {out['compacts']} блоков за "
              f"{out['sec']}с; остановка: {out['stopped']}", flush=True)
        if out["stopped"] == "DEGRADED":
            bad += 1
            print(f"  !! механическая выжимка {out.get('degraded_id')} — остановлено. "
                  f"Убрать файл и разобраться с моделью.")
            break
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
