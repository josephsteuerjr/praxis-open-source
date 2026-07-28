"""
Тесты PASS 17.B — её руки на своих сервисах (вторая ступень «хозяйки сервера»).

Ступень даёт первое действие на уровне машины, поэтому проверяем границы, а не удобство:
  1. руки достают только до тех, кто согласился слушать (allowlist + честные отказы);
  2. стоп-кран и квота действуют — «перезапустить ещё раз» не может стать петлёй;
  3. сервис с read-only кодом (serverapp) не зацикливается: просьба «свежая» только
     если флаг новее старта процесса;
  4. каждая попытка (и отказ тоже) оставляет расписку.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import services


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_svc_"))
        (self.tmp / "memory" / ".state").mkdir(parents=True)
        self._orig = {
            "RECEIPTS": services.RECEIPTS, "STATE_DIR": services.STATE_DIR,
            "PANIC_SENTINEL": services.PANIC_SENTINEL,
        }
        services.STATE_DIR = self.tmp / "memory" / ".state"
        services.RECEIPTS = services.STATE_DIR / "services.jsonl"
        services.PANIC_SENTINEL = self.tmp / "memory" / ".panic"
        self._flags = {n: s.flag for n, s in services.SERVICES.items()}
        for name, svc in services.SERVICES.items():
            if svc.flag is not None:
                svc.flag = self.tmp / "memory" / f".restart_{name}"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(services, k, v)
        for name, flag in self._flags.items():
            services.SERVICES[name].flag = flag
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _receipts(self) -> list[dict]:
        if not services.RECEIPTS.exists():
            return []
        return [json.loads(l) for l in services.RECEIPTS.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
#  1. Границы: до кого руки достают
# --------------------------------------------------------------------------- #

class TestReach(Base):
    def test_signal_service_gets_a_flag(self):
        ok, msg = services.request_restart("serverapp", "глаза ослепли")
        self.assertTrue(ok, msg)
        flag = services.SERVICES["serverapp"].flag
        self.assertTrue(flag.exists())
        self.assertIn("глаза ослепли", flag.read_text(encoding="utf-8"))
        self.assertIn("перезапуст", msg.lower())

    def test_foreign_process_is_an_honest_refusal(self):
        """relay — чужой процесс: он её сигнал не читает. Отказ должен об этом СКАЗАТЬ,
        а не притвориться успехом (и предложить то, что она правда может — логи)."""
        ok, msg = services.request_restart("relay", "завис")
        self.assertFalse(ok)
        self.assertIn("чужой процесс", msg)
        self.assertIn("server_logs", msg)
        self.assertIsNone(services.SERVICES["relay"].flag)

    def test_self_restart_is_a_different_lever(self):
        ok, msg = services.request_restart("praxis", "хочу")
        self.assertFalse(ok)
        self.assertIn("restart_self", msg)

    def test_unknown_service_lists_what_is_possible(self):
        ok, msg = services.request_restart("postgres", "почему бы и нет")
        self.assertFalse(ok)
        self.assertIn("Не знаю сервис", msg)
        self.assertIn("mailbot", msg)
        self.assertIn("serverapp", msg)

    def test_foreign_projects_are_not_in_log_allowlist(self):
        """Чужие проекты Егора на этой машине (dasha, vroom, hardbot…) — не её дело."""
        for foreign in ("dasha-bot", "vroom", "hardbot2-bot-1", "amnezia-awg2",
                        "praxis-dockerproxy"):
            self.assertNotIn(foreign, services.LOG_ALLOWED)
        for mine in ("praxis", "praxis-mailbot", "praxis-serverapp", "relay"):
            self.assertIn(mine, services.LOG_ALLOWED)


# --------------------------------------------------------------------------- #
#  2. Стоп-кран и квота
# --------------------------------------------------------------------------- #

class TestRails(Base):
    def test_panic_stops_these_hands_too(self):
        services.PANIC_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        services.PANIC_SENTINEL.write_text("стоп", encoding="utf-8")
        ok, msg = services.request_restart("serverapp", "всё равно хочу")
        self.assertFalse(ok)
        self.assertIn("Стоп-кран", msg)
        self.assertFalse(services.SERVICES["serverapp"].flag.exists(),
                         "при поднятом стоп-кране флаг не должен появиться")

    def test_quota_turns_a_loop_into_a_refusal(self):
        for i in range(services.RESTARTS_PER_HOUR):
            ok, _ = services.request_restart("serverapp", f"попытка {i}")
            self.assertTrue(ok)
        ok, msg = services.request_restart("serverapp", "ещё разок")
        self.assertFalse(ok, "четвёртый рестарт за час — это петля")
        self.assertIn("петля", msg)
        self.assertIn("server_logs", msg)

    def test_quota_is_per_unit(self):
        for i in range(services.RESTARTS_PER_HOUR):
            services.request_restart("serverapp", f"{i}")
        ok, msg = services.request_restart("mailbot", "другой сервис")
        self.assertTrue(ok, "квота считается на юнит, а не на все руки сразу")

    def test_old_restarts_fall_out_of_the_window(self):
        services.STATE_DIR.mkdir(parents=True, exist_ok=True)
        old = time.time() - 7200      # два часа назад
        with services.RECEIPTS.open("w", encoding="utf-8") as fh:
            for _ in range(5):
                fh.write(json.dumps({"ts": old, "op": "restart", "unit": "serverapp",
                                     "ok": True}) + "\n")
        self.assertEqual(services._recent_restarts("serverapp"), 0)
        ok, _ = services.request_restart("serverapp", "час прошёл")
        self.assertTrue(ok)


# --------------------------------------------------------------------------- #
#  3. Read-only сервис не зацикливается
# --------------------------------------------------------------------------- #

class TestReadOnlyServiceDoesNotLoop(Base):
    def test_flag_older_than_start_is_not_a_request(self):
        """serverapp монтирует код ro и снести флаг НЕ МОЖЕТ. Если бы «есть флаг» значило
        «выйди», он бы выходил вечно. Свежесть определяет метка времени."""
        services.request_restart("serverapp", "перезапустись")
        flag = services.SERVICES["serverapp"].flag
        mtime = flag.stat().st_mtime

        # процесс, стартовавший ДО просьбы, обязан её увидеть
        self.assertIn("перезапустись", services.restart_requested("serverapp", mtime - 5))
        # процесс, поднявшийся ПОСЛЕ (тот же флаг всё ещё лежит) — не должен выходить снова
        self.assertEqual(services.restart_requested("serverapp", mtime + 5), "")

    def test_no_flag_no_request(self):
        self.assertEqual(services.restart_requested("serverapp", time.time()), "")

    def test_unknown_unit_never_asks_to_restart(self):
        self.assertEqual(services.restart_requested("postgres", 0), "")
        self.assertEqual(services.restart_requested("relay", 0), "")

    def test_clear_flag_for_rw_services(self):
        services.request_restart("mailbot", "почта висит")
        self.assertTrue(services.SERVICES["mailbot"].flag.exists())
        services.clear_flag("mailbot")
        self.assertFalse(services.SERVICES["mailbot"].flag.exists())
        services.clear_flag("mailbot")   # повторно — не падает
        services.clear_flag("relay")     # без флага — не падает


# --------------------------------------------------------------------------- #
#  4. Расписки: и успех, и отказ
# --------------------------------------------------------------------------- #

class TestReceipts(Base):
    def test_success_is_written(self):
        services.request_restart("serverapp", "ослепла")
        rows = self._receipts()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["op"], rows[0]["unit"], rows[0]["ok"]), ("restart", "serverapp", True))
        self.assertIn("ослепла", rows[0]["note"])

    def test_quota_refusal_is_written_too(self):
        for i in range(services.RESTARTS_PER_HOUR + 1):
            services.request_restart("serverapp", f"{i}")
        rows = self._receipts()
        self.assertTrue(any(r["ok"] is False and "квота" in r["note"] for r in rows),
                        "отказ по квоте обязан оставить след — иначе его не видно в разборе")

    def test_describe_mentions_quota_and_foreign(self):
        services.request_restart("serverapp", "раз")
        text = services.describe()
        self.assertIn("serverapp", text)
        self.assertIn("1/3", text)
        self.assertIn("только Егор", text)
        self.assertIn("relay", text)


# --------------------------------------------------------------------------- #
#  5. Тулы: действие — не разведчику, чтение — можно
# --------------------------------------------------------------------------- #

class TestToolWiring(unittest.TestCase):
    def test_manage_service_is_owner_only_and_not_for_the_scout(self):
        import agent
        self.assertIn("manage_service", agent.TOOL_IMPL)
        self.assertIn("manage_service", [t["name"] for t in agent.OWNER_TOOLS])
        self.assertNotIn("manage_service", [t["name"] for t in agent.BASE_TOOLS])
        self.assertNotIn("manage_service", [t["name"] for t in agent.FAMILY_TOOLS])
        self.assertNotIn("manage_service", agent._SCOUT_TOOL_NAMES,
                         "разведчик ничего не меняет — это его определение")

    def test_server_logs_is_readable_by_the_scout(self):
        import agent
        self.assertIn("server_logs", agent._SCOUT_TOOL_NAMES)
        self.assertIn("server_logs", [t["name"] for t in agent.OWNER_TOOLS])
        self.assertNotIn("server_logs", [t["name"] for t in agent.BASE_TOOLS])

    def test_restart_mailbot_still_works_through_the_registry(self):
        """Старый рычаг PASS 12.x жив, но теперь под квотой, стоп-краном и распиской."""
        import agent
        self.assertIs(agent.RESTART_MAILBOT_FLAG, services.SERVICES["mailbot"].flag)

    def test_list_action_describes(self):
        import agent
        out = agent.TOOL_IMPL["manage_service"]("list")
        self.assertIn("serverapp", out)
        self.assertIn("relay", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
