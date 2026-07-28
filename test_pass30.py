"""PASS 30 «Корабль Тесея», Этап 0 — тесты симптомных фиксов.

0.c observe(file): глаза на файлы-картинки (computer.observe path=... и fs_read).
0.e гигиена: рестарт-сироты без улик закрываются; транспорт-сенсор честен.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PNG = b"\x89PNG\r\n\x1a\n" + b"DATA"
JPG = b"\xff\xd8\xff\xe0" + b"DATA"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"DATA"
GIF = b"GIF89a" + b"DATA"


class SniffImageMimeTests(unittest.TestCase):
    def test_magic_covers_png_jpeg_webp_gif_and_rejects_rest(self):
        import workshop

        self.assertEqual(workshop.sniff_image_mime(PNG[:16]), "image/png")
        self.assertEqual(workshop.sniff_image_mime(JPG[:16]), "image/jpeg")
        self.assertEqual(workshop.sniff_image_mime(WEBP[:16]), "image/webp")
        self.assertEqual(workshop.sniff_image_mime(GIF[:16]), "image/gif")
        self.assertIsNone(workshop.sniff_image_mime(b"MZ\x90\x00binary"))
        self.assertIsNone(workshop.sniff_image_mime(b""))
        # WebP-магия требует полных 12 байт — усечённый RIFF не картинка.
        self.assertIsNone(workshop.sniff_image_mime(b"RIFF\x00\x00"))


class FsProbeImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1",
        })
        self.env.start()
        self.base = Path(self.tmp.name)
        (self.base / "workspace").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_probe_detects_image_and_ignores_text(self):
        import workshop

        image = self.base / "workspace" / "frame.png"
        image.write_bytes(PNG)
        text = self.base / "workspace" / "note.txt"
        text.write_text("привет", encoding="utf-8")
        with mock.patch.object(workshop, "BASE", self.base), \
                mock.patch("hands.guard", return_value=None):
            probe = workshop.fs_probe_image(str(image))
            self.assertIsNotNone(probe)
            resolved, mime, size = probe
            self.assertEqual(mime, "image/png")
            self.assertEqual(size, len(PNG))
            self.assertEqual(Path(resolved), image.resolve())
            self.assertIsNone(workshop.fs_probe_image(str(text)))
            self.assertIsNone(workshop.fs_probe_image(str(self.base / "нет-такого.png")))

    def test_probe_defers_to_guard_refusal_without_reading_bytes(self):
        import workshop

        image = self.base / "workspace" / "frame.png"
        image.write_bytes(PNG)
        with mock.patch.object(workshop, "BASE", self.base), \
                mock.patch("hands.guard", return_value={"ok": False, "msg": "рельсы"}):
            self.assertIsNone(workshop.fs_probe_image(str(image)))

    def test_fs_read_wrapper_returns_pixels_for_image_and_text_for_text(self):
        import agent
        import workshop

        image = self.base / "workspace" / "frame.jpg"
        image.write_bytes(JPG)
        text = self.base / "workspace" / "note.txt"
        text.write_text("строка раз", encoding="utf-8")
        memory = self.base / "memory"
        with mock.patch.object(workshop, "BASE", self.base), \
                mock.patch.object(agent, "MEM_DIR", memory), \
                mock.patch("hands.guard", return_value=None):
            seen = agent.tool_fs_read(str(image))
            self.assertIsInstance(seen, agent.ToolObservation)
            self.assertEqual(seen.images[0]["origin"], "computer-observe")
            self.assertEqual(seen.images[0]["mime"], "image/jpeg")
            self.assertTrue(Path(seen.images[0]["path"]).is_file())
            self.assertIn("frame.jpg", seen.text)
            plain = agent.tool_fs_read(str(text))
            self.assertIsInstance(plain, str)
            self.assertIn("строка раз", plain)

    def test_fs_read_wrapper_refuses_oversized_image_without_loading(self):
        import agent
        import workshop

        image = self.base / "workspace" / "huge.png"
        image.write_bytes(PNG)
        with mock.patch.object(workshop, "BASE", self.base), \
                mock.patch("hands.guard", return_value=None), \
                mock.patch.object(agent, "_OBSERVE_FILE_CAP", 4):
            answer = agent.tool_fs_read(str(image))
        self.assertIsInstance(answer, str)
        self.assertIn("больше капа", answer)


class ObserveFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _observe_path(self, payload: bytes, artifact: dict):
        import agent

        def fake_fetch(_artifact, path):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            return {"ok": True, "path": str(path),
                    "sha256": artifact.get("sha256"), "size": artifact.get("size")}

        with mock.patch.object(agent, "MEM_DIR", Path(self.tmp.name) / "memory"), \
                mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.call", return_value={"ok": True, "artifact": artifact}) as export, \
                mock.patch("body_client.fetch_artifact", side_effect=fake_fetch), \
                mock.patch("body_client.desktop_screen_capture") as capture, \
                mock.patch("workshop.send_file") as send:
            result = agent.tool_computer(
                "observe", path=r"C:\\Users\\yegor\\frame.jpg",
            )
        capture.assert_not_called()
        send.assert_not_called()
        export.assert_called_once_with(
            "fs.export", {"path": r"C:\\Users\\yegor\\frame.jpg"},
            execution="interactive", timeout=600,
        )
        return result

    def test_observe_with_path_attaches_file_pixels(self):
        import agent

        artifact = {"name": "frame.jpg", "sha256": "d" * 64, "size": len(JPG)}
        result = self._observe_path(JPG, artifact)
        self.assertIsInstance(result, agent.ToolObservation)
        self.assertEqual(result.images[0]["mime"], "image/jpeg")
        self.assertEqual(result.images[0]["origin"], "computer-observe")
        self.assertTrue(Path(result.images[0]["path"]).is_file())

    def test_observe_with_path_refuses_non_image(self):
        import agent

        artifact = {"name": "tool.exe", "sha256": "e" * 64, "size": 10}
        result = self._observe_path(b"MZ\x90\x00DATA", artifact)
        self.assertIsInstance(result, str)
        self.assertIn("не картинка", result)

    def test_observe_with_path_refuses_oversized_file_before_fetch(self):
        import agent

        artifact = {"name": "movie.mp4", "sha256": "f" * 64,
                    "size": agent._OBSERVE_FILE_CAP + 1}
        with mock.patch.object(agent, "MEM_DIR", Path(self.tmp.name) / "memory"), \
                mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.call", return_value={"ok": True, "artifact": artifact}), \
                mock.patch("body_client.fetch_artifact") as fetch:
            result = agent.tool_computer("observe", path=r"C:\\movie.mp4")
        fetch.assert_not_called()
        self.assertIsInstance(result, str)
        self.assertIn("больше капа", result)

    def test_observe_with_path_is_hers_regardless_of_the_speakers_grant(self):
        import agent
        import computer_access

        computer_access.change("grant", "202", name="A", scopes=["computer.apps"], actor="101")
        ctx = agent.ChannelContext(chat_id="202", principal_id="202", is_dm=True, known=True)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            # Решение Егора 26.07 (вариант 1): в её ходе действует ОНА, кто бы ни
            # заговорил. Грант, выданный человеку, больше не сужает ЕЁ руки: раньше
            # «computer.apps» у собеседника закрывал ей чтение файла с её же тела.
            with mock.patch("body_client.call",
                            return_value={"ok": True, "artifact": None}) as export:
                agent.tool_computer("observe", path=r"C:\\frame.png")
            export.assert_called_once()
            # без path observe остаётся десктопным правом computer.apps
            with mock.patch("body_client.desktop_screen_capture",
                            return_value={"ok": False, "error": "nope"}):
                screen = agent.tool_computer("observe")
            self.assertNotIn("computer.files", str(screen))
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_screen_observe_still_returns_png_pixels(self):
        import agent

        artifact = {"name": "desktop.png", "sha256": "c" * 64, "size": len(PNG)}

        def fake_fetch(_artifact, path):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)
            return {"ok": True, "path": str(path), "sha256": "c" * 64, "size": len(PNG)}

        with mock.patch.object(agent, "MEM_DIR", Path(self.tmp.name) / "memory"), \
                mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.desktop_screen_capture",
                           return_value={"ok": True, "artifact": artifact}), \
                mock.patch("body_client.fetch_artifact", side_effect=fake_fetch):
            result = agent.tool_computer("observe", target="desktop")
        self.assertIsInstance(result, agent.ToolObservation)
        self.assertEqual(result.images[0]["mime"], "image/png")


class TransportStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_three_states_and_unknown_without_bridge(self):
        import agent

        with mock.patch.dict(agent._TELETHON, {"transport_state": lambda: {
                "connected": True, "intentional_window": False}}):
            self.assertEqual(agent.telegram_transport_status(), "connected")
        with mock.patch.dict(agent._TELETHON, {"transport_state": lambda: {
                "connected": False, "intentional_window": True}}):
            self.assertEqual(agent.telegram_transport_status(), "closed_for_window")
        with mock.patch.dict(agent._TELETHON, {"transport_state": lambda: {
                "connected": False, "intentional_window": False}}):
            self.assertEqual(agent.telegram_transport_status(), "disconnected")
        clean = dict(agent._TELETHON)
        clean.pop("transport_state", None)
        with mock.patch.object(agent, "_TELETHON", clean):
            self.assertEqual(agent.telegram_transport_status(), "unknown")

    def test_read_chat_answers_honestly_when_transport_intentionally_closed(self):
        import agent

        read = mock.Mock()
        with mock.patch.dict(agent._TELETHON, {
                "read_chat": read,
                "transport_state": lambda: {"connected": False, "intentional_window": True},
        }):
            answer = agent.tool_read_chat("@somebody")
        read.assert_not_called()
        self.assertIn("фокус-окно", answer)
        self.assertNotIn("не смогла прочитать", answer)

    def test_search_chats_reports_real_disconnect_as_channel_failure(self):
        import agent

        search = mock.Mock()
        with mock.patch.dict(agent._TELETHON, {
                "search_chats": search,
                "transport_state": lambda: {"connected": False, "intentional_window": False},
        }):
            answer = agent.tool_search_chats("Маша")
        search.assert_not_called()
        self.assertIn("сбой канала", answer)


class EvidenceFreeOrphanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _plan(self, kind="not_resumable", reason=None):
        import agent

        return SimpleNamespace(
            kind=kind,
            reason=agent._NO_EVIDENCE_RESUME_REASON if reason is None else reason,
        )

    def _manager(self, *, status="paused", control=None, kind="chat_turn", events=()):
        manager = mock.Mock()
        manager.manifest.return_value = {
            "status": status, "control": control or {}, "context": {"kind": kind},
        }
        manager.iter_events.return_value = iter(events)
        return manager

    def test_zero_evidence_restart_orphan_is_cancelled_via_request_cancel(self):
        import agent

        manager = self._manager(events=[
            {"kind": "run_created"}, {"kind": "status_changed"}, {"kind": "status_changed"},
        ])
        closed = agent._close_evidence_free_restart_orphan(manager, "run-x", self._plan())
        self.assertTrue(closed)
        manager.request_cancel.assert_called_once()
        self.assertEqual(manager.request_cancel.call_args.args, ("run-x",))
        self.assertEqual(
            manager.request_cancel.call_args.kwargs["actor"], "resume:evidence-free-orphan")

    def test_any_doubt_leaves_the_run_untouched(self):
        import agent

        plan = self._plan()
        # чужая причина not_resumable — не трогаем
        manager = self._manager()
        self.assertFalse(agent._close_evidence_free_restart_orphan(
            manager, "run-x", self._plan(reason="interrupted model request has no checkpoint")))
        # есть tool-событие — не трогаем
        manager = self._manager(events=[{"kind": "tool_started", "tool": "telegram.deliver"}])
        self.assertFalse(agent._close_evidence_free_restart_orphan(manager, "run-x", plan))
        # висит control — не трогаем
        manager = self._manager(control={"action": "pause"})
        self.assertFalse(agent._close_evidence_free_restart_orphan(manager, "run-x", plan))
        # не когнитивный вид — не трогаем
        manager = self._manager(kind="coding_window")
        self.assertFalse(agent._close_evidence_free_restart_orphan(manager, "run-x", plan))
        # не paused — не трогаем
        manager = self._manager(status="blocked")
        self.assertFalse(agent._close_evidence_free_restart_orphan(manager, "run-x", plan))
        manager.request_cancel.assert_not_called()


class PromiseWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1",
        })
        self.env.start()
        import tasks
        self.tasks_path = mock.patch.object(
            tasks, "TASKS", Path(self.tmp.name) / "memory" / "tasks.json")
        self.tasks_path.start()

    def tearDown(self):
        self.tasks_path.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_detect_catches_the_actual_failure_phrase_of_2107(self):
        import promises

        gist = promises.detect(
            "Нашла: «Запись экрана 2026-07-21 161835.mp4», ровно 2:00. Уже вытащила "
            "кадры каждые 15 секунд; сейчас добью просмотр и скажу по котельной предметно."
        )
        self.assertIsNotNone(gist)
        self.assertIn("добью", gist)
        self.assertIsNone(promises.detect("Люблю котиков — они пушистые."))
        self.assertIsNone(promises.detect("Вчера всё сделала и рассказала."))
        self.assertIsNone(promises.detect(""))

    def test_detect_ignores_quoted_or_meta_descriptions_but_keeps_direct_promises(self):
        import promises
        import tasks

        meta = "Фраза обещания («Сейчас добью») создаёт 15-минутное promise-window."
        quoted = 'Детектор не должен считать обещанием цитату: "Сейчас добью".'
        self.assertIsNone(promises.detect(meta))
        self.assertIsNone(promises.detect(quoted))
        self.assertIsNone(promises.note_outbound("777", meta))
        self.assertEqual(tasks.list_open(), [])

        self.assertIsNotNone(promises.detect("Сейчас добью."))
        self.assertIsNotNone(promises.detect("Тест называется «Сейчас добью», а я сейчас проверю фикс."))
        self.assertIsNotNone(promises.note_outbound("777", "Сейчас добью и пришлю."))

    def test_note_outbound_schedules_one_wake_per_chat(self):
        import promises
        import tasks

        first = promises.note_outbound("777", "Окей, сейчас проверю и доложу.")
        self.assertIsNotNone(first)
        # 26.07: было "window", стало "wake". Возврат к обещанию — это прочитать нить и
        # ответить ЧЕЛОВЕКУ; в окне Telegram закрыт, то есть ровно эта работа в нём и
        # невозможна. Имя promise-wake было честнее механики — теперь совпало.
        self.assertEqual(first["kind"], "wake")
        self.assertEqual(first["target"], "promise:777")
        self.assertIsNotNone(first["when"])
        self.assertIn(promises.PROMISE_PREFIX, first["goal"])
        # дубль в той же нити не спамит вторым окном
        self.assertIsNone(promises.note_outbound("777", "Сейчас добью и пришлю."))
        opened = [t for t in tasks.list_open() if t.get("target") == "promise:777"]
        self.assertEqual(len(opened), 1)
        # другая нить — своё пробуждение
        self.assertIsNotNone(promises.note_outbound("888", "Иду чинить рельсы."))
        # снятое ей намерение освобождает нить для нового обещания
        tasks.cancel(first["id"])
        self.assertIsNotNone(promises.note_outbound("777", "Сейчас всё-таки доделаю."))

    def test_kill_switch_disables_detection(self):
        import promises
        import tasks

        with mock.patch.dict(os.environ, {"PRAXIS_PROMISE_WAKE": "0"}):
            self.assertIsNone(promises.note_outbound("777", "сейчас сделаю"))
        self.assertEqual(tasks.list_open(), [])

    def test_detect_catches_intermediate_pass_phrases_of_2307(self):
        """Промежуточный пасс A: живые фразы 23.07, которые сеть НЕ ловила."""
        import promises

        # 17:37 — «теперь обратно к…» ушло в простой ~18 минут без окна-возврата
        self.assertIsNotNone(promises.detect(
            "Спасибо за гард. Теперь обратно к audit-demo — плод пора встречать."))
        # 14:34 — «принесу до 18:00» без механизма (и не принеслось)
        self.assertIsNotNone(promises.detect("Пакет принесу до 18:00, Арет ждёт."))
        self.assertIsNotNone(promises.detect("Теперь сделаю зеркало придержек."))
        self.assertIsNotNone(promises.detect("К 19 соберу baseline и выложу."))
        self.assertIsNotNone(promises.detect("Возвращаюсь к пакету Арета."))
        # прошедшее время и чужие слова обещанием не считаются
        self.assertIsNone(promises.detect("Теперь всё понятно, спасибо."))
        self.assertIsNone(promises.detect("Он обещал, что принесёт до 18:00."))
        # её фикс «цитаты ≠ обещания» (51bfe745) не сломан новыми паттернами
        self.assertIsNone(promises.detect('Тест назывался «принесу до 18:00» — smoke прошёл.'))

    def test_her_own_remind_self_silences_the_net(self):
        """Анти-костыль «не двойной драйвер»: её УСПЕШНЫЙ remind_self в том же ходе —
        сеть молчит. Но только по факту успеха (маркер «→ Наметила #»), не по вызову."""
        import promises
        import tasks

        took_her_own_hand = ["remind_self(kind=window, goal=добить пакет) → Наметила #7 [window]: …"]
        self.assertIsNone(promises.note_outbound(
            "777", "Сейчас добью пакет и пришлю.", tools=took_her_own_hand))
        self.assertEqual(tasks.list_open(), [])
        # а без её руки — сеть подхватывает
        self.assertIsNotNone(promises.note_outbound(
            "777", "Сейчас добью пакет и пришлю.",
            tools=["read_chat(chat=777) → …"]))

    def test_failed_remind_self_does_not_silence_the_net(self):
        """Адверсарка round-1 (P2): отказ remind_self (задача НЕ создана) не гасит сеть —
        иначе обещание растворяется вообще без драйвера (ровно инцидент 14:34 23.07)."""
        import promises
        import tasks

        for refusal in (
            "remind_self(kind=message, target=@aret) → Не нахожу «@aret» в Telegram. … пока не намечаю.",
            "remind_self(kind=message) → Telegram-мост сейчас недоступен — … попробуй позже.",
            "remind_self(after_run=run-x) → Ран «run-x» мне неизвестен — … пока не намечаю.",
        ):
            tasks.list_open() and [tasks.cancel(t["id"]) for t in tasks.list_open()]
            self.assertFalse(promises.self_scheduled([refusal]),
                             f"отказ не считается взятием себя за руку: {refusal[:40]}")
            got = promises.note_outbound("777", "Пакет принесу до 18:00.", tools=[refusal])
            self.assertIsNotNone(got, "сеть страхует упавшую напоминалку")
            tasks.cancel(got["id"])

    def test_return_to_pattern_ignores_conversational_fillers(self):
        """Адверсарка round-1 (P3): «возвращаюсь к твоему вопросу» — не обещание."""
        import promises
        # рабочее «обратно к X» — ловим
        self.assertIsNotNone(promises.detect("Теперь обратно к audit-demo."))
        self.assertIsNotNone(promises.detect("Возвращаюсь к пакету Арета."))
        # разговорные филлеры (без ACT-глагола/срока рядом) — НЕ ловим
        self.assertIsNone(promises.detect("Возвращаюсь к твоему вопросу: да, согласна."))
        self.assertIsNone(promises.detect("Обратно к теме: ты прав про хэши."))
        self.assertIsNone(promises.detect("Возвращаюсь к сказанному ранее — это важно."))

    def test_self_intent_eligibility_skips_rest_and_promise_windows(self):
        """Адверсарка round-1 (P2): окно-отдых (её время) и окно-обещание (наг-петля)
        не должны рождать новых само-намерений."""
        import agent
        self.assertTrue(agent._self_intent_eligible("разобрать гард и допилить рельсы"))
        self.assertTrue(agent._self_intent_eligible("код: собери CLI"))
        self.assertFalse(agent._self_intent_eligible("отдых: просто побуду с музыкой"))
        self.assertFalse(agent._self_intent_eligible(
            "[обещание] напоминание себе: ~15 мин назад я сказала «довожу пакет»…"))

    def test_note_self_intent_from_her_own_windows(self):
        """Промежуточный пасс A: «теперь сделаю X» в task_window/forge_event — не воздух."""
        import promises
        import tasks

        first = promises.note_self_intent(
            "task_window", "Разобрала гард. Теперь сделаю зеркало для Арета.")
        self.assertIsNotNone(first)
        self.assertEqual(first["target"], "promise:self")
        self.assertIn("в своём окне", first["goal"])
        # одна нить self на все окна — второй интент не спамит
        self.assertIsNone(promises.note_self_intent(
            "forge_event", "Сейчас проверю манифест."))
        # её remind_self в том же окне — сеть молчит
        tasks.cancel(first["id"])
        self.assertIsNone(promises.note_self_intent(
            "task_window", "Теперь допилю рельсы.",
            tools=["remind_self(kind=window, goal=рельсы) → Наметила #9"]))
        self.assertEqual([t for t in tasks.list_open()
                          if t.get("target") == "promise:self"], [])

    def test_promise_is_armed_by_acceptance_not_by_authorship(self):
        """⚠ Тест назывался `..._on_sent_chat_turns` и проверял, что обещание рождает
        `guard_outbound_reply`. Но guard не знает и не может знать, отправлено ли
        что-нибудь: приёмка Telegram случается на двадцать шагов позже, и между ними
        лежат отмена преемником (PASS 29 делает её ТЕРМИНАЛЬНОЙ), privacy hold, отказ
        транспорта и рестарт. Имя теста содержало сам баг. Теперь обещание взводит
        проекция расписки — по фактически ушедшему тексту."""
        import agent

        ctx = agent.ChannelContext(chat_id="101", principal_id="101", is_dm=True, owner=True)
        turn = {"kind": "chat", "run_id": "run-promise-1"}
        with mock.patch.object(agent.turns, "record"), \
                mock.patch.object(agent.identity, "load_from_turn"), \
                mock.patch.object(agent.notes, "append"), \
                mock.patch.object(agent.promises, "note_outbound") as noted:
            out = agent.guard_outbound_reply(
                "Принято! Сейчас соберу кадры и пришлю.", ctx=ctx, turn=turn)
            self.assertTrue(out)
            noted.assert_not_called()
        with mock.patch.object(agent.turns, "update_delivery", return_value=turn), \
                mock.patch.object(agent.notes, "append"), \
                mock.patch.object(agent.promises, "note_outbound") as armed:
            agent.project_delivery_outcome(
                "run-promise-1", "accepted",
                text="Принято! Сейчас соберу кадры и пришлю.", chat_id="101")
        armed.assert_called_once()
        self.assertEqual(armed.call_args.args[0], "101")
        # придержанный ход обещания не рождает
        held_turn = {"kind": "chat"}
        with mock.patch.object(agent.turns, "record"), \
                mock.patch.object(agent.identity, "load_from_turn"), \
                mock.patch.object(agent.notes, "append"), \
                mock.patch.object(agent, "evaluate_reply", return_value=("deny", "x")), \
                mock.patch.object(agent, "tool_journal"), \
                mock.patch.object(agent.promises, "note_outbound") as noted_held:
            group = agent.ChannelContext(chat_id="-100200", principal_id="202", is_dm=False)
            agent.guard_outbound_reply("Сейчас всё принесу.", ctx=group, turn=held_turn)
        noted_held.assert_not_called()


class HeldSelfWakeTests(unittest.TestCase):
    """0.a v2 (22.07): придержка — её собственное окно-решение; владельца в петле НЕТ."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1",
        })
        self.env.start()
        import tasks
        self.tasks_path = mock.patch.object(
            tasks, "TASKS", Path(self.tmp.name) / "memory" / "tasks.json")
        self.tasks_path.start()

    def tearDown(self):
        self.tasks_path.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_privacy_rubric_grants_requesters_and_her_own_material(self):
        import agent

        self.assertIn("their own material where they asked for it", agent._OUTBOUND_PRIVACY_SYS)
        self.assertIn("never cross-chat leakage", agent._OUTBOUND_PRIVACY_SYS)
        self.assertIn("her own decisions, commitments, boundaries, plans, and actions",
                      agent._OUTBOUND_PRIVACY_SYS)
        self.assertIn("I am waiting for their PASS/FAIL", agent._OUTBOUND_PRIVACY_SYS)

    def _hold(self, reply, reason, chat="-100200"):
        import agent

        group = agent.ChannelContext(chat_id=chat, principal_id="202", is_dm=False)
        with mock.patch.object(agent, "evaluate_reply", return_value=("deny", reason)), \
                mock.patch.object(agent, "tool_journal"), \
                mock.patch.object(agent.turns, "record"), \
                mock.patch.object(agent.identity, "load_from_turn"):
            return agent.guard_outbound_reply(reply, ctx=group, turn={"kind": "chat"})

    def test_privacy_hold_wakes_her_not_the_owner(self):
        import agent
        import owner_delivery
        import tasks

        with mock.patch.object(owner_delivery, "LEDGER") as ledger:
            out = self._hold("Точный размер кодовой базы сейчас не назову.",
                             "privacy:cross-chat-private")
        self.assertEqual(out, "")
        ledger.emit.assert_not_called()  # никакого подтверждения, даже уведомлений
        opened = [t for t in tasks.list_open() if t.get("target") == "held:-100200"]
        self.assertEqual(len(opened), 1)
        goal = opened[0]["goal"]
        self.assertIn("privacy:cross-chat-private", goal)
        self.assertIn("кодовой базы", goal)
        self.assertIn("как сочтёшь нужным", goal)
        # напоминалка, не меню и не увещевание: без перечня опций и морали
        self.assertNotIn("send_message", goal)
        self.assertNotIn("нельзя", goal)
        # вторая придержка той же нити не молотит вторым окном
        self._hold("ещё текст", "privacy:cross-chat-private")
        opened = [t for t in tasks.list_open() if t.get("target") == "held:-100200"]
        self.assertEqual(len(opened), 1)

    def test_credential_hold_keeps_gist_out_of_agenda(self):
        import tasks

        self._hold("вот ключ sk-abc123secret", "privacy:credential", chat="-100300")
        opened = [t for t in tasks.list_open() if t.get("target") == "held:-100300"]
        self.assertEqual(len(opened), 1)
        self.assertNotIn("sk-abc123secret", opened[0]["goal"])
        self.assertIn("privacy:credential", opened[0]["goal"])

    def test_her_own_silence_choice_wakes_nobody(self):
        import agent
        import tasks

        group = agent.ChannelContext(chat_id="-100200", principal_id="202", is_dm=False)
        with mock.patch.object(agent, "evaluate_reply") as evaluate, \
                mock.patch.object(agent.turns, "record"), \
                mock.patch.object(agent.identity, "load_from_turn"), \
                mock.patch.object(agent.notes, "append"):
            out = agent.guard_outbound_reply("[молчу]", ctx=group, turn={"kind": "chat"})
        self.assertEqual(out, "")
        evaluate.assert_not_called()
        self.assertEqual(tasks.list_open(), [])

    def test_wake_failure_never_breaks_the_hold(self):
        import agent
        import tasks

        with mock.patch.object(tasks, "add", side_effect=RuntimeError("disk full")):
            out = self._hold("текст", "privacy:cross-chat-private")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
