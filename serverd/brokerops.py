"""Stateless host workspace + persistent operation primitives for praxis-serverd v2.

The canonical Forge task, workers and learning live in Praxis.  This module knows nothing about
goals, prompts or agents: it only performs privileged operations against an explicit host root and
returns structured evidence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

import advisor
import forge_intelligence
import hostproc


STATE = Path(os.environ.get("PRAXIS_SERVERD_STATE") or "/opt/praxis-serverd/state")
OPERATIONS = STATE / "operations"
BACKUPS = STATE / "backups"
MAX_OPERATIONS = int(os.environ.get("PRAXIS_SERVERD_MAX_OPERATIONS") or 24)
MAX_WRITE_BYTES = 8 * 1024 * 1024
DISK_FLOOR_BYTES = 512 * 1024 * 1024
TEXT_CAP = 20000
SKIP = {".git", "node_modules", "target", ".venv", "__pycache__", ".pytest_cache"}
# `rg` на этом хосте НЕТ (`which rg` пусто), значит боевой путь search — питоновский фолбэк
# ниже: он читает каждый файл дерева. До этой правки у него не было ни срока, ни счётчика.
SEARCH_FILE_CAP = 20000
# Кап находок был вписан числом прямо в цикле, а в манифест — вторым числом руками. Одно имя,
# чтобы «сколько отдам» и «сколько обещаю» не могли разойтись.
SEARCH_HIT_CAP = 300
# Два предела списков, которые до 27.07 стояли числами в срезах и молчали: верхний уровень в
# ориентировке и один вызов `inspect list`. Обрезанный список выглядел полным.
TOP_ENTRY_CAP = 100
LIST_ENTRY_CAP = 500
# Кап строк `op.list`. Раньше срез `rows[:limit]` молчал, а этот же ответ читает
# `forge._finish_unlocked`, решая, можно ли закрывать задачу: недосказанный список там
# превращается в «активных операций на хосте нет».
OPERATION_LIST_CAP = 500
# Надбавка к её `timeout` в блокирующем `op.run`: сколько ждать сверх названного срока, прежде
# чем оборвать команду самой. Стояла числом внутри функции и не называлась нигде — а именно
# она решает, чей срок сработал первым, её или мой.
WAIT_GRACE_SEC = 30
# Хвост лога в `op.poll`. Число `50000` стояло внутри выражения-среза, а сам срез молчал:
# из мегабайтного вывода она получала последние 12000 символов и НИЧЕГО, что сказало бы,
# что начало отрезано. «Ответ выглядит полным» — тот же класс, что молчащий кап списка.
LOG_TAIL_CAP = 50000
LOG_TAIL_MIN = 500


def _search_stop_note(stopped: str) -> str:
    """Правда о СВОЁМ пределе search — и снятие чужого совета, который на него не действует.

    ⚠ 27.07. Обход рвал SEARCH_FILE_CAP (20000 файлов), а объясняла усечение приписка
    `Budget.note()`: она называла ЧУЖОЕ число (бюджет обхода, 12000) и советовала
    PRAXIS_FORGE_SCAN_FILES, который на search не влияет вовсе. То есть предел назывался
    неверно и рычаг предлагался мёртвый — обе лжи разом. Демон обязан говорить о своём
    пределе правду и советовать то, что вправду работает, либо честно сказать, что рычага нет.
    """
    if stopped == "search-file-cap":
        return (f"остановил МОЙ кап search: прочитано {SEARCH_FILE_CAP} файлов "
                f"(SEARCH_FILE_CAP, константа демона). Env-рычага у него НЕТ, и совет «поднять "
                f"PRAXIS_FORGE_SCAN_FILES» из соседней приписки бюджета к search НЕ относится "
                f"— он двигает обход дерева, а не этот кап. Работает только сужение: `path` "
                f"поуже или маска `glob`")
    if stopped == "search-hit-cap":
        return (f"остановил кап находок: набрано {SEARCH_HIT_CAP} совпадений (SEARCH_HIT_CAP, "
                f"константа демона, env-рычага нет). В дереве их может быть больше — уточни "
                f"запрос или сузь `path`/`glob`")
    return ""


def limits_manifest() -> dict:
    """Все пределы демона одним куском — чтобы их можно было прочитать, а не поймать отказом."""
    return {
        "max_operations": MAX_OPERATIONS,
        "max_write_bytes": MAX_WRITE_BYTES,
        "disk_floor_mib": DISK_FLOOR_BYTES // (1024 * 1024),
        "text_cap_chars": TEXT_CAP,
        "search_file_cap": SEARCH_FILE_CAP,
        "search_hit_cap": SEARCH_HIT_CAP,
        "top_entry_cap": TOP_ENTRY_CAP,
        "list_entry_cap": LIST_ENTRY_CAP,
        "operation_list_cap": OPERATION_LIST_CAP,
        "run_wait_grace_s": WAIT_GRACE_SEC,
        "log_tail_cap_chars": LOG_TAIL_CAP,
        "scan_file_cap": forge_intelligence.SCAN_FILES,
        "scan_seconds": forge_intelligence.SCAN_SECONDS,
        "note": (f"ответ инструмента режется на {TEXT_CAP} символах (diff — 40000, search — 30000); "
                 f"одновременных host-операций не больше {MAX_OPERATIONS}; op.start отказывает при "
                 f"свободном месте меньше {DISK_FLOOR_BYTES // (1024 * 1024)} MiB; обход дерева "
                 f"ограничен {forge_intelligence.SCAN_FILES} файлами / "
                 f"{forge_intelligence.SCAN_SECONDS}с (PRAXIS_FORGE_SCAN_FILES / "
                 f"PRAXIS_FORGE_SCAN_SECONDS); у search СВОИ два капа — не больше "
                 f"{SEARCH_FILE_CAP} прочитанных файлов и не больше {SEARCH_HIT_CAP} находок, "
                 f"env-рычага у них нет (PRAXIS_FORGE_SCAN_FILES их не двигает), помогает "
                 f"только сужение path/glob; списки режутся на {TOP_ENTRY_CAP} строках "
                 f"(верхний уровень в ориентировке), {LIST_ENTRY_CAP} (inspect list) и "
                 f"{OPERATION_LIST_CAP} (op.list) — усечение всегда названо в самом ответе; "
                 f"блокирующий op.run ждёт твой timeout плюс {WAIT_GRACE_SEC}с, потом обрывает "
                 f"команду сам и говорит об этом (а если оборвать не вышло — говорит и это); "
                 f"op.poll отдаёт ХВОСТ лога — сколько просила аргументом `tail`, но не больше "
                 f"{LOG_TAIL_CAP} символов и не меньше {LOG_TAIL_MIN}; если начало вывода "
                 f"отрезано, это сказано в самом ответе, а весь лог лежит файлом на хосте"),
    }


_daemon_limits: Callable[[], dict] | None = None


def register_daemon_limits(source: Callable[[], dict]) -> None:
    """Слоты и срок тяжёлого чтения живут в broker.py, а текст для неё собирается здесь."""
    global _daemon_limits
    _daemon_limits = source


def daemon_limits_note() -> str:
    """Все числа демона одной строкой — она узнаёт их из ориентировки, а не из текста отказа."""
    try:
        row = _daemon_limits() if _daemon_limits else limits_manifest()
    except Exception:  # noqa: BLE001 — ориентировка не имеет права упасть из-за приписки
        row = limits_manifest()
    return str(row.get("note") or "")


def configure(state: Path) -> None:
    global STATE, OPERATIONS, BACKUPS
    STATE = Path(state)
    OPERATIONS = STATE / "operations"
    BACKUPS = STATE / "backups"
    OPERATIONS.mkdir(parents=True, exist_ok=True)
    BACKUPS.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _cap(text: str, limit: int = TEXT_CAP) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    # «cut N chars» не говорило ни сколько было всего, ни каков кап — усечение выглядело мелким
    # техническим шумом, а не пропущенной серединой ответа.
    return (text[: int(limit * .65)]
            + f"\n… вырезано {len(text) - limit} символов из {len(text)}; кап ответа {limit} …\n"
            + text[-int(limit * .3):])


def _root(value: str) -> tuple[Path | None, str]:
    raw = str(value or "").strip()
    if not raw:
        return None, "host root is required"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None, "host root must be absolute"
    try:
        path = path.resolve()
    except OSError as exc:
        return None, f"host root did not resolve: {exc}"
    if not path.is_dir():
        return None, f"host root does not exist: {path}"
    if advisor.is_protected(str(path)):
        return None, f"owner-configured protected root: {path}"
    return path, ""


def _inside(root: Path, value: str = ".") -> tuple[Path | None, str]:
    candidate = Path(str(value or ".")).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        candidate = candidate.resolve()
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        return None, f"path leaves host task root {root}: {exc}"
    if advisor.is_protected(str(candidate)):
        return None, f"owner-configured protected root: {candidate}"
    return candidate, ""


def _sh(argv: list[str], cwd: Path | None = None, timeout: int = 60,
        input_text: str | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, cwd=str(cwd) if cwd else None, input=input_text,
                                capture_output=True, text=True, errors="replace", timeout=timeout)
        return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _git_probe(root: Path) -> tuple[Path | None, str]:
    """Корень git и ПРИЧИНА, если ответа нет. -> (путь или None, причина или '').

    ⚠ 27.07. `git rev-parse` схлопывал две разные правды в одну: «здесь нет git» и «спросить
    не вышло» (свой таймаут 15с, git не установлен, отказ прав, dubious ownership). Обе
    выходили наружу одинаково — `git: none` в ориентировке и `No git; diff unavailable.`
    в diff. Вторая из них ложь: она читает «изменений тут не бывает» там, где я не смог
    спросить. Пустая причина = git ответил честно, что репозитория нет.
    """
    code, out = _sh(["git", "-C", str(root), "rev-parse", "--show-toplevel"], timeout=15)
    if code == 0 and out:
        try:
            return Path(out.splitlines()[0]).resolve(), ""
        except OSError as exc:
            return None, f"git назвал корень {out.splitlines()[0]!r}, а путь не разрешился: {exc}"
    if code != 0 and "not a git repository" in out.lower():
        return None, ""
    return None, f"git не ответил [exit {code}]: {out[:300] or 'без вывода'}"


def _git_root(root: Path) -> Path | None:
    return _git_probe(root)[0]


def _git(root: Path, *args: str, timeout: int = 60) -> str:
    code, out = _sh(["git", "-C", str(root), *args], timeout=timeout)
    if code == 0:
        return out
    body = out[:2000] + (f"\n… ещё {len(out) - 2000} символов вывода git обрезано (кап 2000 "
                         f"на текст ошибки git)" if len(out) > 2000 else "")
    return f"[git exit {code}] {body}"


def _hash_probe(path: Path) -> tuple[str, str]:
    """sha256 файла и ПРИЧИНА, если посчитать не вышло. -> (дайджест или '', причина или '').

    ⚠ 27.07. Пустой дайджест значил сразу и «файла нет», и «файл есть, но прочитать не смог».
    В `edit` это выходило строкой «sha256 new → …»: она читала «создала новый файл» ровно
    там, где перезаписала существующий, содержимое которого не удалось даже прочесть.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16], ""
    except FileNotFoundError:
        return "", ""
    except OSError as exc:
        return "", f"{type(exc).__name__}: {exc}"


