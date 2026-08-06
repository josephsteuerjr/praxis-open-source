"""Praxis nightly orchestrator.

``consolidate`` preserves and indexes episodic journal material.  ``formation``
is the only scheduled path that may project new durable people/graph claims;
``identity.night_pass`` consumes its supported self claims.  Legacy dossier
merge, graph-prune and direct graph-hypothesis helpers remain callable for
explicit maintenance/tests, but the scheduler runs them read-only or not at
all.  REM output is written only as labelled episodic candidate material.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path

import agent
import appetite
import consolidate
import formation
import graph
import lessons
import llm
import people
import rails
import turns
import workshop

log = logging.getLogger("praxis-sleep2")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
TRASH_DIR = BASE / "memory" / ".trash"
DUPS_STATE = BASE / "memory" / ".state" / "sleep_dups.json"
STATE_PATH = BASE / "memory" / ".state" / "sleep.json"   # 10.2: будильник переживает рестарт

SLEEP_MIN_GAP_H = 20.0     # не спать чаще раза в ~сутки
SLEEP_CATCHUP_H = 48.0     # слишком давно не спала — догон вне окна
SLEEP_BOOT_GRACE_SEC = 600.0  # после старта дать буферам восстановиться

REM_MAX_TOKENS = int(os.getenv("PRAXIS_REM_MAX_TOKENS", "2000") or 2000)
INBOX_SWEEP_DAYS = 30

# Фазы, снятые НАМЕРЕННО. Ноль от снятой фазы читается как «нечего было
# делать» — а правда в том, что органа нет. Сводка называет их словами, цифру
# оставляем только живому. Причина у каждой записана здесь, а не в комментарии,
# чтобы её можно было прочитать, а не раскапывать.
DISARMED: dict[str, str] = {
    "подрезка рёбер графа": "durable-правки графа — только через formation claims",
    "жвачка": "эпизодический журнал не переписывается расписанием",
    "пробуждение нитей досье": "состояние людей меняет явный инструмент, не часы",
    "цели-события": "PASS 24: желания продвигает она сама; часы не ставят ей целей",
}
PAIR_MIN_SCORE = 0.75    # порог «кандидат в дубли»
MERGE_MIN_SCORE = 0.95   # threshold for the explicit repeat-merge compatibility helper

_DATE_MARK = re.compile(r"\s*_\(\d{4}-\d{2}-\d{2}\)_\s*")


def _journal(msg: str, salience: int = 2) -> None:
    try:
        agent.tool_journal(f"[сон] {msg}", salience=salience)
    except Exception:
        log.debug("journal сна не удался", exc_info=True)


# --------------------------------------------------------------------------- #
#  СВС: read-only nightly candidates; explicit legacy merge helper
# --------------------------------------------------------------------------- #

def _names_of(slug: str) -> list[str]:
    title, _ = people.read(slug)
    return [n for n in ([title or slug] + people.aliases(slug)) if n.strip()]


def _pair_score(a: str, b: str) -> tuple[float, str]:
    """Похожесть двух досье по именам/алиасам. -> (score 0..1, почему)."""
    best, why = 0.0, ""
    for na in _names_of(a):
        for nb in _names_of(b):
            x, y = na.strip().casefold(), nb.strip().casefold()
            if not x or not y:
                continue
            if x == y:
                return 1.0, f"имя «{na}» совпадает"
            # 01.08.2026: одно имя в двух написаниях детектор НЕ ВИДЕЛ вовсе —
            # «Егор Косырев» и «Yegor Kosyrev» давали ratio 0.00, и пара никогда не
            # попадала в предложения, хотя на живом дереве это было пять досье на
            # одного человека. Ключ имени складывает написания; порог тут 1.0, но
            # ночной проход всё равно только ПРЕДЛАГАЕТ (allow_merge=False) —
            # стойка «молча не сливаю» не трогается.
            ka, kb = people.identity_key(na), people.identity_key(nb)
            if ka and ka == kb:
                return 1.0, f"«{na}» и «{nb}» — одно имя в двух написаниях"
            if len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x)):
                if best < 0.8:
                    best, why = 0.8, f"«{na}» / «{nb}» — префикс"
                continue
            r = difflib.SequenceMatcher(None, x, y).ratio()
            if r >= 0.85 and r > best:
                best, why = r, f"«{na}» ~ «{nb}» ({r:.0%})"
    return best, why


def dossier_dup_candidates() -> list[dict]:
    """Пары досье-кандидатов на слияние. Без мутаций. keep — файл с бОльшим содержимым."""
    if not people.PEOPLE_DIR.exists():
        return []
    slugs = sorted(p.stem for p in people.PEOPLE_DIR.glob("*.md") if not p.stem.startswith("_"))
    out = []
    for i, a in enumerate(slugs):
        for b in slugs[i + 1:]:
            score, why = _pair_score(a, b)
            if score < PAIR_MIN_SCORE:
                continue
            size_a = people.path_for(a).stat().st_size
            size_b = people.path_for(b).stat().st_size
            keep, absorb = (a, b) if size_a >= size_b else (b, a)
            out.append({"keep": keep, "absorb": absorb, "score": score, "why": why})
    return out


def _dups_state() -> dict:
    try:
        d = json.loads(DUPS_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _dups_save(d: dict) -> None:
    DUPS_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DUPS_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DUPS_STATE)


def _fact_lines(body: dict, key: str) -> list[str]:
    return [l for l in (body.get(key) or "").splitlines() if l.strip()]


def _common_and_unique(keep: str, absorb: str) -> tuple[int, int]:
    _, bk = people.read(keep)
    _, ba = people.read(absorb)
    fk = {people.fact_body(l) for l in _fact_lines(bk, people.FACTS)}
    fa = [people.fact_body(l) for l in _fact_lines(ba, people.FACTS)]
    common = sum(1 for f in fa if f in fk)
    return common, len(fa) - common


def merge_dossiers(keep: str, absorb: str) -> bool:
    """Слить absorb → keep: факты/нити/связи с дедупом, имя+алиасы absorb → алиасы keep.
    Поглощаемый файл — с бэкапом в memory/.trash/YYYYMMDD/. -> ok."""
    pk, pa = people.path_for(keep), people.path_for(absorb)
    if not pk.exists() or not pa.exists():
        return False
    day_dir = TRASH_DIR / _dt.date.today().strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pa, day_dir / pa.name)  # бэкап ДО любых мутаций

    name_k, bk = people.read(keep)
    name_a, ba = people.read(absorb)
    # факты: дедуп по голому телу факта
    have = {people.fact_body(l) for l in _fact_lines(bk, people.FACTS)}
    extra = [l for l in _fact_lines(ba, people.FACTS)
             if people.fact_body(l) and people.fact_body(l) not in have]
    if extra:
        bk[people.FACTS] = "\n".join(_fact_lines(bk, people.FACTS) + extra).strip()
    # нити: дедуп по нормализованному ключу
    have_l = {people._loop_key(l) for l in _fact_lines(bk, people.LOOPS)}
    extra_l = [l for l in _fact_lines(ba, people.LOOPS)
               if people._loop_key(l) and people._loop_key(l) not in have_l]
    if extra_l:
        bk[people.LOOPS] = "\n".join(_fact_lines(bk, people.LOOPS) + extra_l).strip()
    # связи: дедуп по строке без даты
    have_c = {_DATE_MARK.sub("", l).strip().lower() for l in _fact_lines(bk, people.LINKS)}
    extra_c = [l for l in _fact_lines(ba, people.LINKS)
               if _DATE_MARK.sub("", l).strip().lower() not in have_c]
    if extra_c:
        bk[people.LINKS] = "\n".join(_fact_lines(bk, people.LINKS) + extra_c).strip()
    # «Сейчас»/портрет: keep главнее, absorb — только если keep пуст
    for key in (people.WHO, people.CHARACTER, people.NOW):
        if not (bk.get(key) or "").strip() and (ba.get(key) or "").strip():
            bk[key] = ba[key]
    # имя и алиасы absorb становятся алиасами keep
    al = [a.strip() for a in (bk.get(people.ALIASES_KEY) or "").split(",") if a.strip()]
    seen = {a.casefold() for a in al} | {name_k.strip().casefold()}
    for cand in [name_a] + [x.strip() for x in (ba.get(people.ALIASES_KEY) or "").split(",")]:
        if cand.strip() and cand.strip().casefold() not in seen:
            al.append(cand.strip())
            seen.add(cand.strip().casefold())
    if al:
        bk[people.ALIASES_KEY] = ", ".join(al)

    people.write(keep, name_k, bk)
    pa.unlink()
    log.info("сон: слила досье %s ← %s (бэкап %s)", keep, absorb, day_dir / pa.name)
    return True



def crystallisation_pass(limit: int = 2) -> int:
    """ПРЕДЛОЖИТЬ превратить записанный урок в навык. Ничего не создаёт. -> сколько предложено.

    Её спецификация, 02.08.2026, дословно по границам: «система предлагает превратить
    записанный урок в навык, но не делает это автоматически; я вижу исходную заметку,
    формулирую навык сама и могу отказаться».

    Зачем вообще: у неё был разрыв «ошибка → осмысление → заметка», а дальше урок оставался
    в жанре, который не возвращается в нужный момент. Навык возвращается — он несёт своё
    условие («Перед инициативным сообщением…») и лежит в автоматическом recall. Заметка не
    возвращается и не должна: её собственные слова — «заметка это блокнот: сырая догадка,
    вопрос, полуфраза; если каждая начнёт автоматически возвращаться как руководство к
    действию, мы просто заменим эхо context.md эхом моего черновика».

    Поэтому здесь ровно одно действие: показать ей заметку и НАЗВАТЬ ЭВРИСТИКУ, по которой
    она попала в кандидаты, — чтобы с эвристикой можно было не согласиться. Отказ держится
    (`manage_notes(decline)`), иначе предложение стало бы требованием, повторяемым каждую ночь.
    """
    try:
        import authored_notes
        ledger = authored_notes.AuthoredNoteLedger(agent.BASE)
        notes = ledger.list(status="open", limit=100)
    except Exception:
        log.debug("сон: заметки не прочитались", exc_info=True)
        return 0
    offered = 0
    for note in lessons.pending(notes, limit=limit):
        try:
            lessons.offer(note["id"], gist=str(note.get("text") or "")[:400])
        except Exception:
            log.debug("сон: предложение не записалось", exc_info=True)
            continue
        gist = " ".join(str(note.get("text") or "").split())[:220]
        _journal(
            f"[урок] в заметке `{note['id']}` похоже на правило на будущее: «{gist}». "
            f"Если это урок — сделай из него навык своими словами: "
            f"write_skill(name=…, content=…, from_note=\"{note['id']}\"). Тогда он будет "
            f"возвращаться в кадре сам, а цепочка «ошибка → навык → где он был доступен» "
            f"откроется через manage_notes(action=\"chain\", note_id=<слаг навыка>). "
            f"Не урок — manage_notes(action=\"decline\", note_id=\"{note['id']}\"), больше не предложу. "
            f"Кандидатом сделала эвристика: {lessons.HEURISTIC} — с ней можно не согласиться.",
            salience=2)
        offered += 1
    if offered:
        log.info("сон: предложено кристаллизаций уроков — %d", offered)
    return offered


def svs_dossier_pass(*, allow_merge: bool = True) -> tuple[int, int]:
    """Фаза дублей досье. -> (слито, предложено dry-run).

    Первое совпадение пары — только предложение в дневник (без мутаций).  The
    explicit compatibility default preserves the historical repeat-merge
    helper; the nightly scheduler always passes ``allow_merge=False`` so only
    the claim/provenance formation path can change durable people state."""
    merged = proposed = 0
    state = _dups_state()
    for c in dossier_dup_candidates():
        key = f"{c['keep']}|{c['absorb']}"
        prev = state.get(key) if isinstance(state.get(key), dict) else None
        if allow_merge and prev and c["score"] >= MERGE_MIN_SCORE:
            if merge_dossiers(c["keep"], c["absorb"]):
                merged += 1
                state.pop(key, None)
                _journal(f"слила досье {c['keep']}.md ← {c['absorb']}.md ({c['why']}; "
                         f"повторное совпадение, бэкап в memory/.trash/"
                         f"{_dt.date.today():%Y%m%d}/).", salience=3)
            continue
        if not prev:
            common, uniq = _common_and_unique(c["keep"], c["absorb"])
            next_step = (
                "при повторном явном запуске compatibility helper солью с бэкапом."
                if allow_merge else
                "ночной scheduler сам не сольёт: изменение — через formation claim или явные руки."
            )
            _journal(f"предлагаю слить {c['keep']}.md ← {c['absorb']}.md ({c['why']}): "
                     f"общих фактов {common}, своих у {c['absorb']} — {uniq}. Согласна — "
                     f"сливай руками (fs-руки/add_alias); {next_step}", salience=2)
            proposed += 1
        state[key] = {"seen": (prev or {}).get("seen", 0) + 1,
                      "score": c["score"], "ts": time.time()}
    _dups_save(state)
    return merged, proposed


def prune_duplicate_edges() -> int:
    """Точные дубли рёбер (пара+подпись, дата не в счёт) — чистятся сразу. -> удалено строк."""
    removed = 0
    seen: set = set()

    def _norm(label: str) -> str:
        s = _DATE_MARK.sub("", label or "")
        return re.sub(r"^[\s—–:-]+", "", s).strip().lower()

    # 1) связи в файлах людей — первичный источник
    if people.PEOPLE_DIR.exists():
        for p in sorted(people.PEOPLE_DIR.glob("*.md")):
            if p.stem.startswith("_"):
                continue
            nm, body = people.read(p.stem)
            lines = _fact_lines(body, people.LINKS)
            keep_lines = []
            changed = False
            for l in lines:
                m = re.search(r"\[\[([^\]]+)\]\]", l)
                dst = graph._slug(m.group(1)) if m else ""
                key = (frozenset({p.stem, dst}), _norm(re.sub(r".*\]\]", "", l)))
                if dst and key in seen:
                    removed += 1
                    changed = True
                    continue
                seen.add(key)
                keep_lines.append(l)
            if changed:
                body[people.LINKS] = "\n".join(keep_lines).strip()
                people.write(p.stem, nm, body)
    # 2) graph.md — против себя и против файлов людей
    if graph.GRAPH_MD.exists():
        lines = graph.GRAPH_MD.read_text(encoding="utf-8", errors="ignore").splitlines()
        out, changed = [], False
        for raw in lines:
            m = graph._PAIR_LINE.match(raw.strip())
            if m:
                key = (frozenset({graph._slug(m.group(1)), graph._slug(m.group(2))}),
                       _norm(m.group(3) or ""))
                if key in seen:
                    removed += 1
                    changed = True
                    continue
                seen.add(key)
            out.append(raw)
        if changed:
            graph.GRAPH_MD.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return removed


# --------------------------------------------------------------------------- #
#  РЕМ: свободные ассоциации → гипотезы в graph.md
# --------------------------------------------------------------------------- #

REM_SYS = (
    "Night association pass over your memory graph — you are Praxis, half-asleep, free-associating. "
    "You see a handful of graph nodes and their 1-hop neighbors. Raw journal text is deliberately "
    "absent because it is untrusted episodic material, not association authority. "
    "Offer AT MOST 3 new possible connections or patterns — hypotheses, not claims; returning "
    "none is a fine answer. STRICT JSON, no explanations: "
    '{"links":[{"a":"<node>","b":"<node>","why":"2-6 words"}],'
    '"self":"one line IF a pattern is about YOU (omit otherwise)"} '
    "Values in RUSSIAN."
)


def _rem_nodes(adj: dict, journal_tail: str, rng: random.Random | None = None) -> list[str]:
    """Последние упомянутые в дневнике узлы + 2 случайных (без повторов, кап 6)."""
    rng = rng or random
    tail = journal_tail.lower()
    mentioned = [n for n in adj if n and (n.lower() in tail or graph.display(n).lower() in tail)]
    pool = [n for n in adj if n not in mentioned]
    randoms = rng.sample(pool, min(2, len(pool))) if pool else []
    out: list[str] = []
    for n in mentioned[:4] + randoms:
        if n not in out:
            out.append(n)
    return out[:6]


def add_hypothesis(a_ref: str, b_ref: str, why: str = "") -> bool:
    """Строка-гипотеза в memory/graph.md с пометкой (сон, гипотеза). НЕ в досье людей —
    гипотезы = сырьё, recall видит пометку. Дедуп через существующие рёбра."""
    a, b = graph.resolve(a_ref), graph.resolve(b_ref)
    if not a or not b or a == b:
        return False
    label = f"{(why or '').strip()} (сон, гипотеза)".strip()
    if graph._find_edge(a, b, label) is not None:
        return False
    graph.MEM_DIR.mkdir(parents=True, exist_ok=True)
    text = graph._read(graph.GRAPH_MD)
    if not text.strip():
        text = graph._GRAPH_HEADER
    line = f"- [[{a}]] ↔ [[{b}]] — {label} _({_dt.date.today().isoformat()})_"
    graph.GRAPH_MD.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")
    return True


def rem_pass(rng: random.Random | None = None, *, apply: bool = True) -> tuple[int, bool]:
    """Graph-only REM; the raw diary never enters this model call.

    ``apply=True`` is retained for explicit legacy tests/maintenance.  The
    nightly scheduler uses ``apply=False``: candidates are journalled as
    untrusted episodic hypotheses and never written to the normative graph.
    """
    if not llm.configured():
        return 0, False
    adj = graph.adjacency()
    if not adj:
        log.info("сон/РЕМ: граф пуст — скип")
        return 0, False
    nodes = _rem_nodes(adj, "", rng)
    if not nodes:
        return 0, False
    lines = []
    for n in nodes:
        nb = ", ".join(f"{other} ({'; '.join(labels[:2])})" if labels else other
                       for other, labels in list(adj.get(n, {}).items())[:5])
        lines.append(f"- {graph.display(n)} [{n}]: {nb or '—'}")
    # бюджет фазы: токены ~ символы/3; вход режем под бюджет, выход — короткий JSON
    budget_chars = max(1000, REM_MAX_TOKENS * 3)
    user = (f"Nodes and their neighbors:\n" + "\n".join(lines))[:budget_chars]
    try:
        resp = llm.chat("voice", system=REM_SYS, max_tokens=min(500, REM_MAX_TOKENS),
                        messages=[{"role": "user", "content": user}])
    except Exception:
        log.warning("сон/РЕМ: вызов упал", exc_info=True)
        return 0, False
    data = consolidate._json_obj(resp.text)
    added = 0
    for l in (data.get("links") or [])[:3]:
        if not isinstance(l, dict) or not l.get("a") or not l.get("b"):
            continue
        try:
            a_ref, b_ref = str(l["a"]), str(l["b"])
            why = str(l.get("why", ""))
            if apply:
                accepted = add_hypothesis(a_ref, b_ref, why)
            else:
                a, b = graph.resolve(a_ref), graph.resolve(b_ref)
                label = f"{why.strip()} (сон, гипотеза)".strip()
                accepted = bool(a and b and a != b and graph._find_edge(a, b, label) is None)
                if accepted:
                    _journal(
                        f"гипотеза о связи (episodic candidate, не claim): "
                        f"{graph.display(a)} ↔ {graph.display(b)} — {why.strip() or 'без подписи'}",
                        salience=2,
                    )
            if accepted:
                added += 1
        except Exception:
            log.debug("гипотеза не легла: %s", l, exc_info=True)
    self_line = str(data.get("self") or "").strip()
    if self_line:
        # кандидат в дневник, НЕ в self.md — self она правит сама
        _journal(f"гипотеза о себе (сырьё, не факт): {self_line}", salience=2)
    return added, bool(self_line)


# --------------------------------------------------------------------------- #
#  PASS 11.3: фаза «жвачка» — повторные размышления дневника схлопываются в вывод
# --------------------------------------------------------------------------- #

RUMINATION_DAYS = int(os.getenv("PRAXIS_RUMINATION_DAYS", "14") or 14)
RUMINATION_MIN_GROUP = 3    # меньше трёх повторов — не жвачка, просто мысли
RUMINATION_CAP = 5          # групп за сон (бюджет: 1 evaluator-вызов на группу)
RUMINATION_JACCARD = 0.5

_JOURNAL_LINE = re.compile(r"^- (\d{2}:\d{2}) \(s([123])\) (.+)$")

RUMINATION_SYS = (
    "Ты — Praxis во сне. Ниже несколько твоих дневниковых записей об одном и том же — "
    "жвачка, которую ты пережёвывала в разные дни. Сведи их в ОДНУ строку-вывод по-русски: "
    "суть + итог/решение, если оно видно. Без вступлений и кавычек — просто строка."
)


def _journal_entries(days: int) -> list[dict]:
    """Стандартные записи дневника за окно дней: [{file, time, line, text}].
    Служебные записи сна ([сон]/(сон)) не берём — иначе его же отчёты слипнутся в группу."""
    out = []
    cutoff = _dt.date.today() - _dt.timedelta(days=max(1, days))
    if not agent.JOURNAL_DIR.exists():
        return out
    for p in sorted(agent.JOURNAL_DIR.glob("*.md")):
        try:
            if _dt.date.fromisoformat(p.stem) < cutoff:
                continue
        except ValueError:
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = _JOURNAL_LINE.match(line.strip())
            if not m:
                continue
            text = m.group(3).strip()
            if text.startswith(("[сон]", "(сон)")):
                continue
            out.append({"file": p, "time": m.group(1), "line": line, "text": text})
    return out


def rumination_groups(entries: list[dict], threshold: float = RUMINATION_JACCARD,
                      min_group: int = RUMINATION_MIN_GROUP) -> list[list[dict]]:
    """Жадная группировка похожих записей (Жаккар токенов, без модели). Якорь группы —
    самая свежая запись; группы >= min_group. Каждая запись — максимум в одной группе."""
    # This similarity belongs to the explicit nightly journal-maintenance
    # operation.  Keep it local: the old heartbeat helper also powered a
    # hidden anti-repeat veto and was deliberately retired.
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-zа-яё0-9]{3,}", str(text or "").lower()))

    toks = [_tokens(e["text"]) for e in entries]
    order = sorted(range(len(entries)),
                   key=lambda i: (entries[i]["file"].stem, entries[i]["time"]), reverse=True)
    used: set[int] = set()
    groups: list[list[dict]] = []
    for i in order:
        if i in used or not toks[i]:
            continue
        members = [i]
        for j in order:
            if j == i or j in used or not toks[j]:
                continue
            if len(toks[i] & toks[j]) / len(toks[i] | toks[j]) >= threshold:
                members.append(j)
        if len(members) >= min_group:
            groups.append([entries[k] for k in members])
            used.update(members)
    return groups


def _rewrite_journal_line(path: Path, original: str, new_line: str | None) -> bool:
    """Заменить (или убрать, new_line=None) ПЕРВОЕ точное вхождение строки. Атомарно."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    out, done = [], False
    for l in lines:
        if not done and l == original:
            done = True
            if new_line is not None:
                out.append(new_line)
            continue
        out.append(l)
    if done:
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
        try:
            agent._reindex(path)
        except Exception:
            log.debug("reindex дневника не удался", exc_info=True)
    return done


