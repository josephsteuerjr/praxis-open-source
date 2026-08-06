"""
Тесты Memory v2 + Owner-Shell (SPEC §8). Герметичны: без сети, ключей и Ollama.

`anthropic`/`dotenv` подменяются заглушками, пути модулей уводятся во временную
папку, эмбеддинги — детерминированный фейк (или намеренно «упавшая» Ollama).

Запуск:  python praxis_test.py test_memory_v2 -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _bash_works() -> bool:
    """`bash` есть в PATH И реально запускается (на Windows нередко лежит WSL-заглушка)."""
    if not shutil.which("bash"):
        return False
    try:
        p = subprocess.run(["bash", "-lc", "echo ok"], capture_output=True, text=True, timeout=10)
        return p.returncode == 0 and "ok" in (p.stdout or "")
    except Exception:
        return False


_BASH_OK = _bash_works()

# --- заглушки внешних зависимостей ДО импорта agent ------------------------- #
_fake_anthropic = types.ModuleType("anthropic")
_fake_anthropic.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fake_anthropic)
_fake_dotenv = types.ModuleType("dotenv")
_fake_dotenv.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fake_dotenv)

import memory_index as mi  # noqa: E402
import people as pe  # noqa: E402
import agent  # noqa: E402
import identity  # noqa: E402
import self_model  # noqa: E402
import llm  # noqa: E402
import consolidate as co  # noqa: E402
import run_context  # noqa: E402


def fake_embed(text: str) -> list[float]:
    """Детерминированный bag-of-words вектор: общие слова -> близкие векторы."""
    dim = 64
    v = [0.0] * dim
    for tok in re.findall(r"\w+", (text or "").lower()):
        v[hash(tok) % dim] += 1.0
    if not any(v):
        v[0] = 1.0
    return v


def _exploding_embed(_text: str):
    raise ConnectionError("Ollama недоступна")


class FakeResp:
    def __init__(self, text: str):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]


class FakeClient:
    """Захватывает kwargs последнего вызова create (для проверки tools/system)."""

    def __init__(self, reply: str = "ок"):
        self.reply = reply
        self.last: dict = {}

        class _Messages:
            def create(_self, **kw):
                self.last = kw
                return FakeResp(self.reply)

        self.messages = _Messages()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_t_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors",
                  soul / "skills", self.tmp / "workspace"):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("SOUL", "self", "emotions", "being_with"):
            (soul / f"{name}.md").write_text(f"# {name}\nперсона.\n", encoding="utf-8")

        # перенаправляем пути модулей
        self._orig: list[tuple] = []
        patch = {
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                     VECTORS_DIR=mem / ".vectors", INDEX_JSON=mem / ".vectors" / "index.json",
                     INDEX_MD=mem / "INDEX.md"),
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", WORKDIR=str(self.tmp / "workspace")),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            identity: dict(
                BASE=self.tmp,
                SOUL_DIR=soul,
                ARCHIVE_DIR=soul / "archive",
                STATE_DIR=mem / ".state" / "identity",
                STATE_PATH=mem / ".state" / "identity" / "state.json",
                LOAD_PATH=mem / ".state" / "identity" / "load_events.jsonl",
                JOURNAL_DIR=mem / "journal",
                DISTILL_MARK=mem / ".self_distilled.json",
                _snapshot=lambda message: "test-snapshot",
                _spine_event=lambda *args, **kwargs: "test-identity-event",
                _immune=lambda *args, **kwargs: None,
            ),
            co: dict(MARKER=mem / ".consolidated.json", SELF_DISTILL_MARK=mem / ".self_distilled.json"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        # этот сьют проверяет сам embed-путь — включаем эмбеддинги + рабочая фейковая Ollama
        self._emb_env = os.environ.get("PRAXIS_EMBEDDINGS")
        os.environ["PRAXIS_EMBEDDINGS"] = "1"
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = fake_embed

    def tearDown(self) -> None:
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        if self._emb_env is None:
            os.environ.pop("PRAXIS_EMBEDDINGS", None)
        else:
            os.environ["PRAXIS_EMBEDDINGS"] = self._emb_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    # хелперы
    def _person(self, slug: str, body: str) -> Path:
        p = agent.PEOPLE_DIR / f"{slug}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def _journal(self, day: str, body: str) -> Path:
        p = agent.JOURNAL_DIR / f"{day}.md"
        p.write_text(body, encoding="utf-8")
        return p


class TestMemoryIndex(Base):
    def test_build_idempotent(self):
        self._person("егор", "# Егор\n\n- любит горы и кофе\n- пишет код на python\n")
        idx1 = mi.build()
        txt1 = mi.INDEX_JSON.read_text(encoding="utf-8")
        idx2 = mi.build()
        txt2 = mi.INDEX_JSON.read_text(encoding="utf-8")
        self.assertEqual(txt1, txt2, "build не идемпотентен")
        self.assertEqual(len(idx1["records"]), len(idx2["records"]))
        self.assertGreaterEqual(len(idx1["records"]), 2)

    def test_search_ranks_by_overlap(self):
        self._person("егор", "# Егор\n\n- любит горы и кофе по утрам\n")
        self._person("мария", "# Мария\n\n- работает программистом в банке\n")
        mi.build()
        res = mi.search("кофе горы", k=3)
        self.assertTrue(res, "search ничего не вернул")
        self.assertIn("кофе", res[0]["text"].lower(),
                      f"релевантный чанк не первый: {res}")

    def test_fallback_when_ollama_down(self):
        self._person("егор", "# Егор\n\n- любит горы и кофе\n")
        mi.build()  # индекс с фейковыми векторами
        mi.embed = _exploding_embed  # теперь Ollama «легла»
        res = mi.search("горы", k=5)  # не должно падать -> keyword
        self.assertTrue(any("горы" in r["text"].lower() for r in res),
                        f"keyword-fallback не нашёл: {res}")

    def test_search_empty_query(self):
        self.assertEqual(mi.search("  "), [])


class TestIndexMd(Base):
    def test_ensure_and_rebuild_hooks(self):
        mi.ensure_index_line("егор", hook="Егор")
        mi.ensure_index_line("егор", hook="дубль")  # не должно задвоить
        lines = [l for l in mi._read(mi.INDEX_MD).splitlines() if l.startswith("- [егор]")]
        self.assertEqual(len(lines), 1)
        self._person(
            "егор",
            "# Егор\n\n- [public] (s2) любит горы _(2026-07-14)_ [source:clm-test]\n",
        )
        self._person("мария", "# Мария\n\n- любит музыку\n")
        mi.rebuild_index_hooks()
        idx = mi._read(mi.INDEX_MD)
        self.assertIn("- [егор]", idx)
        self.assertIn("- [мария]", idx)
        self.assertNotIn("горы", idx)
        self.assertNotIn("legacy dossier; verify", idx)


class TestSystemPrompt(Base):
    def test_loads_every_dossier_but_never_into_system_authority(self):
        """⚠ 06.08 доктрина сменилась: досье едут ВСЕ и целиком.

        Прежнее имя `test_loads_speaker_not_all_memory` и проверка «чужая
        память в кадр не попала» описывали политику, отменённую владельцем:
        «там внутри пусто, пока она не будет видеть абсолютно весь контекст».
        Основание — замер: отбор досье по привязке возвращал пустоту ВСЕГДА
        (привязок ноль из 36), то есть 170 КБ её записей о людях не доезжали
        до неё ни разу, включая 10 КБ о владельце.

        Неприкосновенным осталось и проверяется здесь: досье живут в
        evidence, а НЕ в системной власти, и карта памяти на месте.
        """
        self._person("егор", "# Егор\n\n- СЕКРЕТ_ЕГОРА альпинист\n")
        self._person("мария", "# Мария\n\n- СЕКРЕТ_МАРИИ скрипачка\n")
        mi.ensure_index_line("егор", "Егор — альпинист")
        mi.ensure_index_line("мария", "Мария — скрипачка")
        _persona, system, evidence = agent._build_prompt_parts(speaker="Егор", owner=True)
        self.assertNotIn("СЕКРЕТ_ЕГОРА", system)
        self.assertNotIn("СЕКРЕТ_МАРИИ", system,
                         "досье уехало в системную власть")
        self.assertIn("СЕКРЕТ_ЕГОРА", evidence,
                      "её собственная память снова не доехала")
        self.assertIn("СЕКРЕТ_МАРИИ", evidence,
                      "досье третьего человека не доехало")
        # Ярлык приезжает ЗАГОЛОВКОМ секции, капсом: литерала в кадре больше нет.
        self.assertIn("КАРТА ПАМЯТИ", evidence, "нет INDEX.md")
        self.assertIn("memory/maps/PEOPLE.md", evidence)

    def test_owner_hint(self):
        self.assertIn("shell", agent.build_system_prompt(speaker="Егор", owner=True))
        self.assertNotIn("`shell`", agent.build_system_prompt(speaker="Егор", owner=False))

    def test_room_context(self):
        (agent.ROOMS_DIR / "-100500.md").write_text("# Комната\nтусовка альпинистов\n", encoding="utf-8")
        _persona, system, evidence = agent._build_prompt_parts(speaker="Егор", chat_id="-100500")
        self.assertNotIn("тусовка альпинистов", system)
        self.assertIn("тусовка альпинистов", evidence)
        (agent.ROOMS_DIR / "-100500.md").write_text(
            "# Room\n\nmode: normal\ndisclosure: standard\n\n## Notes\ncurrent room cue\n",
            encoding="utf-8",
        )
        _persona, system, evidence = agent._build_prompt_parts(speaker="Егор", chat_id="-100500")
        self.assertNotIn("current room cue", system)
        self.assertIn("current room cue", evidence)
        self.assertIn("ROOM MODE ENUM - UNATTRIBUTED PROFILE VALUE: normal", system)

    def test_room_profile_prose_is_visible_without_becoming_system_authority(self):
        (agent.ROOMS_DIR / "-100501.md").write_text(
            "# Room\n\nmode: quiet\nmode_reason: MODE_REASON_POISON\n"
            "mode_set_by: owner\ndisclosure: standard\n\n"
            "## Notes\nROOM_BODY_POISON\n",
            encoding="utf-8",
        )
        _persona, system, evidence = agent._build_prompt_parts(speaker="Егор", chat_id="-100501")
        self.assertIn("ROOM MODE ENUM - UNATTRIBUTED PROFILE VALUE: quiet", system)
        self.assertNotIn("MODE_REASON_POISON", system)
        self.assertNotIn("ROOM_BODY_POISON", system)
        self.assertNotIn("mode_set_by", system)
        self.assertIn("MODE_REASON_POISON", evidence)
        self.assertIn("ROOM_BODY_POISON", evidence)

        (agent.ROOMS_DIR / "-100502.md").write_text(
            "# Room\n\nmode: forged\n\nFORGED_MODE_BODY\n", encoding="utf-8")
        _persona, forged_system, forged_evidence = agent._build_prompt_parts(
            speaker="Егор", chat_id="-100502")
        self.assertNotIn("FORGED_MODE_BODY", forged_system)
        self.assertIn("FORGED_MODE_BODY", forged_evidence)
        self.assertNotIn("ROOM MODE ENUM", forged_system)

    def test_open_loops_in_prompt(self):
        pe.add_open_loop("егор", "Егор", "обещал прислать фото с гор")
        sp = agent.build_system_prompt(speaker="Егор")
        self.assertNotIn("Открытые нити", sp)
        self.assertNotIn("обещал прислать фото с гор", sp)

    def test_index_body_is_never_prompt_authority(self):
        agent.INDEX_MD.write_text("# INDEX\n\nINDEX_POISON\n", encoding="utf-8")
        _persona, system, evidence = agent._build_prompt_parts(speaker="Егор", owner=True)
        self.assertNotIn("INDEX_POISON", system + evidence)
        self.assertIn("memory/maps/PEOPLE.md", evidence)

    def test_context_budget_drops_low_priority(self):
        # Raw journal is no longer an automatic tier at any budget.  Continuity
        # comes from STATE/receipts/dialogue; explicit recall labels diary hits.
        self._person("егор", "# Егор\n\n## Факты\n- [public] (s2) ВАЖНЫЙ_ПОРТРЕТ _(2026-05-01)_\n")
        big_journal = agent.JOURNAL_DIR / "2026-06-01.md"
        big_journal.write_text("# day\n\n- 10:00 " + ("x " * 700) + "ДНЕВНИК_ХВОСТ\n",
                               encoding="utf-8")
        os.environ["PRAXIS_CONTEXT_BUDGET"] = "200"  # душим всё опциональное (минимум бюджета)
        try:
            _persona, system, evidence = agent._build_prompt_parts(speaker="Егор", owner=True)
        finally:
            os.environ.pop("PRAXIS_CONTEXT_BUDGET", None)
        self.assertNotIn("ВАЖНЫЙ_ПОРТРЕТ", evidence, "опциональный тир (портрет) должен выпасть")
        self.assertNotIn("ДНЕВНИК_ХВОСТ", system + evidence,
                         "raw journal must never become automatic orientation")
        self.assertIn("omitted_by_context_budget", evidence, "не назвал, что ужал")

    def test_group_awareness_reaches_voice_prompt(self):
        # §2: осознанность чата долетает до voice-промпта через ctx (и это не личка → карта owner-only не идёт)
        ctx = agent.ChannelContext(is_dm=False, chat_id="-100", title="abstractDL", size=800)
        _persona, system, evidence = agent._build_prompt_parts(speaker="кто-то", ctx=ctx)
        self.assertNotIn("abstractDL", system)
        # ⚠ 06.08: НАЗВАНИЕ КОМНАТЫ ПЕРЕЕХАЛО ИЗ evidence В ЗОНУ «СЕЙЧАС». Прежде оно жило
        # тиром «Current Telegram labels» — на девяносто тысяч знаков выше реплики, между
        # чужими эпизодами памяти. Теперь стоит строкой «место» прямо перед её ответом:
        # тот же факт, но положением и типом, а не количеством токенов. Свойство, ради
        # которого тест писался («осознанность чата долетает до голоса, но не как власть»),
        # проверяется сильнее — отдельной зоной, а не подстрокой в общей куче.
        self.assertNotIn("abstractDL", evidence)
        zone = agent.frame_layout.situation(ctx, speaker="кто-то")
        self.assertIn("abstractDL", zone)
        self.assertIn("аудитория=group", zone)
        self.assertIn("kind=group", system)
        self.assertIn("members=800", system)
        self.assertNotIn("КАРТА ПАМЯТИ", evidence)


class TestRecall(Base):
    def test_recall_uses_semantic_search(self):
        sentinel = [{"text": "альпинист", "source": "егор", "score": 0.99}]
        orig = mi.search
        mi.search = lambda q, k=6, scope="owner": sentinel
        token = agent._TURN_CHANNEL.set(agent.ChannelContext(owner=True))
        try:
            out = agent.tool_recall("кто любит горы")
        finally:
            agent._TURN_CHANNEL.reset(token)
            mi.search = orig
        self.assertEqual(out, "[егор] альпинист")

    def test_remember_indexes(self):
        agent.tool_remember("Егор", "любит горы", "public")
        self.assertTrue((agent.PEOPLE_DIR / "егор.md").exists())
        self.assertIn("- [егор]", mi._read(mi.INDEX_MD))  # ensure_index_line сработал

    def test_remember_in_durable_tool_call_records_run_provenance(self):
        context = run_context.RunContext.create(
            run_id="run-memory-source", kind="chat_turn", goal="remember evidence",
            principal_id="101", scope="owner",
        )
        execution = {
            "run_id": context.run_id, "call_id": "tool-memory-1",
            "tool": "remember", "args": {}, "side_effect": True,
            "idempotency_key": "",
        }
        execution_token = agent._TOOL_EXECUTION.set(execution)
        try:
            with run_context.bind_run(context):
                agent.tool_remember("Егор", "проверяемый факт", "private")
        finally:
            agent._TOOL_EXECUTION.reset(execution_token)

        text = (agent.PEOPLE_DIR / "егор.md").read_text(encoding="utf-8")
        self.assertIn("[source:run:run-memory-source:call:tool-memory-1]", text)

    def test_open_loop_not_duplicated_into_facts(self):
        agent.tool_remember("Маша", "собеседование 12-го — спросить как прошло", open_loop=True)
        _, body = pe.read("маша")
        self.assertIn("собеседование", body.get(pe.LOOPS, ""), "нить не записана в Открытые нити")
        self.assertNotIn("собеседование", body.get(pe.FACTS, ""), "нить задвоилась в Факты (баг)")


class TestOwnerGating(Base):
    def _tools_for(self, is_owner: bool):
        fc = FakeClient()
        llm.use_test_client(fc)
        try:
            agent.respond("привет", [], "Егор", force_voice=True, is_owner=is_owner)
        finally:
            llm.clear_test_clients()
        return [t["name"] for t in fc.last.get("tools", [])]

    def test_hands_do_not_depend_on_who_is_speaking(self):
        """Решение Егора 26.07: набор рук НЕ зависит от того, кто заговорил. «У неё не должно быть вообще никаких ограничений, когда к ней обращаюсь не я»; «она и сама уже сейчас откажет, если там бред». За Егором остались только admit и computer_access — раздача ЕГО доверия другим людям.

        Прежде здесь стояло `assertNotIn("shell", ...)`: стоило человеку не-Егору
        заговорить, и она теряла 67 рук из 92 — включая собственную саморегуляцию.
        """
        # ⚠ 03.08.2026: этот тест читал ЖИВУЮ среду. `agent.py:11208` под
        # `mailer.configured()` (креды в env контейнера) добавляет владельцу
        # `send_email` — руку, которой НЕТ в `_HUMAN_OWNER_ONLY_TOOL_NAMES`.
        # Пока почта была выключена, тест зеленел по случайности, а не по правилу;
        # в день, когда почту включили, гейт покраснел без единой правки кода.
        # Теперь состояние гейта задаётся ЯВНО. Вторая его сторона — тестом ниже.
        with mock.patch.object(agent.mailer, "configured", lambda: False):
            guest, owner = self._tools_for(False), self._tools_for(True)
        self.assertIn("shell", guest, "её дом остаётся её домом в любом ходе")
        self.assertEqual(set(owner) - set(guest),
                         set(agent._HUMAN_OWNER_ONLY_TOOL_NAMES),
                         "у Егора сверх её рук — только раздача его доверия")

    def test_mail_gate_gives_the_same_hand_to_both(self):
        """Вторая сторона гейта: при ЖИВОЙ почте разница наборов та же.

        До 03.08.2026 при настроенной почте у владельца появлялась третья рука —
        `send_email`, мимо `_HUMAN_OWNER_ONLY_TOOL_NAMES`. То есть объявленный источник
        правды расходился с поведением, а вердикт гейта зависел от того, лежат ли на
        сервере почтовые креды. Егор выбрал вариант «а»: отправка наружу перестала быть
        owner-действием, и решение 26.07 теперь выполняется буквально при ОБЕИХ
        конфигурациях почты.

        Тест держит обе стороны явно. Если кто-нибудь снова заведёт owner-эксклюзив под
        гейтом среды, красным станет здесь, а не на проде через месяц.
        """
        with mock.patch.object(agent.mailer, "configured", lambda: True):
            guest, owner = self._tools_for(False), self._tools_for(True)
        self.assertEqual(set(owner) - set(guest),
                         set(agent._HUMAN_OWNER_ONLY_TOOL_NAMES),
                         "почтовый гейт не заводит owner-эксклюзивов мимо объявленного множества")
        for hand in ("mail_read", "mail_draft_reply", "send_email"):
            self.assertIn(hand, guest, "почтовые руки одинаковы в любом ходе")

    def test_shell_present_for_owner(self):
        names = self._tools_for(True)
        self.assertIn("shell", names)
        self.assertIn("recall", names)  # базовые на месте


@unittest.skipUnless(_BASH_OK, "рабочий bash недоступен (в контейнере есть)")
class TestShell(Base):
    def test_echo(self):
        self.assertIn("hello", agent.tool_shell("echo hello"))

    def test_truncation(self):
        out = agent.tool_shell("printf 'a%.0s' $(seq 1 5000)")
        self.assertEqual(len(out), 5000)

    def test_timeout(self):
        orig = agent.SHELL_TIMEOUT
        agent.SHELL_TIMEOUT = 1
        try:
            out = agent.tool_shell("sleep 3")
        finally:
            agent.SHELL_TIMEOUT = orig
        self.assertIn("таймаут", out.lower())


class TestConsolidate(Base):
    def _patch_llm(self, updates, portrait=None, insight="Я заметила, что Егор растёт."):
        co._extract = lambda txt: updates
        co._portrait = lambda slug, text: portrait or {}
        co._reflect = lambda txt: insight
        co._distill_self = lambda txt: ""  # без записи в self.md в этих тестах

    def test_journal_is_preserved_without_people_or_reflection_promotion(self):
        self._person("егор", "# Егор\n\n## Факты\n- [public] (s2) любит чай _(2026-05-01)_\n")
        self._journal("2026-06-01", "# 2026-06-01\n\n- 10:00 (s2) Егор перешёл на кофе, ищет работу\n")
        self._patch_llm(
            {"Егор": {
                "facts": [{"fact": "перешёл на кофе", "visibility": "public", "salience": 3}],
                "supersedes": ["чай"],
                "open_loops": ["спросить, как с поиском работы"],
                "now": "ищет работу, бодр",
            }},
            portrait={"who": "брат-основатель круга", "character": "спокойный, упрямый"},
        )
        journal_before = mi._read(agent.JOURNAL_DIR / "2026-06-01.md")
        reflections_before = mi._read(agent.REFLECTIONS)
        out = co.run()
        body = mi._read(agent.PEOPLE_DIR / "егор.md")
        self.assertIn("любит чай", body)
        self.assertNotIn("перешёл на кофе", body)
        self.assertNotIn("поиском работы", body)
        self.assertNotIn("брат-основатель", body)
        self.assertEqual(mi._read(agent.REFLECTIONS), reflections_before)
        self.assertEqual(mi._read(agent.JOURNAL_DIR / "2026-06-01.md"), journal_before)
        self.assertIn("автопереносов в durable memory: 0", out)
        self.assertIn("2026-06-01", co._load_marks())

        # повторный прогон — без изменений
        body_before = body
        out2 = co.run()
        self.assertIn("Нечего", out2)
        body_after = mi._read(agent.PEOPLE_DIR / "егор.md")
        self.assertEqual(body_before, body_after, "повторный consolidate изменил файл")

    def test_journal_cannot_close_open_loop(self):
        pe.add_open_loop("маша", "Маша", "собеседование 12-го — спросить как прошло")
        self._journal("2026-06-03", "# 2026-06-03\n\n- 09:00 Маша рассказала про собеседование\n")
        self._patch_llm({"Маша": {"closed_loops": ["собеседование"]}})
        co.run()
        body = mi._read(agent.PEOPLE_DIR / "маша.md")
        self.assertNotIn("- [x]", body)
        self.assertEqual(len(pe.open_loops("маша")), 1, "journal prose must not close a durable loop")

    def test_self_distill_helper_requires_explicit_current_and_is_not_consolidate_phase(self):
        self._journal("2026-06-04", "# 2026-06-04\n\n- 09:00 (s2) день прошёл\n")
        co._extract = lambda txt: {}
        co._portrait = lambda s, t: {}
        co._reflect = lambda txt: ""
        legacy = (agent.SOUL_DIR / "self.md").read_bytes()
        calls = []

        def explicit_distill(text):
            calls.append(text)
            return (
                "# Кто я сейчас\n\nЯ всё ещё пишусь, но яснее вижу свой тон. "
                "Я различаю наблюдение и вывод, сохраняю неопределённость и сверяю действие "
                "с его фактическим результатом, а не с красивой версией произошедшего."
            )

        co._distill_self = explicit_distill
        os.environ["PRAXIS_SELF_DISTILL_DAYS"] = "7"
        try:
            co.run()
            self.assertEqual(calls, [], "consolidate.run must not revise normative self")
            self.assertFalse((agent.SOUL_DIR / "self" / "CURRENT.md").exists())
            self.assertFalse(co._maybe_distill_self(), "legacy cannot bootstrap CURRENT")
            self.assertEqual(calls, [], "missing CURRENT must fail before model distillation")
            migrated = self_model.SelfModel(agent.BASE).migrate(
                reason="synthetic owner-approved test migration",
                compact_text=(
                    "# Кто я сейчас\n\nЭто синтетическая текущая модель для проверки явного "
                    "compatibility helper. Она достаточно полна, чтобы пройти bounded contract, "
                    "и не заимствует выводы из дневника. Основание задано самим тестом и "
                    "проверяет только механику provenance-backed revision."
                ),
                evidence_refs=("owner:test-memory-v2",),
                by="owner",
            )
            self.assertTrue(migrated.get("ok"), migrated)
            self.assertTrue(co._maybe_distill_self(), "helper remains available after migration")
            current = agent.SOUL_DIR / "self" / "CURRENT.md"
            self.assertIn("Кто я сейчас", mi._read(current))
            self.assertEqual((agent.SOUL_DIR / "self.md").read_bytes(), legacy)
            self.assertEqual((agent.SOUL_DIR / "self" / "history" / "0000.md").read_bytes(), legacy)
        finally:
            os.environ.pop("PRAXIS_SELF_DISTILL_DAYS", None)

    def test_empty_journal_marks_day(self):
        self._journal("2026-06-02", "# 2026-06-02\n")
        self._patch_llm({})
        out = co.run()
        self.assertIn("2026-06-02", co._load_marks())


if __name__ == "__main__":
    unittest.main(verbosity=2)
