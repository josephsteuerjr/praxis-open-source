"""Deterministic, grep-friendly navigation over Praxis's canonical memory.

``memory/INDEX.md`` is a short router.  Six bounded maps provide deeper navigation,
but remain generated views: canonical Markdown/JSONL stays where its writers put it
and the maps themselves are excluded from recall indexing.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Iterable

import memory_provenance
import rooms

MAP_NAMES = ("PEOPLE", "ROOMS", "PROJECTS", "THREADS", "RUNS", "COMPUTERS")


def _base() -> Path:
    return Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)


def _memory() -> Path:
    return _base() / "memory"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _title(path: Path) -> str:
    for line in _read(path).splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return path.stem


def _one_line(value: object, limit: int = 140) -> str:
    """Keep generated navigation data from becoming Markdown structure/instructions."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _derive_person_hook(path: Path) -> str:
    """Build a grep hook while keeping a dossier projection tied to its claim receipt."""
    title = _one_line(_title(path), 120)
    saw_noncurrent_claim = False
    for raw in _read(path).splitlines():
        line = raw.strip()
        if re.match(r"^[-*]\s+", line) and "~~устарело~~" not in line.casefold():
            source = re.search(r"\[source:(clm-[0-9a-f]{16})\]\s*$", line, re.I)
            if not source:
                continue
            claim_path = path.parent.parent / "life" / "claims" / f"{source.group(1)}.md"
            source_kind, meta = memory_provenance.claim_source(claim_path)
            if source_kind not in {
                memory_provenance.CLAIM_SUPPORTED_OBSERVED_KIND,
                memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND,
            } or not meta.get("_evidence_valid"):
                saw_noncurrent_claim = True
                continue
            value = re.sub(r"^[-*]\s+", "", line)
            value = re.sub(r"^\[(public|private)\]\s*", "", value, flags=re.I)
            value = re.sub(r"^\(s[123]\)\s*", "", value, flags=re.I)
            value = re.sub(r"\s*_\(.*?\)_\s*", " ", value).strip()
            if value and not value.startswith("_("):
                return _one_line(f"{title} — {value}")
    if saw_noncurrent_claim:
        return _one_line(f"{title} — claim not current; verify")
    return _one_line(f"{title} — legacy dossier; verify")


def _limit() -> int:
    try:
        return max(20, min(1000, int(os.getenv("PRAXIS_MEMORY_MAP_LIMIT", "240"))))
    except ValueError:
        return 240


def _router_people_limit() -> int:
    try:
        return max(4, min(40, int(os.getenv("PRAXIS_MEMORY_INDEX_PEOPLE", "12"))))
    except ValueError:
        return 12


def _bounded(rows: Iterable, limit: int | None = None) -> tuple[list, int]:
    values = list(rows)
    cap = _limit() if limit is None else limit
    return values[:cap], max(0, len(values) - cap)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _map_link(path: Path, memory: Path) -> str:
    return "../" + _rel(path, memory)


# `_field(path, key)` отсюда убран намеренно: именно он позволял карте самой решать,
# что значит поле в чужом файле, и так родилось «ушедшая комната названа активной».
# Смысл полей принадлежит владельцу формата — спрашивай его модуль (rooms.parse_profile).


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _map_header(title: str, purpose: str) -> list[str]:
    return [f"# {title}", "", f"> {purpose}",
            "> Generated navigation; canonical files linked below remain the source of truth.", ""]


def _omitted(lines: list[str], count: int) -> None:
    if count:
        lines.extend(["", f"- … ещё {count}; увеличь `PRAXIS_MEMORY_MAP_LIMIT` для полного локального обзора."])


