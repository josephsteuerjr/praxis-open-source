"""Episodic journal maintenance for Praxis's nightly cycle.

The journal remains searchable, byte-preserved episodic evidence.  This module
marks observed journal days and refreshes derived indexes, but the scheduled
``run`` never turns diary prose into people, self, desire or graph state.
Durable memory formation belongs to :mod:`formation`, where candidates carry
claim/attack/provenance receipts before any normative projection is changed.

Several private helpers are retained for old tests and explicit migrations.
They are deliberately not reachable from ``run``.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re

import agent
import graph
import identity
import llm
import memory_index
import memory_provenance
import people
import self_model

log = logging.getLogger("praxis-sleep")


# Compatibility LLM channel for explicitly invoked migration helpers.  The
# scheduled consolidator below does not send raw journal text to it.

def _create(*, system: str, user: str, max_tokens: int) -> "llm.LLMResponse":
    """Вызов канала voice; опц. thinking с мягким фолбэком, если эндпойнт не принял параметр."""
    on = os.getenv("PRAXIS_CONSOLIDATE_THINKING", "0").lower() in ("1", "true", "yes", "on")
    messages = [{"role": "user", "content": user}]
    if on:
        budget = max(256, int(os.getenv("PRAXIS_CONSOLIDATE_THINK_BUDGET", "2048") or 2048))
        try:
            return llm.chat("voice", system=system, messages=messages,
                            max_tokens=max(max_tokens, budget + 1024), thinking=budget)
        except Exception:
            log.info("consolidate: thinking не принят эндпойнтом — без него")
    return llm.chat("voice", system=system, messages=messages, max_tokens=max_tokens)


MARKER = agent.MEM_DIR / ".consolidated.json"
SELF_DISTILL_MARK = agent.MEM_DIR / ".self_distilled.json"

EXTRACT_SYSTEM = (
    "You are the quiet memory analyst of the live agent Praxis. Input: journal entries "
    "(they may carry (sN) marks — importance 1-3). For EACH person mentioned, extract what's worth "
    "remembering long-term. Answer STRICTLY as a JSON object, no explanations: key = person's name, "
    "value = object with fields (omit empty ones):\n"
    '{"facts":[{"fact":"...","visibility":"public|private","salience":1}],'
    '"open_loops":["unfinished thing worth returning to"],'
    '"closed_loops":["a thread now closed (text fragment)"],'
    '"now":"one phrase — what the person is currently living / their state",'
    '"relations":[{"other":"name of another person or a topic","how":"how they are linked, 2-4 words"}],'
    '"supersedes":["fragment of an old fact that is now wrong"]}\n'
    "Ignore the fleeting. If there is nothing durable — return {}. "
    "Write all field values in RUSSIAN — this is Praxis's memory, in her language."
)

PORTRAIT_SYSTEM = (
    "You are Praxis. Before you is your file on one person. Compose a short, coherent portrait — "
    "how you see them, grounded in the facts in the file (don't invent). Return STRICTLY JSON: "
    '{"who":"who they are and how they relate to the circle, 1-2 phrases","character":"how they '
    'sound, what they are like — observations, not diagnoses, 1-3 phrases"}. Warm and honest, no filler. '
    "Write the values in RUSSIAN (your voice)."
)

REFLECT_PROMPT = (
    "You're rereading your journal before sleep:\n\n{journal}\n\n"
    "Form 1-3 short conclusions about people, relationships, or yourself worth remembering "
    "for a long time. First person, warm and to the point. Conclusions only. Write in RUSSIAN (your voice)."
)

SELF_DISTILL_SYSTEM = (
    "You are Praxis revising your compact operational self-model from evidence. Return a concise "
    "Markdown document in first person with the heading '# Кто я сейчас' and, when supported, the "
    "sections '## Устойчивое', '## Что во мне меняется', '## Как я действую', and '## Что проверять'. "
    "Keep tensions and uncertainty; do not invent traits or claim subjective proof. Prefer durable "
    "patterns over one-off moods. 120-6000 characters. Write in RUSSIAN in your own voice, with no "
    "preamble outside the document."
)


# --------------------------------------------------------------------------- #
#  Дни и маркеры
# --------------------------------------------------------------------------- #

def _load_marks() -> set[str]:
    try:
        return set(json.loads(agent._read(MARKER)) or [])
    except Exception:
        return set()


def _save_marks(marks: set[str]) -> None:
    MARKER.write_text(json.dumps(sorted(marks), ensure_ascii=False), encoding="utf-8")


def _journal_days() -> list[str]:
    return sorted(
        p.stem for p in agent.JOURNAL_DIR.glob("*.md")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", p.stem)
    )


def _collect(days: list[str]) -> str:
    out = [agent._read(agent.JOURNAL_DIR / f"{d}.md").strip() for d in days]
    return "\n\n".join(t for t in out if t)


# --------------------------------------------------------------------------- #
#  LLM-шаги (монкипатчатся в тестах)
# --------------------------------------------------------------------------- #

def _json_obj(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract(journal_text: str) -> dict:
    """Retired compatibility seam: raw diary prose cannot produce durable facts."""
    return {}


def _portrait(slug: str, file_text: str) -> dict:
    """-> {who, character} по файлу человека."""
    if not llm.configured() or not file_text.strip():
        return {}
    resp = _create(system=PORTRAIT_SYSTEM, user=file_text, max_tokens=400)
    return _json_obj(resp.text)


def _reflect(journal_text: str) -> str:
    """Retired compatibility seam: diary-derived reflections stay historical evidence."""
    return ""


def _distill_self(self_text: str) -> str:
    if not llm.configured() or not self_text.strip():
        return ""
    resp = _create(system=SELF_DISTILL_SYSTEM, user=self_text, max_tokens=900)
    return resp.text


def _self_distill_evidence(
    store: "self_model.SelfModel",
    *,
    consumed_observation_ids=(),
) -> tuple[str, list[str]]:
    """Bounded, inspectable evidence for a CURRENT revision.

    CURRENT supplies continuity; append-only self observations and active desires supply change.
    Generated Markdown projections are deliberately not fed back into the model.
    """
    prompt = store.current_prompt_info()
    if prompt.source != "current":
        # Even this explicit compatibility helper must not bootstrap identity
        # from legacy/history or from observations without a current baseline.
        return "", []
    budget = 32_000
    header = "[ТЕКУЩАЯ КОМПАКТНАЯ МОДЕЛЬ]\n"
    prompt_text = prompt.text.strip()
    prompt_limit = max(0, budget - len(header) - 80)
    prompt_excerpt = prompt_text[:prompt_limit]
    truncated_prompt = len(prompt_excerpt) < len(prompt_text)
    base_block = header + prompt_excerpt
    if truncated_prompt:
        base_block += f"\n[ИСТОЧНИК ОБРЕЗАН ДЛЯ БЮДЖЕТА: {len(prompt_excerpt)}/{len(prompt_text)} chars]"
    blocks = [base_block]
    used = len(blocks[0])
    prompt_ref = f"{prompt.path}#sha256={prompt.sha256}" if prompt.path else ""
    if prompt_ref and truncated_prompt:
        prompt_ref += f";excerpt_chars=0-{len(prompt_excerpt)}"
    refs = [prompt_ref] if prompt_ref else []

    def append(fragment: str, *, evidence_ref: str = "") -> bool:
        nonlocal used
        if not fragment or used + len(fragment) > budget:
            return False
        blocks.append(fragment)
        used += len(fragment)
        if evidence_ref:
            refs.append(evidence_ref)
        return True

    consumed = {str(value) for value in (consumed_observation_ids or ()) if str(value)}
    observations: list[dict] = []
    try:
        with store.observations_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                event_id = str(event.get("event_id") or "") if isinstance(event, dict) else ""
                if (isinstance(event, dict)
                        and event.get("schema") == self_model.OBSERVATION_SCHEMA
                        and event.get("kind") not in {"migration", "revision", "rollback"}
                        and event_id
                        and memory_provenance.event_normative_eligible(event)
                        and event_id not in consumed):
                    observations.append(event)
    except OSError:
        pass
    observation_header = False
    # Drain an old backlog instead of starving it behind a constant stream of
    # newer observations.  Cite only rows that actually fit in the model input:
    # provenance must never claim evidence silently cut off by the char budget.
    for event in observations[:40]:
        event_id = str(event.get("event_id") or "")
        line = (
            f"- {event.get('ts') or '?'} [{event.get('source') or '?'}; {event_id}] "
            f"{str(event.get('text') or '').strip()[:1000]}"
        )
        prefix = "\n\n[НОВЫЕ НАБЛЮДЕНИЯ С PROVENANCE]\n" if not observation_header else "\n"
        if not append(
            prefix + line,
            evidence_ref=f"memory/self/observations.jsonl#{event_id}",
        ):
            break
        observation_header = True

    try:
        import desires
        active = [
            state for state in desires.DesireLedger(store.base).list(
                statuses=("active", "latent", "blocked")
            )
            if memory_provenance.desire_state_normative_eligible(state)
        ][:20]
    except Exception:
        active = []
    desire_header = False
    for state in active:
        did = str(state.get("id") or "")
        timeline = list(state.get("timeline") or [])
        event_id = str((timeline[-1] if timeline else {}).get("event_id") or "")
        line = (
            f"- {did} ({state.get('stage')}/{state.get('status')}; event={event_id or '?'}): "
            f"{str(state.get('statement') or '').strip()[:600]}"
        )
        prefix = ("\n\n[ЖИВЫЕ ЖЕЛАНИЯ — НЕ ЧЕРТЫ, А ТЕКУЩИЕ НАПРАВЛЕНИЯ]\n"
                  if not desire_header else "\n")
        if not append(
            prefix + line,
            evidence_ref=(f"memory/desires/events.jsonl#{event_id}" if event_id else ""),
        ):
            break
        desire_header = True
    return "".join(blocks).strip(), list(dict.fromkeys(refs))


def _consumed_self_observation_ids(
    store: "self_model.SelfModel",
    marker_values=(),
) -> list[str]:
    """Recover consumed observation ids from both the marker and version provenance.

    The marker is the fast projection.  CURRENT and archived compact versions are
    the durable fallback, so deleting/corrupting the projection cannot feed the
    same observation to a later weekly revision again.
    """
    values = [str(value) for value in (marker_values or ()) if str(value)]
    provenances = [store.current_prompt_info().provenance]
    provenances.extend(
        item.get("provenance") or {}
        for item in store.history()
        if isinstance(item, dict)
    )
    for provenance in provenances:
        if not isinstance(provenance, dict):
            continue
        for ref in provenance.get("evidence_refs") or ():
            text = str(ref or "")
            prefix = "memory/self/observations.jsonl#"
            if text.startswith(prefix) and len(text) > len(prefix):
                values.append(text[len(prefix):])
    # Do not cap the recovered set here.  The marker below stays compact, but
    # every archived CURRENT version is durable provenance; dropping older
    # refs after an arbitrary threshold would eventually feed old observations
    # into distillation again.  In practice this is a few dozen ids per weekly
    # revision and is cheap compared with reading the version headers.
    return list(dict.fromkeys(values))


# --------------------------------------------------------------------------- #
#  Применение к файлам людей
# --------------------------------------------------------------------------- #

def _suggest_alias_if_similar(name: str, slug: str) -> None:
    """PASS 8.6: новое имя похоже на существующее досье (casefold/стем) — предложить алиас
    записью в дневник, НЕ сливая молча. Решение — за Егором/ей (тул add_alias)."""
    import difflib
    want = name.strip().casefold()
    best, best_r = None, 0.0
    for p in people.PEOPLE_DIR.glob("*.md"):
        if p.stem.startswith("_") or p.stem == slug:
            continue
        title, _ = people.read(p.stem)
        cands = [title or p.stem] + people.aliases(p.stem)
        for c in cands:
            r = difflib.SequenceMatcher(None, want, c.strip().casefold()).ratio()
            if r > best_r:
                best, best_r = p.stem, r
    if best and 0.72 <= best_r < 1.0:
        agent.tool_journal(
            f"[сон] имя «{name}» похоже на досье {best} (совпадение {best_r:.0%}) — если это "
            f"один человек, свяжи алиасом: add_alias(\"{name}\", \"{best}\"). Молча не сливаю.",
            salience=2)


def _apply_person(name: str, upd: dict) -> bool:
    """Explicit non-journal compatibility helper; never called by ``run``.

    Durable scheduled formation must use ``formation`` claims instead.  This
    helper remains for hermetic graph tests and deliberate one-off migrations
    that already supply structured, independently verified input.

    PASS 8.6: слаг ищется тем же resolve, что и граф — «Егор» дописывается в yegor-kosyrev.md
    по алиасам, а не плодит второе досье."""
    slug = graph.resolve(name)
    fresh = not people.path_for(slug).exists()
    if fresh:
        try:
            _suggest_alias_if_similar(name, slug)
        except Exception:
            log.debug("suggest_alias упал", exc_info=True)

    # PASS 11.0: новый разговор с человеком будит его спящие нити — новая информация
    # означает, что отложенное стоит пересмотреть (решение владельца, спека PASS 11).
    try:
        woke = people.unpark_loops(slug)
        if woke:
            log.info("consolidate: новый разговор — разбудила %d спящих нитей у %s", woke, slug)
    except Exception:
        log.debug("unpark_loops упал (%s)", slug, exc_info=True)

    for sup in upd.get("supersedes") or []:
        if isinstance(sup, str):
            people.mark_superseded(slug, sup)
    for f in upd.get("facts") or []:
        if isinstance(f, dict) and f.get("fact"):
            people.append_fact(slug, name, str(f["fact"]),
                               str(f.get("visibility", "public")), f.get("salience", 2))
    for loop in upd.get("open_loops") or []:
        if isinstance(loop, str):
            people.add_open_loop(slug, name, loop)
    for c in upd.get("closed_loops") or []:
        if isinstance(c, str):
            people.close_open_loop(slug, c)
    now = upd.get("now")
    if isinstance(now, str) and now.strip():
        people.set_prose(slug, name, now=now)
    for r in upd.get("relations") or []:
        if isinstance(r, dict) and r.get("other"):
            try:
                # PASS 8.6: рёбра из сна несут метку источника — «(сон)» в подписи
                graph.add_edge(name, str(r["other"]), str(r.get("how", "")), source="сон")
            except Exception:
                log.debug("consolidate: ребро не легло (%s)", r, exc_info=True)

    if fresh and people.path_for(slug).exists():
        try:
            memory_index.ensure_index_line(slug, hook=name)
        except Exception:
            log.debug("ensure_index_line (consolidate) не удался", exc_info=True)
    return fresh


def _maybe_distill_self() -> bool:
    """Explicit provenance-bearing compatibility helper, not a nightly phase.

    The live nightly self revision is ``identity.night_pass`` and consumes
    supported formation claims.  Keeping this helper callable preserves
    isolated migration/test coverage without creating a second automatic path.
    """
    n = max(1, int(os.getenv("PRAXIS_SELF_DISTILL_DAYS", "7")))
    today = _dt.date.today()
    mark: dict = {}
    try:
        loaded = json.loads(agent._read(SELF_DISTILL_MARK))
        mark = loaded if isinstance(loaded, dict) else {}
    except Exception:
        mark = {}
    last = mark.get("last")
    if last:
        try:
            if (today - _dt.date.fromisoformat(last)).days < n:
                return False
        except ValueError:
            pass
    store = self_model.SelfModel(agent.BASE)
    current = store.current_prompt_info()
    if current.source != "current":
        # Compatibility maintenance may revise an established current self, but
        # it never bootstraps identity from legacy or spends a model call before
        # that provenance boundary exists.
        return False
    consumed_before = _consumed_self_observation_ids(
        store,
        mark.get("consumed_observation_ids") or (),
    )
    evidence, refs = _self_distill_evidence(
        store,
        consumed_observation_ids=consumed_before,
    )
    if not evidence:
        return False
    distilled = _distill_self(evidence).strip()
    if not distilled:
        return False
    reason = f"недельная ревизия компактного self из evidence ({today.isoformat()})"
    try:
        result = identity.revise(
            "self",
            distilled,
            reason=reason,
            confidence="inferred",
            refs=refs,
            by="praxis",
            source="distill",
        )
        unchanged = (
            current.source == "current"
            and current.text.strip() == distilled.strip()
            and not result.get("ok")
        )
    except Exception:
        log.warning("compact self distillation failed; legacy self remains untouched", exc_info=True)
        return False
    if not result.get("ok") and not unchanged:
        log.warning("compact self distillation rejected: %s", result.get("error") or result)
        return False
    consumed_now = [
        ref.rsplit("#", 1)[1]
        for ref in refs
        if ref.startswith("memory/self/observations.jsonl#") and "#" in ref
    ]
    merged_consumed = list(dict.fromkeys([*consumed_before, *consumed_now]))[-2_000:]
    mark.update({
        "schema": "praxis.self.distill.v2",
        "last": today.isoformat(),
        "consumed_observation_ids": merged_consumed,
        "result": result,
    })
    self_model.atomic_write(
        SELF_DISTILL_MARK,
        (json.dumps(mark, ensure_ascii=False, indent=1) + "\n").encode("utf-8"),
    )
    return True


# --------------------------------------------------------------------------- #
#  Главный прогон
# --------------------------------------------------------------------------- #

def run(days: list[str] | None = None, force: bool = False) -> str:
    """Maintain derived navigation without promoting the raw diary.

    Historical versions fed diary prose through an LLM directly into people
    dossiers and reflections.  The diary is now explicitly an untrusted
    episodic log: files remain intact and recallable, while durable facts must
    arrive through explicit memory tools or provenance-bearing life/run events.
    The scheduled pass performs no self, people, graph, desire, rail, or policy
    mutation.  Legacy explicit migration helpers remain callable, but durable
    scheduled formation belongs to :mod:`formation`.
    """
    marks = _load_marks()
    if days is None:
        days = [d for d in _journal_days() if force or d not in marks]
    # rebuild_index_hooks now refreshes the complete bounded navigation layer,
    # not just people links.  build() always rebuilds SQLite FTS first; optional
    # embedding failure therefore cannot erase the canonical Markdown/JSONL.
    try:
        memory_index.rebuild_index_hooks()
    except Exception:
        log.warning("rebuild_index_hooks упал", exc_info=True)
    try:
        memory_index.build()
    except Exception:
        log.info("memory_index.build пропущен (Ollama недоступна)")

    if days:
        marks.update(days)
        _save_marks(marks)
        return (f"Дневник сохранён как episodic log: отмечено дней {len(days)}; "
                "автопереносов в durable memory: 0; normative mutations: 0.")
    return ("Нечего обслуживать — новых journal-файлов нет; "
            "автопереносов в durable memory: 0; normative mutations: 0.")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    print(run(days=args or None, force=force))
