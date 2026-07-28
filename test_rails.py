"""
PASS 18.1/18.2 — rails: инвентарь рельсов кодом, манифест, события отказов.

Инвариант «нет молчаливой клетки»: каждый известный гейт-механизм кода представлен
в registry() с честным классом и рычагом; появление нового рельса без записи — падение
чек-листа здесь.

Запуск:  python praxis_test.py test_rails -v
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import llm
import rails


class _NoCallClient:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise AssertionError("rails не должен звать модель")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_rails_"))
        self._orig = [(rails, "BASE", rails.BASE),
                      (rails, "RAILS_MD", rails.RAILS_MD),
                      (rails, "DENIALS_PATH", rails.DENIALS_PATH)]
        rails.BASE = self.tmp
        rails.RAILS_MD = self.tmp / "soul" / "rails.md"
        rails.DENIALS_PATH = self.tmp / "memory" / ".state" / "denials.jsonl"
        self._env = {k: os.environ.get(k) for k in (
            "PRAXIS_FLOOR_SKIP", "PRAXIS_CORE_EDIT_CHECK")}
        for k in self._env:
            os.environ.pop(k, None)
        llm.use_test_client(_NoCallClient())
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for m, k, v in self._orig:
            setattr(m, k, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


# Чек-лист «нет молчаливой клетки»: каждый механизм, который держит её ход,
# обязан быть виден в реестре. Новый гейт в коде → добавь запись в rails.registry().
EXPECTED_RAILS = {
    "proposal_risk",      # selfdev.PROTECTED_PATTERNS is evidence, not authority
    "proposal_review",    # selfdev.submit: ревью + кап идентичных диффов
    "immune_review",      # immune red is recorded advice, not veto
    "core_edit_check",    # PRAXIS_CORE_EDIT_CHECK
    "host_scope",         # optional explicit PRAXIS_PROTECTED_ROOTS
    "bootguard_panic",    # bootguard + .panic
    "hands_floor",        # Rust-пол / джейл / shrink-guard
    "evaluator_mirror",   # privacy-only destination authority
    "mode_and_silence",   # РЕЖИМ / [молчу]
    "perception_pacing",  # дебаунс/кулдауны/LAST_N
    "unknown_cap",        # кап незнакомцев
    "absence_cap",        # кап «в отсутствие»
    "window_gates",       # тихие часы/зазор/кап/дедуп окон
    "tool_budget",        # max_tool_iters / context budget
    "appetite_pause",     # PASS 18: пауза фона по слову Егора / её состоянию
    "server_eyes",        # hostview за токеном
    "server_hands",       # services файл-сигнал
    "host_edits",         # hostops stage / hostagent
    "provider_remaining", # физика: остаток не виден
}

VALID_CLASSES = {rails.CLS_HOME, rails.CLS_OWNER, rails.CLS_SELF, rails.CLS_PHYS}


class TestRegistry(Base):
    def test_no_silent_cage_checklist(self):
        ids = {r["id"] for r in rails.registry()}
        missing = EXPECTED_RAILS - ids
        self.assertFalse(missing, f"рельсы без записи в реестре (молчаливая клетка): {missing}")

    def test_every_rail_honest(self):
        for r in rails.registry():
            self.assertIn(r["cls"], VALID_CLASSES, f"{r['id']}: класс {r['cls']!r} не из честных")
            self.assertTrue(str(r["lever"]).strip(), f"{r['id']}: пустой рычаг")
            self.assertTrue(str(r["holds"]).strip(), f"{r['id']}: пусто «что держит»")
            self.assertTrue(str(r["why"]).strip(), f"{r['id']}: пусто «почему»")
            self.assertTrue(str(r["value"]).strip(), f"{r['id']}: пустое значение")

    def test_floor_skip_lever_visible(self):
        os.environ["PRAXIS_FLOOR_SKIP"] = "services.py"
        row = next(r for r in rails.registry() if r["id"] == "proposal_risk")
        self.assertNotIn("services.py", str(row["value"]))  # снятый паттерн ушёл из пола

    def test_registry_never_calls_model(self):
        rails.registry()  # _NoCallClient упадёт, если кто-то позовёт модель


class TestManifest(Base):
    def test_sync_writes_once(self):
        self.assertTrue(rails.sync_md())          # первый раз — записала
        self.assertFalse(rails.sync_md())         # без изменений — не трогает файл
        text = rails.RAILS_MD.read_text(encoding="utf-8")
        self.assertIn("Генерится кодом", text)
        for rid in EXPECTED_RAILS:
            self.assertIn(rid, text, f"манифест потерял рельс {rid}")

    def test_sync_rewrites_on_change(self):
        rails.sync_md()
        os.environ["PRAXIS_FLOOR_SKIP"] = "services.py"
        self.assertTrue(rails.sync_md())          # живая константа сменилась → перегенерация
        self.assertNotIn("services.py", rails.RAILS_MD.read_text(encoding="utf-8"))


class TestDeny(Base):
    def test_deny_writes_and_reads(self):
        rails.deny("secrets", "shell", "чтение llm.json")
        rails.deny("hands_floor", "fs_write", "x" * 500)  # detail режется
        recs = rails.recent_denials(10)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["rail"], "secrets")
        self.assertLessEqual(len(recs[1]["detail"]), 200)
        self.assertEqual(rails.denials_today(), 2)

    def test_compaction(self):
        n = rails.DENIALS_KEEP * 2 + 1                      # ровно первый перелив порога
        for i in range(n):
            rails.deny("secrets", "shell", f"n{i}")
        lines = rails.DENIALS_PATH.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), rails.DENIALS_KEEP)    # компакт сработал на переливе
        self.assertIn(f"n{n - 1}", lines[-1])               # хвост живой

    def test_deny_never_raises(self):
        rails.DENIALS_PATH = Path("\0bad" if os.name != "nt" else "?:*bad") / "x.jsonl"
        rails.deny("secrets", "shell", "не должен упасть")  # просто не бросает


class TestStateLine(Base):
    def test_state_line(self):
        line = rails.state_line()
        self.assertIn("рельсы:", line)
        self.assertIn("rails.md", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