def _people_records(people: list[Path], people_hook, people_hooks: dict[str, str] | None,
                    extra_people: list[tuple[str, str]] | None, memory: Path) -> list[dict]:
    records = []
    for path in people:
        slug = path.stem
        if people_hooks is not None and slug in people_hooks:
            hook = _one_line(people_hooks[slug])
        else:
            try:
                hook = _one_line(people_hook(slug, _read(path))) if people_hook else _derive_person_hook(path)
            except Exception:
                try:
                    hook = _derive_person_hook(path)
                except Exception as exc:
                    # Одно нечитаемое досье не имеет права обрушить пересборку: до этого
                    # исключение отсюда оставляло ВСЕ шесть карт в прежнем виде — молча.
                    hook = _one_line(f"{slug} — досье не прочиталось ({type(exc).__name__}); проверь файл")
        records.append({"slug": slug, "hook": hook, "path": _rel(path, memory), "exists": True})
    present = {row["slug"] for row in records}
    for slug, hook in extra_people or []:
        clean_slug = _one_line(slug, 120)
        if clean_slug and clean_slug not in present:
            records.append({"slug": clean_slug, "hook": _one_line(hook),
                            "path": f"people/{clean_slug}.md", "exists": False})
            present.add(clean_slug)
    return sorted(records, key=lambda row: row["slug"].casefold())


def _people_count(records: list[dict]) -> str:
    """Счётчик в роутере считал заведённые досье вместе с ещё не заведёнными."""
    existing = sum(1 for row in records if row.get("exists"))
    pending = len(records) - existing
    return f"{existing}" + (f"; ещё {pending} без досье" if pending else "")


def _render_people(memory: Path, records: list[dict]) -> Path:
    lines = _map_header("PEOPLE — люди", "Досье, роли и короткие hooks для входа в отношения.")
    lines.append("> Hooks are previews of linked canonical dossiers, not a separate truth layer.")
    lines.append("")
    selected, omitted = _bounded(records)
    # Досье, которого нет, ссылкой не притворяется: 27.07 в PEOPLE.md висел живой
    # с виду указатель на удалённый файл, и по нему было некуда пойти. Имя в скобках
    # остаётся (по нему ищут глазами и grep-ом) — исчезает только ложная ссылка.
    lines += [f"- [{row['slug']}](../{row['path']}) {row['hook']}".rstrip() if row["exists"]
              else f"- [{row['slug']}] — досье ещё не заведено (`{row['path']}`); {row['hook']}".rstrip()
              for row in selected]
    if not selected:
        lines.append("- Пока нет карточек.")
    _omitted(lines, omitted)
    path = memory / "maps" / "PEOPLE.md"
    _atomic(path, "\n".join(lines))
    return path


def _render_rooms(memory: Path) -> Path:
    """Состояние комнаты берётся из той же маски, которой живёт восприятие.

    До 27.07.2026 карта выводила состояние из полей `membership:`/`left_at:` самого
    профиля — а их не заполнял никто, факт ухода жил в `rooms_departed.json`. Живьём
    это выглядело как «[Комната -1003908850919] — active; mode=dead»: производный
    документ, который она читает, звал активным то, откуда она ушла.
    """
    paths = sorted((p for p in (memory / "rooms").glob("*.md") if p.is_file()
                    and not p.name.startswith("_")), key=lambda p: p.name.casefold()) \
        if (memory / "rooms").exists() else []
    frozen = rooms.frozen_ids(memory)
    rows: list[str] = []
    for path in paths:
        profile = rooms.parse_profile(_read(path))
        state = rooms.membership_state(path.stem, mem_dir=memory, header=profile["header"])
        details = [state["state"] + (f" — {state['why']}" if state["why"] else "")]
        details.append(f"mode={profile['mode']}")
        if not profile["structured"]:
            details.append("профиль без машинной шапки — режим по умолчанию")
        if state["state"] == "active" and profile["mode"] in ("normal", "observer") \
                and path.stem in frozen:
            # Старый флаг заморозки в профиль не пишется, но глушит комнату живьём.
            details.append("заморожена флагом frozen_chats.json — сюда её не пускают")
        until = _one_line(profile["mode_until"], 40)
        if until:
            details.append(f"mode_until={until}" + (
                " (срок вышел — режим уже вернулся к normal)"
                if rooms.mode_until_expired(profile["mode_until"]) else ""))
        if state["left_at"]:
            details.append(f"left_at={_one_line(state['left_at'], 40)}")
        if state["stale_mark"]:
            details.append(state["stale_mark"])
        rows.append(f"- [{_title(path)}]({_map_link(path, memory)}) — {'; '.join(details)}")
    # Доверенная комната без профиля существует, но раньше не попадала на карту вовсе.
    for chat_id in sorted(rooms.allowed_chats(memory) - {p.stem for p in paths}):
        safe = _one_line(chat_id, 60)
        rows.append(f"- Комната {safe} — active; профиля ещё нет (`memory/rooms/{safe}.md`)")
    lines = _map_header("ROOMS — комнаты", "Активные и архивные пространства без стирания прежней памяти.")
    lines.append("> Состояние — из `rooms_departed.json`, той же маски, которой живёт восприятие;"
                 " поля профиля тут только провенанс.")
    lines.append("> Список неполон по устройству: комната, у которой нет ни профиля, ни записи"
                 " в каталоге, сюда не попадает — реальное членство в Telegram шире.")
    lines.append("")
    selected, omitted = _bounded(rows)
    lines += selected
    if not selected:
        lines.append("- Пока нет профилей комнат.")
    _omitted(lines, omitted)
    path = memory / "maps" / "ROOMS.md"
    _atomic(path, "\n".join(lines))
    return path


