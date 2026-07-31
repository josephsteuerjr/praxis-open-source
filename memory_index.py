"""
Praxis — hybrid recall по каноническому Markdown (PASS 19).

Принцип: Markdown/JSONL — источник правды; `.vectors/index.json` — полностью
пересобираемый кэш. Retrieval объединяет полнотекст, опциональные hosted embeddings,
hosted semantic rerank явного tool-recall, свежесть, значимость, надёжность и
противоречия. Старый Ollama-путь оставлен только для обратной совместимости тестов/
локальных инсталляций; live Praxis его не вызывает.

CLI:
    python -m memory_index build
    python -m memory_index search "запрос"
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import memory_provenance

log = logging.getLogger("praxis-mem")

# --------------------------------------------------------------------------- #
#  Пути и конфиг
# --------------------------------------------------------------------------- #

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
SOUL_DIR = BASE / "soul"
SKILLS_DIR = SOUL_DIR / "skills"
PEOPLE_DIR = MEM_DIR / "people"
ROOMS_DIR = MEM_DIR / "rooms"
VECTORS_DIR = MEM_DIR / ".vectors"
INDEX_JSON = VECTORS_DIR / "index.json"
INDEX_MD = MEM_DIR / "INDEX.md"

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "30"))
HOSTED_EMBED_BASE_URL = os.getenv("PRAXIS_EMBED_BASE_URL", "").rstrip("/")
HOSTED_EMBED_API_KEY = os.getenv("PRAXIS_EMBED_API_KEY", "")
HOSTED_EMBED_MODEL = os.getenv("PRAXIS_EMBED_MODEL", "text-embedding-3-small")
_RERANK_CACHE: dict[str, tuple[float, list[float]]] = {}


def _embeddings_on() -> bool:
    """Любой реальный embedding adapter. ``1`` — legacy Ollama для совместимости."""
    return embedding_mode() in ("legacy-ollama", "hosted-embeddings")


def embedding_mode() -> str:
    raw = os.getenv("PRAXIS_EMBEDDINGS", "0").strip().lower()
    if raw in ("hosted", "openai", "remote"):
        return "hosted-embeddings"
    if raw in ("1", "true", "yes", "on", "ollama"):
        return "legacy-ollama"
    return "off"


def retrieval_mode() -> str:
    if embedding_mode() == "hosted-embeddings":
        return "hybrid-hosted-embeddings"
    if embedding_mode() == "legacy-ollama":
        return "hybrid-legacy-ollama"
    try:
        import llm
        if os.getenv("PRAXIS_SEMANTIC_RERANK", "1").lower() in ("1", "true", "yes", "on") \
                and llm.configured("evaluator"):
            return "hybrid-hosted-rerank"
    except Exception:
        pass
    return "hybrid-fulltext"


# --------------------------------------------------------------------------- #
#  Утилиты файлов и чанкинг
# --------------------------------------------------------------------------- #

def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _rel(path: Path) -> str:
    """Стабильный id-префикс источника — путь относительно BASE."""
    p = Path(path).resolve()
    try:
        return p.relative_to(BASE).as_posix()
    except ValueError:
        return p.as_posix()


def _iter_source_files() -> list[Path]:
    """Canonical Markdown used by the optional vector cache.

    Generated navigation and materialized projections must never become a second
    recall corpus.  Reuse :mod:`memory_fts`'s explicit source registry so SQLite
    FTS and the optional vector cache cannot silently disagree about canon.
    JSONL and inventory JSON stay searchable through FTS; this legacy cache embeds
    canonical Markdown only.
    """
    import memory_fts

    sources = memory_fts.iter_sources(base=BASE, memory_dir=MEM_DIR, skills_dir=SKILLS_DIR)
    files = [source.path for source in sources if source.kind in {
        "markdown", "skill", "self_history", "journal_episode", "journal_reflection",
    } or source.kind in memory_provenance.CLAIM_KINDS]
    return sorted(set(files), key=lambda p: _rel(p))


def _vector_source_type(path: str | Path) -> str:
    """Restore trust metadata even for vector records created by older builds."""
    rel = _rel(path) if isinstance(path, Path) else str(path or "").replace("\\", "/")
    episodic = memory_provenance.episodic_kind(rel)
    if episodic:
        return episodic
    if memory_provenance.is_archived_self(rel):
        return memory_provenance.ARCHIVED_SELF_KIND
    if rel.startswith("memory/life/claims/"):
        target = Path(path) if isinstance(path, Path) else BASE / rel
        return memory_provenance.claim_source(target)[0]
    if rel.startswith("soul/skills/"):
        return "skill"
    return "markdown"


def _chunks(text: str) -> list[str]:
    """Чанк = пункт списка (факт) или абзац. Заголовки — границы, не чанки."""
    chunks: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if block:
            joined = " ".join(l.strip() for l in block).strip()
            if joined:
                chunks.append(joined)
            block.clear()

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            flush()
            continue
        if s.startswith("#"):            # заголовок — граница раздела
            flush()
            continue
        if re.match(r"^[-*]\s+", s):      # пункт списка — самостоятельный факт
            flush()
            item = re.sub(r"^[-*]\s+", "", s).strip()
            if item:
                chunks.append(item)
            continue
        block.append(s)
    flush()
    return [c for c in chunks if len(c) >= 3]


# --------------------------------------------------------------------------- #
#  Эмбеддинги (hosted OpenAI-compatible; legacy Ollama only for compatibility)
# --------------------------------------------------------------------------- #

def embed(text: str) -> list[float]:
    """Один вектор. Бросает исключение: caller честно деградирует в full-text/rerank."""
    mode = embedding_mode()
    if mode == "hosted-embeddings":
        if not HOSTED_EMBED_BASE_URL or not HOSTED_EMBED_API_KEY:
            raise RuntimeError("hosted embeddings выбраны, но endpoint/key не настроены")
        url = HOSTED_EMBED_BASE_URL
        if not url.endswith("/embeddings"):
            url = url.rstrip("/") + "/embeddings"
        body = json.dumps({"model": HOSTED_EMBED_MODEL, "input": [text or ""]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {HOSTED_EMBED_API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("data") or []
        vec = (rows[0] if rows else {}).get("embedding") or []
        if not vec:
            raise ValueError("hosted endpoint вернул пустой эмбеддинг")
        return [float(x) for x in vec]
    if mode != "legacy-ollama":
        raise RuntimeError("embeddings выключены")
    body = json.dumps({"model": EMBED_MODEL, "prompt": text or ""}).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding") or []
    if not vec:
        raise ValueError("Ollama вернула пустой эмбеддинг")
    return [float(x) for x in vec]


def _index_model() -> str:
    return HOSTED_EMBED_MODEL if embedding_mode() == "hosted-embeddings" else EMBED_MODEL


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return -1.0
    dot = sa = sb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        sa += x * x
        sb += y * y
    if sa == 0.0 or sb == 0.0:
        return -1.0
    return dot / math.sqrt(sa * sb)


# --------------------------------------------------------------------------- #
#  Хранилище индекса (.vectors/index.json) с кэшем по mtime
# --------------------------------------------------------------------------- #

_CACHE: dict[str, object] = {"mtime": None, "index": None}


def _load() -> dict:
    if not INDEX_JSON.exists():
        return {}
    mt = INDEX_JSON.stat().st_mtime
    if _CACHE["mtime"] == mt and _CACHE["index"] is not None:
        return _CACHE["index"]  # type: ignore[return-value]
    try:
        idx = json.loads(_read(INDEX_JSON)) or {}
    except Exception:
        idx = {}
    _CACHE["mtime"] = mt
    _CACHE["index"] = idx
    return idx


def _save(index: dict) -> None:
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_JSON.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    _CACHE["mtime"] = INDEX_JSON.stat().st_mtime
    _CACHE["index"] = index


def _current_files() -> dict[str, float]:
    return {_rel(p): p.stat().st_mtime for p in _iter_source_files()}


def _is_stale(index: dict) -> bool:
    if not index or index.get("model") != _index_model() or "records" not in index:
        return True
    stored = index.get("files") or {}
    current = _current_files()
    if set(stored) != set(current):
        return True
    for k, v in current.items():
        if abs(float(stored.get(k, -1.0)) - v) > 1e-6:
            return True
    return False


# --------------------------------------------------------------------------- #
#  build / upsert
# --------------------------------------------------------------------------- #

def build() -> dict:
    """Перечитать весь корпус, посчитать эмбеддинги, сохранить индекс.

    Транзакционно: если Ollama недоступна, embed() бросит исключение и индекс
    на диске не будет испорчен частичной/пустой записью.
    """
    fts = {}
    try:
        import memory_fts
        fts = memory_fts.rebuild(base=BASE, memory_dir=MEM_DIR, skills_dir=SKILLS_DIR)
    except Exception as exc:
        # Markdown remains canonical and the legacy in-process scanner below is a complete
        # fallback.  A broken disposable DB must never break memory continuity.
        log.warning("memory FTS rebuild не удался: %s", exc)
    if not _embeddings_on():
        return {"model": _index_model(), "files": {}, "records": [], "fts": fts}
    records: list[dict] = []
    for path in _iter_source_files():
        src = _rel(path)
        for i, chunk in enumerate(_chunks(_read(path))):
            vec = embed(chunk)  # бросит при недоступной Ollama -> build прерывается
            records.append({"id": f"{src}#{i}", "text": chunk, "source": path.stem,
                            "path": src, "source_type": _vector_source_type(src),
                            "vector": vec})
    index = {"model": _index_model(), "files": _current_files(), "records": records,
             "fts": fts}
    _save(index)
    log.info("memory_index.build: %d файлов, %d чанков", len(index["files"]), len(records))
    return index


def upsert(path: str | Path) -> None:
    """Пересчитать чанки одного файла. Вызывать после каждой записи памяти.

    Тихо ничего не делает, если Ollama недоступна (старый индекс не трогаем).
    """
    p = Path(path).resolve()
    try:
        import memory_fts
        memory_fts.upsert(p, base=BASE, memory_dir=MEM_DIR, skills_dir=SKILLS_DIR)
    except Exception:
        log.debug("memory FTS upsert не удался для %s", p, exc_info=True)
    if not _embeddings_on():
        return  # embeddings off: FTS is already refreshed; do not touch Ollama
    src = _rel(p)
    eligible = p in {source.resolve() for source in _iter_source_files()}
    if not eligible:
        # Generated/deleted files are not vector canon either.  Remove a legacy
        # incremental entry if one exists, but never embed a projection merely
        # because its writer called the compatibility hook.
        index = _load()
        if not index:
            return
        records = index.get("records") or []
        filtered = [
            row for row in records
            if str(row.get("path") or "") != src
            and not str(row.get("id") or "").startswith(f"{src}#")
        ]
        files = index.setdefault("files", {})
        changed = len(filtered) != len(records) or src in files
        index["records"] = filtered
        files.pop(src, None)
        if changed:
            _save(index)
        return
    index = _load()
    if _is_stale(index):
        # базового индекса нет/устарел — попробуем построить целиком
        try:
            build()
        except Exception:
            pass
        return

    new_records: list[dict] = []
    if p.exists():
        try:
            for i, chunk in enumerate(_chunks(_read(p))):
                vec = embed(chunk)
                new_records.append({"id": f"{src}#{i}", "text": chunk, "source": p.stem,
                                    "path": src, "source_type": _vector_source_type(src),
                                    "vector": vec})
        except Exception:
            return  # Ollama недоступна — оставляем индекс как есть

    # транзакционная замена записей этого источника
    index["records"] = [r for r in index.get("records", []) if not str(r.get("id", "")).startswith(f"{src}#")]
    index["records"].extend(new_records)
    files = index.setdefault("files", {})
    if p.exists():
        files[src] = p.stat().st_mtime
    else:
        files.pop(src, None)
    _save(index)


def _ensure_index() -> dict:
    index = _load()
    if _is_stale(index):
        try:
            index = build()
        except Exception as e:
            log.warning("memory_index: build не удался (%s), работаю по старому/без индекса", e)
    return index


# --------------------------------------------------------------------------- #
#  Hybrid search: full-text + embeddings/rerank + provenance
# --------------------------------------------------------------------------- #

_JOURNAL_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCOPE_DROP_STEMS = {"reflections", "INDEX"}
_DATE_RE = re.compile(r"_\((\d{4}-\d{2}-\d{2})\)_")
_WORD_RE = re.compile(r"[\wа-яё]+", re.I)
_RU_ENDINGS = ("иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ая", "яя",
               "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ам", "ям", "ах", "ях",
               "ов", "ев", "ом", "ем", "ую", "юю", "а", "я", "ы", "и", "у", "ю", "е", "о")


def _scope_allows(source: str, text: str, scope: str, path: str = "") -> bool:
    """Optional audience-safe projection for external/legacy index callers.

    Praxis's own ``agent.tool_recall`` intentionally requests owner/full scope in every
    room; disclosure is checked later at the outbound boundary.
    """
    if scope == "owner":
        return True
    if re.search(r"\[private\]", text or "", re.I):
        return False
    rel = (path or "").replace("\\", "/")
    if rel.startswith("memory/life/") or rel.startswith("memory/journal/") \
            or rel.startswith("memory/dialogues/") or rel.startswith("memory/rooms/"):
        return False
    src = source or ""
    if _JOURNAL_STEM.match(src) or src in _SCOPE_DROP_STEMS or src.lstrip("-").isdigit():
        return False
    return True


def _stem(token: str) -> str:
    t = token.casefold().replace("ё", "е")
    if len(t) <= 4:
        return t
    for ending in _RU_ENDINGS:
        if len(t) - len(ending) >= 4 and t.endswith(ending):
            return t[:-len(ending)]
    return t


def _terms(text: str) -> list[str]:
    return [_stem(x) for x in _WORD_RE.findall(str(text or "")) if len(x) > 1]


def _trigrams(text: str) -> set[str]:
    s = " ".join(_terms(text))
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def _artifact_header(path: str) -> str:
    if not path:
        return ""
    try:
        return (BASE / path).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return ""


def _salience(text: str, path: str = "") -> int:
    material = f"{_artifact_header(path)}\n{text or ''}"
    m = re.search(r"\(s([123])\)", material, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'"salience"\s*:\s*([123])', material)
    return int(m.group(1)) if m else 2


def _recency(text: str, path: str = "") -> float:
    m = _DATE_RE.search(text or "")
    date = None
    if m:
        try:
            date = _dt.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    if date is None:
        m2 = re.search(r"(20\d{2}-\d{2}-\d{2})", Path(path).name)
        if m2:
            try:
                date = _dt.date.fromisoformat(m2.group(1))
            except ValueError:
                pass
    if date is None:
        return 0.55
    age = max(0, (_dt.date.today() - date).days)
    return max(0.0, 1.0 - min(1.0, age / 730.0))


def _reliability(text: str, path: str) -> float:
    low = f"{_artifact_header(path)}\n{text or ''}".casefold()
    rel = (path or "").replace("\\", "/")
    if "legacy=true" in low or '"legacy":true' in low:
        return 0.45
    if "status: `supported`" in low or '"status":"supported"' in low:
        return 0.95
    if "гипотез" in low or "uncertain" in low or "contested" in low:
        return 0.45
    if rel.startswith("memory/people/"):
        return 0.85
    if rel.startswith("memory/life/compacts/"):
        return 0.72
    return 0.65


def _contradiction(text: str) -> float:
    low = (text or "").casefold()
    return 1.0 if any(x in low for x in ("contradict", "противореч", "contested", "оспорен")) else 0.0


def _all_chunks(scope: str, purpose: str = "explicit") -> list[dict]:
    if purpose == "automatic":
        # Only v5 FTS can emit canon-bound structured automatic projections.
        return []
    out = []
    for path in _iter_source_files():
        rel = _rel(path)
        for i, chunk in enumerate(_chunks(_read(path))):
            if not _scope_allows(path.stem, chunk, scope, rel):
                continue
            source_type = _vector_source_type(rel)
            out.append({"id": f"{rel}#{i}", "text": chunk, "source": path.stem,
                        "path": rel, "source_type": source_type})
    return out


def _fulltext_candidates(query: str, limit: int, scope: str,
                         purpose: str = "explicit") -> list[dict]:
    try:
        import memory_fts
        return memory_fts.search(query, base=BASE, memory_dir=MEM_DIR,
                                 skills_dir=SKILLS_DIR, limit=limit, scope=scope,
                                 purpose=purpose)
    except Exception:
        if purpose == "automatic":
            return []
        # Compatibility/failsafe for Python builds without FTS5, corrupt disposable DBs,
        # and tests deliberately exercising the old scanner.
        log.warning("memory FTS search не удался — сканирую canonical Markdown", exc_info=True)
    docs = _all_chunks(scope, purpose)
    qterms = _terms(query)
    if not docs or not qterms:
        return []
    counters = [Counter(_terms(d["text"])) for d in docs]
    df = Counter()
    for c in counters:
        df.update(c.keys())
    avg_len = sum(sum(c.values()) for c in counters) / max(1, len(counters))
    qtri = _trigrams(query)
    scored = []
    for doc, counts in zip(docs, counters):
        length = max(1, sum(counts.values()))
        bm25 = 0.0
        for term in set(qterms):
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1.0 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
            bm25 += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * length / max(1.0, avg_len)))
        dtri = _trigrams(doc["text"])
        tri = len(qtri & dtri) / max(1, len(qtri)) if qtri else 0.0
        raw = bm25 + 0.35 * tri
        if raw > 0:
            scored.append((raw, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]
    hi = top[0][0] if top else 1.0
    return [dict(doc, lexical=min(1.0, raw / max(hi, 1e-9))) for raw, doc in top]


_BROAD_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_BROAD_KEEP = 200  # держим с запасом, чтобы кэш не зависел от limit конкретного вызова


def _broad_ttl() -> float:
    """Сколько секунд общий пул считается свежим. 0 — кэш выключен."""
    if os.getenv("PRAXIS_TEST"):
        # Тесты меняют канон в пределах одного процесса; кэш сделал бы их лживыми.
        return 0.0
    try:
        return max(0.0, float(os.getenv("PRAXIS_BROAD_TTL", "3600")))
    except (TypeError, ValueError):
        return 3600.0


def _broad_disk_path() -> Path:
    return MEM_DIR / ".state" / "broad_pool.json"


def _broad_from_disk(key: tuple[str, str], ttl: float, now: float) -> tuple[float, list[dict]] | None:
    """Пул, переживший рестарт.

    Кэш только в памяти лечил бы половину болезни: она перезапускает себя на каждом
    смёрженном proposal, а холодный пересчёт стоит ~450с (замер 31.07) — то есть первый
    же её «вспомни» после самоправки снова вставал бы на семь минут."""
    try:
        raw = json.loads(_broad_disk_path().read_text(encoding="utf-8"))
        if tuple(raw.get("key") or ()) != tuple(key):
            return None
        at = float(raw.get("at") or 0.0)
        if not at or now - at >= ttl:
            return None
        pool = raw.get("pool")
        if not isinstance(pool, list) or not pool:
            return None
        # Возраст возвращаем ИСХОДНЫЙ: если штамповать «сейчас», пул с диска выглядит
        # только что посчитанным, упреждающее обновление не срабатывает никогда, а
        # реальная несвежесть доходит до двух сроков вместо одного.
        return at, [dict(d) for d in pool]
    except Exception:
        return None  # нет файла/битый — просто считаем заново, это кэш, а не память


def _broad_to_disk(key: tuple[str, str], pool: list[dict], now: float) -> None:
    try:
        path = _broad_disk_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"key": list(key), "at": now, "pool": pool},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.debug("общий пул не сохранился на диск", exc_info=True)


_BROAD_REFRESH: object | None = None


def _broad_compute(scope: str, purpose: str = "explicit") -> list[dict]:
    """Собственно пересчёт пула — 453с на живом корпусе (замер 31.07). Зовётся ИЗ ФОНА."""
    docs = _all_chunks(scope, purpose)
    docs.sort(key=lambda d: (_reliability(d["text"], d["path"]), _salience(d["text"], d["path"]),
                             _recency(d["text"], d["path"])), reverse=True)
    top = [dict(d, lexical=0.0) for d in docs[:_BROAD_KEEP]]
    _broad_to_disk((scope, purpose), top, time.time())
    return top


def _broad_refresh_async(scope: str, purpose: str) -> None:
    """Пересчитать пул ОТДЕЛЬНЫМ процессом и не ждать его.

    Пересчёт стоит семь с половиной минут. В её процессе это означало бы, что она стоит
    посреди хода — и неважно, раз в час или раз в день: столько ждать нельзя вообще
    никогда. Отдельный процесс не держит GIL, поэтому её ход идёт своим темпом, а
    готовый пул попадает в общий файл и подхватывается следующим вызовом.

    Пока пул не готов, `_broad_candidates` отдаёт пусто — и это честно: общий пул это
    лишь СЕМЯ для семантического ранжира, а не часть ответа. Без него она отвечает по
    точным попаданиям (их даёт FTS, он мгновенный), просто без фонового «а ещё у меня
    вообще есть вот это». Хуже пустого пула только восьмиминутная тишина.
    """
    global _BROAD_REFRESH
    proc = _BROAD_REFRESH
    if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
        return  # уже считается — второй такой же процесс только отнимет ядро
    code = (
        "import memory_index as m; m._broad_compute(%r, %r)" % (str(scope), str(purpose))
    )
    try:
        _BROAD_REFRESH = subprocess.Popen(
            [sys.executable, "-c", code], cwd=str(BASE),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # переживёт её самоперезапуск, а осиротев — будет сжат bootguard
            preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None,
        )
        log.info("общий пул recall пересчитывается в фоне (pid %s)", _BROAD_REFRESH.pid)
    except Exception:
        log.debug("фоновый пересчёт пула не запустился", exc_info=True)
        _BROAD_REFRESH = None


def _broad_candidates(scope: str, limit: int = 40,
                      purpose: str = "explicit") -> list[dict]:
    """Общий пул «что вообще есть важного в памяти» — семя для семантического ранжира.

    Пул НЕ зависит от запроса, но считался он заново на КАЖДЫЙ явный recall: полный
    обход источников, чтение и чанкование всего markdown-канона (~216к чанков) и
    сортировка тремя оценщиками. На живом проде это давало p50 ~490с на recall —
    она пропадала на восемь минут, когда честно шла вспоминать.

    Поэтому пул кэшируется на TTL. Запрос-зависимая часть (FTS-кандидаты) кэшем не
    тронута вовсе, так что свежесть самого ответа не страдает: устареть на минуты
    может только фоновое «что вообще важно», которое и меняется медленно.
    """
    key = (scope, purpose)
    ttl = _broad_ttl()
    if not ttl:
        # Тесты (PRAXIS_TEST) и явно выключенный кэш: считаем на месте, как раньше.
        return [dict(d) for d in _broad_compute(scope, purpose)[:limit]]

    now = time.time()
    hit = _BROAD_CACHE.get(key)
    if hit is None:
        stored = _broad_from_disk(key, ttl, now)
        if stored:
            hit = stored  # (когда посчитан, пул) — возраст берём с диска, не «сейчас»
            _BROAD_CACHE[key] = hit
    if hit and (now - hit[0]) < ttl:
        # Обновляемся ЗАРАНЕЕ, пока пул ещё годен: иначе на истечении срока она получила
        # бы пустое семя ровно в тот момент, когда пошла вспоминать.
        if (now - hit[0]) > ttl * 0.8:
            _broad_refresh_async(scope, purpose)
        return [dict(d) for d in hit[1][:limit]]

    _broad_refresh_async(scope, purpose)
    return []  # пул не готов — отвечаем без семени, но НЕ ждём семь минут


def _vector_candidates(query: str, limit: int, scope: str,
                       purpose: str = "explicit") -> list[dict]:
    if purpose == "automatic":
        # The legacy vector cache has no canonical structured-chunk locator.
        return []
    if not _embeddings_on():
        return []
    try:
        qv = embed(query)
        index = _ensure_index()
    except Exception:
        return []
    scored = []
    for r in (index or {}).get("records") or []:
        path = str(r.get("path") or str(r.get("id", "")).split("#", 1)[0])
        if not r.get("vector") or not _scope_allows(r.get("source", ""), r.get("text", ""), scope, path):
            continue
        # Trust classification comes from the canonical path/current claim metadata,
        # never from a legacy cache row that may have called every Markdown file safe.
        source_type = _vector_source_type(path)
        scored.append((_cosine(qv, r["vector"]), dict(r, path=path, source_type=source_type)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [dict(r, semantic=max(0.0, min(1.0, score))) for score, r in scored[:limit]]


def _semantic_rerank(query: str, candidates: list[dict]) -> list[float]:
    if not candidates or os.getenv("PRAXIS_SEMANTIC_RERANK", "1").lower() not in ("1", "true", "yes", "on"):
        return [0.0] * len(candidates)
    fp = hashlib.sha256((query + "\0" + "\0".join(c["text"] for c in candidates)).encode("utf-8")).hexdigest()
    cached = _RERANK_CACHE.get(fp)
    if cached and time.time() - cached[0] < 86400:
        return list(cached[1])
    try:
        import llm
        if not llm.configured("evaluator"):
            return [0.0] * len(candidates)
        rows = "\n".join(f"[{i}] ({c['path']}) {c['text'][:1000]}" for i, c in enumerate(candidates))
        system = ("Rank memory passages by semantic usefulness for the query. Return STRICT JSON "
                  '{"scores":[{"i":0,"score":0.0}]}. Score 0..1. '
                  "Do not reward a passage merely for being old/deep; contradictions remain useful evidence.")
        resp = llm.chat("evaluator", system=system,
                        messages=[{"role": "user", "content": f"QUERY: {query}\n\n{rows}"}],
                        max_tokens=1500)
        m = re.search(r"\{.*\}", resp.text or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        scores = [0.0] * len(candidates)
        for item in data.get("scores") or []:
            try:
                i = int(item.get("i")); score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(scores):
                scores[i] = max(0.0, min(1.0, score))
        _RERANK_CACHE[fp] = (time.time(), scores)
        return scores
    except Exception:
        log.warning("semantic rerank не удался — остаюсь на full-text", exc_info=True)
        return [0.0] * len(candidates)


def _provenance(path: str) -> list[str]:
    p = BASE / path
    try:
        first = p.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        m = re.match(r"^<!--\s*praxis-(?:compact|episode|claim):\s*(\{.*\})\s*-->$", first)
        if m:
            meta = json.loads(m.group(1))
            return [str(x) for x in (meta.get("source_event_ids") or []) +
                    (meta.get("source_compact_ids") or []) + (meta.get("evidence_ids") or [])]
    except Exception:
        pass
    return []


def _adjust(cos: float, text: str) -> float:
    """Compatibility helper retained for callers/tests; truth confidence is handled separately."""
    score = cos
    low = text.lower()
    if "устарело" in low or "superseded" in low:
        score -= 0.25
    score -= float(os.getenv("PRAXIS_RECENCY_PENALTY", "0.08")) * (1.0 - _recency(text))
    return score


def search(query: str, k: int = 6, scope: str = "owner", semantic: bool = False,
           purpose: str = "explicit") -> list[dict]:
    """Hybrid top-k with visible ranking signals and provenance.

    ``semantic=True`` may spend one hosted evaluator call when no embedding index is configured;
    automatic prompt recall deliberately keeps it False.
    """
    query = (query or "").strip()
    if not query:
        return []
    purpose = "automatic" if purpose == "automatic" else "explicit"
    lexical = _fulltext_candidates(query, max(24, k * 5), scope, purpose)
    vectors = _vector_candidates(query, max(24, k * 5), scope, purpose)
    pool: dict[tuple[str, str], dict] = {}
    for item in lexical + vectors:
        key = (str(item.get("path") or ""), str(item.get("text") or ""))
        cur = pool.setdefault(key, {"text": item["text"], "source": item["source"],
                                    "path": item.get("path") or "", "lexical": 0.0,
                                    "semantic": 0.0,
                                    "source_type": item.get("source_type") or "markdown",
                                    "visibility": item.get("visibility") or "",
                                    "at": item.get("at") or "",
                                    "event_id": item.get("event_id") or "",
                                    "run_id": item.get("run_id") or "",
                                    "automatic_canonical": bool(item.get("automatic_canonical")),
                                    "refs": list(item.get("refs") or []),
                                    "supersedes": list(item.get("supersedes") or []),
                                    "provenance": list(item.get("provenance") or [])})
        cur["lexical"] = max(float(cur.get("lexical") or 0.0), float(item.get("lexical") or 0.0))
        cur["semantic"] = max(float(cur.get("semantic") or 0.0), float(item.get("semantic") or 0.0))
    if semantic and not vectors:
        for item in _broad_candidates(scope, 40, purpose):
            key = (item["path"], item["text"])
            pool.setdefault(key, {"text": item["text"], "source": item["source"],
                                  "path": item["path"], "lexical": 0.0, "semantic": 0.0,
                                  "source_type": item.get("source_type") or "markdown",
                                  "visibility": item.get("visibility") or "",
                                  "at": item.get("at") or "", "event_id": "", "run_id": "",
                                  "automatic_canonical": False,
                                  "refs": [], "supersedes": [], "provenance": []})
        candidates = list(pool.values())[:40]
        scores = _semantic_rerank(query, candidates)
        for item, score in zip(candidates, scores):
            item["semantic"] = max(float(item.get("semantic") or 0.0), score)
    ranked = []
    for item in pool.values():
        if purpose == "automatic":
            if (item.get("automatic_canonical") is not True
                    or not memory_provenance.automatic_recall_allowed(
                        source_type=item.get("source_type"), path=item.get("path"),
                        text=item.get("text"), memory_dir=MEM_DIR,
                    )):
                continue
        sal = _salience(item["text"], item["path"]); rec = _recency(item["text"], item["path"])
        rel = _reliability(item["text"], item["path"]); con = _contradiction(item["text"])
        # Contradictions are retrieval-worthy (small boost), but reliability remains visible separately.
        score = (0.46 * item["lexical"] + 0.42 * item["semantic"] +
                 0.04 * ((sal - 1) / 2) + 0.04 * rec + 0.04 * rel + 0.02 * con)
        if "устарело" in item["text"].casefold() or "superseded" in item["text"].casefold():
            score -= 0.12
        ranked.append((score, {"text": item["text"], "source": item["source"],
                               "path": item["path"], "score": round(score, 4),
                               "source_type": item.get("source_type") or "markdown",
                               "visibility": item.get("visibility") or "",
                               "at": item.get("at") or "",
                               "event_id": item.get("event_id") or "",
                               "run_id": item.get("run_id") or "",
                               "automatic_canonical": bool(item.get("automatic_canonical")),
                               "refs": list(item.get("refs") or []),
                               "supersedes": list(item.get("supersedes") or []),
                               "signals": {"fulltext": round(item["lexical"], 3),
                                           "semantic": round(item["semantic"], 3),
                                           "recency": round(rec, 3), "salience": sal,
                                           "reliability": round(rel, 3),
                                           "contradiction": bool(con)},
                               "provenance": (list(item.get("provenance") or [])
                                              or _provenance(item["path"]))}))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for score, item in ranked[:k] if score > 0.0]


def _keyword_search(query: str, k: int = 6, scope: str = "owner") -> list[dict]:
    """Compatibility name: now a real ranked full-text search, not line grep."""
    hits = _fulltext_candidates(query, k, scope, "explicit")
    return [{"text": h["text"], "source": h["source"], "path": h["path"],
             "score": round(float(h.get("lexical") or 0.0), 4),
             "signals": {"fulltext": round(float(h.get("lexical") or 0.0), 3)},
             "provenance": _provenance(h["path"])} for h in hits]


# --------------------------------------------------------------------------- #
#  INDEX.md — карта «что я знаю» (строка на человека/тему)
# --------------------------------------------------------------------------- #

_INDEX_HEADER = "# INDEX — карта памяти\n\n"


def _derive_hook(path: Path) -> str:
    """Однострочный хук: только sourced факт; legacy prose не становится картой."""
    title = ""
    first_fact = ""
    for raw in _read(path).splitlines():
        s = raw.strip()
        if s.startswith("# ") and not title:
            title = s[2:].strip()
        elif (re.match(r"^[-*]\s+", s) and not first_fact
              and re.search(r"\[source:[^\]]+\]\s*$", s, re.I)):
            f = re.sub(r"^[-*]\s+", "", s)
            f = re.sub(r"^\[(public|private)\]\s*", "", f, flags=re.I)
            f = re.sub(r"^\(s[123]\)\s*", "", f, flags=re.I)
            f = re.sub(r"\s*~~устарело~~.*$", "", f)
            f = re.sub(r"\s*\[source:[^\]]+\]\s*$", "", f, flags=re.I)
            f = re.sub(r"\s*_\(.*?\)_\s*$", "", f).strip()
            if f:
                first_fact = f
    if title and first_fact:
        hook = f"{title} — {first_fact}"
    else:
        hook = (title + " — legacy dossier; verify") if title else first_fact
    return hook[:140].strip()


def ensure_index_line(slug: str, hook: str = "") -> None:
    """Дописать строку про человека/тему в INDEX.md, если её ещё нет."""
    # INDEX is now a complete generated catalogue.  Rebuild it instead of appending a
    # person below unrelated sections and slowly corrupting the navigation structure.
    import memory_catalog
    # Do not preserve arbitrary text from a previous generated INDEX: old builds
    # copied the first dossier bullet and could therefore keep a model-derived claim
    # alive after the canonical file was corrected.  Re-derive every other hook.
    requested = hook or next(
        (line[2:].strip() for line in _read(PEOPLE_DIR / f"{slug}.md").splitlines()
         if line.startswith("# ")),
        slug,
    )
    requested = re.sub(r"\s+", " ", str(requested or "")).strip()[:140]
    memory_catalog.rebuild(memory_dir=MEM_DIR, people_dir=PEOPLE_DIR, index_path=INDEX_MD,
                           extra_people=[(slug, requested)], people_hooks={slug: requested})


def rebuild_index_hooks(hook_fn=None) -> None:
    """Перестроить глобальную карту памяти, включая хуки файлов людей.

    hook_fn(slug, text) -> str — опциональный генератор хука (напр. LLM в consolidate).
    По умолчанию хук выводится из файла (_derive_hook).
    """
    import memory_catalog
    memory_catalog.rebuild(people_hook=hook_fn, memory_dir=MEM_DIR,
                           people_dir=PEOPLE_DIR, index_path=INDEX_MD)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        idx = build()
        print(f"built: {len(idx['records'])} чанков из {len(idx['files'])} файлов")
    elif cmd == "search":
        q = " ".join(sys.argv[2:])
        for h in search(q):
            print(f"[{h['source']}] ({h['score']}) {h['text']}")
    else:
        print("usage: python -m memory_index [build | search <query>]")