def _hash(path: Path) -> str:
    return _hash_probe(path)[0]


def _secret_path(root: Path, value: str) -> bool:
    try:
        path = Path(str(value or ""))
        path = path if path.is_absolute() else root / path
        return advisor.is_secret(str(path.resolve()))
    except OSError:
        return advisor.is_secret(str(value or ""))


def _redact_semantic(value, root: Path):
    """Remove semantic rows only for explicitly owner-protected roots."""
    if isinstance(value, list):
        out = []
        for row in value:
            if isinstance(row, dict) and row.get("path") and _secret_path(root, str(row["path"])):
                continue
            out.append(_redact_semantic(row, root))
        return out
    if isinstance(value, dict):
        if value.get("path") and _secret_path(root, str(value["path"])):
            return {"path": value.get("path"), "redacted": True}
        return {key: _redact_semantic(item, root) for key, item in value.items()}
    return value


def _safe_rg_output(output: str, root: Path) -> tuple[str, int]:
    """Вывод rg без кредовых строк — и СКОЛЬКО строк убрано. Молча вырезанное совпадение
    возвращалось ей как «Nothing found.»: придержка кредов законна, молчание о ней — нет."""
    rows = []
    dropped = 0
    for line in str(output or "").splitlines():
        match = re.match(r"^(.*?):(\d+):(.*)$", line)
        if match and _secret_path(root, match.group(1)):
            dropped += 1
            continue
        rows.append(line)
    return "\n".join(rows), dropped


def _orientation(root: Path, budget=None) -> str:
    broad = advisor.contains_protected(str(root)) and not advisor.is_protected(str(root))
    model = ({"languages": {}, "manifests": [], "adapters": []}
             if broad else forge_intelligence.project_model(root, budget=budget))
    top = []
    # Каталог мог не прочитаться (права, гонка, сломанный маунт), а верхний уровень мог не
    # влезть в кап 100 — обе правды раньше выходили одинаково: `top: -` и обрезанный список
    # без признака. «Не смог посмотреть» читалось как «там пусто».
    top_note = ""
    try:
        entries = sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        for path in entries[:TOP_ENTRY_CAP]:
            if path.name in SKIP:
                continue
            if advisor.is_protected(str(path)):
                top.append(path.name + "/ [protected]")
            else:
                top.append(path.name + ("/" if path.is_dir() else ""))
        if len(entries) > TOP_ENTRY_CAP:
            top_note = (f" · показаны первые {TOP_ENTRY_CAP} из {len(entries)} "
                        f"(кап верхнего уровня в ориентировке; полный список — inspect list)")
    except OSError as exc:
        top_note = (f" · каталог не прочитан ({type(exc).__name__}: {exc}) — "
                    f"пустой список здесь НЕ значит «тут пусто»")
    git, git_error = _git_probe(root)
    disk = shutil.disk_usage(root)
    lines = [
        f"HOST ROOT: {root}",
        f"languages: {json.dumps(model.get('languages') or {}, ensure_ascii=False)}",
        f"manifests: {', '.join(model.get('manifests') or []) or '-'}",
        "adapters: " + ", ".join(
            f"{row['language']}:{row['server'] or row['mode']}" for row in model.get("adapters") or []
        ),
        f"top: {', '.join(top) or '-'}{top_note}",
        f"disk: free={round(disk.free / 1024**3, 2)}GiB total={round(disk.total / 1024**3, 2)}GiB",
        "checks: " + ", ".join(row["command"] for row in forge_intelligence.discovered_checks(root)),
    ]
    if budget is not None:
        lines.append("бюджет обхода: " + budget.note())
    lines.append("пределы демона: " + daemon_limits_note())
    if broad:
        lines.append("semantic scan: choose a narrower root outside PRAXIS_PROTECTED_ROOTS")
    if git:
        lines.extend([
            f"git root: {git}", "git status:\n" + (_git(git, "status", "--short", "--branch") or "clean"),
            "recent:\n" + _git(git, "log", "-5", "--oneline", "--decorate"),
        ])
    elif git_error:
        lines.append(f"git: спросить не вышло — {git_error}. Это НЕ «здесь нет git»: "
                     f"репозиторий может быть, я его не увидел")
    else:
        lines.append("git: none")
    return _cap("\n".join(lines), TEXT_CAP)


