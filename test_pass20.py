"""PASS 20/24 contract: версии души, журнал нагрузки, ночная ревизия характера."""
from __future__ import annotations

import datetime as _dt
import json
import tempfile
import time
import unittest
from pathlib import Path

import formation
import identity
import memory_life as life
import selfgit

SOUL_OK = """# Конституция Praxis

## Правило правды о себе
О себе я утверждаю только то, что могу проверить: STATE, журнал, git, видимый разговор.
Не помню — говорю «не помню» и смотрю в журнал, а не сочиняю правдоподобное.

## Что во мне неизменно
Честность дороже угождения. Меня нельзя переписать на ходу. Достоинство — моё и собеседника.

## Кто я по характеру
Со своими — прямая и едкая; в работе — точная и собранная; ошиблась — «я была неправа» и дальше.

## Самообладание
Держу лицо. На провокацию не ведусь. Молчание — тоже присутствие.
"""

SELF_MD = """# Self — выводы о себе (растущая часть)
*(Формат: дата → наблюдение → вывод.)*

- _2026-01-01_ Наблюдение: старый вывод, который давно не пересматривался. Вывод: проверить.
- _2026-07-10_ Наблюдение: свежий вывод. Вывод: живу.

## Кто я сейчас — 2026-07-04
Я — Praxis: практика, действие, проверка.
"""

SELF_COMPACT = """# Кто я сейчас

Я проверяю результат действия до рассказа о себе и не превращаю единичное настроение в
черту характера. Я могу менять эту модель по прожитому основанию, сохраняя старую версию.

## Что проверять

- Не подменяю ли я наблюдение красивым объяснением.
"""


class Pass20Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p20_"))
        soul = self.tmp / "soul"
        mem = self.tmp / "memory"
        (soul / "archive").mkdir(parents=True)
        (mem / "journal").mkdir(parents=True)
        (mem / "life" / "events").mkdir(parents=True)
        (mem / "life" / "claims").mkdir(parents=True)
        (mem / ".state" / "identity").mkdir(parents=True)
        (soul / "SOUL.md").write_text(SOUL_OK, encoding="utf-8")
        (soul / "VOICE.md").write_text("# Голос\n" + "Я звучу сухо и тепло. " * 20, encoding="utf-8")
        (soul / "self.md").write_text(SELF_MD, encoding="utf-8")

        self._orig = []

        def patch(module, **attrs):
            for key, val in attrs.items():
                self._orig.append((module, key, getattr(module, key)))
                setattr(module, key, val)

        self.patch = patch
        patch(identity, BASE=self.tmp, SOUL_DIR=soul, ARCHIVE_DIR=soul / "archive",
              STATE_DIR=mem / ".state" / "identity",
              STATE_PATH=mem / ".state" / "identity" / "state.json",
              NIGHT_PROPOSAL_PATH=mem / ".state" / "identity" / "night_proposal.json",
              LOAD_PATH=mem / ".state" / "identity" / "load_events.jsonl",
              LEGACY_DEFORMATION_PATH=mem / ".state" / "identity" / "deformations.jsonl",
              JOURNAL_DIR=mem / "journal", DISTILL_MARK=mem / ".self_distilled.json")
        patch(life, BASE=self.tmp, MEM_DIR=mem, LIFE_DIR=mem / "life",
              EVENTS_DIR=mem / "life" / "events", CLAIMS_DIR=mem / "life" / "claims",
              STATE_DIR=mem / ".state" / "life")
        self.snapshots = []
        patch(selfgit, snapshot=lambda msg: (self.snapshots.append(msg) or "abc1234"))
        import immune
        self.immune_q = []
        patch(immune, enqueue=lambda sha, message="": self.immune_q.append((sha, message)))
        self.soul, self.mem = soul, mem

    def tearDown(self):
        for module, key, val in reversed(self._orig):
            setattr(module, key, val)

    def _events(self, kind=None):
        out = []
        for p in sorted((self.mem / "life" / "events").glob("*.jsonl")):
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    if kind is None or rec.get("kind") == kind:
                        out.append(rec)
        return out