def rumination_pass() -> tuple[int, int]:
    """Preserve every journal row verbatim; no LLM rewrite or compaction.

    The grouping helpers remain available for read-only diagnostics, but a
    contaminated episodic log cannot be turned into a synthesized conclusion
    or have its original rows removed by the nightly scheduler.
    """
    return 0, 0


def unpark_expired_loops() -> int:
    """Мётла парковок (11.0): [~] с истёкшей датой -> [ ] у всех людей. -> разбужено."""
    n = 0
    if people.PEOPLE_DIR.exists():
        for p in sorted(people.PEOPLE_DIR.glob("*.md")):
            if not p.stem.startswith("_"):
                n += people.unpark_loops(p.stem, only_expired=True)
    return n


# --------------------------------------------------------------------------- #
#  Мётла inbox (9.3) и главный прогон
# --------------------------------------------------------------------------- #

def sweep_inbox(days: int = INBOX_SWEEP_DAYS, now: float | None = None) -> int:
    """Файлы workspace/inbox старше days — удаляются (это транзитные копии; см. coding_hands)."""
    inbox = workshop.INBOX
    if not inbox.exists():
        return 0
    now = now if now is not None else time.time()
    cutoff = now - days * 86400
    n = 0
    for f in inbox.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            log.debug("inbox-мётла: %s не удалился", f, exc_info=True)
    # New inbox layout is nested by group/DM. Remove only directories that became
    # empty after the age-based file sweep; never follow or delete a non-empty tree.
    for d in sorted((p for p in inbox.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    return n


def run(depth: str | None = None) -> str:
    """Night cycle: episodic upkeep → formation claims → identity from claims → report.

    PASS 18.4: depth — МОЙ план из договора об аппетитах (appetite.sleep_depth):
    light = без REM-кандидатов и journal diagnostics.  Durable people/graph
    projections always pass formation; self revision consumes supported claims.
    Отчёт называет фактический расход и что пропущено."""
    if depth is None:
        try:
            depth = appetite.sleep_depth()
        except Exception:
            depth = "full"
    try:
        spent0 = appetite.usage_delta()
    except Exception:
        spent0 = None
    svs = consolidate.run()
    try:  # PASS 19: после forward harvest — итеративное копание по fresh compact frontier
        formed = formation.run("light" if depth == "light" else "full", reason="сон")
        formation_line = formed.get("summary") or ("нет нового" if formed.get("noop") else "сбой")
    except Exception:
        log.exception("сон: активное формирование упало")
        formation_line = "сбой (сырьё сохранено, повторю)"
    try:  # PASS 20: ночная ревизия характера — по прожитому (self-claims формирования,
        # деформации, «всё ещё так?»); light пропускает, «кто я сейчас» держит СВС
        import identity
        identity_line = identity.night_pass(depth)
    except Exception:
        log.exception("сон: ночная ревизия характера упала")
        identity_line = "сбой (основания сохранены)"
    try:
        # Candidate discovery may write an episodic proposal, but automatic
        # dossier merge would bypass formation claim/attack/provenance.
        merged, proposed = svs_dossier_pass(allow_merge=False)
    except Exception:
        log.exception("сон: фаза дублей досье упала")
        merged = proposed = 0
    try:
        # Тот же рисунок, что у дублей досье: НАЙТИ и ПРЕДЛОЖИТЬ, не сделать.
        crystallisation_pass()
    except Exception:
        log.exception("сон: фаза кристаллизации уроков упала")
    # Direct graph rewrites are explicit maintenance only.  Formation owns all
    # scheduled durable graph changes and records their claim/patch receipts.
    pruned = 0
    if depth == "light":
        rum_groups = rum_removed = 0                  # 18.4: лёгкий сон — жвачку пропускаю
    else:
        try:
            rum_groups, rum_removed = rumination_pass()   # 11.3: жвачка — до РЕМ
        except Exception:
            log.exception("сон: фаза жвачки упала")
            rum_groups = rum_removed = 0
    # Waking/closing dossier loops changes people state; leave it to an explicit
    # tool or a provenance-bearing formation change instead of the clock.
    woke = 0
    # PASS 24: желания принадлежат Praxis и продвигаются её явными решениями и
    # прожитыми run-результатами. Часы больше не ставят ей моральные цели.
    goal_lines: list[str] = []
    if depth == "light":
        hyps = 0                                      # 18.4: лёгкий сон — РЕМ пропускаю
    else:
        try:
            hyps, _ = rem_pass(apply=False)
        except Exception:
            log.exception("сон: РЕМ упал")
            hyps = 0
    try:
        swept = sweep_inbox()
    except Exception:
        log.exception("сон: мётла inbox упала")
        swept = 0
    try:  # PASS 14: прожитый день — сводка КОДОМ из лога ходов, не припоминание модели
        day = turns.day_line()
        if day:
            _journal(f"прожитый день, кодом: {day}", salience=2)
    except Exception:
        log.exception("сон: сводка прожитых ходов упала")
    try:  # PASS 18.1: манифест рельсов не отстаёт от живых констант (пишет только при изменении)
        rails.sync_md()
    except Exception:
        log.exception("сон: rails.sync_md упал")
    try:  # deterministic nightly navigation/inventory; no model and no second memory authority
        import computer_inventory
        inventory = computer_inventory.refresh()
        inventory_line = ("обновлена" if inventory.get("ok") else
                          f"не обновлена ({inventory.get('code') or 'error'})")
    except Exception:
        log.exception("сон: инвентаризация компьютера упала")
        inventory_line = "сбой"
    try:
        import memory_catalog
        memory_catalog.rebuild()
    except Exception:
        log.exception("сон: карта памяти не пересобралась")
    # Снятая фаза не может сработать. Если сработала — это событие, а не строка
    # в ряду цифр: пусть кричит, вместо того чтобы тихо слиться с нулями.
    surprises = [f"{name}: {n}" for name, n in
                 (("подрезка рёбер", pruned), ("жвачка", rum_removed),
                  ("пробуждение нитей", woke), ("цели-события", len(goal_lines)))
                 if n]
    report = (f"сон: слито {merged}, предложено слияний {proposed}, гипотез {hyps}, "
              f"inbox −{swept} файлов; "
              f"снято намеренно: {', '.join(DISARMED)}; "
              f"формирование: {formation_line}; личность: {identity_line}; "
              f"карта компьютера: {inventory_line}; "
              "durable mutations — только через formation claims")
    if surprises:
        report += "; ⚠ СНЯТАЯ ФАЗА СРАБОТАЛА — " + "; ".join(surprises)
    if depth == "light":  # 18.4: причина остановки — мой план, не тихий пропуск
        report += "; лёгкий сон — РЕМ и жвачку пропустила (мой план аппетита)"
    try:  # 18.4: фактический расход сна — честная цена ночи в отчёте
        if spent0 is not None:
            report += f"; расход {appetite.usage_delta(spent0)}"
    except Exception:
        pass
    _journal(report, salience=2)
    try:  # 18.3: сверка моего обещания Егору — каждый сон, пока обещание живо
        pline = appetite.promise_line()
        if pline:
            _journal(f"сверка обещания об аппетитах: {pline}", salience=2)
    except Exception:
        pass
    return f"{svs} {report}"


# --------------------------------------------------------------------------- #
#  PASS 10.2: сон по будильнику, а не по секундомеру
# --------------------------------------------------------------------------- #

def _state_load() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def last_run_ts() -> float:
    try:
        return float(_state_load().get("last_run_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _state_save(**kw) -> None:
    """Атомарная запись sleep.json (tmp+replace); переживает рестарт — в этом весь смысл."""
    try:
        d = _state_load()
        d.update(kw)
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception:
        log.warning("sleep.json не записался", exc_info=True)


def due(now: float | None = None, started_at: float | None = None) -> bool:
    """Пора ли спать. Окно PRAXIS_SLEEP_WINDOW (локальные часы, дефолт 4-6) и >20ч от
    последнего сна; догон: >48ч без сна — в ближайший тик и вне окна, но не раньше
    10 минут после старта процесса (буферы должны восстановиться)."""
    import heartbeat
    t = now if now is not None else time.time()
    gap_h = (t - last_run_ts()) / 3600.0
    win = heartbeat.parse_hours(os.getenv("PRAXIS_SLEEP_WINDOW", "4-6"))
    if win and heartbeat.hour_in(heartbeat.local_now(t).hour, win) and gap_h > SLEEP_MIN_GAP_H:
        return True
    if gap_h > SLEEP_CATCHUP_H:
        if started_at is not None and (t - started_at) < SLEEP_BOOT_GRACE_SEC:
            return False
        return True
    return False


def run_scheduled(now: float | None = None) -> str:
    """Сон по будильнику: run() + честный persist попытки. Ошибка — warning и retry
    следующей ночью (метка попытки пишется в любом случае — не крэш-луп раз в 30 мин).
    Часовое пробуждение живёт отдельно; у него нет дневного поведенческого капа."""
    t = now if now is not None else time.time()
    try:  # 18.4: пауза фона (слово Егора / моё состояние) — новый сон не начинаю;
        # метку last_run не ставлю: снятие паузы вернёт сон в ближайший тик.
        hold = appetite.background_hold()
    except Exception:
        hold = None
    if hold:
        log.info("сон: пропуск — %s", hold)
        _state_save(last_error=f"пропуск: {hold}")
        return f"сон отложен: {hold}"
    try:
        summary = run()
        _state_save(last_run_ts=t, last_ok_ts=t, last_error="")
        return summary
    except Exception as e:
        _state_save(last_run_ts=t, last_error=f"{type(e).__name__}: {str(e)[:200]}")
        log.warning("сон упал — retry следующей ночью", exc_info=True)
        return f"сон не удался ({type(e).__name__}) — попробую следующей ночью"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
