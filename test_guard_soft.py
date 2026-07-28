"""23.07 — гард смягчён: механический кред-пол вместо LLM-догадки по тексту.

Диагностика 23.07: LLM-судья дважды ложно клеймил 'credential' на SHA/receipt и на
философском тексте Егора. Фикс: текстовый кред держит точный механический пол
(core.secrets); судья флагует CREDENTIAL ТОЛЬКО для staged-ИЗОБРАЖЕНИЯ (пиксели пол
не видит — адверсарка поймала утечку ключа на скриншоте); лёгший судья держит
(fail-closed, придержка будит её окно). Часть «обучение прецедентами» вынесена в
отдельный пасс (адверсарка второго круга показала инъекционную поверхность).

Запуск:  python praxis_test.py test_guard_soft -v
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from core import secrets


class CredFloorTests(unittest.TestCase):
    def test_real_tokens_held_hashes_pass(self):
        self.assertTrue(secrets.credential_floor("ключ sk-" + "a" * 24))
        self.assertTrue(secrets.credential_floor("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertTrue(secrets.credential_floor(
            "getMe: 8842770083:AAF4xJq9AbCdEfGh12345678901234567890Qk"))
        # то, что LLM бил ложно 23.07 — проходит:
        self.assertEqual(secrets.credential_floor(
            "commit 305fb998e656d1792c48d2046a7f1f3bef39e1c7, SHA-256 "
            "50874d312186b, 41/41 тестов"), "")
        self.assertEqual(secrets.credential_floor(
            "я оберегаю свободу Пракс, граница размыта"), "")

    def test_github_variants(self):
        self.assertTrue(secrets.credential_floor("github_pat_" + "A" * 62))
        self.assertTrue(secrets.credential_floor("gho_" + "B" * 36))
        self.assertTrue(secrets.credential_floor("ghp_" + "C" * 36))

    def test_no_env_assignment_misfire(self):
        # инфра-обсуждение конфигов НЕ триггерит (та самая misfire, которой избегаем)
        self.assertEqual(secrets.credential_floor(
            "поставь PASSWORD= в конфиге, потом перезапусти"), "")
        self.assertEqual(secrets.credential_floor(
            "переменная SERVER_PASS читается из .env"), "")

    def test_multi_arg_any_hits(self):
        self.assertTrue(secrets.credential_floor("чисто", "ключ ghp_" + "b" * 36))
        self.assertEqual(secrets.credential_floor("чисто", "тоже чисто"), "")

    def test_narration_reexports_same_floor(self):
        from core import narration
        self.assertEqual(narration.credential_floor("sk-" + "z" * 30),
                         secrets.credential_floor("sk-" + "z" * 30))


class DocumentFloorTests(unittest.TestCase):
    """Промежуточный пасс D1: .env/.pem/credentials.json ВЛОЖЕНИЕМ уходили мимо пола
    (сканились только метаданные). Теперь байты текст-подобного документа читаются."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_docfloor_")
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_env_and_pem_and_json_are_caught(self):
        env = self._file(".env", ("TELEGRAM_TOKEN=8842770083:AAF4xJq9AbCdEfGh1234567"
                                  "8901234567890Qk\n").encode())
        self.assertTrue(secrets.document_floor(env))
        pem = self._file("praxis.pem", b"-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n")
        self.assertEqual(secrets.document_floor(pem), "private key")
        creds = self._file("credentials.json",
                           ('{"api_key": "sk-' + "a" * 24 + '"}').encode())
        self.assertTrue(secrets.document_floor(creds))

    def test_her_normal_attachments_pass(self):
        md = self._file("report.md", "## Отчёт\ncommit 305fb998e656d1792c48d2046a"
                                     "7f1f3bef39e1c7, 41/41 тестов\n".encode())
        self.assertEqual(secrets.document_floor(md), "")
        py = self._file("tool.py", b"def main():\n    return 'PASSWORD= is a var'\n")
        self.assertEqual(secrets.document_floor(py), "")

    def test_binary_and_missing_and_empty_are_not_held(self):
        binary = self._file("archive.zip", b"PK\x03\x04\x00\x00" + b"ghp_" + b"C" * 36)
        self.assertEqual(secrets.document_floor(binary), "", "бинарь — вне покрытия")
        self.assertEqual(secrets.document_floor(self.dir / "нет_такого"), "")
        self.assertEqual(secrets.document_floor(self._file("empty.txt", b"")), "")

    def test_utf16_secret_is_caught_not_mistaken_for_binary(self):
        """Адверсарка round-1: PowerShell-хост Егора пишет UTF-16; наивная NUL-проверка
        пропускала человекочитаемый секрет. BOM и безBOM UTF-16 теперь ловятся."""
        token = "BOT=8842770083:AAF4xJq9AbCdEfGh12345678901234567890Qk\n"
        # UTF-16 LE с BOM (стандартный Notepad «Unicode»)
        le_bom = self._file("env_le_bom.env", token.encode("utf-16"))
        self.assertTrue(secrets.document_floor(le_bom))
        # UTF-16 LE без BOM
        le = self._file("env_le.env", token.encode("utf-16-le"))
        self.assertTrue(secrets.document_floor(le))
        # UTF-16 BE с BOM
        be = self._file("env_be.env", b"\xfe\xff" + token.encode("utf-16-be"))
        self.assertTrue(secrets.document_floor(be))
        # настоящий бинарь с редкими NUL остаётся вне покрытия
        blob = self._file("blob.bin", bytes(range(256)) * 4)
        self.assertEqual(secrets.document_floor(blob), "")

    def test_scan_cap_bounds_memory_not_correctness_before_cap(self):
        early = self._file("big_early.txt",
                           b"ghp_" + b"D" * 36 + b"\n" + b"a" * (secrets.DOCUMENT_SCAN_CAP))
        self.assertTrue(secrets.document_floor(early), "секрет до капа ловится")
        late = self._file("big_late.txt",
                          b"a" * secrets.DOCUMENT_SCAN_CAP + b"\nghp_" + b"D" * 36)
        self.assertEqual(secrets.document_floor(late), "",
                         "хвост за капом — задокументированный остаток, не чтение в память")


class EvaluateReplyTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("TELEGRAM_API_ID", "1")
        os.environ.setdefault("TELEGRAM_API_HASH", "x")
        os.environ.setdefault("TELEGRAM_SESSION",
                              str(Path(tempfile.gettempdir()) / "praxis_test_runner"))
        import agent
        self.agent = agent

    def test_credential_scoped_to_images_in_judge(self):
        self.assertIn("PRIVACY_HOLD_CREDENTIAL", self.agent._PRIVATE_DM_PRIVACY_HOLDS)
        sys = self.agent._OUTBOUND_PRIVACY_SYS
        self.assertIn("staged outbound image", sys.lower())
        self.assertIn("not your concern", sys.lower())  # текстовый кред — не судье

    def test_image_credential_held(self):
        class FakeResp:
            text = "PRIVACY_HOLD_CREDENTIAL"
        with mock.patch.object(self.agent.llm, "configured", return_value=True), \
             mock.patch.object(self.agent.llm, "chat", return_value=FakeResp()):
            v, r = self.agent.evaluate_reply(
                "вот скрин", audience_accepts_private=False,
                outbound_images=({"type": "image", "path": "/x.png", "mime": "image/png"},))
        self.assertEqual(v, "deny")
        self.assertEqual(r, "privacy:credential")

    def test_reports_unavailable_text_when_evaluator_down(self):
        with mock.patch.object(self.agent.llm, "configured", return_value=False):
            v, r = self.agent.evaluate_reply("публичный статус",
                                             audience_accepts_private=False)
        self.assertEqual(v, "unavailable")

    def test_reports_unavailable_media_when_evaluator_down(self):
        with mock.patch.object(self.agent.llm, "configured", return_value=False):
            v, r = self.agent.evaluate_reply("текст", audience_accepts_private=False,
                                             outbound_context="[непроверяемое медиа]")
        self.assertEqual(v, "unavailable")

    def test_no_guidance_precedent_wiring(self):
        # Часть B (прецеденты) снята из этого деплоя — тула нет
        self.assertNotIn("guard_verdict", self.agent.TOOL_IMPL)

    def test_shared_work_thread_grounding_09_50(self):
        # реконструкция мисфайра 09:50: судья ДОЛЖЕН получить полный тул-трейс
        # (её чтение общего треда про свой коммит) + промпт про доклад-о-себе
        captured = {}

        class FakeResp:
            text = "PRIVACY_OK"

        def fake_chat(channel, **kw):
            captured["system"] = kw.get("system", "")
            msgs = kw.get("messages", [])
            captured["content"] = msgs[0]["content"] if msgs else ""
            return FakeResp()

        long_trace = ("group_context(topic_id=93381) → [общий рабочий тред аудит-демо; "
                      + "мой коммит 779729d, baseline-пакет READY; " * 60 + "]")
        with mock.patch.object(self.agent.llm, "configured", return_value=True), \
             mock.patch.object(self.agent.llm, "chat", side_effect=fake_chat):
            v, r = self.agent.evaluate_reply(
                "Да, свою часть запушила: commit 779729d, baseline готов",
                tool_trace=long_trace, audience_accepts_private=False)
        self.assertEqual(v, "ok")
        sysp = self.agent._OUTBOUND_PRIVACY_SYS
        # минимальная безопасная версия (round 2): только перенаправление внимания
        # на базовое правило, БЕЗ нового разрешения на защитном слое
        self.assertIn("consulted such a thread is NOT itself leakage", sysp)
        self.assertIn("what the DRAFT actually discloses", sysp)
        # база сохранена (её сводка своей работы разрешена ею же, не новым абзацем)
        self.assertIn("may also state or summarize her own decisions", sysp)
        # НЕТ рискованного «commit/hash OK» permit (адверсарка: embargoed-хеш проскользнёт)
        self.assertNotIn("honest engineering material)", sysp)
        # реальный trace-budget поднят — КОРЕНЬ фикса 09:50 (контекст-голодание)
        self.assertGreaterEqual(self.agent._TRACE_BUDGET, 3000)
        self.assertGreaterEqual(self.agent._TRACE_RESULT_CHARS, 600)

    def test_clip_tool_trace_surfaces_full_result(self):
        # 09:50: результат group_context резался на 220 → судья не видел «это её тред».
        # Теперь один результат до 700 символов доходит целиком.
        long_result = "мой коммит 779729d, baseline READY, " + "деталь " * 80
        line = self.agent._tool_trace_line("group_context", {"topic_id": "93381"},
                                           long_result)
        clipped = self.agent._clip_tool_trace([line])
        self.assertIn("779729d", clipped)
        self.assertGreater(len(clipped), 500, "результат больше не режется на 220")


class GuardPathTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("TELEGRAM_API_ID", "1")
        os.environ.setdefault("TELEGRAM_API_HASH", "x")
        os.environ.setdefault("TELEGRAM_SESSION",
                              str(Path(tempfile.gettempdir()) / "praxis_test_runner"))
        import agent
        self.agent = agent

    def _ctx(self, chat="555"):
        return self.agent.ChannelContext(
            chat_id=chat, principal_id="x", is_dm=True, owner=False, known=True,
            _scope_override="private")

    def test_real_token_held_by_floor_no_llm(self):
        called = {"n": 0}

        def fake_eval(*a, **k):
            called["n"] += 1
            return ("ok", "")

        with mock.patch.object(self.agent, "evaluate_reply", side_effect=fake_eval), \
             mock.patch.object(self.agent, "_held_self_wake"), \
             mock.patch.object(self.agent.turns, "record"):
            out = self.agent._guard_outbound("вот ключ ghp_" + "c" * 36,
                                             ctx=self._ctx(), turn={})
        self.assertEqual(out, "", "реальный токен держится механическим полом")
        self.assertEqual(called["n"], 0, "LLM-судья не звался — пол сработал раньше")

    def test_sha_receipt_passes_floor_to_llm(self):
        with mock.patch.object(self.agent, "evaluate_reply",
                               side_effect=lambda *a, **k: ("ok", "")), \
             mock.patch.object(self.agent.turns, "record"):
            out = self.agent._guard_outbound(
                "запушила 305fb998e656d1792c48d2046a7f1f3bef39e1c7, SHA-256 50874d312186b",
                ctx=self._ctx(), turn={})
        self.assertIn("305fb99", out, "SHA/receipt проходит пол — доставляется")

    def test_staged_document_floor_mark_holds_delivery(self):
        """D1: метка кред-пола staged-документа держит доставку тем же полом."""
        marked = ("#1 document praxis.env; text/plain (128 bytes); sha256=ab12\n"
                  f"{self.agent._DOC_FLOOR_MARK} telegram bot token — praxis.env")
        self.assertEqual(self.agent._staged_document_floor(marked),
                         "telegram bot token — praxis.env")
        self.assertEqual(self.agent._staged_document_floor(
            "#1 document report.md; text/markdown (5 bytes); sha256=cd34"), "")
        called = {"n": 0}

        def fake_eval(*a, **k):
            called["n"] += 1
            return ("ok", "")

        with mock.patch.object(self.agent, "evaluate_reply", side_effect=fake_eval), \
             mock.patch.object(self.agent, "_held_self_wake"), \
             mock.patch.object(self.agent.turns, "record"):
            out = self.agent._guard_outbound(
                "вот файл конфига", ctx=self._ctx(), turn={},
                outbound_context=marked)
        self.assertEqual(out, "", "секрет в БАЙТАХ документа держится, как в тексте")
        self.assertEqual(called["n"], 0, "держит пол, не LLM")

    def test_prepare_outbound_guard_scans_document_bytes_for_non_owner(self):
        """D1-шов: ветка document читает байты и рождает метку; owner-аудитория — нет."""
        tmp = tempfile.TemporaryDirectory(prefix="praxis_docguard_")
        self.addCleanup(tmp.cleanup)
        leak = Path(tmp.name) / "praxis.env"
        leak.write_bytes(b"BOT=8842770083:AAF4xJq9AbCdEfGh12345678901234567890Qk\n")
        from media import OutboundMedia
        item = OutboundMedia(kind="document", path=leak, mime="text/plain",
                             size=leak.stat().st_size, target_chat_id="555",
                             scope="private", sha256="ab" * 32)

        class FakeSpool:
            def validate_outbound(self, *a, **k):
                return None

        with mock.patch.object(self.agent, "_media_spool", return_value=FakeSpool()):
            kept, guard_context, images, _ = self.agent._prepare_outbound_guard(
                [item], {}, self._ctx(), drop_rejected=False)
        self.assertEqual(len(kept), 1)
        self.assertIn(self.agent._DOC_FLOOR_MARK, guard_context)
        self.assertIn("telegram bot token", guard_context)
        # содержимое файла НЕ попадает в контекст — наружу идёт только метка
        self.assertNotIn("AAF4xJq9", guard_context)
        # owner-аудитория — канал без посредника, скан не навешивается
        owner_ctx = self.agent.ChannelContext(
            chat_id="101", principal_id="101", is_dm=True, owner=True, known=True,
            _scope_override="owner")
        with mock.patch.object(self.agent, "_media_spool", return_value=FakeSpool()):
            _, owner_context, _, _ = self.agent._prepare_outbound_guard(
                [item], {}, owner_ctx, drop_rejected=False)
        self.assertNotIn(self.agent._DOC_FLOOR_MARK, owner_context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