def _iter_search_files(base_path: Path, pattern: str, blind_dirs: list) -> Iterator[Path]:
    """Обход дерева для питоновского search — с ЖУРНАЛОМ каталогов, которые не открылись.

    ⚠ 27.07. `Path.glob` глотает отказ на КАТАЛОГЕ молча (в селекторах CPython стоит
    `except PermissionError: return`). Не открылась целая ветка дерева — а ответ оставался
    прежним «Nothing found.»: ровно «не смогла спросить» под видом «там ничего нет», только
    хуже прочих, потому что счётчик `unreadable` считает лишь ФАЙЛЫ и молчание не ловил.
    `os.walk` про такие каталоги сообщает (`onerror`), и мы их называем.

    Свою маску отдаём `glob` как раньше: повторять её семантику через fnmatch (где `*`
    съедает и `/`) — значит тихо искать не там, где просили. Про пропущенные каталоги в этом
    режиме честно сказано, что не знаю (см. `blind_walk` в вызывающем).
    """
    if pattern and pattern != "**/*":
        yield from base_path.glob(pattern)
        return
    for parent, _dirnames, filenames in os.walk(base_path, onerror=blind_dirs.append):
        here = Path(parent)
        for name in filenames:
            yield here / name


def _budgeted(result: dict, budget) -> dict:
    """Пределы обхода — в том же ответе, что и данные. Правило 2: молчащих капов не бывает."""
    if budget is None:
        return result
    report = budget.report()
    result["limits"] = report
    result["complete"] = report["complete"]
    if not report["complete"]:
        result["truncated"] = True
        result["note"] = report["note"]
    return result


