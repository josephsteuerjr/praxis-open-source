"""
Тесты PASS 12.

12.0.a — семья одной командой владельца:
  * tool_admit(role="family") пишет И known_ids, И role: family в шапку досье за один шаг
    (обе дыры: без known роль вообще не читается; панель роль не ставила никак);
  * role: family переживает консолидацию (parse/render round-trip — раньше преамбульная
    строка выпадала при первой же правке досье, и family молча слетал);
  * неизвестная роль — честный отказ, без мусора в файле и без впуска;
  * панельная кнопка «сделать семьёй» закрывает те же две дыры через один вызов.
12.0.b — единый резолвер постановки=отправки:
  * задача message резолвится тем же богатым путём, что и отправка (resolve_entity), а не
    одной попыткой get_entity — то, что дорезолвилось бы на отправке, резолвится и на постановке;
  * исчерпав резолвер, честно называет ограничение протокола (нужен @ник), не выдумывает.

Герметичны (харнесс test_perceive.Base; модель не зовём).
Запуск:  python praxis_test.py test_pass12 -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from test_perceive import Base  # герметичный харнесс: tmp BASE для agent/people/memory_index
import time

import agent
import people
import social
import panel
import absence
import unanswered
import tasks as tasks_mod


def _patch_social_to_tmp(test, mem: Path):
    for k, val in dict(BASE=mem.parent, MEM_DIR=mem, KNOWN_IDS=mem / "known_ids.json",
                       UNKNOWN_COUNTS=mem / ".unknown_counts.json").items():
        test._orig.append((social, k, getattr(social, k)))
        setattr(social, k, val)


# --------------------------------------------------------------------------- #
#  12.0.a — семья одной командой
# --------------------------------------------------------------------------- #

class AdmitRoleBase(Base):
    def setUp(self):
        super().setUp()
        _patch_social_to_tmp(self, self.tmp / "memory")
        self._owner_env = os.environ.get("PRAXIS_OWNER_ID")
        os.environ["PRAXIS_OWNER_ID"] = "606060"
        self._owner_token = agent._TURN_CHANNEL.set(agent.ChannelContext(
            chat_id="606060", principal_id=606060, is_dm=True, owner=True, known=True,
        ))

    def tearDown(self):
        agent._TURN_CHANNEL.reset(self._owner_token)
        if self._owner_env is None:
            os.environ.pop("PRAXIS_OWNER_ID", None)
        else:
            os.environ["PRAXIS_OWNER_ID"] = self._owner_env
        super().tearDown()


class TestAdmitFamily(AdmitRoleBase):
    def test_family_in_one_call(self):
        out = agent.tool_admit("Виктор", id="700700", role="family")
        self.assertIn("семь", out.lower())
        self.assertIn("700700", social.known_ids(), "family-впуск обязан писать known_ids")
        self.assertEqual(social.role_of("700700"), "family")

    def test_role_survives_consolidation(self):
        agent.tool_admit("Виктор", id="700700", role="family")
        slug = people._slug("Виктор")
        # так консолидация правит досье — через parse/render; роль слетать не должна
        people.append_fact(slug, "Виктор", "любит горы")
        people.set_prose(slug, "Виктор", now="сейчас в отпуске")
        self.assertEqual(people.role(slug), "family", "role: family обязан пережить round-trip")
        self.assertEqual(social.role_of("700700"), "family")

    def test_unknown_role_refused_no_side_effects(self):
        out = agent.tool_admit("Икс", id="800800", role="босс")
        self.assertIn("не знаю", out.lower())
        self.assertNotIn("800800", social.known_ids(), "мусорная роль — ни впуска, ни файла")
        self.assertFalse(people.path_for(people._slug("Икс")).exists())

    def test_plain_admit_unchanged(self):
        agent.tool_admit("Гость", id="900900")
        self.assertEqual(social.role_of("900900"), "known")

    def test_role_is_owner_only_by_construction(self):
        # tool_admit — owner-тул (в OWNER_TOOLS, не в BASE_TOOLS): чужой путь до параметра не доходит
        self.assertIn(agent.ADMIT_TOOL, agent.OWNER_TOOLS)
        self.assertNotIn(agent.ADMIT_TOOL, agent.BASE_TOOLS)
        self.assertIn("role", agent.ADMIT_TOOL["input_schema"]["properties"])


class TestRoleRoundTrip(Base):
    """Точечно про people.parse/render: header-строки (role, Алиасы) round-trip-safe."""

    def test_role_and_aliases_both_survive(self):
        slug = "kolya"
        text = "# Коля\nrole: family\nАлиасы: Николай, Kolya\n\n## Факты\n- [public] (s2) любит чай _(2026-07-01)_\n"
        (self.tmp / "memory" / "people").mkdir(parents=True, exist_ok=True)
        people.path_for(slug).write_text(text, encoding="utf-8")
        name, body = people.read(slug)
        self.assertEqual(body.get(people.ROLE_KEY), "family")
        people.write(slug, name, body)  # round-trip
        self.assertEqual(people.role(slug), "family")
        self.assertIn("Николай", people.read_text(slug))

    def test_set_role_creates_and_clears(self):
        slug = "dora"
        people.set_role(slug, "Дора", "family")
        self.assertEqual(people.role(slug), "family")
        people.set_role(slug, "Дора", "")  # снять
        self.assertEqual(people.role(slug), "")


class TestPanelMakeFamily(Base):
    """Панельный путь: contact_make_family закрывает обе дыры (known_ids + role)."""

    def setUp(self):
        super().setUp()
        mem = self.tmp / "memory"
        _patch_social_to_tmp(self, mem)
        for k, val in dict(BASE=self.tmp, ABSENCE_FILE=mem / "absence.json",
                           PANEL_DIR=mem / ".panel").items():
            self._orig.append((panel, k, getattr(panel, k)))
            setattr(panel, k, val)
        panel._absence_save({"contacts": [
            {"slug": "vika", "name": "Вика", "id": "500500", "username": "vika"}]})

    def test_make_family_writes_known_and_role(self):
        r = panel.contact_make_family("vika")
        self.assertTrue(r.get("ok"), r)
        self.assertIn("500500", social.known_ids())
        self.assertEqual(social.role_of("500500"), "family")
        # статус виден в absence_state (флаг вычисляется, не хранится)
        c = next(c for c in panel.absence_state()["contacts"] if c["slug"] == "vika")
        self.assertTrue(c.get("family"))

    def test_make_family_without_id_honest_error(self):
        panel._absence_save({"contacts": [{"slug": "noid", "name": "БезИд"}]})
        r = panel.contact_make_family("noid")
        self.assertIn("error", r)
        self.assertEqual(social.known_ids(), {}, "без id — ничего не пишем")


# --------------------------------------------------------------------------- #
#  12.0.b — единый резолвер постановки=отправки
# --------------------------------------------------------------------------- #

class TaskResolveBase(Base):
    def setUp(self):
        super().setUp()
        _patch_social_to_tmp(self, self.tmp / "memory")
        self._orig.append((tasks_mod, "TASKS", tasks_mod.TASKS))
        tasks_mod.TASKS = self.tmp / "memory" / "tasks.json"
        for key in ("resolve_entity", "get_id"):
            agent._TELETHON.pop(key, None)
        self.addCleanup(lambda: [agent._TELETHON.pop(k, None)
                                 for k in ("resolve_entity", "get_id")])


class TestUnifiedResolver(TaskResolveBase):
    def test_resolve_entity_used_over_get_id(self):
        # ставим только богатый резолвер (get_id-одна-попытка отбилась бы) — и он резолвит id
        agent._TELETHON["resolve_entity"] = lambda ref: 424242
        self._orig.append((social, "category", social.category))
        social.category = lambda sid: "known"
        out = agent.tool_remind_self("message", "напомни про встречу", "in 1h", "12345")
        self.assertIn("Наметила #", out)
        self.assertEqual(tasks_mod.list_open()[-1]["target_id"], 424242,
                         "постановка резолвит тем же путём, что и отправка")

    def test_get_id_fallback_when_no_resolve_entity(self):
        # обратная совместимость: если мост зарегистрировал только get_id — работаем через него
        agent._TELETHON["get_id"] = lambda ref: 555
        self._orig.append((social, "category", social.category))
        social.category = lambda sid: "known"
        out = agent.tool_remind_self("message", "привет", "in 1h", "@vasya")
        self.assertIn("Наметила #", out)
        self.assertEqual(tasks_mod.list_open()[-1]["target_id"], 555)

    def test_exhausted_resolver_names_protocol_limit(self):
        agent._TELETHON["resolve_entity"] = lambda ref: None
        out = agent.tool_remind_self("message", "напиши Вике", "in 1h", "999999")
        self.assertIn("Не нахожу", out)
        self.assertIn("@", out, "честно: нужен точный @ник (ограничение протокола, не наше)")
        self.assertEqual(tasks_mod.list_open(), [], "тихого провала в планировщике нет")


# --------------------------------------------------------------------------- #
#  12.1 — движок ответа в отсутствие
# --------------------------------------------------------------------------- #

class AbsenceBase(Base):
    def setUp(self):
        super().setUp()
        mem = self.tmp / "memory"
        (mem / ".state").mkdir(parents=True, exist_ok=True)
        for k, val in dict(BASE=self.tmp, ABSENCE_FILE=mem / "absence.json",
                           SENT_FILE=mem / ".state" / "absence_sent.json").items():
            self._orig.append((absence, k, getattr(absence, k)))
            setattr(absence, k, val)
        for k, val in dict(BASE=self.tmp, STATE_DIR=mem / ".state",
                           PATH=mem / ".state" / "unanswered.json").items():
            self._orig.append((unanswered, k, getattr(unanswered, k)))
            setattr(unanswered, k, val)
        import os
        os.environ["PRAXIS_ABSENCE_CAP"] = "3"
        os.environ["PRAXIS_COOLDOWN_DM"] = "8"
        # Убирались обе, а возвращалась одна: кулдаун доставался соседям по прогону.
        for _name in ("PRAXIS_ABSENCE_CAP", "PRAXIS_COOLDOWN_DM"):
            self.addCleanup(os.environ.pop, _name, None)

    def _write_absence(self, *, window=True, note="", contact_id="700"):
        import json
        w = {"until": time.time() + 3600, "started": "win-1", "hours": 1} if window else None
        d = {"window": w, "schedule_note": note,
             "contacts": [{"slug": "vika", "name": "Вика", "id": str(contact_id)}]}
        absence.ABSENCE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


class TestAbsenceTrigger(AbsenceBase):
    def test_fires_only_on_all_three(self):
        # все три: окно активно ∩ важный ∩ unanswered старше кулдауна
        self._write_absence(window=True, contact_id="700")
        unanswered.note_incoming("700", "Вика", ts=time.time() - 100)
        due = absence.due()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["chat_id"], "700")

    def test_no_window_no_fire(self):
        self._write_absence(window=False, contact_id="700")
        unanswered.note_incoming("700", "Вика", ts=time.time() - 100)
        self.assertEqual(absence.due(), [])

    def test_not_important_no_fire(self):
        self._write_absence(window=True, contact_id="700")
        unanswered.note_incoming("999", "Чужой", ts=time.time() - 100)  # неотвеченный, но не важный
        self.assertEqual(absence.due(), [])

    def test_fresh_unanswered_below_cooldown_no_fire(self):
        self._write_absence(window=True, contact_id="700")
        unanswered.note_incoming("700", "Вика", ts=time.time())  # только что — младше кулдауна
        self.assertEqual(absence.due(), [])

    def test_delivery_receipts_do_not_hide_a_still_open_thread(self):
        self._write_absence(window=True, contact_id="700")
        unanswered.note_incoming("700", "Вика", ts=time.time() - 100)
        w = absence.window()
        for _ in range(3):
            self.assertEqual(len(absence.due()), 1)
            absence.note_sent("700", w)
        self.assertEqual(absence.sent_count("700", w), 3)
        self.assertEqual(len(absence.due()), 1,
                         "receipts are context; only resolving the thread closes it")


class TestAbsenceNarrowVoice(Base):
    def test_absence_frame_only_states_identity_authority_and_evidence(self):
        self.assertIn("You are Praxis, not Yegor", agent._ABSENCE_FRAME)
        self.assertIn("no authority", agent._ABSENCE_FRAME)
        self.assertIn("your decisions", agent._ABSENCE_FRAME)
        for coercive in ("full warmth", "otherwise just talk", "say you'll let him know",
                         "Output ONLY"):
            self.assertNotIn(coercive, agent._ABSENCE_FRAME)

    def test_compose_absence_reply_has_no_tools(self):
        import llm
        captured = {}

        def fake_chat(role, system="", messages=None, tools=None, **kw):
            captured["tools"] = tools

            class R:
                stop_reason = "end_turn"
                blocks = [{"type": "text", "text": "Привет, я Praxis — Егор офлайн, передам ему."}]
                text = "Привет, я Praxis — Егор офлайн, передам ему."
            return R()

        self._orig.append((llm, "chat", llm.chat))
        self._orig.append((llm, "configured", llm.configured))
        llm.chat = fake_chat
        llm.configured = lambda *a, **k: True
        out = agent.compose_absence_reply("Вика", portrait="# Вика\nлюбит кофе",
                                          schedule_note="вечером на связи", convo="Вика: ты тут?")
        self.assertTrue(out)
        # 06.07 ревизия: не 0 тулов (звучало вестником) — именованный safe read-only набор.
        names = {t["name"] for t in (captured.get("tools") or [])}
        self.assertEqual(names, {"recall", "connections"}, "safe read-only, не пусто и не всё")
        for dangerous in ("shell", "send_message", "remember", "journal", "admit", "restart_self"):
            self.assertNotIn(dangerous, names, f"{dangerous} не должен быть в узком проходе")


class TestAbsenceRunnerGuard(Base):
    def test_absence_runner_uses_outbound_guard_before_send(self):
        import asyncio
        import os
        import tempfile

        env = {k: os.environ.get(k) for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")}
        os.environ.setdefault("TELEGRAM_API_ID", "1")
        os.environ.setdefault("TELEGRAM_API_HASH", "x")
        os.environ.setdefault("TELEGRAM_SESSION", str(Path(tempfile.gettempdir()) / "praxis_test_absence"))

        def _restore_env():
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore_env)
        import mtproto_runner as mr

        class FakeClient:
            def __init__(self):
                self.sent = []

            async def send_message(self, ent, text):
                self.sent.append((ent, text))

        guard_calls = []
        note_sent = []
        tg = FakeClient()

        async def fake_last_n(chat_id):
            return "Вика: что там с Машиным секретом?"

        async def fake_resolve(chat_id):
            return object()

        def fake_guard(reply, convo, *, ctx=None, orient=""):
            guard_calls.append((reply, convo, ctx, orient))
            return ""

        patches = [
            (mr, "client", tg),
            (mr, "OWNER_ID", 0),
            (mr, "_last_n_text", fake_last_n),
            (mr, "_resolve_entity", fake_resolve),
            (mr, "_absence_portrait", lambda name, slug="": ""),
            (agent, "compose_absence_reply", lambda name, portrait="", schedule_note="", convo="": "сырой draft"),
            (agent, "guard_outbound_reply", fake_guard),
            (absence, "due", lambda: [{"chat_id": "700", "person": {"name": "Вика", "slug": "vika"}}]),
            (absence, "window", lambda: {"active": True, "until": "later"}),
            (absence, "schedule_note", lambda: "вечером"),
            (absence, "note_sent", lambda chat_id, w: note_sent.append(chat_id) or 1),
        ]
        for module, name, value in patches:
            self._orig.append((module, name, getattr(module, name)))
            setattr(module, name, value)

        asyncio.run(mr._absence_once())

        self.assertEqual(tg.sent, [], "guard-held absence draft must not be sent")
        self.assertEqual(note_sent, [], "held drafts do not consume absence send quota")
        self.assertEqual(len(guard_calls), 1)
        self.assertEqual(guard_calls[0][2].chat_id, "700")
        self.assertTrue(guard_calls[0][2].is_dm)


class TestRestartMailbotSignal(Base):
    """PASS 12.x: файл-флаг вместо Docker-сокета -- она может попросить mailbot перезапуститься."""

    def setUp(self):
        super().setUp()
        state = self.tmp / "memory" / ".state"
        flag = self.tmp / "memory" / ".restart_mailbot"
        service = agent.services.SERVICES["mailbot"]
        for obj, name, value in (
            (agent.services, "STATE_DIR", state),
            (agent.services, "RECEIPTS", state / "services.jsonl"),
            (agent.services, "PANIC_SENTINEL", self.tmp / "memory" / ".panic"),
            (service, "flag", flag),
            (agent, "RESTART_MAILBOT_FLAG", flag),
        ):
            self._orig.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

    def test_writes_flag_with_reason(self):
        if agent.RESTART_MAILBOT_FLAG.exists():
            agent.RESTART_MAILBOT_FLAG.unlink()
        out = agent.tool_restart_mailbot("починили openai")
        self.assertTrue(agent.RESTART_MAILBOT_FLAG.exists())
        text = agent.RESTART_MAILBOT_FLAG.read_text(encoding="utf-8")
        self.assertIn("починили openai", text)
        self.assertIn("перезапуст", out.lower())

    def test_owner_only(self):
        names = {t["name"] for t in agent.OWNER_TOOLS}
        self.assertIn("restart_mailbot", names)
        base_names = {t["name"] for t in agent.BASE_TOOLS}
        self.assertNotIn("restart_mailbot", base_names, "не должен быть доступен не-владельцу")


# --------------------------------------------------------------------------- #
#  12.2 — http:// только для локальных адресов
# --------------------------------------------------------------------------- #

class TestBaseUrlValidation(unittest.TestCase):
    def test_https_ok_http_local_ok_http_remote_rejected(self):
        for u in ("https://api.z.ai/api/anthropic",
                  "http://127.0.0.1:5011", "http://localhost:5011",
                  "http://host.docker.internal:5011"):
            self.assertTrue(panel._base_url_ok(u), u)
        for u in ("http://production.example.com", "http://evil", "http://8.8.8.8",
                  "ftp://x", ""):
            self.assertFalse(panel._base_url_ok(u), u)

    def test_llm_validate_routes_local_http_through(self):
        clean, errors = panel._llm_validate(
            {"frameworks": {"openai": {"base_url": "http://host.docker.internal:5011"}}})
        self.assertEqual(errors, [])
        self.assertEqual(clean["frameworks"]["openai"]["base_url"], "http://host.docker.internal:5011")
        _clean, errs = panel._llm_validate(
            {"frameworks": {"openai": {"base_url": "http://production.example.com"}}})
        self.assertTrue(any("https://" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
