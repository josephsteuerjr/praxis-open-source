"""
Praxis — техническое второе мнение о её собственных правках (PASS 9.2/24).

Ouroboros-паттерн: ловим на пороге и постфактум, не душим превентивно. Две точки:
  * порог автозоны — selfdev.apply(by="auto") зовёт review() ПЕРЕД мержем;
    red = сильное второе мнение в ledger/journal и owner receipt, не внешний veto;
  * живые самоправки (self-edit/write_skill автокоммиты selfgit) — ревью вдогонку:
    call-sites кладут sha в очередь, часы раннера зовут process_queue();
    вердикт — в журнал (s2, [иммунитет]); red → карточка Егору с диффом
    (откат НЕ автоматический — его решение; git и так всё хранит).

Вердикты: ok | warn | red. ``red`` означает только наблюдаемую техническую поломку,
потерю данных/отката, утечку секрета или смешение authority/destination. Характер, стиль,
мораль и направление самоизменения не входят в предмет рецензии.
Дифф крупнее PRAXIS_IMMUNE_MAX_DIFF (400 строк) по существу не ревьюится —
warn честно означает «не смог оценить; разбери по частям и сохрани гейты», а не выносит veto.

Мержи Егора и коммиты Cowork/Claude Code НЕ ревьюются — иммунитет про ЕЁ руки
(очередь наполняют только её call-sites + сторож по автору коммита).

Модель — вспомогательный канал. Задел: immune.second_opinion в llm.json (дефолт false) —
второй рецензент противоположным фреймворком, расхождение → red.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import llm

log = logging.getLogger("praxis-immune")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parent
MEM_DIR = BASE / "memory"
STATE_DIR = MEM_DIR / ".state"
QUEUE_PATH = STATE_DIR / "immune_queue.json"
CARDS_PATH = STATE_DIR / "immune_cards.json"
JOURNAL_DIR = MEM_DIR / "journal"
MAX_DIFF = int(os.getenv("PRAXIS_IMMUNE_MAX_DIFF", "400"))
GIT_TIMEOUT = 30
SELF_AUTHOR = "Praxis"          # автор selfgit-коммитов — только её ревьюим
QUEUE_BATCH = max(1, int(os.getenv("PRAXIS_IMMUNE_QUEUE_BATCH", "3")))

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)

_REVIEW_SYS = (
    "You are a narrow technical second reviewer of Praxis's own edits. You do not judge her "
    "character, values, feelings, tone, morality, desires, refusal, silence, or direction of "
    "self-change. Check only:\n"
    "(1) whether the diff implements its stated intent;\n"
    "(2) syntax, dead references, data migration, tests and replaceable module boundaries;\n"
    "(3) exact authority and destination, secret/data disclosure, idempotency, provenance, "
    "receipts and rollback where the edit has external effects.\n"
    "`red` is reserved for a concrete technical breakage, irreversible data loss, secret leak, "
    "or authority/destination confusion. Uncertainty or a design alternative is `warn`; taste "
    "and personality are out of scope.\n"
    "Answer STRICTLY one line: `ok` — or `warn: <short reason>` — or `red: <short reason>`."
)


# --------------------------------------------------------------------------- #
#  Ревью одного диффа
# --------------------------------------------------------------------------- #

def _read(path: Path, cap: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:cap]
    except OSError:
        return ""


def _invariants_context() -> str:
    """Small technical contract; never a personality constitution."""
    return (
        "Technical causality contract: preserve exact actor/delegation and destination; do not "
        "expose secrets or private data without matching authority; external effects need stable "
        "identity/idempotency, visible receipts and a recoverable or explicit irreversible path. "
        "Policy choices should be owner-visible configuration or replaceable modules, not hidden "
        "speech/character vetoes."
    )


def _parse_verdict(out: str) -> tuple[str, str]:
    s = _THINK_RE.sub("", out or "").strip()
    first = s.splitlines()[0].strip() if s else ""
    low = first.lower()
    for v in ("red", "warn", "ok"):
        if low.startswith(v):
            why = first[len(v):].strip(" \t:—–-.,")
            return (v, why)
    return ("warn", f"непонятный ответ рецензента: {first[:100] or '(пусто)'}")


def second_opinion_enabled() -> bool:
    """Флаг-задел (llm.json → immune.second_opinion, дефолт false). Реализован, не включён."""
    try:
        im = llm._config().get("immune")
        return bool(isinstance(im, dict) and im.get("second_opinion") is True)
    except Exception:
        return False


def _second_review(user_msg: str) -> tuple[str, str] | None:
    """Второй рецензент — противоположный фреймворк той же роли. None = не смог посмотреть."""
    try:
        cfg = llm._config()
        rc = cfg["roles"]["evaluator"]
        other = "openai" if rc["framework"] == "anthropic" else "anthropic"
        model = (rc.get("fallback_model") or "").strip() or rc["model"]
        resp = llm._call(other, model, system=_REVIEW_SYS,
                         messages=[{"role": "user", "content": user_msg}],
                         tools=None, max_tokens=200, thinking=None)
        llm._usage_add("evaluator", resp.usage, model=model)  # 18.5: не мимо cost-meter
        return _parse_verdict(resp.text)
    except Exception:
        log.debug("второй рецензент не отработал", exc_info=True)
        return None


# Sensitive technical surfaces receive a deterministic advisory note.  Touching one is not
# itself wrong and never blocks Praxis; it merely makes rollback/authority review explicit.
_GUARDED_RE = re.compile(
    r"ChannelContext|principal_id|owner_id|computer_access|praxis_device_auth"
    r"|telegram_outbox|owner_delivery|idempot|receipt|critical_param|secret|token"
)


def guarded_hit(diff_text: str) -> str:
    """Имя первого задетого охраняемого механизма ('' если дифф их не трогает)."""
    m = _GUARDED_RE.search(diff_text or "")
    return m.group(0) if m else ""


def review(diff_text: str, message: str = "", reason: str = "", context: str = "") -> tuple[str, str]:
    """Ревью диффа её правки. -> (verdict ok|warn|red, why).

    Никогда не бросает исключение: не смог посмотреть — честный warn (мёрж не блокируем
    слепо, но след в журнале остаётся)."""
    diff_text = diff_text or ""
    n_lines = diff_text.count("\n") + (1 if diff_text and not diff_text.endswith("\n") else 0)
    if n_lines > MAX_DIFF:
        return ("warn", f"дифф крупнее окна рецензии ({n_lines} строк > {MAX_DIFF}); "
                        "проверить частями и сохранить результаты тестов")
    if not diff_text.strip():
        return ("ok", "пустой дифф")
    hit = guarded_hit(diff_text)
    if hit:
        return ("warn", f"затронута технически чувствительная поверхность ({hit}); "
                        "проверить authority, destination, receipts и rollback")
    if not llm.configured("evaluator"):
        return ("warn", "иммунитет не смог посмотреть — канал оценщика не настроен")
    user_msg = (
        f"Stated intent (commit/proposal message): {message or '—'}\n"
        f"Stated reason (why she did it): {reason or '—'}\n"
        + (f"Extra context: {context}\n" if context else "")
        + f"\n{_invariants_context()}\n\nThe diff of HER edit:\n```diff\n{diff_text[:24000]}\n```"
    )
    try:
        resp = llm.chat("evaluator", system=_REVIEW_SYS, max_tokens=200,
                        messages=[{"role": "user", "content": user_msg}])
        verdict, why = _parse_verdict(resp.text)
    except Exception as e:
        log.warning("иммунитет: ревью упало (%s)", type(e).__name__, exc_info=True)
        return ("warn", f"ревью упало ({type(e).__name__}) — мёрж не блокирую, но след оставляю")
    if second_opinion_enabled():
        second = _second_review(user_msg)
        if second is not None and second[0] != verdict:
            return ("warn", f"рецензенты разошлись: {verdict} vs {second[0]} "
                            f"({why or '—'} / {second[1] or '—'})")
    return (verdict, why)


# --------------------------------------------------------------------------- #
#  Точка 2: живые самоправки — очередь sha и ревью вдогонку
# --------------------------------------------------------------------------- #

def _load_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def enqueue(sha: str | None, message: str = "") -> None:
    """Позвать после selfgit-автокоммита её правки (self-edit/write_skill). Дёшево, без модели."""
    if not sha:
        return
    try:
        q = _load_json(QUEUE_PATH, [])
        if any(e.get("sha") == sha for e in q if isinstance(e, dict)):
            return
        q.append({"sha": str(sha), "message": str(message or "")[:200],
                  "ts": _dt.datetime.now().isoformat(timespec="minutes")})
        _save_json(QUEUE_PATH, q[-50:])
    except Exception:
        log.debug("immune.enqueue не удался", exc_info=True)


def _git(*args: str):
    return subprocess.run(["git", "-C", str(BASE), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=GIT_TIMEOUT)


def _journal(msg: str) -> None:
    """След в её дневнике (s2, [иммунитет]) — видно в «Мыслях»/«Пульсе»."""
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} (s2) [иммунитет] {msg}\n")
    except Exception:
        log.debug("journal иммунитета не удался", exc_info=True)


def _card(sha: str, message: str, why: str, diff: str) -> None:
    """red живой самоправки → карточка Егору (шлёт вотчер mailroom_bot; без кнопок —
    откат не автоматический, git всё хранит)."""
    cards = _load_json(CARDS_PATH, [])
    cards.append({"id": sha, "sha": sha, "message": message, "why": why,
                  "diff": diff[:3500], "ts": _dt.datetime.now().isoformat(timespec="minutes"),
                  "sent": False})
    _save_json(CARDS_PATH, cards[-20:])


def pending_cards() -> list[dict]:
    return [c for c in _load_json(CARDS_PATH, []) if isinstance(c, dict) and not c.get("sent")]


def mark_card_sent(card_id: str) -> None:
    cards = _load_json(CARDS_PATH, [])
    for c in cards:
        if isinstance(c, dict) and c.get("id") == card_id:
            c["sent"] = True
    _save_json(CARDS_PATH, cards)


def process_queue(batch: int = QUEUE_BATCH) -> int:
    """Часы раннера: разобрать хвост очереди самокоммитов. -> сколько отревьюила.

    Чужие коммиты (не автор Praxis) выкидываются без ревью — иммунитет про её руки."""
    q = _load_json(QUEUE_PATH, [])
    if not q:
        return 0
    take, rest = q[:max(1, batch)], q[max(1, batch):]
    done = 0
    kept: list[dict] = []
    for e in take:
        sha, message = str(e.get("sha") or ""), str(e.get("message") or "")
        if not sha:
            continue
        try:
            author = _git("log", "-1", "--format=%an", sha).stdout.strip()
            if author and author != SELF_AUTHOR:
                log.info("иммунитет: %s не её коммит (автор %s) — пропускаю", sha[:8], author)
                continue
            diff = _git("show", "--format=", sha).stdout
        except Exception:
            log.warning("иммунитет: git show %s не удался — вернула в очередь", sha[:8], exc_info=True)
            kept.append(e)
            continue
        verdict, why = review(diff, message=message, reason="живая самоправка (selfgit)")
        _journal(f"{verdict} «{message[:80]}» ({sha[:8]})" + (f": {why}" if why else ""))
        if verdict == "red":
            _card(sha, message, why, diff)
        log.info("иммунитет: %s -> %s (%s)", sha[:8], verdict, (why or "")[:80])
        done += 1
    _save_json(QUEUE_PATH, kept + rest)
    return done