def _render_projects(memory: Path, base: Path) -> Path:
    rows: list[tuple[str, str, str]] = []
    project_root = memory / "projects"
    if project_root.exists():
        for path in sorted(project_root.rglob("*.md"), key=lambda p: _rel(p, memory).casefold()):
            if path.is_file() and not path.name.startswith("_"):
                rows.append((_title(path), _map_link(path, memory), "memory"))
    workshop_root = base / "workspace" / "projects"
    if workshop_root.exists():
        for directory in sorted((p for p in workshop_root.iterdir() if p.is_dir()),
                                key=lambda p: p.name.casefold()):
            readme = next((p for p in (directory / "README.md", directory / "README") if p.is_file()), None)
            target = readme or directory
            link = "../../" + _rel(target, base)
            rows.append((directory.name, link, "workspace"))
    forge_root = memory / ".forge" / "tasks"
    if forge_root.exists():
        for task_file in sorted(forge_root.glob("*/task.json"), key=lambda p: p.parent.name.casefold()):
            task = _json(task_file)
            goal = str(task.get("goal") or task_file.parent.name)[:140]
            status = str(task.get("status") or "unknown")
            rows.append((goal, _map_link(task_file, memory), f"forge {status}"))
    # One canonical target should have one navigation entry.
    dedup = {(link, title): (title, link, kind) for title, link, kind in rows}
    ordered = sorted(dedup.values(), key=lambda row: (row[0].casefold(), row[1]))
    selected, omitted = _bounded(ordered)
    lines = _map_header("PROJECTS — проекты", "Проектная память, workshop и постоянные Forge-задачи.")
    lines += [f"- [{title}]({link}) — {kind}" for title, link, kind in selected]
    if not selected:
        lines.append("- Пока нет проектных карточек.")
    _omitted(lines, omitted)
    path = memory / "maps" / "PROJECTS.md"
    _atomic(path, "\n".join(lines))
    return path


def _render_threads(memory: Path) -> Path:
    rows: list[tuple[str, str, str]] = []
    people_root = memory / "people"
    for path in sorted(people_root.glob("*.md"), key=lambda p: p.name.casefold()) \
            if people_root.exists() else []:
        for raw in _read(path).splitlines():
            line = raw.strip()
            if re.match(r"^[-*]\s*\[(?: |~)\]", line):
                text = re.sub(r"^[-*]\s*\[(?: |~)\]\s*", "", line).strip()
                if text:
                    rows.append((path.stem, text[:300], _map_link(path, memory)))
    try:
        import desires
        for state in desires.DesireLedger(memory.parent).list(
                statuses=("latent", "active", "blocked")):
            statement = str(state.get("statement") or "").strip()
            next_move = str(state.get("next_move") or "").strip()
            if statement:
                text = statement + (f" — дальше: {next_move}" if next_move else "")
                rows.append(("Praxis", text[:300], "../desires/CURRENT.md"))
    except Exception as exc:
        # Порванный desires/events.jsonl раньше делал карту «открытых нитей нет» —
        # при живых желаниях. Пустая карта и нечитаемая карта должны выглядеть по-разному.
        rows.append(("Praxis", _one_line(
            f"мои желания сейчас не читаются ({type(exc).__name__}: {exc}) — "
            "это не значит, что их нет", 300), "../desires/CURRENT.md"))
    rows = sorted(dict.fromkeys(rows), key=lambda row: (row[0].casefold(), row[1].casefold()))
    selected, omitted = _bounded(rows)
    lines = _map_header("THREADS — открытые нити", "Проверяемые незакрытые обещания, вопросы и цели.")
    lines += [f"- **{owner}** — {text} ([source]({link}))" for owner, text, link in selected]
    if not selected:
        lines.append("- Открытых нитей в канонических карточках не найдено.")
    _omitted(lines, omitted)
    path = memory / "maps" / "THREADS.md"
    _atomic(path, "\n".join(lines))
    return path