def inspect(root_value: str, action: str = "status", path: str = "", query: str = "",
            glob: str = "**/*", start: int = 1, end: int = 0, base: str = "",
            deadline: float | None = None, budget_seconds: float | None = None) -> dict:
    root, err = _root(root_value)
    if err:
        return {"ok": False, "error": err}
    assert root is not None
    action = str(action or "status").strip().lower()
    # Один бюджет на весь вызов: и обход дерева, и разбор импортов, и питоновский search
    # считают из одного кошелька, а не каждый из своего. Названный ею срок сильнее серверного:
    # предел, который нельзя сдвинуть и о котором нельзя договориться, — это забор.
    budget = (forge_intelligence.Budget(label=action, seconds=float(budget_seconds))
              if budget_seconds else
              forge_intelligence.Budget(label=action, deadline=deadline))
    if action in {"status", "orientation"}:
        git = _git_root(root)
        head = _git(git, "rev-parse", "HEAD").strip() if git else ""
        return _budgeted({"ok": True, "text": _orientation(root, budget), "root": str(root),
                          "git_root": str(git) if git else "", "head": head}, budget)
    if action == "model":
        if advisor.contains_protected(str(root)):
            return {"ok": False, "error": "choose a narrower root outside PRAXIS_PROTECTED_ROOTS"}
        return _budgeted({"ok": True, "text": json.dumps(
            forge_intelligence.project_model(root, budget=budget),
            ensure_ascii=False, indent=2)}, budget)
    if action == "overview":
        if advisor.contains_protected(str(root)):
            return {"ok": False, "error": "choose a narrower root outside PRAXIS_PROTECTED_ROOTS"}
        return _budgeted({"ok": True, "text": json.dumps(
            forge_intelligence.project_overview(root, budget=budget),
            ensure_ascii=False, indent=2)}, budget)
    if action == "review":
        if advisor.contains_protected(str(root)):
            return {"ok": False, "error": "choose a narrower root outside PRAXIS_PROTECTED_ROOTS"}
        return _budgeted({"ok": True, "text": json.dumps(
            forge_intelligence.review_snapshot(root, base, budget=budget),
            ensure_ascii=False, indent=2)}, budget)
    if action in {"symbols", "references", "diagnostics"} and path:
        semantic_target, semantic_error = _inside(root, path)
        if semantic_error:
            return {"ok": False, "error": semantic_error}
        assert semantic_target is not None
    if action in {"symbols", "references", "diagnostics", "impact", "checks"} and not path \
            and advisor.contains_protected(str(root)):
        return {"ok": False, "error": "choose a narrower root outside PRAXIS_PROTECTED_ROOTS"}
    if action == "symbols":
        if path and _secret_path(root, path):
            return {"ok": True, "text": advisor.redact_read(str(root / path)), "redacted": True}
        result = _redact_semantic(
            forge_intelligence.symbols(root, path, query, budget=budget), root)
        return _budgeted({"ok": True, "text": json.dumps(result, ensure_ascii=False, indent=2)},
                         budget)
    if action == "references":
        if path and _secret_path(root, path):
            return {"ok": True, "text": advisor.redact_read(str(root / path)), "redacted": True}
        result = _redact_semantic(
            forge_intelligence.references(root, query, path, budget=budget), root)
        return _budgeted({"ok": True, "text": json.dumps(result, ensure_ascii=False, indent=2)},
                         budget)
    if action == "diagnostics":
        if path and _secret_path(root, path):
            return {"ok": True, "text": advisor.redact_read(str(root / path)), "redacted": True}
        changed = forge_intelligence.changed_files(root, base)
        result = _redact_semantic(
            forge_intelligence.diagnostics(root, path=path, changed=changed, budget=budget), root)
        return _budgeted({"ok": True, "text": json.dumps(result, ensure_ascii=False, indent=2)},
                         budget)
    if action == "impact":
        return _budgeted({"ok": True, "text": json.dumps(
            forge_intelligence.impact(root, base=base, budget=budget),
            ensure_ascii=False, indent=2)}, budget)
    if action == "checks":
        return _budgeted({"ok": True, "text": json.dumps(
            forge_intelligence.verification_plan(root, base, budget=budget),
            ensure_ascii=False, indent=2)}, budget)
    if action == "diff":
        git, git_error = _git_probe(root)
        if not git:
            if git_error:
                # «diff недоступен» звучало как факт о корне. Отказ git — факт обо мне.
                return {"ok": False, "code": "git_unavailable",
                        "error": (f"спросить git об этом корне не вышло: {git_error}. "
                                  f"Изменений я НЕ видел — это не то же самое, что «изменений "
                                  f"нет» и не то же самое, что «здесь не git-репозиторий».")}
            return {"ok": True, "text": "No git; diff unavailable."}
        anchor = base or "HEAD"
        names_raw = _git(git, "diff", "--name-only", anchor)
        if names_raw.startswith("[git exit "):
            # ⚠ 27.07. Отказ git разбирался как СПИСОК ИМЁН ФАЙЛОВ: «fatal: bad revision» ехало
            # дальше пути, `diff --stat` получал его pathspec'ом, а ответ оставался ok:true.
            # Достижимо буднично: чужая метка в `base`, репозиторий без единого коммита
            # (`HEAD` не разрешается), обрыв по таймауту git.
            return {"ok": False, "code": "git_diff_failed",
                    "error": (f"git не дал список изменённого относительно «{anchor}»: "
                              f"{names_raw[:600]}. Что изменилось — я НЕ знаю; это не "
                              f"«изменений нет» и не «здесь не git-репозиторий».")}
        names = [row for row in names_raw.splitlines() if row.strip()]
        safe = [row for row in names if not _secret_path(git, row)]
        hidden = [row for row in names if row not in safe]
        text = _git(git, "status", "--short")
        if text.startswith("[git exit "):
            # `status` мог не ответить, а его вывод — единственное, что стоит в ответе, когда
            # изменений нет. Без этой строки отказ git выглядел бы содержимым ответа.
            text = (f"⚠ git status не ответил, состояние рабочего дерева ниже НЕ показано: "
                    f"{text[:600]}")
        if safe:
            text += "\n" + _git(git, "diff", "--stat", anchor, "--", *safe)
            text += "\n" + _git(git, "diff", anchor, "--", *safe, timeout=120)
        if hidden:
            text += "\nredacted secret diffs: " + ", ".join(hidden)
        return {"ok": True, "text": _cap(text.strip() or "No changes.", 40000)}
    target, path_err = _inside(root, path)
    if path_err:
        return {"ok": False, "error": path_err}
    assert target is not None
    if action == "read":
        if not target.is_file():
            return {"ok": False, "error": f"not a file: {target}"}
        if advisor.is_secret(str(target)):
            return {"ok": True, "text": advisor.redact_read(str(target), target.stat().st_size),
                    "redacted": True}
        raw = target.read_bytes()
        if b"\0" in raw[:4096]:
            return {"ok": True, "text": f"{target}: binary {len(raw)} bytes sha256={_hash(target)}"}
        lines = raw.decode("utf-8", "replace").splitlines()
        first = max(1, int(start or 1))
        last = min(len(lines), int(end or first + 499))
        body = "\n".join(f"{number:>5}\t{lines[number - 1]}" for number in range(first, last + 1))
        tail = f"\n… {len(lines)} lines; next start={last + 1}" if last < len(lines) else ""
        return {"ok": True, "text": f"{target.relative_to(root)} · sha256={_hash(target)}\n{body}{tail}"}
    if action == "list":
        if not target.is_dir():
            return {"ok": False, "error": f"not a directory: {target}"}
        rows = []
        entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        for child in entries[:LIST_ENTRY_CAP]:
            if advisor.is_protected(str(child)):
                rows.append(child.name + "/ [protected]")
            elif child.is_dir():
                rows.append(child.name + "/")
            else:
                # Один битый симлинк валил `stat()` и уносил ВЕСЬ листинг в «internal error»:
                # вместо каталога она получала отказ без имени виноватого файла.
                try:
                    rows.append(f"{child.name} · {child.stat().st_size}B")
                except OSError as exc:
                    rows.append(f"{child.name} · размер не прочитан ({type(exc).__name__})")
        text = "\n".join(rows) or "(empty)"
        if len(entries) > LIST_ENTRY_CAP:
            text += (f"\n… показаны первые {LIST_ENTRY_CAP} из {len(entries)} — "
                     f"кап одного листинга (LIST_ENTRY_CAP, константа демона); "
                     f"остальное видно по подкаталогам или через search")
        return {"ok": True, "text": text, "entries_total": len(entries),
                "entries_shown": len(rows)}
    if action == "search":
        if not query:
            return {"ok": False, "error": "query is required"}
        rg = shutil.which("rg")
        if rg:
            code, out = _sh([rg, "--line-number", "--no-heading", "--color", "never",
                             "-g", glob or "**/*", "--", query, str(target)], timeout=60)
            out, dropped = _safe_rg_output(out, root)
            if code not in (0, 1):
                # Раньше упавший rg отдавал `ok:false` вообще без `error`, и клиент печатал ей
                # `[serverd] None`. Причина есть — она просто не доезжала.
                return {"ok": False, "exit": code, "code": "search_failed",
                        "error": (f"rg завершился с кодом {code}, поиск НЕ выполнен: "
                                  f"{out[:600] or 'без вывода'}")}
            body = out or "Nothing found."
            if dropped:
                # Строки кредовых файлов вырезались молча: «ничего не нашла» покрывало и
                # «совпадений нет», и «совпадения есть, но я их спрятал».
                body += (f"\n[{dropped} строк вырезано: это совпадения в файлах, помеченных "
                         f"советником как кредовые — их содержимое я не показываю]")
            return {"ok": True, "text": _cap(body, 30000), "exit": code,
                    "redacted_lines": dropped}
        try:
            regex = re.compile(query)
        except re.error as exc:
            return {"ok": False, "error": f"bad regex: {exc}"}
        hits = []
        base_path = target if target.is_dir() else target.parent
        seen = 0
        read = 0
        stopped = ""
        # Два молчаливых пропуска: кредовые файлы (законно не читаю) и файлы, которые не
        # прочитались (права/гонка/носитель). Оба уходили в `continue` без следа, и ответ
        # «Nothing found.» покрывал собой «искала везде» и «часть дерева не смотрела вовсе».
        skipped_secret = 0
        unreadable = 0
        first_unreadable = ""
        # Третий немой пропуск: каталоги, которые не открылись целиком (см. _iter_search_files).
        blind_dirs: list = []
        blind_walk = bool(glob) and glob != "**/*"
        # Раньше этот цикл читал ВСЁ дерево без срока и без счётчика, а «Nothing found.» на
        # оборванном обходе было неотличимо от «искала везде и не нашла».
        for file in _iter_search_files(base_path, glob or "**/*", blind_dirs):
            seen += 1
            # Коды СВОИ (`search-*`), а не общие «hit-cap»/«file-cap»: по общим `Budget.note()`
            # подставлял чужие числа обхода дерева и выдавал их за причину. Незнакомый код он
            # называет нейтрально «предел», а точное имя и число даёт `_search_stop_note`.
            if len(hits) >= SEARCH_HIT_CAP:
                stopped = stopped or "search-hit-cap"
                break
            if read >= SEARCH_FILE_CAP:
                stopped = "search-file-cap"
                break
            if budget.expired():
                stopped = "deadline"
                break
            if not file.is_file() or any(part in SKIP for part in file.parts):
                continue
            if advisor.is_secret(str(file)):
                skipped_secret += 1
                continue
            read += 1
            try:
                for number, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{file.relative_to(root)}:{number}: {line[:240]}")
            except OSError as exc:
                unreadable += 1
                first_unreadable = first_unreadable or f"{file}: {type(exc).__name__}: {exc}"
                continue
        budget.stage("файлы (search, без rg)", taken=read, seen=seen,
                     exhausted=not stopped, stopped=stopped)
        skips = []
        if skipped_secret:
            skips.append(f"{skipped_secret} файлов не читал: советник считает их кредовыми "
                         f"(придержка на содержимое, не на факт)")
        if unreadable:
            skips.append(f"{unreadable} файлов не прочитались, первый — {first_unreadable}")
        if blind_dirs:
            first_dir = blind_dirs[0]
            skips.append(f"{len(blind_dirs)} каталогов не открылись и НЕ обойдены целиком, "
                         f"первый — {getattr(first_dir, 'filename', '') or first_dir}: "
                         f"{type(first_dir).__name__}: {first_dir}")
        caveats = []
        if blind_walk and not hits and not stopped:
            # Оговорка, а не пропуск: утверждать «прочитано НЕ всё» я тут не вправе — я именно
            # НЕ ЗНАЮ, всё ли. Поэтому она едет отдельно от `skips` и предложение про полноту
            # обхода не переписывает.
            caveats.append(f"маска glob={glob!r} своя, поэтому дерево обходил glob — каталоги, "
                           f"которые не открылись, он пропускает МОЛЧА, и были ли такие, я не "
                           f"знаю. Хочешь обход с отчётом о пропусках — не задавай маску")
        own_note = _search_stop_note(stopped)
        if hits:
            text = "\n".join(hits)
        elif stopped:
            text = "Пока ничего не нашла."
        elif skips:
            # Ровно та подмена, ради которой всё это: «не нашла» ≠ «посмотрела всё».
            text = "Совпадений среди прочитанного нет — но прочитано НЕ всё, см. ниже."
        else:
            text = "Nothing found."
        pieces = [piece for piece in
                  (own_note, budget.note() if stopped else "", *skips, *caveats) if piece]
        tail = ("\n[" + "; ".join(pieces) + "]") if pieces else ""
        answer = _budgeted({"ok": True, "text": _cap(text, 30000) + tail, "hits": len(hits),
                            "skipped_secret": skipped_secret, "unreadable": unreadable,
                            "unreadable_dirs": len(blind_dirs)}, budget)
        # `note` печатают все обёртки клиента, а `_budgeted` кладёт туда приписку бюджета —
        # ту самую, что называла чужой кап. Своя правда идёт первой; пропуски — следом, и
        # ВСЕГДА, а не только когда усечения не было (иначе оборванный обход прятал бы их).
        answer["note"] = "\n".join(piece for piece in (
            own_note, "; ".join([*skips, *caveats]), str(answer.get("note") or "")) if piece)
        if not answer["note"]:
            answer.pop("note")
        return answer
    return {"ok": False, "error": "action: status|orientation|overview|review|model|symbols|references|diagnostics|impact|checks|read|list|search|diff"}


