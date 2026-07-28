"""
Миграция ключа разговора: одна беседа перестаёт быть несколькими.

Что делает
----------
1. Читает архивы комнат и записывает в реестр маршрутов ДВА факта, оба по свидетельству:
   форум ли комната (по служебным «создана тема» на пройденном диапазоне) и какие ветки
   на самом деле наши артефакты, а не места Telegram (по правилу «корень ветки лежит в
   ней самой»).
2. Пересобирает состояние ленты жизни на КЛЮЧ МЕСТА: горячее кольцо, курсор свёртки,
   дедуп. Состояние производное — оно всегда восстанавливается из событий и компактов.
3. Проверяет три инварианта и печатает контрольную сумму.
4. Отодвигает состояния, оставшиеся от расщеплённых ключей, в `_pre_places/`.

Чего НЕ делает
--------------
Не трогает ни одного события, ни одного компакта, ни одного архива комнаты. Ничего не
удаляет. События по-прежнему хранят ключ, под которым были сказаны, — это честная
запись «где», и переписывать её незачем: место считается на чтении.

Откат
-----
    python migrate_places.py --rollback
Удаляет файлы реестра маршрутов и возвращает состояния из `_pre_places/`. После этого
всё читается ровно как до миграции: предикат места без реестра вырождается в равенство
ключей.

Инварианты
----------
    1. Ни одно событие не пропало и не задвоилось: горячее ∪ покрытое = все события места,
       пересечение пусто.
    2. Ни один канонический компакт не перестал быть каноническим.
    3. Ни один блок сводки, который она видела по старому ключу, не исчез из сводки места.

Запуск:  python migrate_places.py [--base /app] [--dry-run] [--rollback]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path


def _modules(base: Path):
    """Импорт после установки PRAXIS_BASE: каталоги считаются на импорте."""
    os.environ["PRAXIS_BASE"] = str(base)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import group_context
    import memory_life
    import telegram_routes
    return group_context, memory_life, telegram_routes


# --------------------------------------------------------------------------- #
#  1. свидетельства из архивов
# --------------------------------------------------------------------------- #
def seed_registry(group_context, telegram_routes, *, dry_run: bool) -> list[dict]:
    """Записать в реестр то, что уже видно в архивах. Идемпотентно."""
    rows = []
    root = group_context.GROUPS_DIR
    peers = []
    if root.exists():
        for item in sorted(root.iterdir()):
            if item.is_dir():
                peer = str(item.name).rsplit("-", 1)[0]
                if peer and peer not in peers:
                    peers.append(peer)
    for peer in peers:
        ids, openers, branches = [], 0, set()
        for record in group_context.iter_records(peer):
            kind = record.get("kind")
            if kind == "topic":
                openers += 1
            elif kind == "message":
                if record.get("message_id"):
                    ids.append(int(record["message_id"]))
                if record.get("topic_id") is not None:
                    branches.add(int(record["topic_id"]))
        if not ids:
            continue
        mapping = group_context.branch_containers(peer)
        row = {"peer": peer, "messages": len(ids), "branches": len(branches),
               "openers": openers, "artifacts": len(mapping),
               "verdict": "форум" if openers else "обычная комната"}
        if not dry_run:
            telegram_routes.observe(
                peer,
                kind=("topic_opener_seen" if openers else "no_topic_openers_in_range"),
                since_message_id=min(ids), until_message_id=max(ids),
                detail=f"миграция: пройдено {len(ids)}, openers {openers}")
            if mapping:
                telegram_routes.observe_branches(peer, mapping)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
#  2-3. пересборка и контрольная сумма
# --------------------------------------------------------------------------- #
def _summary_blocks(text: str) -> set[str]:
    return {block.strip() for block in str(text or "").split("\n\n")
            if block.strip().startswith("[compact")}


def snapshot_states(memory_life) -> int:
    """Копия КАЖДОГО состояния до пересборки. Без неё откат — не откат.

    ⚠ `retire_split_states` отодвигает только осиротевшие ключи. А состояние ключа,
    который САМ является местом (у обычной комнаты это её корневой ключ), миграция
    перезаписывает сведённым кольцом — и его не сохранял никто. Проверено на копии
    живой памяти: 80 горячих событий до, 2760 после, 2760 же после «отката». Реестра
    при этом уже нет, значит корневой ключ несёт события 173 чужих веток, и следующая
    свёртка родила бы компакт, который не проходит собственную проверку привязки.
    """
    attic = memory_life.STATE_DIR / "_pre_places"
    if attic.exists():
        # ⚠ Второй прогон не дополняет снимок. Иначе в него попали бы файлы, которые
        # СОЗДАЛ первый прогон, и откат вернул бы не «как было», а промежуточное
        # состояние: и ветка, и место одновременно, то есть два курсора над одной
        # лентой (вторая адверсарка 25.07). Снимок делает только первый прогон — он и
        # есть «до».
        return 0
    attic.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sorted(memory_life.STATE_DIR.glob("*.json")):
        (attic / path.name).write_bytes(path.read_bytes())
        copied += 1
    return copied


def snapshot_before(memory_life) -> dict:
    out = {}
    for key in memory_life.state_keys():
        canonical, _legacy = memory_life._canonical_compact_graph(key)
        out[key] = {
            "compacts": set(canonical),
            "blocks": _summary_blocks(memory_life.context_summary(key, max_chars=400_000)),
        }
    return out


def bind_places(memory_life, before: dict) -> int:
    """Закрепить состав каждого места в неизменяемом журнале свёрток.

    Реестр маршрутов — знание, и оно может уточниться: ключ, отвечавший «эта комната»,
    после новой эпохи проваливается в `unknown`. Если бы достижимость исторических
    компактов зависела только от него, её сводка теряла бы блоки от каждого нового
    наблюдения. Журнал `places.json` пишется один раз и только растёт — поэтому состав
    места закрепляем здесь, а не оставляем на усмотрение будущих наблюдений.
    """
    members = collections.defaultdict(set)
    for key in before:
        members[memory_life.place_key(key)].add(key)
    root = memory_life.COMPACTS_DIR
    if root.exists():
        for item in sorted(root.iterdir()):
            if item.is_dir():
                members[memory_life.place_key(item.name)].add(item.name)
    return sum(memory_life.bind_place(place, keys) for place, keys in members.items())


def verify(memory_life, before: dict) -> tuple[list[str], dict]:
    """Пересобрать места и проверить инварианты. -> (нарушения, сводка)."""
    problems: list[str] = []
    members = collections.defaultdict(list)
    for key in before:
        members[memory_life.place_key(key)].append(key)

    totals = {}
    for place, keys in sorted(members.items()):
        state = memory_life.rebuild_state(place)
        events = memory_life.iter_events(chat_id=place, kinds={"conversation_message"})
        canonical, _legacy = memory_life._canonical_compact_graph(place)

        every = {str(item.get("id")) for item in events}
        hot = {str(item.get("id")) for item in state.get("hot") or []}
        covered = {str(event) for meta, _recap in canonical.values()
                   for event in meta.get("source_event_ids") or []}
        lost = every - hot - covered
        both = hot & covered
        if lost:
            problems.append(f"{place}: потеряно {len(lost)} событий")
        if both:
            problems.append(f"{place}: {len(both)} событий одновременно горячие и свёрнутые")

        gone = set().union(*[before[key]["compacts"] for key in keys]) - set(canonical)
        if gone:
            problems.append(f"{place}: перестали быть каноническими {sorted(gone)}")

        after_blocks = _summary_blocks(
            memory_life.context_summary(place, max_chars=400_000))
        for key in keys:
            missing = before[key]["blocks"] - after_blocks
            if missing:
                problems.append(f"{place}: из сводки {key} пропало блоков {len(missing)}")

        totals[place] = {"keys": len(keys), "events": len(every), "hot": len(hot),
                         "compacts": len(canonical)}
    return problems, totals


# --------------------------------------------------------------------------- #
#  откат
# --------------------------------------------------------------------------- #
def rollback(memory_life, telegram_routes) -> dict:
    """Вернуть группировку к тому, что было. Историю свёрток НЕ трогает — и не должна.

    Три действия: вернуть каждое состояние из снимка; убрать состояния, которых до
    миграции не существовало; удалить файлы реестра маршрутов.

    `memory/life/places.json` (журнал уже состоявшихся свёрток) остаётся на месте
    СОЗНАТЕЛЬНО. Откат отменяет наше знание о том, что считать одной комнатой, но не
    может отменить факт «эти события были свёрнуты вместе». Сотри журнал — и всякий
    компакт, написанный после миграции, перестанет быть каноническим, то есть её
    сводка молча потеряет блоки. Откат обязан быть безопаснее миграции, а не опаснее.
    """
    restored, removed, dropped = [], [], []
    attic = memory_life.STATE_DIR / "_pre_places"
    if attic.exists():
        keep = {path.name for path in attic.glob("*.json")}
        for path in sorted(memory_life.STATE_DIR.glob("*.json")):
            # Удаляем ТОЛЬКО состояния разговоров. `.state/life` — общий каталог: там же
            # лежит заявка на переплавку, которую ничем не восстановишь. Фильтр по
            # содержимому уже есть у `retire_split_states`; откат обязан спрашивать тот
            # же вопрос (вторая адверсарка 25.07).
            if path.name not in keep and memory_life.is_state_file(path):
                path.unlink()             # состояние, которого до миграции не было
                dropped.append(path.stem)
        for path in sorted(attic.glob("*.json")):
            path.replace(memory_life.STATE_DIR / path.name)
            restored.append(path.stem)
        try:
            attic.rmdir()
        except OSError:
            pass
    if telegram_routes.DIR.exists():
        for path in sorted(telegram_routes.DIR.glob("*.route.json")):
            path.unlink()
            removed.append(path.stem)
    return {"restored": restored, "registry_removed": removed, "dropped": dropped}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Миграция ключа разговора в места")
    parser.add_argument("--base", default=os.environ.get("PRAXIS_BASE") or ".")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не писать: только показать, что получится")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)

    base = Path(args.base).resolve()
    group_context, memory_life, telegram_routes = _modules(base)

    if args.rollback:
        out = rollback(memory_life, telegram_routes)
        print(f"откат: возвращено состояний {len(out['restored'])}, "
              f"убрано появившихся {len(out['dropped'])}, "
              f"удалено файлов реестра {len(out['registry_removed'])}")
        print("журнал свёрток memory/life/places.json оставлен намеренно: "
              "он держит уже написанные компакты каноническими")
        return 0

    print(f"=== база: {base} ===")
    before = snapshot_before(memory_life)
    print(f"ключей состояния до: {len(before)}; "
          f"канонических компактов: {len(set().union(*[v['compacts'] for v in before.values()]) if before else set())}")

    for row in seed_registry(group_context, telegram_routes, dry_run=args.dry_run):
        print(f"  {row['peer']}: {row['messages']} сообщений, {row['branches']} веток, "
              f"{row['verdict']}, наших псевдоветок {row['artifacts']}")

    if args.dry_run:
        print("\n(сухой прогон: реестр не записан, состояния не пересобраны)")
        return 0

    print(f"снимок состояний до пересборки: {snapshot_states(memory_life)} файлов")
    print(f"закреплено привязок ключ→место: {bind_places(memory_life, before)}")
    problems, totals = verify(memory_life, before)
    print(f"\nмест после сведения: {len(totals)}")
    for place, row in sorted(totals.items(), key=lambda kv: -kv[1]["events"])[:8]:
        print(f"  {place}: ключей {row['keys']}, событий {row['events']}, "
              f"горячих {row['hot']}, свёрнутых блоков {row['compacts']}")

    if problems:
        print("\nИНВАРИАНТЫ НАРУШЕНЫ — состояния оставлены на месте, откат не нужен:")
        for line in problems:
            print(f"  ! {line}")
        return 1

    retired = memory_life.retire_split_states()
    print(f"\nинварианты целы: событий не потеряно, компактов не потеряно, "
          f"блоков сводки не потеряно")
    print(f"состояний отодвинуто в _pre_places: {len(retired['moved'])}; "
          f"осталось живых: {len(retired['kept'])}")
    print("откат: python migrate_places.py --rollback")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