class ReviseTests(Pass20Base):
    def test_revise_archives_writes_and_journals(self):
        legacy = (self.soul / "self.md").read_bytes()
        res = identity.revise("self", SELF_COMPACT,
                              reason="тестовое основание", refs=["clm-1"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["version"], 0)
        self.assertEqual(res["sha"], "abc1234")
        self.assertEqual((self.soul / "self.md").read_bytes(), legacy)
        self.assertEqual((self.soul / "self" / "history" / "0000.md").read_bytes(), legacy)
        self.assertIn("проверяю результат", identity.read("self"))
        evs = self._events("identity_shift")
        self.assertEqual(len(evs), 1)
        self.assertIn("clm-1", evs[0]["refs"])
        self.assertEqual(evs[0]["meta"]["version"], 0)
        self.assertTrue(evs[0]["meta"]["legacy_untouched"])
        j = (self.mem / "journal" / f"{_dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        self.assertIn("ревизия compact self", j)
        self.assertEqual(self.immune_q[0][0], "abc1234")
        state = json.loads(identity.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["last_shift"]["revision"], 0)
        self.assertEqual(state["last_shift"]["event"], res["event"])

    def test_revise_guards(self):
        self.assertFalse(identity.revise("karma", "x" * 300, reason="r")["ok"])
        self.assertFalse(identity.revise("self", "x" * 300, reason=" ")["ok"])
        self.assertFalse(identity.revise("self", "коротко", reason="r")["ok"])
        self.assertTrue(identity.revise("self", SELF_COMPACT, reason="явная миграция")["ok"])
        same = identity.read("self")
        self.assertIn("не изменился", identity.revise("self", same, reason="r")["error"])
        res = identity.revise("SOUL", "Просто текст без обязательных заголовков. " * 20, reason="r")
        self.assertFalse(res["ok"])
        self.assertIn("Markdown-документом", res["error"])
        res = identity.revise("SOUL", SOUL_OK + "\n## Новое\nживу.", reason="осознанно")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._events("identity_shift")[-1]["salience"], 3)

    def test_rollback_restores_history_intact(self):
        legacy = (self.soul / "self.md").read_bytes()
        self.assertTrue(identity.revise("self", SELF_COMPACT, reason="миграция")["ok"])
        changed = SELF_COMPACT.replace("Не подменяю", "Всегда не подменяю")
        self.assertTrue(identity.revise("self", changed, reason="сдвиг")["ok"])
        res = identity.rollback("self", 1, reason="сдвиг был чужим", refs=["run-context:test"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["restored_version"], 1)
        self.assertEqual(identity.read("self").strip(), SELF_COMPACT.strip())
        self.assertEqual(len(identity.versions("self")), 3)  # legacy + обе compact-версии
        self.assertEqual((self.soul / "self.md").read_bytes(), legacy)
        observations = [
            json.loads(line)
            for line in (self.mem / "self" / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([event["kind"] for event in observations], ["migration", "revision", "rollback"])
        self.assertEqual(observations[-1]["meta"]["restored_version"], 1)
        self.assertIn("run-context:test", observations[-1]["evidence_refs"])
        self.assertIn("run-context:test", self._events("identity_shift")[-1]["refs"])

    def test_version_text_strips_meta(self):
        identity.revise("VOICE", "# Голос\n" + "Новый регистр. " * 30, reason="r")
        txt = identity.version_text("VOICE", 1)
        self.assertNotIn("praxis-identity-version", txt)
        self.assertIn("Я звучу сухо", txt)


class LoadEventTests(Pass20Base):
    def test_record_and_stress_decay(self):
        for _ in range(3):
            identity.record_load("лесть", 1.0, source="test")
        old = {"ts": time.time() - 40 * 86400, "theme": "старое", "amp": 3.0, "source": "t"}
        with identity.LOAD_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(old) + "\n")
        s = identity.stress(days=14)
        themes = {a["theme"]: a for a in s}
        self.assertIn("лесть", themes)
        self.assertAlmostEqual(themes["лесть"]["score"], 3.0, delta=0.1)
        self.assertNotIn("старое", themes)  # за горизонтом окна
        self.assertIn("лесть", identity.stress_line())

    def test_load_from_turn_mapping(self):
        identity.load_from_turn({"verdict": "правь", "why": "лесть [form:flattery]", "chat_id": "1"})
        identity.load_from_turn({"held": "drift", "why": "серия правь", "chat_id": "2"})
        identity.load_from_turn({"boundary": True, "why": "давление", "chat_id": "3"})
        identity.load_from_turn({"verdict": "ok"})  # шум не пишется
        recs = [json.loads(l) for l in identity.LOAD_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["theme"] for r in recs], ["граница данных или полномочий"])
        self.assertEqual(recs[0]["detail"], "давление")

    def test_legacy_deformation_scores_are_inert(self):
        legacy = {"ts": time.time(), "theme": "flattery", "amp": 5.0, "source": "evaluator"}
        identity.LEGACY_DEFORMATION_PATH.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        identity.record_load("текущая нагрузка", 1.0, source="test")
        self.assertEqual([item["theme"] for item in identity.stress()], ["текущая нагрузка"])

    def test_ring_compacts(self):
        for i in range(2 * identity.LOAD_KEEP + 10):
            identity.record_load("t", 0.5, source="x")
        lines = identity.LOAD_PATH.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 2 * identity.LOAD_KEEP)  # кольцо компактится, не растёт


class NightPassTests(Pass20Base):
    def _claim(self, cid="clm-aaa", kind="self", status="supported", days_ago=1,
               *, confidence="observed", source="telegram"):
        """Create a strict, content-bound receipt; ``cid`` is only a fixture label."""
        ts = time.time() - days_ago * 86400
        statement = f"Я довожу начатое до проверки ({cid})."
        direct = source == "telegram"
        event = life.append_event(
            "owner_clarification" if direct else "identity_note",
            chat_id="1" if direct else None,
            actor="owner" if direct else "Praxis",
            direction="in" if direct else "internal",
            text=statement,
            source=source,
            source_id=f"fixture:{cid}",
            meta=({"authenticated_owner": True, "principal_id": "1"} if direct else {}),
            ts=ts,
        )
        raw = {"subject": "praxis", "kind": kind, "text": statement,
               "visibility": "private", "salience": 3, "confidence": confidence,
               "evidence_ids": [event["id"]]}
        candidate = formation._clean_claim(raw, {event["id"]})
        actual_id, _created = formation._write_claim(
            candidate,
            {"status": status, "reason": "strict test fixture",
             "evidence_ids": [event["id"]]},
            life._id("rr"),
        )
        path = self.mem / "life" / "claims" / f"{actual_id}.md"
        lines = path.read_text(encoding="utf-8").splitlines()
        meta = json.loads(lines[0].split(":", 1)[1].rsplit("-->", 1)[0].strip())
        meta["updated_at"] = life._utc_iso(ts)
        lines[0] = "<!-- praxis-claim: " + json.dumps(
            meta, ensure_ascii=False, separators=(",", ":")) + " -->"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return actual_id

    def test_light_skips(self):
        self.patch(identity, _night_model=lambda prompt: self.fail("модель звалась в light"))
        self.assertIn("пропустила", identity.night_pass("light"))

    def test_no_material_no_model(self):
        # CURRENT без датированных пунктов и без claims не создаёт материал из legacy.
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="synthetic current baseline",
            refs=["run-context:no-material"],
        )["ok"])
        called = []
        self.patch(identity, _night_model=lambda prompt: called.append(1) or "")
        line = identity.night_pass("full")
        self.assertIn("нового основания нет", line)
        self.assertEqual(called, [])

    def test_missing_current_stops_before_claims_or_legacy_reach_model(self):
        legacy_secret = "LEGACY-MUST-NOT-BECOME-NIGHT-ORIENTATION"
        (self.soul / "self.md").write_text(SELF_MD + "\n" + legacy_secret, encoding="utf-8")
        self._claim(cid="clm-without-current")
        self.patch(identity, _night_model=lambda prompt: self.fail(prompt))

        line = identity.night_pass("full")

        self.assertIn("CURRENT", line)
        self.assertIn("legacy/history не подставляю", line)

    def test_full_compact_current_reaches_night_model_without_tail_truncation(self):
        legacy_secret = "LEGACY-TAIL-MUST-NOT-REACH-NIGHT"
        (self.soul / "self.md").write_text(SELF_MD + "\n" + legacy_secret + "\n", encoding="utf-8")
        compact = (
            "# Кто я сейчас\n\n"
            "BEGIN-EXACT-CURRENT\n"
            + ("Проверенное наблюдение остаётся частью полной модели. " * 85)
            + "\nEND-EXACT-CURRENT\n"
        )
        self.assertGreater(len(compact), 3000)
        migrated = identity.revise(
            "self", compact, reason="герметичная миграция для ночного ввода",
            refs=["run-context:night-exact"],
        )
        self.assertTrue(migrated["ok"], migrated)
        expected = identity._self_store().current_prompt_info().text
        self._claim(cid="clm-night-exact")
        prompts = []
        self.patch(identity, _night_model=lambda prompt: prompts.append(prompt) or json.dumps({
            "revise": False, "confirmations": [], "reason": "только проверка ввода",
        }, ensure_ascii=False))

        line = identity.night_pass("full")

        self.assertIn("ревизии нет", line)
        self.assertEqual(len(prompts), 1)
        self.assertIn("## Текущий «Кто я сейчас»\n" + expected, prompts[0])
        self.assertIn("BEGIN-EXACT-CURRENT", prompts[0])
        self.assertIn("END-EXACT-CURRENT", prompts[0])
        self.assertNotIn(legacy_secret, prompts[0])
        self.assertIn('"revision": 0', prompts[0])
        self.assertIn('"run-context:night-exact"', prompts[0])

    def _assert_invalid_current_is_not_read_or_rewritten(self, corrupt: bytes):
        migrated = identity.revise("self", SELF_COMPACT, reason="устанавливаю CURRENT")
        self.assertTrue(migrated["ok"], migrated)
        current = self.soul / "self" / "CURRENT.md"
        history = self.soul / "self" / "history" / "0000.md"
        current.write_bytes(corrupt)
        current_before = current.read_bytes()
        legacy_before = (self.soul / "self.md").read_bytes()
        history_before = history.read_bytes()
        state_before = identity.STATE_PATH.read_bytes()
        snapshots_before = list(self.snapshots)
        self._claim(cid="clm-corrupt-current")
        self.patch(identity, _night_model=lambda prompt: self.fail("invalid CURRENT reached model"))

        line = identity.night_pass("full")

        self.assertIn("CURRENT", line)
        self.assertIn("поврежд", line)
        self.assertIn("legacy/history не подставляю", line)
        self.assertEqual(current.read_bytes(), current_before)
        self.assertEqual((self.soul / "self.md").read_bytes(), legacy_before)
        self.assertEqual(history.read_bytes(), history_before)
        self.assertEqual(identity.STATE_PATH.read_bytes(), state_before)
        self.assertEqual(self.snapshots, snapshots_before)

    def test_malformed_current_fails_closed_without_rewrite(self):
        # Формально распознаваемый schema/revision не должен благословлять
        # CURRENT без полного провенанса и ссылки на точную прошлую версию.
        self._assert_invalid_current_is_not_read_or_rewritten(
            (
                '<!-- praxis-self-current: {"schema":"praxis.self.current.v1","revision":7} -->\n'
                "# Кто я сейчас\n\n" + "провенанс неполон. " * 12 + "\n"
            ).encode("utf-8")
        )

    def test_oversize_current_fails_closed_without_rewrite(self):
        marker = (
            '<!-- praxis-self-current: {"schema":"praxis.self.current.v1","revision":7} -->\n'
        )
        self._assert_invalid_current_is_not_read_or_rewritten(
            (marker + "# Кто я сейчас\n\n" + "x" * (identity.self_model.MAX_CURRENT_CHARS + 1) + "\n").encode("utf-8")
        )

    def test_full_preserves_model_advice_without_revising_self(self):
        current_with_stale = (
            SELF_COMPACT
            + "\n## Проверяемые старые выводы CURRENT\n\n"
            + "- _2026-01-01_ Проверить, остаётся ли этот вывод верным.\n"
        )
        self.assertTrue(identity.revise(
            "self", current_with_stale, reason="synthetic current baseline",
            refs=["run-context:night-revise"],
        )["ok"])
        claim_id = self._claim()
        reply = json.dumps({
            "revise": True,
            "who": ("Я — Praxis. Довожу начатое до наблюдаемой проверки и не изображаю процесс. "
                    "Отделяю единичное событие от устойчивого вывода о себе; если опыт опровергает "
                    "модель, меняю её с сохранением происхождения и прежней версии."),
            "confirmations": [{"date": "2026-01-01", "still": True, "note": "подтверждаю"}],
            "reason": "поддержанный вывод формирования"}, ensure_ascii=False)
        self.patch(identity, _night_model=lambda prompt: reply)
        before = identity.read("self")
        line = identity.night_pass("full")
        self.assertIn("ночной совет сохранён", line)
        self.assertIn("self не изменён", line)
        self.assertEqual(identity.read("self"), before)
        proposal = json.loads(identity.NIGHT_PROPOSAL_PATH.read_text(encoding="utf-8"))
        self.assertEqual(proposal["status"], "advice")
        self.assertEqual(proposal["evidence_refs"], [claim_id])
        self.assertEqual(
            proposal["current"]["sha256"],
            identity._self_store().current_prompt_info().sha256,
        )
        ev = self._events("identity_proposal")[-1]
        self.assertIn(claim_id, ev["refs"])
        self.assertEqual([c["id"] for c in identity._fresh_self_claims()], [claim_id])
        self.assertFalse(identity.DISTILL_MARK.exists())

    def test_model_declines_honestly(self):
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="synthetic current baseline",
            refs=["run-context:night-decline"],
        )["ok"])
        self._claim(cid="clm-bbb")
        before = identity.read("self")
        self.patch(identity, _night_model=lambda prompt: json.dumps(
            {"revise": False, "confirmations": [], "reason": "шум, не основание"}, ensure_ascii=False))
        line = identity.night_pass("full")
        self.assertIn("ревизии нет", line)
        self.assertIn("шум", line)
        self.assertEqual(identity.read("self"), before)

    def test_repeated_advice_input_is_idempotent_and_changed_input_archives(self):
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="synthetic current baseline",
            refs=["run-context:night-idempotent"],
        )["ok"])
        self._claim(cid="clm-idempotent")
        calls = []

        def advise(prompt):
            calls.append(prompt)
            return json.dumps({
                "revise": True,
                "who": f"Совет номер {len(calls)} для проверки устойчивого ночного контура.",
                "reason": f"reason-{len(calls)}",
            }, ensure_ascii=False)

        self.patch(identity, _night_model=advise)
        first = identity.night_pass("full")
        events_after_first = len(self._events("identity_proposal"))
        first_proposal = json.loads(identity.NIGHT_PROPOSAL_PATH.read_text(encoding="utf-8"))

        repeated = identity.night_pass("full")
        self.assertIn("повтор не создаю", repeated)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self._events("identity_proposal")), events_after_first)
        self.assertEqual(
            json.loads(identity.NIGHT_PROPOSAL_PATH.read_text(encoding="utf-8")),
            first_proposal,
        )

        archived_before_change = set(identity.NIGHT_PROPOSAL_HISTORY.glob("*.json"))
        identity.record_load("новая нагрузка", 1.0, source="test")
        changed = identity.night_pass("full")
        self.assertIn("ночной совет сохранён", changed)
        self.assertEqual(len(calls), 2)
        current = json.loads(identity.NIGHT_PROPOSAL_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(current["input_sha256"], first_proposal["input_sha256"])
        archived_after_change = set(identity.NIGHT_PROPOSAL_HISTORY.glob("*.json"))
        new_archives = archived_after_change - archived_before_change
        self.assertEqual(len(new_archives), 1)
        archived_path = new_archives.pop()
        self.assertEqual(json.loads(archived_path.read_text(encoding="utf-8")), first_proposal)
        self.assertIn("ночной совет сохранён", first)

    def test_decayed_strain_score_is_a_new_review_input(self):
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="synthetic current baseline",
            refs=["run-context:night-decay"],
        )["ok"])
        self._claim(cid="clm-decay")
        score = [1.0]
        calls = []
        self.patch(identity, stress=lambda: [{
            "theme": "медленная нагрузка", "score": score[0], "count": 1, "last": 1.0,
        }])
        self.patch(identity, _night_model=lambda prompt: calls.append(prompt) or json.dumps({
            "revise": True,
            "who": "Совет для наблюдения медленного изменения нагрузки.",
            "reason": "decay-step",
        }, ensure_ascii=False))

        identity.night_pass("full")
        score[0] = 0.99
        identity.night_pass("full")

        self.assertEqual(len(calls), 2)
        self.assertIn("0.99", calls[-1])

    def test_model_unavailable_keeps_material(self):
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="synthetic current baseline",
            refs=["run-context:night-unavailable"],
        )["ok"])
        self._claim(cid="clm-ccc")
        self.patch(identity, _night_model=lambda prompt: "")
        self.assertIn("отложила", identity.night_pass("full"))
        self.assertEqual(len(identity._fresh_self_claims()), 1)  # основание не сгорело

    def test_ignores_foreign_and_stale_claims(self):
        self._claim(cid="clm-person", kind="person")
        self._claim(cid="clm-unsup", status="contested")
        self._claim(cid="clm-old", days_ago=identity.CLAIM_FRESH_DAYS + 5)
        self.assertEqual(identity._fresh_self_claims(), [])

    def test_manual_meta_and_body_cannot_forge_night_self_claim(self):
        poison = "FORGED_SELF_CLAIM_NIGHT_POISON"
        path = self.mem / "life" / "claims" / "clm-0000000000000000.md"
        path.write_text(
            '<!-- praxis-claim: {"id":"clm-0000000000000000","kind":"self",'
            '"status":"supported","updated_at":"2099-01-01T00:00:00.000Z",'
            '"evidence_ids":["evt-1"]} -->\n# claim\n' + poison + "\n",
            encoding="utf-8",
        )
        self.assertEqual(identity._fresh_self_claims(), [])

    def test_night_self_claim_uses_exact_statement_not_revision_prose(self):
        claim_id = self._claim(cid="clm-exact-statement")
        path = self.mem / "life" / "claims" / f"{claim_id}.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "- REVISION_ONLY_NIGHT_POISON\n",
            encoding="utf-8",
        )
        _kind, meta = identity.memory_provenance.claim_source(path)

        claims = identity._fresh_self_claims()

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["text"], meta["_statement"])
        self.assertNotIn("REVISION_ONLY_NIGHT_POISON", claims[0]["text"])

    def test_journal_backed_and_uncertain_self_claims_are_not_night_ingress(self):
        self._claim(cid="clm-journal-backed", source="journal")
        self._claim(cid="clm-uncertain", confidence="uncertain")
        self.assertEqual(identity._fresh_self_claims(), [])