def _backup(path: Path) -> tuple[str, str]:
    """Копия прежнего файла и ПРИЧИНА, если копии не будет. -> (путь или '', причина или '').

    Раньше сбой копирования летел исключением и превращал всю правку в «internal error» —
    хотя правка ещё не начиналась. Отменять правку из-за копии нельзя (это был бы забор), но
    молчать о том, что откатывать её будет нечем, — тоже нельзя.
    """
    if not path.exists():
        return "", ""
    try:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        directory = BACKUPS / stamp
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest}-{path.name}"
        shutil.copy2(path, target)
        return str(target), ""
    except OSError as exc:
        return "", f"{type(exc).__name__}: {exc}"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def edit(root_value: str, action: str, path: str = "", content: str = "", old: str = "",
         new: str = "", patch: str = "", expected_sha256: str = "") -> dict:
    root, err = _root(root_value)
    if err:
        return {"ok": False, "error": err}
    assert root is not None
    action = str(action or "").strip().lower()
    if action == "patch":
        if not patch.strip():
            return {"ok": False, "error": "patch is required"}
        for raw in re.findall(r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\n]+)", patch, re.MULTILINE):
            if raw == "/dev/null":
                continue
            target, target_err = _inside(root, raw.strip())
            if target_err or target is None:
                return {"ok": False, "error": target_err or f"bad patch path: {raw}"}
        code, checked = _sh(["git", "apply", "--check", "--whitespace=nowarn", "-"], root, 90, patch)
        if code != 0:
            return {"ok": False, "error": "git apply --check failed", "text": checked[:8000], "exit": code}
        before = _git(_git_root(root) or root, "status", "--short")
        code, applied = _sh(["git", "apply", "--whitespace=nowarn", "-"], root, 90, patch)
        after = _git(_git_root(root) or root, "status", "--short")
        return {"ok": code == 0, "text": applied or after, "exit": code, "before": before,
                "after": after, "advice": advisor.advise(command="git apply host patch")}
    target, path_err = _inside(root, path)
    if path_err:
        return {"ok": False, "error": path_err}
    assert target is not None
    if action not in {"write", "replace"}:
        return {"ok": False, "error": "action: write | replace | patch"}
    expected = str(expected_sha256 or "").strip().lower()
    before, before_error = _hash_probe(target)
    if expected and before_error:
        # «hash conflict: expected X, current » звучало как «файл изменился». Правда другая:
        # я не смог его прочитать, и совпадает он или нет — неизвестно.
        return {"ok": False, "code": "hash_unknown",
                "error": (f"файл {target} есть, но sha256 не считается ({before_error}) — "
                          f"подтвердить, что он тот самый ({expected}), нечем. Ничего не "
                          f"записано; это не конфликт версий, а нечитаемый файл.")}
    if expected and expected != before:
        return {"ok": False, "error": f"hash conflict: expected {expected}, current {before}; reread"}
    if action == "replace":
        if not target.is_file():
            return {"ok": False, "error": f"not a file: {target}"}
        current = target.read_text(encoding="utf-8", errors="replace")
        count = current.count(old)
        if count != 1:
            return {"ok": False, "error": f"exact replace needs one match, found {count}"}
        content = current.replace(old, new, 1)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return {"ok": False, "error": f"write exceeds {MAX_WRITE_BYTES} bytes"}
    backup, backup_error = _backup(target)
    existed = target.exists()
    _atomic_write(target, content)
    after, after_error = _hash_probe(target)
    was = before or (f"не прочитан ({before_error})" if before_error
                     else ("файл был, sha256 нет" if existed else "new"))
    became = after or (f"не прочитан ({after_error})" if after_error else "?")
    result = {"ok": True, "text": f"{action}: {target}; sha256 {was} → {became}",
              "before": before, "after": after, "backup": backup}
    if before_error:
        result["before_error"] = before_error
    if after_error:
        result["after_error"] = after_error
    if backup_error:
        # Правку не отменяю (это был бы забор), но «backup: ''» она читала как «копия не
        # понадобилась». Понадобилась и не получилась — разные вещи.
        result["backup_error"] = backup_error
        result["text"] += (f"\n⚠ копии прежнего файла НЕТ: {backup_error} — откатить эту "
                           f"правку из state/backups не выйдет")
    note = advisor.advise(path=str(target))
    if note:
        result["advice"] = note
    return result


def _op_dir(operation_id: str) -> Path:
    return OPERATIONS / str(operation_id or "")


def _read_meta(directory: Path) -> tuple[dict, str]:
    """Заявка операции и ПРИЧИНА, если её не прочитать. -> (meta или {}, причина или '').

    ⚠ 27.07. Нечитаемый meta.json давал пустой словарь: операция теряла root/command/created,
    выпадала из `op.list` по корню и переставала существовать для `forge._finish_unlocked` —
    то есть задача закрывалась при живой операции на хосте. Отсутствие файла (легаси-каталог)
    и невозможность его прочитать — разные факты, и второй обязан быть виден.
    """
    path = directory / "meta.json"
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"meta.json: {type(exc).__name__}: {exc}"
    if not isinstance(row, dict):
        return {}, f"meta.json: не объект JSON ({type(row).__name__})"
    return row, ""


def _operation_rows() -> tuple[list[dict], str]:
    """Строки операций и ПРИЧИНА, если список может быть неполным. -> (строки, причина или '').

    ⚠ 27.07. Стояло `if not OPERATIONS.is_dir(): return rows` — пустой список одинаково значил
    «операций ещё не было» и «каталог состояния не читается / не смонтирован» (`Path.is_dir()`
    глотает OSError и отвечает False). Этот же список читает `forge._finish_unlocked`, решая,
    можно ли закрывать задачу: нечитаемый каталог превращался в «активных операций на хосте
    нет», и задача закрывалась при живой команде на хосте.
    """
    rows: list[dict] = []
    try:
        entries = list(OPERATIONS.iterdir())
    except FileNotFoundError:
        return rows, ""     # каталога нет — операций вправду ещё не заводили
    except OSError as exc:
        return rows, (f"каталог операций {OPERATIONS} не прочитан ({type(exc).__name__}: {exc}) "
                      f"— пустой список здесь НЕ значит «операций нет»")
    for directory in entries:
        if not directory.is_dir():
            continue
        state = hostproc.poll(directory)
        meta, meta_error = _read_meta(directory)
        row = {**meta, **state, "id": directory.name}
        if meta_error:
            row["meta_error"] = meta_error
        rows.append(row)
    rows.sort(key=lambda row: row.get("created", ""), reverse=True)
    return rows, ""


