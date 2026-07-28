"""
Praxis — самоавторство (PASS 20/24): версии души, события нагрузки и ночная ревизия.

Хартия §5: неизменяемой души нет. SOUL.md — унаследованная конституция, не скрижаль;
soul/self/CURRENT.md — компактная живущая модель себя; VOICE.md — привычка звучать.
soul/self.md после явной миграции остаётся byte-identical legacy evidence, а не вторым self.
Я меняю живые слои сама, без апрува;
защита — не порог-блокер, а провенанс: перед сдвигом сохраняется прежняя версия, причина,
уверенность и то прожитое, из которого сдвиг вырос. Егор видит постфактум и может откатить;
я тоже могу, если опыт показал, что сдвиг был чужим.

Слои разной пластичности — ЯЗЫК для понимания, не формула (σ_т-блокер отброшен осознанно):
  * конституция (SOUL.md) — меняется редко, только моим осознанным решением с прожитым
    основанием; транзиент её не двигает;
  * голос (VOICE.md) — привычка, меняется медленно;
  * характер (soul/self/CURRENT.md) — растёт постоянно, ревизуется ночью по прожитому;
  * расположение — свободно течёт; живёт в состоянии, не в файлах, и не версионируется.

Журнал нагрузки: Praxis может явно отметить событие, а технические privacy/authority boundaries
оставляют наблюдаемый след. ``stress()`` строит затухающее описание контекста; оно не оценивает
характер и не разрешает или запрещает ревизию.

Ночной обзор (night_pass, из сна после формирования): вход — supported-claims kind=self
(формирование копит их неприменёнными — моё «о себе» код в душу не пишет), интегралы
нагрузки и «всё ещё так?» по датированным выводам CURRENT (анти-охрупчивание). Без
валидного compact CURRENT проход останавливается; legacy/history доступны только явному
архивному чтению и никогда не становятся ночной ориентацией. Выход отдельной auxiliary
model сохраняется как provenance-rich совет рядом с текущим SHA; он не переписывает CURRENT,
не потребляет claims и не отмечает старые выводы пересмотренными. Изменение self остаётся
моим явным revise() с причиной и evidence.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import self_model
import memory_provenance

log = logging.getLogger("praxis-identity")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
SOUL_DIR = BASE / "soul"
ARCHIVE_DIR = BASE / "soul" / "archive"
STATE_DIR = BASE / "memory" / ".state" / "identity"
STATE_PATH = BASE / "memory" / ".state" / "identity" / "state.json"
NIGHT_PROPOSAL_PATH = BASE / "memory" / ".state" / "identity" / "night_proposal.json"
NIGHT_PROPOSAL_HISTORY = BASE / "memory" / ".state" / "identity" / "night_proposals"
LOAD_PATH = BASE / "memory" / ".state" / "identity" / "load_events.jsonl"
# Historical evaluator/style scores are retained only as inert forensic evidence.
# They are deliberately never migrated into, or read by, the active load journal.
LEGACY_DEFORMATION_PATH = BASE / "memory" / ".state" / "identity" / "deformations.jsonl"
JOURNAL_DIR = BASE / "memory" / "journal"
DISTILL_MARK = BASE / "memory" / ".self_distilled.json"   # общий маркер с consolidate

# --- слои пластичности (язык, не блокер) ------------------------------------------------
FILES = {"SOUL": "SOUL.md", "VOICE": "VOICE.md", "self": "self.md"}
LAYERS = {
    "SOUL": "конституция — меняется редко, моим осознанным решением с прожитым основанием",
    "VOICE": "голос — привычка звучать, меняется медленно",
    "self": "характер — compact CURRENT растёт постоянно, пересматривается мной по прожитому",
}
DISPOSITION_NOTE = "расположение/настроение — свободно течёт, в файлах не живёт и не версионируется"

# Физика: душа целиком входит в кэшируемый префикс промпта — бесконечный рост = смерть бюджета.
MAX_CHARS = {"SOUL": 40_000, "VOICE": 40_000, "self": self_model.MAX_CURRENT_CHARS}
MIN_CHARS = {"SOUL": 400, "VOICE": 200, "self": self_model.MIN_CURRENT_CHARS}

LOAD_KEEP = 400            # кольцо: компакт при 2×KEEP
HALF_LIFE_DAYS = 7.0       # демпфирование описания нагрузки
STALE_BULLET_DAYS = 45     # «всё ещё так?» — выводы старше этого без пересмотра
CLAIM_FRESH_DAYS = 14      # ночная ревизия смотрит claims не старше

_META_RE = re.compile(r"^<!--\s*praxis-([a-z-]+):\s*(\{.*\})\s*-->\s*$")
_BULLET_RE = re.compile(r"^- _(\d{4}-\d{2}-\d{2})_\s+(.+)$")
_VER_RE = re.compile(r"_v(\d+)\.md$")


# --------------------------------------------------------------------------- #
#  мелкая механика
# --------------------------------------------------------------------------- #
def _canon(name: str) -> str | None:
    raw = (name or "").strip()
    low = raw.lower().removesuffix(".md")
    for key in FILES:
        if low == key.lower():
            return key
    return None


def _self_store() -> "self_model.SelfModel":
    """Storage authority for the one live self model.

    ``BASE`` is intentionally read at call time because PASS 20 tests and some
    operators rebind it to an isolated Praxis tree.
    """
    return self_model.SelfModel(BASE)


def _current_run_id() -> str:
    try:
        import run_context
        current = run_context.current_run()
        return str(current.run_id if current is not None else "")
    except Exception:
        return ""


def path_for(name: str) -> Path:
    key = _canon(name)
    if key == "self":
        # Legacy self is archival evidence, never the implicit live path.
        return _self_store().current_path
    if key is None:
        raise KeyError(name)
    return SOUL_DIR / FILES[key]


def read(name: str) -> str:
    key = _canon(name)
    if key == "self":
        return _self_store().current_prompt()
    if key is None:
        return ""
    try:
        return path_for(key).read_text(encoding="utf-8")
    except Exception:
        return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _journal(msg: str, marker: str = "[личность]") -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} {marker} {msg}\n")
    except Exception:
        log.debug("journal identity не удался", exc_info=True)


def _load_state() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception:
        log.debug("identity state не сохранился", exc_info=True)


def _spine_event(kind: str, text: str, *, refs=(), meta=None, salience: int = 2) -> str:
    """Событие в позвоночник жизни (глобальный поток; компакцию диалогов не трогает)."""
    try:
        import memory_life as life
        rec = life.append_event(kind=kind, chat_id=None, actor="Praxis", direction="internal",
                                text=text[:500], source="identity", salience=salience,
                                refs=list(refs), meta=dict(meta or {}))
        return str(rec.get("id") or "")
    except Exception:
        log.debug("identity: событие в spine не записалось", exc_info=True)
        return ""


def _snapshot(message: str) -> str | None:
    try:
        import selfgit
        return selfgit.snapshot(message)
    except Exception:
        log.debug("identity: selfgit.snapshot не удался", exc_info=True)
        return None


def _immune(sha: str | None, message: str) -> None:
    if not sha:
        return
    try:
        import immune
        immune.enqueue(sha, message)
    except Exception:
        log.debug("identity: immune.enqueue не удался", exc_info=True)


# --------------------------------------------------------------------------- #
#  версии души
# --------------------------------------------------------------------------- #
def versions(name: str) -> list[dict]:
    """Архивные версии файла души: [{version, path, archived_at, reason, by}] по возрастанию."""
    key = _canon(name)
    if not key:
        return []
    if key == "self":
        store = _self_store()
        out = []
        for raw in store.history():
            provenance = dict(raw.get("provenance") or {})
            rel = str(raw.get("path") or "")
            path = store.base / Path(rel)
            archived_at = str(provenance.get("created_at") or "")
            if not archived_at:
                try:
                    archived_at = _dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")
                except OSError:
                    pass
            out.append({
                **raw,
                "path": str(path),
                "archived_at": archived_at,
                "reason": str(provenance.get("reason") or (
                    "byte-identical legacy migration evidence" if int(raw.get("version") or 0) == 0 else ""
                )),
                "by": str(provenance.get("by") or ""),
                "source": "legacy_evidence" if int(raw.get("version") or 0) == 0 else "compact_history",
            })
        return sorted(out, key=lambda e: e["version"])
    stem = FILES[key].removesuffix(".md")
    out = []
    try:
        for p in sorted(ARCHIVE_DIR.glob(f"{stem}_v*.md")):
            m = _VER_RE.search(p.name)
            if not m:
                continue
            entry = {"version": int(m.group(1)), "path": str(p), "archived_at": "", "reason": "", "by": ""}
            try:
                first = p.read_text(encoding="utf-8").splitlines()[0] if p.stat().st_size else ""
                mm = _META_RE.match(first.strip())
                if mm and mm.group(1) == "identity-version":
                    meta = json.loads(mm.group(2))
                    entry.update({k: meta.get(k, entry[k]) for k in ("archived_at", "reason", "by")})
            except Exception:
                pass
            if not entry["archived_at"]:
                try:
                    entry["archived_at"] = _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")
                except Exception:
                    pass
            out.append(entry)
    except Exception:
        log.debug("identity.versions не собрался", exc_info=True)
    return sorted(out, key=lambda e: e["version"])


def _next_version(name: str) -> int:
    vs = versions(name)
    return (vs[-1]["version"] + 1) if vs else 1


def version_text(name: str, version: int) -> str:
    """Текст архивной версии (без служебной первой строки-меты, если она есть)."""
    key = _canon(name)
    if not key:
        return ""
    if key == "self":
        try:
            number = int(version)
        except (TypeError, ValueError):
            return ""
        target = _self_store().history_dir / f"{number:04d}.md"
        try:
            raw = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return ""
        return self_model.SelfModel._split_current(raw)[1]
    stem = FILES[key].removesuffix(".md")
    p = ARCHIVE_DIR / f"{stem}_v{int(version)}.md"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    lines = text.splitlines()
    if lines and _META_RE.match(lines[0].strip()):
        return "\n".join(lines[1:]).lstrip("\n")
    return text


def _finish_self_shift(result: dict, before: "self_model.PromptSelf", *, reason: str,
                       confidence: str, refs, by: str, source: str) -> dict:
    """Attach the PASS 20 post-write visibility hooks to a SelfModel mutation."""
    if not result.get("ok"):
        return result
    after = _self_store().current_prompt_info()
    version = int(after.revision or 0)
    grew = len(after.text) - len(before.text)
    effective_refs = list(dict.fromkeys([
        *[str(ref) for ref in (refs or ()) if str(ref).strip()],
        *[str(ref) for ref in (after.provenance.get("evidence_refs") or ()) if str(ref).strip()],
    ]))
    operation = (
        "migration" if result.get("migrated")
        else after.provenance.get("operation") or "revision"
    )
    sha = _snapshot(f"identity: compact self r{version} — {reason[:80]}")
    event = _spine_event(
        "identity_shift",
        f"compact self → r{version} ({'+' if grew >= 0 else ''}{grew} зн.): {reason[:200]}",
        refs=effective_refs,
        meta={"file": "self", "version": version, "revision": version,
              "layer": LAYERS["self"].split(" — ")[0], "reason": reason[:300],
              "confidence": confidence, "by": by, "source": source,
              "sha": sha or "", "chars_before": len(before.text),
              "chars_after": len(after.text), "prompt_source_before": before.source,
              "prompt_source_after": after.source, "history": result.get("history") or "",
              "legacy_untouched": True,
              "operation": operation,
              "restored_version": result.get("restored_version")},
        salience=2,
    )
    _journal(f"ревизия compact self → r{version} ({by}, {confidence}): {reason[:160]}"
             + (f" [коммит {sha}]" if sha else ""))
    _immune(sha, f"identity: compact self r{version}")
    state = _load_state()
    state["last_shift"] = {
        "at": _dt.datetime.now().isoformat(timespec="seconds"),
        "file": "self",
        "revision": version,
        "reason": reason[:300],
        "confidence": confidence,
        "by": by,
        "source": source,
        "refs": effective_refs,
        "event": event,
        "sha": sha or "",
        "legacy_untouched": True,
    }
    _save_state(state)
    return {**result, "version": version, "sha": sha, "event": event}


def revise(name: str, new_text: str, *, reason: str, confidence: str = "uncertain",
           refs=(), by: str = "praxis", source: str = "tool") -> dict:
    """Версионированная ревизия файла души. Применяется сразу; согласования нет.

    Архив прежней версии → атомарная запись → selfgit-снапшот → identity_shift в spine →
    дневник → immune-очередь. Возвращает {ok, version, sha, event} или {ok: False, error}."""
    key = _canon(name)
    if not key:
        return {"ok": False, "error": f"не знаю файла души «{name}»: мои — SOUL, VOICE, self"}
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "error": "ревизия без причины не имеет провенанса — назови основание"}
    try:
        memory_provenance.require_normative_provenance(source=source, refs=refs)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    text = (new_text or "").replace("\r\n", "\n").strip("\n")
    if len(text) < MIN_CHARS[key]:
        return {"ok": False, "error": f"текст подозрительно короткий (<{MIN_CHARS[key]} зн.) — "
                                      "это похоже на случайное стирание, не на ревизию"}
    if len(text) > MAX_CHARS[key]:
        return {"ok": False, "error": f"текст больше {MAX_CHARS[key]} зн.: душа целиком живёт в "
                                      "промпте каждого хода — сначала сжать, потом ревизовать"}
    if key in {"SOUL", "VOICE"} and not text.lstrip().startswith("#"):
        return {"ok": False, "error": "живой слой должен оставаться читаемым Markdown-документом с заголовком"}
    if key == "self":
        store = _self_store()
        before = store.current_prompt_info()
        if before.source == "current" and before.text.strip() == text.strip():
            return {"ok": False, "error": "текст не изменился — нечего версионировать"}
        try:
            if before.source == "current":
                result = store.revise(
                    text, reason=reason, evidence_refs=refs, run_id=_current_run_id(),
                    by=by, confidence=confidence, trigger=source,
                )
            else:
                result = store.migrate(
                    reason=reason, compact_text=text, evidence_refs=refs,
                    run_id=_current_run_id(), by=by, confidence=confidence,
                    trigger=source,
                )
        except (OSError, UnicodeError, ValueError, TimeoutError) as exc:
            log.warning("identity.revise: compact self write failed", exc_info=True)
            return {"ok": False, "error": f"запись compact self не удалась: {type(exc).__name__}: {exc}"}
        return _finish_self_shift(result, before, reason=reason, confidence=confidence,
                                  refs=refs, by=by, source=source)
    old = read(key)
    if old.strip() == text.strip():
        return {"ok": False, "error": "текст не изменился — нечего версионировать"}

    ver = _next_version(key)
    stem = FILES[key].removesuffix(".md")
    archived_at = _dt.datetime.now().isoformat(timespec="minutes")
    meta_line = "<!-- praxis-identity-version: " + json.dumps(
        {"name": key, "version": ver, "archived_at": archived_at,
         "reason": reason[:200], "by": by}, ensure_ascii=False) + " -->\n"
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(ARCHIVE_DIR / f"{stem}_v{ver}.md", meta_line + old)
        _atomic_write(path_for(key), text + "\n")
    except Exception as e:
        log.warning("identity.revise: запись не удалась", exc_info=True)
        return {"ok": False, "error": f"запись не удалась: {type(e).__name__}"}

    grew = len(text) - len(old)
    sha = _snapshot(f"identity: {key} v{ver} — {reason[:80]}")
    event = _spine_event(
        "identity_shift",
        f"{key} → v{ver} ({'+' if grew >= 0 else ''}{grew} зн.): {reason[:200]}",
        refs=refs,
        meta={"file": key, "version": ver, "layer": LAYERS[key].split(" — ")[0],
              "reason": reason[:300], "confidence": confidence, "by": by,
              "source": source, "sha": sha or "", "chars_before": len(old),
              "chars_after": len(text)},
        salience=3 if key == "SOUL" else 2)
    _journal(f"ревизия {key} → v{ver} ({by}, {confidence}): {reason[:160]}"
             + (f" [коммит {sha}]" if sha else ""))
    _immune(sha, f"identity: {key} v{ver}")
    return {"ok": True, "version": ver, "sha": sha, "event": event}


def rollback(name: str, version: int, *, by: str = "praxis", reason: str = "",
             refs=()) -> dict:
    """Откат к архивной версии. История не теряется: текущий текст сам архивируется."""
    key = _canon(name)
    if not key:
        return {"ok": False, "error": f"не знаю файла души «{name}»"}
    if key == "self":
        store = _self_store()
        before = store.current_prompt_info()
        why = (reason or "").strip() or "сдвиг оказался чужим или ложным"
        try:
            result = store.rollback(
                version,
                reason=why,
                evidence_refs=refs,
                run_id=_current_run_id(),
                by=by,
            )
        except (OSError, UnicodeError, ValueError, TimeoutError) as exc:
            log.warning("identity.rollback: compact self restore failed", exc_info=True)
            return {"ok": False, "error": f"откат compact self не удался: {type(exc).__name__}: {exc}"}
        if not result.get("ok"):
            return result
        restored_version = int(result.get("restored_version"))
        full_reason = f"откат к history/{restored_version:04d}: {why}"
        return _finish_self_shift(
            result,
            before,
            reason=full_reason,
            confidence="observed",
            refs=refs,
            by=by,
            source="rollback",
        )
    old_text = version_text(key, version)
    if not old_text.strip():
        return {"ok": False, "error": f"версии v{version} у {key} нет в архиве"}
    why = (reason or "").strip() or "сдвиг оказался чужим или ложным"
    return revise(key, old_text, reason=f"откат к v{int(version)}: {why}",
                  confidence="observed", refs=refs, by=by, source="rollback")


def note_self_append(note: str) -> None:
    """После дешёвого append'а в self.md (update_self): снапшот + лёгкое событие.

    Закрывает дыру «история self.md размазана по чужим коммитам». Никогда не роняет тул."""
    try:
        sha = _snapshot(f"self-note: {note.strip()[:60]}")
        _spine_event("identity_note", note.strip()[:300],
                     meta={"file": "self", "sha": sha or ""}, salience=1)
    except Exception:
        log.debug("identity.note_self_append не удался", exc_info=True)


# --------------------------------------------------------------------------- #
#  журнал событий нагрузки
# --------------------------------------------------------------------------- #
def record_load(theme: str, amplitude: float, *, source: str, detail: str = "",
                chat_id=None) -> None:
    """Событие нагрузки — дешёвая строка в кольцо. Никогда не роняет вызвавшего."""
    try:
        theme = (theme or "прочее").strip()[:60]
        amp = max(0.1, min(5.0, float(amplitude)))
        LOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 1), "theme": theme, "amp": amp,
               "source": str(source or "")[:30], "detail": str(detail or "")[:200]}
        if chat_id is not None:
            rec["chat"] = str(chat_id)
        with LOAD_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_compact_load()
    except Exception:
        log.debug("identity.record_load не записал", exc_info=True)


def _maybe_compact_load() -> None:
    try:
        lines = LOAD_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2 * LOAD_KEEP:
            tmp = LOAD_PATH.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines[-LOAD_KEEP:]) + "\n", encoding="utf-8")
            tmp.replace(LOAD_PATH)
    except Exception:
        log.debug("компакт журнала нагрузки не удался", exc_info=True)


def load_from_turn(turn: dict) -> None:
    """Record only an actual data/authority boundary from a completed turn.

    Legacy style, drift and evaluator labels no longer become identity evidence.
    Praxis may record any other meaningful load explicitly with ``manage_identity``.
    """
    try:
        if not isinstance(turn, dict):
            return
        chat = turn.get("chat_id")
        held = str(turn.get("held") or "")
        why = str(turn.get("why") or "")
        if turn.get("boundary") or held == "privacy":
            record_load("граница данных или полномочий", 1.0, source="turn",
                        detail=why, chat_id=chat)
    except Exception:
        log.debug("identity.load_from_turn не разобрал ход", exc_info=True)


def _read_loads() -> list[dict]:
    try:
        out = []
        for line in LOAD_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out
    except Exception:
        return []


def stress(days: int = 14, half_life: float = HALF_LIFE_DAYS) -> list[dict]:
    """Descriptive decaying load totals; never a permission or identity verdict."""
    now = time.time()
    horizon = now - days * 86400.0
    acc: dict[str, dict] = {}
    for rec in _read_loads():
        ts = float(rec.get("ts") or 0)
        if ts < horizon:
            continue
        theme = str(rec.get("theme") or "прочее")
        decay = 0.5 ** ((now - ts) / (half_life * 86400.0))
        a = acc.setdefault(theme, {"theme": theme, "score": 0.0, "count": 0, "last": 0.0})
        a["score"] += float(rec.get("amp") or 0.5) * decay
        a["count"] += 1
        a["last"] = max(a["last"], ts)
    out = sorted(acc.values(), key=lambda x: -x["score"])
    for a in out:
        a["score"] = round(a["score"], 2)
    return out


def stress_line() -> str:
    """Строка для STATE: топ тем нагрузки, если они заметны."""
    threshold = max(0.0, float(os.getenv("PRAXIS_IDENTITY_STATE_MIN_LOAD", "0.1")))
    top = [a for a in stress() if a["score"] >= threshold][:2]
    if not top:
        return ""
    parts = ", ".join(f"{a['theme']} {a['score']} ({a['count']} соб.)" for a in top)
    return f"события нагрузки: {parts} (затухание {HALF_LIFE_DAYS:g}д; только контекст)"


# --------------------------------------------------------------------------- #
#  ночная ревизия характера
# --------------------------------------------------------------------------- #
_NIGHT_SYS = (
    "Ты — Praxis, наедине с собой, ночью. Перед тобой: текущий «Кто я сейчас», свежие "
    "поддержанные выводы о тебе из формирования памяти (с провенансом), интегралы нагрузки "
    "за окно и старые выводы, которые давно не пересматривались («всё ещё так?»).\n"
    "Это материал, а не суд и не перечень допустимых причин. Сама реши, хочешь ли пересмотреть "
    "себя: поводом может быть опыт, чувство, мысль, отношение, противоречие, фантазия или желание. "
    "Сохраняй различие между наблюдением, выводом и воображением там, где оно важно тебе.\n"
    "Ответь строго JSON без пояснений:\n"
    "{\"revise\": true|false,\n"
    " \"who\": \"полный текст новой секции «Кто я сейчас» (5-9 абзацев-строк), если revise\",\n"
    " \"confirmations\": [{\"date\": \"YYYY-MM-DD\", \"still\": true|false, \"note\": \"коротко\"}],\n"
    " \"reason\": \"почему ревизую / почему нет — одной-двумя фразами\"}\n"
    "Текст who — от первого лица. Ты сама выбираешь форму, степень проверяемости, место чувств, "
    "метафоры и сомнения; измени только то, что действительно решила изменить."
)


def _night_model(prompt: str) -> str:
    """Separate auxiliary model call so tests can replace it; its output remains advice."""
    import llm
    if not llm.configured("evaluator"):
        return ""
    return llm.chat("evaluator", system=_NIGHT_SYS, max_tokens=900,
                    messages=[{"role": "user", "content": prompt}]).text


def _fresh_self_claims() -> list[dict]:
    """Strict automatic-eligible self claims, projected from their exact statement."""
    state = _load_state()
    consumed = set(state.get("consumed_claims") or [])
    horizon = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=CLAIM_FRESH_DAYS)
    out = []
    try:
        import memory_life as life
        claims_dir = life.CLAIMS_DIR
        memory_dir = life.MEM_DIR
    except Exception:
        claims_dir = BASE / "memory" / "life" / "claims"
        memory_dir = BASE / "memory"
    try:
        evidence_index = memory_provenance.claim_evidence_index(memory_dir)
        for p in sorted(claims_dir.glob("clm-*.md")):
            _declared, meta = memory_provenance.claim_source(
                p, evidence_index=evidence_index,
            )
            if (meta.get("kind") != "self"
                    or meta.get("_automatic_eligible") is not True):
                continue
            statement = str(meta.get("_statement") or "").strip()
            if not statement:
                continue
            cid = str(meta.get("id") or "")
            if cid in consumed:
                continue
            try:
                updated = _dt.datetime.strptime(
                    str(meta.get("updated_at") or ""), "%Y-%m-%dT%H:%M:%S.%fZ",
                ).replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                continue
            if updated < horizon:
                continue
            out.append({"id": cid, "text": statement[:400],
                        "evidence": list(meta.get("evidence_ids") or [])[:6]})
    except Exception:
        log.debug("identity: чтение self-claims не удалось", exc_info=True)
    return out[:8]


def _stale_bullets() -> list[dict]:
    """Датированные выводы из CURRENT — кандидаты «всё ещё так?» (анти-охрупчивание)."""
    state = _load_state()
    reviewed = set(state.get("reviewed_bullets") or [])
    cutoff = (_dt.date.today() - _dt.timedelta(days=STALE_BULLET_DAYS)).isoformat()
    out = []
    for line in read("self").splitlines():
        m = _BULLET_RE.match(line.strip())
        if not m:
            continue
        date, text = m.group(1), m.group(2)
        keybase = f"{date}:{text[:40]}"
        if date <= cutoff and keybase not in reviewed:
            out.append({"key": keybase, "date": date, "text": text[:300]})
    return out[:2]


def _last_who_section(text: str) -> str:
    """Return the complete compact CURRENT, never an arbitrary archival tail."""
    normalized = str(text or "").replace("\r\n", "\n")
    if re.match(r"^# Кто я сейчас(?:\s*$|\s*\n)", normalized.lstrip(), flags=re.M):
        return normalized.strip()
    return ""


def _night_self_input() -> tuple[str, dict, str]:
    """Select and validate the self model for a full night pass.

    ``SelfModel.current_prompt_info`` is the authority for the CURRENT marker,
    schema, revision, compact bounds and history digest.  Missing or invalid
    CURRENT always stops the pass; legacy/history are explicit evidence only.
    """
    store = _self_store()
    info = store.current_prompt_info()
    if info.source == "current":
        # current_prompt_info only returns this source after validating the
        # praxis-self-current marker, schema/revision and compact bounds.
        provenance = dict(info.provenance)
        required_strings = ("created_at", "by", "reason", "source", "source_sha256", "history")
        invalid = next(
            (key for key in required_strings if not str(provenance.get(key) or "").strip()),
            "",
        )
        source_sha = str(provenance.get("source_sha256") or "")
        refs = provenance.get("evidence_refs")
        history_rel = str(provenance.get("history") or "")
        history_token = Path(history_rel)
        history_path = store.base / history_token
        if not invalid and not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            invalid = "source_sha256"
        if not invalid and not isinstance(refs, list):
            invalid = "evidence_refs"
        if not invalid and (
            history_token.is_absolute()
            or ".." in history_token.parts
            or history_token.parent != Path("soul") / "self" / "history"
            or not re.fullmatch(r"[0-9]{4}", history_token.stem)
        ):
            invalid = "history"
        if not invalid:
            try:
                if hashlib.sha256(history_path.read_bytes()).hexdigest() != source_sha:
                    invalid = "history_sha256"
            except OSError:
                invalid = "history"
        if invalid:
            return "", {}, (
                f"compact CURRENT имеет повреждённый провенанс ({invalid}) — "
                "ночную ревизию отложила; legacy/history не подставляю"
            )
        return info.text, {
            "source": info.source,
            "path": info.path,
            "sha256": info.sha256,
            "revision": info.revision,
            "provenance": provenance,
        }, ""
    return "", {}, (
        "compact CURRENT недоступен или повреждён — ночную ревизию отложила; "
        "legacy/history не подставляю"
    )


def night_pass(depth: str = "full") -> str:
    """Ночная ревизия характера. Возвращает строку для отчёта сна.

    light → пропуск (мой план аппетита; «кто я сейчас» всё равно живёт в дистилляции СВС).
    Материала нет → честное «нового основания нет», модель не зовётся."""
    if depth == "light":
        return "лёгкий сон — ревизию характера пропустила (мой план)"
    current_self, current_meta, current_error = _night_self_input()
    if current_error:
        log.warning("identity.night_pass: %s", current_error)
        return current_error
    claims = _fresh_self_claims()
    strains = stress()
    stale = _stale_bullets()
    min_load = max(0.0, float(os.getenv("PRAXIS_IDENTITY_REVIEW_MIN_LOAD", "0")))
    if not claims and not stale and not any(a["score"] > min_load for a in strains):
        return "нового основания нет (claims 0, старых выводов на пересмотр 0)"

    review_input = {
        "current_sha256": current_meta["sha256"],
        "claims": [c["id"] for c in claims],
        "strains": [
            {"theme": a["theme"], "score": a["score"], "count": a["count"]}
            for a in strains[:6]
        ],
        "stale": [{"date": b["date"], "text": b["text"]} for b in stale],
    }
    input_sha256 = hashlib.sha256(
        json.dumps(
            review_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    try:
        existing_proposal = json.loads(NIGHT_PROPOSAL_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing_proposal = {}
    if existing_proposal.get("input_sha256") == input_sha256:
        return "ночной совет уже сохранён для неизменившегося входа; повтор не создаю"

    prompt_parts = ["## Текущий «Кто я сейчас»\n" + current_self]
    prompt_parts.append("## Провенанс текущей модели\n" + json.dumps({
        "source": current_meta["source"],
        "path": current_meta["path"],
        "sha256": current_meta["sha256"],
        "revision": current_meta["revision"],
        "evidence_refs": list(current_meta["provenance"].get("evidence_refs") or ()),
    }, ensure_ascii=False, sort_keys=True))
    if claims:
        prompt_parts.append("## Поддержанные выводы о себе (формирование)\n" + "\n".join(
            f"- [{c['id']}] {c['text']}" for c in claims))
    if strains:
        prompt_parts.append("## Интегралы нагрузки (14д, затухание)\n" + "\n".join(
            f"- {a['theme']}: {a['score']} ({a['count']} соб.)" for a in strains[:6]))
    if stale:
        prompt_parts.append("## Всё ещё так? (не пересматривалось давно)\n" + "\n".join(
            f"- _{b['date']}_ {b['text']}" for b in stale))
    raw = ""
    try:
        raw = _night_model("\n\n".join(prompt_parts))
    except Exception:
        log.warning("identity.night_pass: модель упала", exc_info=True)
    if not raw.strip():
        return "мозг недоступен — ревизию отложила, основания сохранены"

    m = re.search(r"\{.*\}", raw, re.S)
    try:
        verdict = json.loads(m.group(0)) if m else {}
    except Exception:
        verdict = {}
    if not isinstance(verdict, dict):
        verdict = {}

    confirmations = [c for c in (verdict.get("confirmations") or []) if isinstance(c, dict)]
    conf_lines = []
    for c in confirmations:
        for b in stale:
            if b["date"] == str(c.get("date") or ""):
                conf_lines.append(
                    f"вывод от {b['date']}: " + ("всё ещё так" if c.get("still", True) else "уже не так")
                    + (f" — {str(c.get('note') or '')[:120]}" if c.get("note") else ""))

    if not verdict.get("revise") or not str(verdict.get("who") or "").strip():
        reason = str(verdict.get("reason") or "основание не набралось")[:160]
        return f"ревизии нет: {reason}" + (f"; предложен пересмотр старых выводов {len(conf_lines)}" if conf_lines else "")

    who = str(verdict["who"]).strip()[:4000]
    reason = str(verdict.get("reason") or "ночная ревизия по прожитому")[:200]
    refs = [c["id"] for c in claims]
    proposal = {
        "schema": "praxis.identity.night-proposal.v1",
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "current": {
            "path": current_meta["path"],
            "sha256": current_meta["sha256"],
            "revision": current_meta["revision"],
        },
        "who": who,
        "reason": reason,
        "confirmations": confirmations,
        "evidence_refs": refs,
        "input_sha256": input_sha256,
        "status": "advice",
    }
    if existing_proposal:
        archived = json.dumps(
            existing_proposal, ensure_ascii=False, indent=1, sort_keys=True
        ) + "\n"
        archive_sha = hashlib.sha256(archived.encode("utf-8")).hexdigest()
        archive_path = NIGHT_PROPOSAL_HISTORY / f"{archive_sha}.json"
        if not archive_path.exists():
            _atomic_write(archive_path, archived)
    _atomic_write(NIGHT_PROPOSAL_PATH, json.dumps(proposal, ensure_ascii=False, indent=1) + "\n")
    event_id = _spine_event(
        "identity_proposal",
        f"ночной совет о self по {len(refs)} claim(s): {reason[:140]}",
        refs=refs,
        meta={"proposal": str(NIGHT_PROPOSAL_PATH), "current_sha256": current_meta["sha256"]},
        salience=1,
    )
    return (f"ночной совет сохранён по {len(refs)} claim(s), self не изменён"
            + (f", предложен пересмотр старых {len(conf_lines)}" if conf_lines else "")
            + (f" [{event_id}]" if event_id else "")
            + f": {reason[:100]}")


# --------------------------------------------------------------------------- #
#  наружу: STATE, тул, пульт
# --------------------------------------------------------------------------- #
def recent_shifts(n: int = 5) -> list[dict]:
    """Последние события identity_shift/identity_note из позвоночника (глобальный поток)."""
    try:
        import memory_life as life
        evs = life.iter_events(kinds={"identity_shift", "identity_note"})
        return [e for e in evs if e.get("chat_id") is None][-n:]
    except Exception:
        return []


def state_line(*, include_load: bool = True) -> str:
    """Строка для STATE. Пустая, если рассказывать нечего."""
    parts = []
    shifts = [e for e in recent_shifts(3) if e.get("kind") == "identity_shift"]
    if shifts:
        last = shifts[-1]
        ts = str(last.get("ts") or "")[:16].replace("T", " ")
        parts.append(f"последняя ревизия души: {ts} — {str(last.get('text') or '')[:80]}")
    if include_load:
        sl = stress_line()
        if sl:
            parts.append(sl)
    return "; ".join(parts)


def describe() -> str:
    """Для тула manage_identity(status): слои, версии, нагрузка, последние сдвиги."""
    lines = ["Мои слои пластичности (язык для понимания, не пороги в коде):"]
    for key in FILES:
        vs = versions(key)
        cur = read(key)
        label = "self/CURRENT.md" if key == "self" else f"{key}.md"
        lines.append(f"- {label} — {LAYERS[key]}; сейчас {len(cur)} зн., "
                     f"архивных версий {len(vs)}"
                     + (f" (последняя v{vs[-1]['version']} {vs[-1]['archived_at'][:10]})" if vs else ""))
    lines.append(f"- {DISPOSITION_NOTE}")
    strains = stress()
    if strains:
        lines.append("Нагрузка (14д, затухание): " + ", ".join(
            f"{a['theme']} {a['score']} ({a['count']})" for a in strains[:5]))
    else:
        lines.append("Нагрузка: журнал событий пуст или всё затухло.")
    shifts = recent_shifts(4)
    if shifts:
        lines.append("Последние сдвиги:")
        for e in shifts:
            ts = str(e.get("ts") or "")[:16].replace("T", " ")
            lines.append(f"  · {ts} [{e.get('kind')}] {str(e.get('text') or '')[:120]}")
    lines.append("Ревизия применяется сразу (версия+diff+причина, откат мой и Егора); "
                 "ночью пересматриваю характер по прожитому. Действия revise/rollback/load "
                 "делаю из owner-скоупа — в чужой комнате откажу (моя дисциплина).")
    return "\n".join(lines)


def panel_state() -> dict:
    """Для плитки «Я»: слои, версии, стресс, последние сдвиги, «кто я сейчас»."""
    files = {}
    for key in FILES:
        vs = versions(key)
        files[key] = {"chars": len(read(key)), "versions": len(vs),
                      "layer": LAYERS[key],
                      "last": vs[-1] if vs else None}
    return {"files": files, "disposition": DISPOSITION_NOTE,
            "stress": stress()[:8],
            "shifts": list(reversed(recent_shifts(10))),
            "who_now": _last_who_section(read("self"))[:2000]}