def _render_runs(memory: Path) -> Path:
    root = memory / "runs"
    directories: set[Path] = set()
    if root.exists():
        directories.update(path.parent for path in root.rglob("manifest.json"))
        directories.update(path.parent for path in root.rglob("run.json"))
        directories.update(path.parent for path in root.rglob("events.jsonl"))
        directories.update(path.parent for path in root.rglob("RECAP.md"))
    rows = []
    for directory in sorted(directories, key=lambda p: _rel(p, memory).casefold()):
        data = _json(directory / "manifest.json") or _json(directory / "run.json")
        # Битый манифест раньше читался как «статус неизвестен» — неотличимо от прогона,
        # который его просто не пишет. Запись при этом не теряется: остаётся имя каталога.
        unreadable = not data and any((directory / name).is_file()
                                      for name in ("manifest.json", "run.json"))
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        run_id = str(data.get("id") or data.get("run_id") or context.get("run_id")
                     or directory.name)
        kind = str(data.get("kind") or context.get("kind") or "run")
        status = str(data.get("status") or ("манифест не читается" if unreadable else "unknown"))
        at = str(data.get("updated_at") or data.get("created_at") or "")
        if (directory / "RECAP.md").is_file():
            target = directory / "RECAP.md"
        elif (directory / "manifest.json").is_file():
            target = directory / "manifest.json"
        elif (directory / "run.json").is_file():
            target = directory / "run.json"
        else:
            target = directory / "events.jsonl"
        rows.append((run_id, kind, status, at, _map_link(target, memory)))
    rows.sort(key=lambda row: (row[3], row[0]), reverse=True)
    selected, omitted = _bounded(rows)
    lines = _map_header("RUNS — исполнения", "Durable runs, их статусы, RECAP и evidence.")
    lines += [f"- [{run_id}]({link}) — {kind}; {status}" + (f"; {at}" if at else "")
              for run_id, kind, status, at, link in selected]
    if not selected:
        lines.append("- Durable runs ещё не материализованы.")
    _omitted(lines, omitted)
    path = memory / "maps" / "RUNS.md"
    _atomic(path, "\n".join(lines))
    return path


def _render_computers(memory: Path) -> Path:
    root = memory / "computer"
    lines = _map_header("COMPUTERS — компьютеры", "Устройства, inventory и evidence локальных исполнений.")
    global_map = root / "MAP.md"
    if global_map.is_file():
        lines.append(f"- [Главная карта компьютеров]({_map_link(global_map, memory)})")
    rows: list[tuple[str, Path, str]] = []
    devices = root / "devices"
    if devices.exists():
        rows += [(_title(path), path, "device") for path in devices.glob("*.md") if path.is_file()]
    inventory = root / "inventory"
    if inventory.exists():
        rows += [(f"{directory.name} inventory", directory / "CURRENT.md", "inventory")
                 for directory in inventory.iterdir() if directory.is_dir()
                 and (directory / "CURRENT.md").is_file()]
    tasks = root / "tasks"
    if tasks.exists():
        rows += [(path.stem, path, "evidence") for path in tasks.glob("*.md") if path.is_file()]
    rows.sort(key=lambda row: (row[2], row[0].casefold()))
    selected, omitted = _bounded(rows)
    lines += [f"- [{title}]({_map_link(path, memory)}) — {kind}" for title, path, kind in selected]
    if not global_map.is_file() and not selected:
        lines.append("- Карта появится после первого inventory/evidence события.")
    _omitted(lines, omitted)
    path = memory / "maps" / "COMPUTERS.md"
    _atomic(path, "\n".join(lines))
    return path