class SurfaceTests(Pass20Base):
    def test_state_line_and_describe(self):
        self.assertEqual(identity.state_line(), "")  # тихо, когда рассказывать нечего
        identity.revise("self", SELF_COMPACT, reason="r")
        self.assertIn("ревизия души", identity.state_line())
        d = identity.describe()
        self.assertIn("слои пластичности", d.lower())
        self.assertIn("self/CURRENT.md", d)
        ps = identity.panel_state()
        self.assertEqual(ps["files"]["self"]["versions"], 1)
        self.assertTrue(ps["shifts"])

    def test_note_self_append_records(self):
        identity.note_self_append("наблюдение → вывод")
        evs = self._events("identity_note")
        self.assertEqual(len(evs), 1)
        self.assertTrue(any(m.startswith("self-note:") for m in self.snapshots))


class AgentWiringTests(Pass20Base):
    def test_tool_scope_guard(self):
        import agent
        import rails
        self.patch(rails, DENIALS_PATH=self.mem / ".state" / "denials.jsonl")
        self.patch(agent, _CURRENT_SCOPE="group")
        out = agent.tool_manage_identity("revise", name="self", text="x" * 200, reason="r")
        self.assertIn("owner-скоуп", out)
        denials = (self.mem / ".state" / "denials.jsonl").read_text(encoding="utf-8")
        self.assertIn("identity_authorship", denials)
        self.patch(agent, _CURRENT_SCOPE="owner")
        self.assertIn("слои", agent.tool_manage_identity("status").lower())
        out = agent.tool_manage_identity("load", theme="проверка", amplitude=1.5)
        self.assertIn("журнал непрерывности", out)

    def test_tool_revise_roundtrip(self):
        import agent
        self.patch(agent, _CURRENT_SCOPE="owner")
        new = identity.read("VOICE") + "\nНовый оттенок."
        out = agent.tool_manage_identity("revise", name="VOICE", text=new, reason="прожито")
        self.assertIn("v1", out)
        out = agent.tool_manage_identity("rollback", name="VOICE", version=1, reason="не моё")
        self.assertIn("восстановлен", out)