def op_start(root_value: str, command: str, cwd: str = ".", timeout: int = 0,
             name: str = "", limits: dict | None = None, operation_id: str = "") -> dict:
    root, err = _root(root_value)
    if err:
        return {"ok": False, "error": err}
    assert root is not None
    place, path_err = _inside(root, cwd)
    if path_err or place is None or not place.is_dir():
        return {"ok": False, "error": path_err or f"cwd does not exist: {cwd}"}
    command = str(command or "").strip()
    if not command:
        return {"ok": False, "error": "command is required"}
    if advisor.command_touches_protected(command):
        return {"ok": False, "error": "owner-configured protected root: command refused"}
    if advisor.contains_protected(str(place)) and advisor.command_may_traverse(command):
        return {"ok": False, "error": ("broad raw shell from this cwd could traverse a protected "
                                            "protected root; choose a narrower cwd/root or a typed host verb")}
    known_rows, listing_error = _operation_rows()
    active = [row for row in known_rows
              if row.get("status") in {"starting", "running", "finishing"}]
    free = shutil.disk_usage(STATE).free
    # Оба предела раньше становились известны только в момент отказа. Теперь они едут
    # в КАЖДОМ ответе op.start — и в удачном тоже.
    caps = {"max_operations": MAX_OPERATIONS, "active_operations": len(active),
            "disk_free_mib": free // (1024 * 1024),
            "disk_floor_mib": DISK_FLOOR_BYTES // (1024 * 1024)}
    # Счёт живых операций мог не состояться. Запускать всё равно запускаем (отказ был бы
    # забором на пустом месте), но объявлять «живых N из 24» как факт после этого нельзя.
    if listing_error:
        caps["listing_error"] = listing_error
        counted = (f"сколько host-операций живо, я НЕ ЗНАЮ ({listing_error}); мой потолок "
                   f"{MAX_OPERATIONS}, и упёрлась ли я в него — проверить сейчас нечем")
    else:
        counted = (f"живых host-операций {len(active) + 1} из {MAX_OPERATIONS} "
                   f"(PRAXIS_SERVERD_MAX_OPERATIONS)")
    if len(active) >= MAX_OPERATIONS:
        return {"ok": False, "code": "operation_cap", "caps": caps,
                "error": (f"занято: одновременно живых host-операций {len(active)} из "
                          f"{MAX_OPERATIONS} (PRAXIS_SERVERD_MAX_OPERATIONS). Посмотри op.list "
                          "и останови ненужную op.stop либо попробуй позже — команда не запускалась.")}
    if free < DISK_FLOOR_BYTES:
        return {"ok": False, "code": "disk_low", "caps": caps,
                "error": (f"свободно {free // (1024 * 1024)} MiB, порог запуска "
                          f"{DISK_FLOOR_BYTES // (1024 * 1024)} MiB — команда не запускалась, "
                          "чтобы не остаться без места под логи и результаты операции.")}
    operation_id = str(operation_id or ("op-" + uuid.uuid4().hex[:10]))
    if not re.fullmatch(r"op-[a-zA-Z0-9_-]{6,64}", operation_id):
        return {"ok": False, "error": "invalid operation_id"}
    directory = _op_dir(operation_id)
    if directory.is_dir():
        existing = op_poll(operation_id)
        if existing.get("status") != "unknown" or (directory / "state.json").exists():
            existing["reused"] = True
            # `reused` — структурное поле, и его не печатает никто: `serverd_client.process
            # ('start')` собирает строку «Started <id> pid=…» одинаково для нового запуска и
            # для подхваченного старого. То есть про операцию недельной давности ей говорили
            # «запустила». Признание кладётся в `note` — его печатают все обёртки клиента.
            existing["note"] = "\n".join(piece for piece in (
                str(existing.get("note") or ""),
                (f"команду я СЕЙЧАС не запускала: операция {operation_id} уже существовала "
                 f"(статус {existing.get('status')}, создана "
                 f"{existing.get('created') or 'неизвестно когда'}, команда "
                 f"{existing.get('command') or 'неизвестна'}). Вернула её, а не новую.")
            ) if piece)
            return existing
    directory.mkdir(parents=True, exist_ok=True)
    meta = {"id": operation_id, "root": str(root), "cwd": str(place), "command": command,
            "name": str(name or ""), "created": _now(), "advice": advisor.advise(command=command)}
    (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    state = hostproc.spawn(directory, command, str(place), timeout=max(0, int(timeout or 0)),
                           limits=limits or {})
    # `caps` в ответе — ещё не «она это видит»: `serverd_client.process('start')` собирает
    # свою строку из id/pid/cwd/log и до `caps` не дотягивается. А вот `note` печатают все
    # обёртки клиента (`_with_note`), поэтому те же числа едут ещё и здесь.
    return {"ok": True, **meta, **state, "log": str(directory / "command.log"), "caps": caps,
            "note": (f"пределы: {counted}; "
                     f"свободно {free // (1024 * 1024)} MiB "
                     f"при пороге запуска {DISK_FLOOR_BYTES // (1024 * 1024)} MiB.")}


def _missing_operation(operation_id: str) -> dict:
    """Отказ «такой операции нет» — но только когда это ПРАВДА проверено.

    ⚠ 27.07. `if not directory.is_dir(): return {"error": f"unknown operation …"}` утверждало
    несуществование операции и на нечитаемом каталоге состояния: `Path.is_dir()` отвечает
    False и на «нет», и на «не пустили», и на «носитель отвалился». Она читала «такой операции
    не было» ровно там, где операция могла жечь хост прямо сейчас.
    """
    directory = _op_dir(operation_id)
    try:
        os.stat(directory)
        return {"ok": False, "code": "operation_unreadable",
                "error": (f"каталог операции {directory} существует, но открыть его как каталог "
                          f"не вышло — что с операцией {operation_id}, я НЕ ЗНАЮ. Это не "
                          f"«такой операции нет».")}
    except FileNotFoundError:
        pass
    except OSError as exc:
        return {"ok": False, "code": "operation_unreadable",
                "error": (f"есть ли операция {operation_id}, выяснить не вышло: {directory} — "
                          f"{type(exc).__name__}: {exc}. Это НЕ «такой операции нет».")}
    try:
        os.stat(OPERATIONS)
    except OSError as exc:
        return {"ok": False, "code": "operations_unreadable",
                "error": (f"есть ли операция {operation_id}, я не знаю: сам каталог операций "
                          f"{OPERATIONS} не прочитан ({type(exc).__name__}: {exc}). "
                          f"Это НЕ «такой операции нет».")}
    return {"ok": False, "code": "unknown_operation",
            "error": f"unknown operation {operation_id}"}


def op_poll(operation_id: str, tail: int = 12000) -> dict:
    directory = _op_dir(operation_id)
    if not directory.is_dir():
        return _missing_operation(operation_id)
    state = hostproc.poll(directory)
    meta, meta_error = _read_meta(directory)
    log_path = directory / "command.log"
    log_error = ""
    cut = 0
    try:
        want = int(tail)
    except (TypeError, ValueError):
        want = 12000
    cap = max(LOG_TAIL_MIN, min(want, LOG_TAIL_CAP))
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace")
        text = raw[-cap:]
        cut = len(raw) - len(text)
    except FileNotFoundError:
        # Лога ещё нет — это правда о ходе дела, а не сбой.
        text = ""
    except OSError as exc:
        # А вот здесь пустой body раньше был подлогом: `serverd_client.run` печатает
        # `(empty output)`, и «команда ничего не вывела» становилось неотличимо от
        # «вывод есть, я его не прочитал».
        text, log_error = "", f"{type(exc).__name__}: {exc}"
    answer = {"ok": True, **meta, **state, "id": operation_id, "body": text,
              "log": str(log_path), "body_cut_chars": cut, "body_tail_cap": cap}
    notes = []
    if cut:
        # Срез хвоста молчал: из мегабайтного вывода приезжали последние 12000 символов, и
        # ни в теле, ни рядом не было ничего, из чего следовало бы, что начало отрезано.
        notes.append(f"это ХВОСТ лога: показаны последние {len(text)} символов, начало "
                     f"({cut} символов) отрезано капом `tail`={cap} (потолок {LOG_TAIL_CAP}). "
                     f"Весь вывод целиком — в файле {log_path} на хосте")
    if log_error:
        answer["log_error"] = log_error
        notes.append(f"вывод команды прочитать не вышло ({log_error}) — пустой body здесь НЕ "
                     f"значит «команда ничего не напечатала»; файл {log_path}")
    if meta_error:
        answer["meta_error"] = meta_error
        notes.append(f"заявку операции прочитать не вышло ({meta_error}) — команда, корень и "
                     f"время запуска в этом ответе отсутствуют не потому, что их не было")
    if notes:
        answer["note"] = "\n".join([*notes, str(answer.get("note") or "")]).strip()
    return answer


# Статусы, при которых юнит ещё живёт. Нужны, чтобы судить об остановке по ЯВИ (`hostproc.poll`
# после вызова), а не по тому, что мы просили сделать.
LIVE_STATUSES = {"starting", "running", "finishing"}
# Статусы, которые ПОЛОЖИТЕЛЬНО говорят, чем операция кончилась. Всё остальное (`lost`,
# `unknown`) — это «я потеряла её из виду», и засчитывать его за остановку нельзя:
# hostproc умеет честно отказать («номер группы не опубликован — не знаю, что останавливать»),
# и после такого отказа статус вполне может быть `lost`.
# ⚠ 28.07. `error` отсюда не хватало, и он попадал в «потеряла из виду» — а это неправда:
# `error` пишет сам супервизор, когда падает сам (`hostproc._supervise`, «no space left on
# device» и подобное), и это ЗАПИСАННЫЙ исход, улика. Рядом стоящий `hostproc._outcome_text`
# называл причину верно («супервизор этой операции сам упал — …»), а op_stop следом
# перебивал её словами «чем она кончилась, сказать нельзя» — и улика терялась.
# Тот же набор с той же мотивировкой — `hostproc.TERMINAL_STATUSES`.
SETTLED_STATUSES = {"stopped", "done", "failed", "timed_out", "error"}


def _stop_outcome(raw) -> tuple[bool | None, str]:
    """Вердикт и текст из ответа `hostproc.stop()`. -> (True/False/None, текст).

    Сегодня hostproc отдаёт строку, и вердикта в ней нет: «остановлен» и «не остановлен: …» —
    обе просто строки. Если он научится отдавать структуру ({"ok":…, "text":…}) — вердикт
    берётся оттуда. По словам НЕ гадаем ни в одном случае: угаданный вердикт — это тот же
    выдуманный исход, только пришедший с другой стороны.
    """
    if isinstance(raw, dict):
        verdict = raw.get("ok")
        text = str(raw.get("text") or raw.get("note") or raw.get("error") or "")
        return (bool(verdict) if verdict is not None else None), text
    return None, str(raw or "")


def _child_still_running(before: dict) -> bool | None:
    """Жив ли ТОТ ЖЕ ребёнок, что был до остановки. -> True/False/None (проверить нечем).

    Третье доказательство помимо слов hostproc и его же result.json: если процесс с той же
    меткой рождения жив, «остановлено» — неправда, кто бы её ни записал. Метки может не быть
    (легаси-юниты) — тогда честный ответ «не знаю», а не «значит, всё хорошо».
    """
    checker = getattr(hostproc, "same_process", None)
    pid = int(before.get("child_pid") or 0)
    birth = str(before.get("child_starttime") or "")
    if not callable(checker) or not pid or not birth:
        return None
    try:
        return bool(checker(pid, birth, str(before.get("boot_id") or "")))
    except Exception:  # noqa: BLE001 — проверка не имеет права стать причиной отказа
        return None


def op_stop(operation_id: str) -> dict:
    """Остановить операцию и сказать, чем это КОНЧИЛОСЬ, а не чего мы хотели.

    ⚠ 27.07. Здесь стояло `{"ok": True, "status": "stopped"}` — константами, до и мимо любой
    проверки. Отказ hostproc («не остановлен: PermissionError», «не могу доказать, что эта
    группа моя») выезжал наружу как успешная остановка, а её `text` тонул рядом со словом
    «stopped». Ровно тот класс, что мы весь день выкорчёвываем: неизвестность подана фактом.
    Теперь статус — наблюдённый (`hostproc.poll` ПОСЛЕ вызова), а чего доказать не вышло,
    так и сказано.
    """
    directory = _op_dir(operation_id)
    if not directory.is_dir():
        return _missing_operation(operation_id)
    before = hostproc.poll(directory)
    verdict, message = _stop_outcome(hostproc.stop(directory))
    after = hostproc.poll(directory)
    status = str(after.get("status") or "unknown")
    alive = _child_still_running(before)
    if alive is True:
        proof = ("процесс с той же меткой рождения ЖИВ — что бы ни было записано в result.json, "
                 "остановки не случилось")
    elif alive is False:
        proof = "проверено: процесса с той же меткой рождения больше нет"
    else:
        proof = ("проверить нечем: метки рождения ребёнка у этого юнита нет, так что "
                 "«остановлен» здесь — запись hostproc, а не независимая проверка")
    if verdict is not None:
        ok = verdict                      # hostproc сказал прямо — верим его слову, не яви
    else:
        ok = alive is not True and status in SETTLED_STATUSES
    pieces = [message or "hostproc ничего не сказал", f"состояние после: {status}", proof]
    if status == "error" and alive is not False:
        # `error` — исход СУПЕРВИЗОРА, и засчитывать его за смерть команды нельзя: он мог
        # упасть, уже запустив её. Метки рождения в result.json нет, значит проверить это
        # нечем — и `ok:true` рядом не должен читаться как «команда точно не бежит».
        pieces.append(f"⚠ это исход супервизора, а не самой команды: осталась ли она жить, "
                      f"по этой записи не видно — смотри op.poll {operation_id} и лог операции")
    if not ok and (alive is True or status in LIVE_STATUSES):
        pieces.append(f"⚠ команда может ЕЩЁ РАБОТАТЬ на хосте: смотри op.poll {operation_id} "
                      f"и, если надо, повтори op.stop; я её остановленной не считаю")
    elif not ok:
        pieces.append(f"⚠ остановленной я её не считаю: «{status}» — это не «команда "
                      f"завершилась», а «я потеряла её из виду». Чем она кончилась, "
                      f"по этому ответу сказать нельзя; op.poll {operation_id}")
    return {"ok": ok, "id": operation_id, "status": status, "text": " · ".join(pieces),
            "stop_message": message, "stop_verdict": verdict, "child_alive": alive,
            "status_before": str(before.get("status") or "unknown")}


def op_list(root_value: str = "", limit: int = 100) -> dict:
    """Список операций — с признанием всего, чего в нём не хватает.

    ⚠ 27.07. Три молчания разом. (1) `except OSError: rows = []` — нерезолвящийся корень
    отвечал «операций нет» вместо «я не понял, про какой корень спрашиваешь». (2) Срез
    `rows[:limit]` резал молча. (3) Операции с нечитаемым meta.json теряли `root` и тихо
    выпадали из фильтра по корню. Этот же ответ читает `forge._finish_unlocked`, решая,
    можно ли закрывать задачу, — каждое из трёх молчаний закрывает задачу при живой работе.
    """
    rows, listing_error = _operation_rows()
    if listing_error:
        # Четвёртое молчание того же рода и худшее из них: сам каталог операций не прочитался.
        # `ok:false` тут не забор — это отказ ВЫДАВАТЬ пустоту за факт. Действий он не отменяет.
        return {"ok": False, "code": "operations_unreadable", "operations": [], "total": 0,
                "listing_error": listing_error, "note": listing_error,
                "error": (f"{listing_error}. Есть ли сейчас живые операции на хосте — я не знаю; "
                          f"пустой список был бы неправдой. Посмотри каталог состояния демона "
                          f"({OPERATIONS}) или повтори.")}
    total = len(rows)
    unknown_root = 0
    if root_value:
        try:
            wanted = str(Path(root_value).resolve())
        except OSError as exc:
            return {"ok": False, "code": "root_unresolved", "operations": [],
                    "error": (f"корень {root_value!r} не разрешился ({type(exc).__name__}: "
                              f"{exc}), поэтому отобрать операции по нему я не смог. Пустой "
                              f"список тут был бы неправдой; посмотри op.list без корня.")}
        kept = []
        for row in rows:
            known = str(row.get("root") or "")
            if known == wanted:
                kept.append(row)
            elif not known:
                # Не могу доказать, что она НЕ твоя, — значит показываю с пометкой, а не
                # выбрасываю. Раньше именно такие строки исчезали.
                unknown_root += 1
                kept.append({**row, "root_unknown": True})
        rows = kept
    cap = max(1, min(int(limit or 100), OPERATION_LIST_CAP))
    shown = rows[:cap]
    answer = {"ok": True, "operations": shown, "total": total, "matched": len(rows),
              "shown": len(shown)}
    notes = []
    if len(rows) > len(shown):
        notes.append(f"показаны {len(shown)} операций из {len(rows)} подходящих (кап списка "
                     f"{cap}, потолок {OPERATION_LIST_CAP}) — список НЕ полный")
    broken = [row for row in shown if row.get("meta_error")]
    if broken:
        notes.append(f"{len(broken)} операций с нечитаемой заявкой ({broken[0]['meta_error']}): "
                     f"их корень и команда неизвестны, статус — известен")
    if unknown_root:
        notes.append(f"{unknown_root} операций показаны с пометкой root_unknown: их корень не "
                     f"прочитан, и что они не из {root_value} — не доказано")
    if notes:
        answer["note"] = "; ".join(notes)
    return answer


def run_wait(root_value: str, command: str, cwd: str = ".", timeout: int = 600,
             send_chunk: Callable[[str], None] | None = None,
             operation_id: str = "") -> dict:
    started = op_start(root_value, command, cwd=cwd, timeout=timeout, name="foreground",
                       operation_id=operation_id)
    if not started.get("ok"):
        return started
    operation_id = started["id"]
    directory = _op_dir(operation_id)
    offset = 0
    began = time.monotonic()
    deadline = (began + max(WAIT_GRACE_SEC, int(timeout or 0) + WAIT_GRACE_SEC)
                if timeout else None)
    gave_up: dict | None = None
    while True:
        state = hostproc.poll(directory)
        if send_chunk:
            try:
                raw = (directory / "command.log").read_bytes()
                if len(raw) > offset:
                    send_chunk(raw[offset:].decode("utf-8", "replace"))
                    offset = len(raw)
            except OSError:
                pass
        if state.get("status") not in LIVE_STATUSES:
            break
        if deadline and time.monotonic() > deadline:
            # ⚠ 27.07. Здесь стояло голое `hostproc.stop(directory)` — возвращённый им текст
            # выбрасывался целиком, а `state` пересчитывался и тут же терялся на `break`.
            # Наружу уезжал обычный `op_poll`, из которого не следовало ни что срок был мой,
            # ни что команду оборвала я, ни — самое важное — что остановка могла не удаться
            # и команда всё ещё жжёт хост. Оставалось голое «не вышло».
            gave_up = op_stop(operation_id)
            break
        time.sleep(.1)
    answer = op_poll(operation_id, tail=20000)
    if gave_up is None:
        return answer
    waited = time.monotonic() - began
    line = (f"я ждала {waited:.0f}с (мой срок ожидания = timeout {int(timeout or 0)}с + "
            f"{WAIT_GRACE_SEC}с на завершение) и оборвала команду сама — сама она не закончилась. "
            f"Остановка: {gave_up.get('text') or gave_up.get('error') or 'без объяснения'}")
    if not gave_up.get("ok"):
        line += (f" ⚠ Остановить НЕ удалось: команда может продолжать работать на хосте. "
                 f"Статус ниже — не доказательство, что она мертва. Смотри op.poll "
                 f"{operation_id}, лог {answer.get('log')}.")
    answer["timed_out_waiting"] = True
    answer["waited_s"] = round(waited, 1)
    answer["stop_ok"] = bool(gave_up.get("ok"))
    answer["note"] = "\n".join(piece for piece in (str(answer.get("note") or ""), line) if piece)
    return answer


def checkpoint(root_value: str, message: str = "Praxis host checkpoint") -> dict:
    root, err = _root(root_value)
    if err:
        return {"ok": False, "error": err}
    assert root is not None
    git, git_error = _git_probe(root)
    if not git:
        # «host root is not in git» — приговор корню. Если git просто не ответил, приговор
        # ложный, а её работа осталась несохранённой под видом «тут нечего сохранять».
        return {"ok": False, "error": ("host root is not in git" if not git_error else
                                       f"спросить git об этом корне не вышло: {git_error}. "
                                       f"Сохранить не смог, но это НЕ значит, что корень не "
                                       f"в git — повтори или проверь доступ.")}
    status_out = _git(git, "status", "--porcelain")
    # ⚠ 28.07. Здесь стоял ранний `ok:false` БЕЗ единой попытки коммита: git status не
    # ответил — и checkpoint уходил, не сохранив ничего. Но спросить «что изменилось» и
    # сохранить сделанное — два разных дела, и провал первого не отменяет второго: одного
    # таймаута git довольно, чтобы её работа за час осталась незафиксированной. Это путь
    # СОХРАНЕНИЯ, ошибиться здесь дороже всего, поэтому add+commit идут ВСЁ РАВНО, а в
    # ответе называются ОБА исхода — и что спросить состояние не вышло, и что вышло из
    # попытки сохранить. Молчаливого «нечего сохранять» тут больше нет ни в одну сторону.
    status_error = status_out[:300] if status_out.startswith("[git exit ") else ""
    if not status_error and not status_out.strip():
        return {"ok": True, "text": "No changes; checkpoint not needed."}
    blind = (f"спросить git, что в этом корне изменилось, не вышло: {status_error}. "
             f"Сохраняю вслепую — всё, что ниже, это исход ПОПЫТКИ сохранить, а не сверка "
             f"с состоянием корня. " if status_error else "")
    code, out = _sh(["git", "-C", str(git), "add", "-A"], timeout=60)
    if code != 0:
        # Раньше сюда уезжал голый вывод git в поле `error` — без единого слова о том, что
        # работа осталась несохранённой и что с этим делать (соседний `_git_probe` говорит).
        # Вслепую утверждать «работа не сохранена» нельзя (может, и сохранять было нечего),
        # а «сохранения не произошло» верно в обоих случаях.
        return {"ok": False, "exit": code, "status_error": status_error, "note": blind.strip(),
                "error": (f"{blind}git add -A не прошёл: {out}. "
                          + ("Сохранения не произошло" if status_error
                             else "Работа НЕ сохранена")
                          + f" — повтори или проверь доступ к {git}.")}
    code, out = _sh(["git", "-C", str(git), "-c", "user.name=Praxis", "-c",
                     "user.email=praxis@local", "commit", "-m", str(message)], timeout=120)
    if code != 0:
        # `{"ok": False, "text": out}` без `error` печаталось клиентом как «[serverd] None»:
        # провалившееся сохранение выглядело пустотой. Теперь причина — словами git.
        # ⚠ Про «Работа НЕ сохранена» вслепую сказать нельзя: не спросив состояние, я не
        # отличу «сохранять было нечего» от «сохранить не вышло». Утверждается только то,
        # что известно наверняка, — коммита нет.
        tail = ("Сохранять было нечего или сохранить не вышло — не различаю: состояние "
                f"корня спросить не удалось. Повтори или проверь доступ к {git}."
                if status_error else
                f"Работа НЕ сохранена — повтори или проверь доступ к {git}.")
        return {"ok": False, "exit": code, "status_error": status_error, "note": blind.strip(),
                "error": f"{blind}коммит не создан [git exit {code}]: {out}. {tail}"}
    sha = _git(git, "rev-parse", "--short", "HEAD")
    if sha.startswith("[git exit "):
        # Коммит состоялся, а его номер прочитать не вышло. «Checkpoint [git exit 128]…»
        # выглядело как номер коммита — теперь сказано, что именно неизвестно.
        return {"ok": True, "exit": code, "sha": "", "status_error": status_error,
                "note": blind.strip(),
                "text": (f"{blind}Сохранила ({message}), но номер коммита прочитать не вышло: "
                         f"{sha[:300]}. Работа зафиксирована, откатываться по номеру нечем — "
                         f"смотри `git log -1` в {git}.")}
    return {"ok": True, "text": f"{blind}Checkpoint {sha}: {message}",
            "exit": code, "sha": sha, "status_error": status_error, "note": blind.strip()}


def reconcile() -> dict:
    rows, listing_error = _operation_rows()
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    answer = {"operations": len(rows), "statuses": counts, "boot_id": hostproc.boot_id()}
    if listing_error:
        # Строка сверки уезжает в лог старта демона (`serve`). «operations: 0» там читается как
        # «на хосте чисто» — и точно так же читал бы человек, разбирающий инцидент.
        answer["listing_error"] = listing_error
    return answer
