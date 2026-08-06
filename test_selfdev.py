"""Контур предложений: worktree-ветка, зоны, тесты, мёрж/отказ, запрос перезапуска.
PASS 16.4: submit требует ЕЁ ревью диффа; кап идентичных отклонённых диффов."""
import json
import re
import shutil

import _standenv
import subprocess
import tempfile
import unittest
from pathlib import Path

import selfdev

# Живое ревью для тестов (длиннее REVIEW_MIN_CHARS — иначе честный отказ «отписка»).
RV = "прочитала дифф: меняется ровно заявленное, откатов не трогаю, тесты изменение держат"


def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=30)


def _mk_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="selfdev_repo_"))
    _sh(d, "git", "init", "-q", "-b", "master")
    _sh(d, "git", "config", "user.name", "t")
    _sh(d, "git", "config", "user.email", "t@t")
    (d / ".gitignore").write_text(".env\n", encoding="utf-8")   # как в живом репо: .env не трекается
    (d / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (d / "soul").mkdir()
    (d / "soul" / "note.md").write_text("# n\n", encoding="utf-8")
    (d / "test_smoke.py").write_text(
        "import unittest\nimport core\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_v(self):\n"
        "        self.assertIn(core.VALUE, (1, 2))\n", encoding="utf-8")
    # Дверь фальшивого репо — та же, что у живого, и список её файлов читается из
    # самой двери (см. _standenv.copy_door): набранный руками, он однажды отстанет.
    _standenv.copy_door(Path(__file__).resolve().parent, d)
    _sh(d, "git", "add", "-A")
    _sh(d, "git", "commit", "-q", "-m", "init")
    return d


class SelfdevFlow(unittest.TestCase):
    def setUp(self):
        self.repo = _mk_repo()
        self._old_repo = selfdev.REPO
        selfdev.REPO = self.repo
        # леджер/контроль в песочнице PRAXIS_BASE (см. _sandbox) — чистим между тестами
        selfdev.LEDGER.unlink(missing_ok=True)
        selfdev.POLICY_FILE.unlink(missing_ok=True)
        selfdev.clear_restart_request()

    def tearDown(self):
        selfdev.REPO = self._old_repo

    def _begin_and_edit(self, rel: str, content: str, reason="хочу лучше"):
        r = selfdev.begin(reason)
        self.assertTrue(r["ok"], r)
        (Path(r["path"]) / rel).write_text(content, encoding="utf-8")
        return r["id"]

    def test_zones(self):
        self.assertEqual(selfdev.zone_for(["soul/note.md"]), "auto")
        self.assertEqual(selfdev.zone_for(["core.py"]), "review")
        self.assertEqual(selfdev.zone_for(["soul/note.md", "core.py"]), "review")
        self.assertEqual(selfdev.zone_for(["bootguard.py"]), "protected")
        self.assertEqual(selfdev.zone_for(["docker-compose.deploy.yml"]), "protected")
        self.assertEqual(selfdev.zone_for([".env"]), "review")
        self.assertEqual(selfdev.zone_for([]), "review")

    def test_begin_creates_worktree_branch(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        self.assertTrue((self.repo / ".proposals" / pid).exists())
        branches = _sh(self.repo, "git", "branch", "--list", f"proposal/{pid}").stdout
        self.assertIn(pid, branches)
        self.assertEqual(selfdev.get(pid)["status"], "building")

    def test_submit_review_zone_merges_her_reviewed_decision(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        msg = selfdev.submit(pid, "поднять VALUE", "для дела", review=RV)
        self.assertIn("смёржила сама", msg)
        t = selfdev.get(pid)
        self.assertEqual(t["status"], "merged")
        self.assertEqual(t["zone"], "review")
        self.assertTrue(t["tests"]["ok"], t["tests"])
        self.assertEqual(t["files"], ["core.py"])
        self.assertIn("VALUE = 2", (self.repo / "core.py").read_text())

    def test_apply_merges_and_requests_restart(self):
        pid = self._begin_and_edit("core.py", "VALUE = 99\n")
        selfdev.submit(pid, "поднять VALUE", review=RV)
        res = selfdev.apply(pid, by="egor")
        self.assertTrue(res["ok"], res)
        self.assertIn("VALUE = 99", (self.repo / "core.py").read_text(encoding="utf-8"))
        self.assertEqual(selfdev.get(pid)["status"], "merged")
        self.assertIn("merged", selfdev.restart_requested())
        self.assertFalse((self.repo / ".proposals" / pid).exists())  # прибрано

    def test_auto_zone_merges_itself(self):
        pid = self._begin_and_edit("soul/note.md", "# n\nновая строка\n")
        msg = selfdev.submit(pid, "дописать заметку", review=RV)
        self.assertIn("смёржила сама", msg)
        self.assertEqual(selfdev.get(pid)["status"], "merged")
        self.assertIn("новая строка", (self.repo / "soul" / "note.md").read_text(encoding="utf-8"))

    def test_red_tests_do_not_automerge_but_wait(self):
        pid = self._begin_and_edit("core.py", "VALUE = 99\n")  # smoke-тест упадёт
        msg = selfdev.submit(pid, "сломать всё", review=RV)
        t = selfdev.get(pid)
        self.assertEqual(t["status"], "proposed")
        self.assertFalse(t["tests"]["ok"])
        self.assertIn("ПАДЕНИЯ", t["tests"]["summary"])
        self.assertIn("override_reason", msg)

    def test_protected_zone_is_risk_evidence_not_a_veto(self):
        pid = self._begin_and_edit("bootguard.py", "x = 1\n")
        msg = selfdev.submit(pid, "трогаю рельсы", review=RV)
        t = selfdev.get(pid)
        self.assertEqual(t["zone"], "protected")
        self.assertEqual(t["status"], "merged")
        self.assertIn("смёржила сама", msg)

    def test_red_checks_can_be_explicitly_overridden_with_provenance(self):
        pid = self._begin_and_edit("core.py", "VALUE = 99\n")
        reason = "smoke фиксирует старый диапазон; новая семантика намеренно расширяет его"
        msg = selfdev.submit(pid, "осознанно расширить VALUE", review=RV,
                             override_reason=reason)
        self.assertIn("override", msg)
        item = selfdev.get(pid)
        self.assertEqual(item["status"], "merged")
        self.assertEqual(item["override_reason"], reason)
        self.assertIn("VALUE = 99", (self.repo / "core.py").read_text(encoding="utf-8"))

    def test_reject_writes_journal_signal(self):
        pid = self._begin_and_edit("core.py", "VALUE = 99\n")
        selfdev.submit(pid, "поднять VALUE", review=RV)
        res = selfdev.reject(pid, "не время", by="egor")
        self.assertTrue(res["ok"])
        t = selfdev.get(pid)
        self.assertEqual(t["status"], "rejected")
        self.assertEqual(t["reason"], "не время")
        day = sorted(selfdev.JOURNAL_DIR.glob("*.md"))[-1].read_text(encoding="utf-8")
        self.assertIn("отклонил", day)
        self.assertIn("не время", day)
        self.assertFalse((self.repo / ".proposals" / pid).exists())

    def test_empty_proposal_not_submittable(self):
        r = selfdev.begin("пусто")
        msg = selfdev.submit(r["id"], "ничего", review=RV)
        self.assertIn("нет изменений", msg)

    def test_diff_text_shows_change(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        d = selfdev.diff_text(pid)
        self.assertIn("+VALUE = 2", d)

    def test_notifications_flow(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        selfdev.submit(pid, "поднять VALUE", review=RV)
        ids = [t["id"] for t in selfdev.unnotified()]
        self.assertIn(pid, ids)
        selfdev.mark_notified(pid)
        self.assertNotIn(pid, [t["id"] for t in selfdev.unnotified()])

    def test_policy_file_extends_auto(self):
        selfdev.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
        selfdev.POLICY_FILE.write_text(json.dumps({"auto": ["soul/*", "test_*.py"]}), encoding="utf-8")
        self.assertEqual(selfdev.zone_for(["test_smoke.py"]), "auto")
        self.assertEqual(selfdev.zone_for(["bootguard.py"]), "protected")  # пол не переопределить

    def test_restart_request_roundtrip(self):
        selfdev.request_restart("проверка")
        self.assertIn("проверка", selfdev.restart_requested())
        selfdev.clear_restart_request()
        self.assertEqual(selfdev.restart_requested(), "")


class ReviewIsHers(unittest.TestCase):
    """PASS 16.4: «код она ревьюит сама» — submit без её ревью не уходит."""

    # та же песочница, что у SelfdevFlow (методы — обычные функции, переиспользуем)
    setUp = SelfdevFlow.setUp
    tearDown = SelfdevFlow.tearDown
    _begin_and_edit = SelfdevFlow._begin_and_edit

    def test_submit_without_review_refused(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        msg = selfdev.submit(pid, "поднять VALUE")
        self.assertIn("proposal_diff", msg, "отказ обязан подсказать посмотреть дифф")
        self.assertEqual(selfdev.get(pid)["status"], "building", "предложение осталось открытым")
        self.assertIn("VALUE = 1", (self.repo / "core.py").read_text())

    def test_token_review_refused(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        msg = selfdev.submit(pid, "поднять VALUE", review="ок, норм")
        self.assertIn("отписка", msg)
        self.assertEqual(selfdev.get(pid)["status"], "building")

    def test_review_copying_title_refused(self):
        title = "поднять VALUE до двойки ради smoke-теста и проверки контура"
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        msg = selfdev.submit(pid, title, review=title)
        self.assertIn("title", msg)
        self.assertEqual(selfdev.get(pid)["status"], "building")

    def test_review_and_checked_reach_ledger_and_journal(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        selfdev.submit(pid, "поднять VALUE", review=RV, checked="тесты + прогнала smoke руками")
        t = selfdev.get(pid)
        self.assertEqual(t["review"], RV)
        self.assertIn("smoke", t["checked"])
        self.assertTrue(t.get("fingerprint"), "отпечаток диффа обязан сохраниться")
        day = sorted(selfdev.JOURNAL_DIR.glob("*.md"))[-1].read_text(encoding="utf-8")
        self.assertIn("моё ревью", day)
        self.assertIn("проверено:", day)

    def test_diff_visible_before_submit(self):
        """Она читает дифф ДО submit — незакоммиченные правки видны (по ним пишется ревью)."""
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        d = selfdev.diff_text(pid)
        self.assertIn("+VALUE = 2", d)
        # и новый файл тоже виден (add -A внутри diff_text); содержимое ASCII —
        # чтобы локальный прогон на Windows-локали не спотыкался о декодировку git
        pid2 = self._begin_and_edit("soul/idea.md", "# idea-mark\n")
        self.assertIn("idea-mark", selfdev.diff_text(pid2))

    def test_identical_rejected_diff_capped_at_three(self):
        """3 отклонённых байт-в-байт диффа → «измени подход»; другой дифф проходит."""
        for _ in range(3):
            pid = self._begin_and_edit("core.py", "VALUE = 99\n")
            selfdev.submit(pid, "поднять VALUE", review=RV)
            selfdev.reject(pid, "не время", by="egor")
        pid4 = self._begin_and_edit("core.py", "VALUE = 99\n")
        msg = selfdev.submit(pid4, "поднять VALUE", review=RV)
        self.assertIn("подход", msg, "4-я подача того же диффа должна упереться в кап")
        self.assertEqual(selfdev.get(pid4)["status"], "building")
        # изменившийся дифф обнуляет счёт — уходит нормально
        pid5 = self._begin_and_edit("core.py", "VALUE = 98  # другой путь\n")
        msg5 = selfdev.submit(pid5, "поднять VALUE иначе", review=RV)
        self.assertEqual(selfdev.get(pid5)["status"], "proposed", msg5)


class ReconcileShells(SelfdevFlow):
    """Оболочки после рестартов: submit падал на гейте между коммитом и _update —
    реконсайлер прибирает беспредметные и возвращает титулы, не решая за Праксис."""

    def test_submit_persists_title_before_the_gate(self):
        pid = self._begin_and_edit("core.py", "VALUE = 2\n")
        orig = selfdev.run_tests

        def gate_crash(_pid):
            raise RuntimeError("рестарт посреди гейта")

        selfdev.run_tests = gate_crash
        try:
            with self.assertRaises(RuntimeError):
                selfdev.submit(pid, "поднять VALUE", "для дела", review=RV)
        finally:
            selfdev.run_tests = orig
        row = selfdev.get(pid)
        self.assertEqual(row["status"], "building")
        self.assertEqual(row["title"], "поднять VALUE", "титул фиксируется ДО гейта")

    def test_reconcile_restores_title_from_branch_commit(self):
        pid = self._begin_and_edit("core.py", "VALUE = 3\n")
        wt = selfdev.worktree_path(pid)
        _sh(wt, "git", "add", "-A")
        _sh(wt, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", f"proposal {pid}: живой титул из коммита")
        out = selfdev.reconcile()
        self.assertEqual(out["restored"], 1)
        row = selfdev.get(pid)
        self.assertEqual(row["status"], "building", "решение о submit остаётся за ней")
        self.assertEqual(row["title"], "живой титул из коммита")

    def test_reconcile_closes_empty_diff_shell(self):
        r = selfdev.begin("пустая оболочка")
        pid = r["id"]
        out = selfdev.reconcile()
        self.assertEqual(out["closed"], 1)
        row = selfdev.get(pid)
        self.assertEqual(row["status"], "rejected")
        self.assertIn("не изменил бы живое дерево", row["reason"])
        branches = _sh(self.repo, "git", "branch", "--list", f"proposal/{pid}").stdout
        self.assertNotIn(pid, branches, "ветка пустой оболочки убрана")

    def test_reconcile_closes_shell_with_missing_branch(self):
        r = selfdev.begin("оболочка без ветки")
        pid = r["id"]
        _sh(self.repo, "git", "worktree", "remove", "--force",
            str(selfdev.worktree_path(pid)))
        _sh(self.repo, "git", "branch", "-D", f"proposal/{pid}")
        out = selfdev.reconcile()
        self.assertEqual(out["closed"], 1)
        self.assertIn("ветка предложения пропала", selfdev.get(pid)["reason"])

    def test_reconcile_closes_work_that_the_live_tree_already_contains(self):
        """Ветка писалась против кода двухнедельной давности, а она с тех пор сама
        поправила то же место живой правкой. Старый `main...branch` этого не видел
        никогда: он спрашивал «что ветка изменила относительно СВОЕГО прошлого», и
        ответ оставался непустым навсегда. На 01.08 так висели 35 предложений."""
        pid = self._begin_and_edit("core.py", "VALUE = 42\n")
        wt = selfdev.worktree_path(pid)
        _sh(wt, "git", "add", "-A")
        _sh(wt, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", f"proposal {pid}: то же самое, но раньше")
        # живое дерево пришло к тому же результату своим путём
        (self.repo / "core.py").write_text("VALUE = 42\n", encoding="utf-8")
        _sh(self.repo, "git", "add", "-A")
        _sh(self.repo, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", "self-edit: то же самое, живой рукой")

        self.assertEqual(selfdev.live_effect(f"proposal/{pid}")[0], "noop")
        out = selfdev.reconcile()
        self.assertEqual(out["closed"], 1)
        self.assertIn("уже в нём", selfdev.get(pid)["reason"])

    def test_reconcile_records_the_diff_but_never_decides(self):
        """Реестр обязан говорить правду о предложении, но закрытие — её решение."""
        pid = self._begin_and_edit("core.py", "VALUE = 7\n")
        wt = selfdev.worktree_path(pid)
        _sh(wt, "git", "add", "-A")
        _sh(wt, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", f"proposal {pid}: настоящая правка")
        self.assertEqual(selfdev.get(pid).get("diffstat") or "", "")

        out = selfdev.reconcile()
        row = selfdev.get(pid)
        self.assertEqual(row["status"], "building", "реконсайлер не решает за неё")
        self.assertEqual(out["described"], 1, "правда о диффе считается отдельно от титулов")
        self.assertTrue(str(row.get("diffstat") or "").strip(),
                        "карточка предложения по-прежнему врёт «без диффа»")

    def test_live_effect_names_a_conflict_instead_of_promising_a_merge(self):
        pid = self._begin_and_edit("core.py", "VALUE = 'ветка'\n")
        wt = selfdev.worktree_path(pid)
        _sh(wt, "git", "add", "-A")
        _sh(wt, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", f"proposal {pid}: своя версия")
        (self.repo / "core.py").write_text("VALUE = 'живое дерево'\n", encoding="utf-8")
        _sh(self.repo, "git", "add", "-A")
        _sh(self.repo, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", "self-edit: другая версия того же места")

        effect, stat = selfdev.live_effect(f"proposal/{pid}")
        self.assertEqual(effect, "conflict")
        self.assertIn("конфликт", stat)
        selfdev.reconcile()
        self.assertEqual(selfdev.get(pid)["status"], "building",
                         "конфликт — повод сказать правду, а не закрыть за неё")

    def test_reconcile_leaves_titled_real_work_alone(self):
        pid = self._begin_and_edit("core.py", "VALUE = 4\n")
        wt = selfdev.worktree_path(pid)
        _sh(wt, "git", "add", "-A")
        _sh(wt, "git", "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
            "commit", "-q", "-m", f"proposal {pid}: настоящая работа")
        selfdev.reconcile()
        selfdev.reconcile()  # идемпотентно
        row = selfdev.get(pid)
        self.assertEqual(row["status"], "building")
        self.assertEqual(row["title"], "настоящая работа")


if __name__ == "__main__":
    unittest.main()