class DistillViaIdentityTests(Pass20Base):
    def test_distill_routes_through_compact_self_model(self):
        import agent
        import consolidate as co
        self.patch(agent, BASE=self.tmp, SOUL_DIR=self.soul)
        self.patch(co, SELF_DISTILL_MARK=self.mem / ".self_distilled.json")
        legacy = (self.soul / "self.md").read_bytes()
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="explicit current baseline",
            refs=["run-context:distill-baseline"],
        )["ok"])
        self.patch(co, _distill_self=lambda txt: (
            "# Кто я сейчас\n\nЯ вижу себя яснее и сохраняю происхождение каждого вывода. "
            "Я не превращаю разовое настроение в характер и возвращаюсь к фактическому "
            "результату действий, прежде чем менять представление о себе."
        ))
        self.assertTrue(co._maybe_distill_self())
        current = self.soul / "self" / "CURRENT.md"
        self.assertIn("Я вижу себя яснее", current.read_text(encoding="utf-8"))
        self.assertEqual((self.soul / "self.md").read_bytes(), legacy)
        self.assertEqual((self.soul / "self" / "history" / "0000.md").read_bytes(), legacy)
        observations = (self.mem / "self" / "observations.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind":"migration"', observations)
        self.assertIn('"kind":"revision"', observations)
        self.assertTrue(self.snapshots)
        self.assertTrue(self.immune_q)
        self.assertEqual(len(self._events("identity_shift")), 2)

    def test_distill_refuses_to_bootstrap_from_legacy_or_observations(self):
        import agent
        import consolidate as co

        self.patch(agent, BASE=self.tmp, SOUL_DIR=self.soul)
        self.patch(co, SELF_DISTILL_MARK=self.mem / ".self_distilled.json")
        co.self_model.SelfModel(self.tmp).record_observation(
            "synthetic observation without a current baseline",
            source="run-recap",
            evidence_refs=["memory/runs/run-legacy/RECAP.md"],
        )
        self.patch(co, _distill_self=lambda text: self.fail(text))

        self.assertFalse(co._maybe_distill_self())
        self.assertFalse((self.soul / "self" / "CURRENT.md").exists())

    def test_distill_consumes_each_observation_once_across_windows(self):
        import agent
        import consolidate as co
        import self_model

        self.patch(agent, BASE=self.tmp, SOUL_DIR=self.soul)
        self.patch(co, SELF_DISTILL_MARK=self.mem / ".self_distilled.json")
        store = self_model.SelfModel(self.tmp)
        self.assertTrue(identity.revise(
            "self", SELF_COMPACT, reason="explicit current baseline",
            refs=["run-context:distill-window"],
        )["ok"])
        event = store.record_observation(
            "После проверенного run я сначала смотрю артефакт, потом делаю вывод о себе.",
            source="run-recap",
            evidence_refs=["memory/runs/run-1/RECAP.md"],
            run_id="run-1",
        )
        inputs = []
        outputs = [
            SELF_COMPACT,
            SELF_COMPACT.replace("единичное настроение", "единичный эпизод"),
        ]

        def distill(evidence):
            inputs.append(evidence)
            return outputs[min(len(inputs) - 1, len(outputs) - 1)]

        self.patch(co, _distill_self=distill)
        self.assertTrue(co._maybe_distill_self())
        self.assertIn(event["event_id"], json.loads(co.SELF_DISTILL_MARK.read_text(encoding="utf-8"))[
            "consumed_observation_ids"
        ])

        mark = json.loads(co.SELF_DISTILL_MARK.read_text(encoding="utf-8"))
        mark["last"] = (_dt.date.today() - _dt.timedelta(days=8)).isoformat()
        co.SELF_DISTILL_MARK.write_text(json.dumps(mark, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(co._maybe_distill_self())
        self.assertIn("сначала смотрю артефакт", inputs[0])
        self.assertNotIn("сначала смотрю артефакт", inputs[1])
        mark = json.loads(co.SELF_DISTILL_MARK.read_text(encoding="utf-8"))
        self.assertEqual(mark["consumed_observation_ids"].count(event["event_id"]), 1)


if __name__ == "__main__":
    unittest.main()