def rebuild(people_hook: Callable[[str, str], str] | None = None, *,
            memory_dir: Path | None = None, people_dir: Path | None = None,
            index_path: Path | None = None, extra_people: list[tuple[str, str]] | None = None,
            people_hooks: dict[str, str] | None = None) -> Path:
    """Rewrite the bounded navigation layer without changing canonical memory."""
    memory = Path(memory_dir) if memory_dir is not None else _memory()
    base = memory.parent
    people_root = Path(people_dir) if people_dir is not None else memory / "people"
    people = sorted((p for p in people_root.glob("*.md") if p.is_file()
                     and not p.name.startswith("_")), key=lambda p: p.name.casefold()) \
        if people_root.exists() else []
    records = _people_records(people, people_hook, people_hooks, extra_people, memory)

    renderers = {
        "PEOPLE": lambda: _render_people(memory, records),
        "ROOMS": lambda: _render_rooms(memory),
        "PROJECTS": lambda: _render_projects(memory, base),
        "THREADS": lambda: _render_threads(memory),
        "RUNS": lambda: _render_runs(memory),
        "COMPUTERS": lambda: _render_computers(memory),
    }
    for name in MAP_NAMES:
        renderers[name]()

    lines = [
        "# INDEX — карта памяти", "",
        "> Короткий маршрутизатор. Канон — linked Markdown/JSONL; карты и SQLite можно пересобрать.",
        "", "## Карты", "",
        f"- [PEOPLE](maps/PEOPLE.md) — люди и отношения ({_people_count(records)}).",
        f"- [ROOMS](maps/ROOMS.md) — комнаты, режимы и архив.",
        f"- [PROJECTS](maps/PROJECTS.md) — проекты и Forge-задачи.",
        f"- [THREADS](maps/THREADS.md) — открытые обещания, вопросы и цели.",
        f"- [RUNS](maps/RUNS.md) — durable исполнения, RECAP и evidence.",
        f"- [COMPUTERS](maps/COMPUTERS.md) — локальные машины, inventory и evidence.",
        "", "## Быстрые входы", "",
    ]
    entries = [
        ("home.md", "общий домашний слой"),
        ("desires/CURRENT.md", "живые желания, выборы и следующие ходы"),
        ("graph.md", "карта связей"),
        ("reflections.md", "долгие размышления"),
        ("computer/MAP.md", "детальная карта компьютеров"),
        ("access/TRUST.md", "владелец и явно выданный доступ"),
    ]
    for rel, label in entries:
        if (memory / rel).exists():
            lines.append(f"- [{rel}]({rel}) — {label}")

    # Compatibility and prompt orientation: a small bounded people preview remains in the
    # router; the complete bounded list lives in PEOPLE.md.
    lines += ["", "## Люди — быстрый вход", ""]
    preview = records[:_router_people_limit()]
    requested = {slug for slug, _ in (extra_people or [])}
    for row in records:
        if row["slug"] in requested and row not in preview:
            preview.append(row)
    # The always-in-prompt router is navigation only.  Factual hooks remain in
    # PEOPLE.md for explicit inspection and never acquire authority by repetition.
    lines += [f"- [{row['slug']}]({row['path']})" if row["exists"]
              else f"- [{row['slug']}] — досье ещё не заведено" for row in preview]
    if not preview:
        lines.append("- Пока нет карточек.")
    if len(records) > len(preview):
        lines.append(f"- … ещё {len(records) - len(preview)} в [PEOPLE](maps/PEOPLE.md).")

    lines += ["", "## Роль SQL", "",
              "- SQLite под `memory/.state/` — только пересобираемые индексы и operational state.",
              "- Каноническая память остаётся в Markdown и append-only JSONL: её можно читать, grep-ить и восстановить без БД."]
    path = Path(index_path) if index_path is not None else memory / "INDEX.md"
    _atomic(path, "\n".join(lines))
    return path


def refresh_recall() -> dict:
    """Rebuild maps, FTS and the optional embedding cache."""
    path = rebuild()
    try:
        import memory_index
        built = memory_index.build()
        fts = built.get("fts") or {}
        return {"ok": True, "path": str(path), "records": len(built.get("records") or []),
                "fts_chunks": int(fts.get("chunks") or 0),
                "fts_sources": int(fts.get("sources") or 0),
                "corrupt_lines": int(fts.get("corrupt_lines") or 0)}
    except Exception as exc:
        return {"ok": True, "path": str(path), "index_error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    print(json.dumps(refresh_recall(), ensure_ascii=False, indent=2))
