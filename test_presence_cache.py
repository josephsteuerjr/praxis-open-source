"""
Тесты карточки присутствия и кэш-сплита системного промпта (Слой 2). Герметичны.

Запуск:  python praxis_test.py test_presence_cache -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import memory_index as mi  # noqa: E402
import people as pe  # noqa: E402
import agent  # noqa: E402

SOUL = """# Конституция Praxis

## Мой характер сейчас
Тёплая, честная, колкая со своими.

## Моё авторство
Держу лицо, не ведусь на подъёб. ЯКОРЬ_ХАРАКТЕРА.

## Правда и воображение
Чаще думаю налегке, иногда зову полное внимание.

## Кем я не стану
Не бот.
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_pc_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        (soul / "SOUL.md").write_text(SOUL, encoding="utf-8")
        for n in ("self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\nтекст.\n", encoding="utf-8")
        self._orig = []
        patch = {
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md"),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", VECTORS_DIR=mem / ".vectors",
                     INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = lambda t: (_ for _ in ()).throw(ConnectionError("no ollama"))
        self._cache_env = os.environ.get("PRAXIS_PROMPT_CACHE")
        os.environ.pop("PRAXIS_PROMPT_CACHE", None)

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        if self._cache_env is None:
            os.environ.pop("PRAXIS_PROMPT_CACHE", None)
        else:
            os.environ["PRAXIS_PROMPT_CACHE"] = self._cache_env
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPresenceCard(Base):
    def test_distillate_not_full_soul(self):
        card = agent.build_presence_card()
        self.assertIn("ЯКОРЬ_ХАРАКТЕРА", card, "якорь самообладания должен быть в карточке")
        self.assertIn("Мой характер сейчас", card)
        self.assertNotIn("Кем я не стану", card, "карточка — дистиллят, не вся SOUL")


class TestSystemCache(Base):
    def test_blocks_when_cache_on(self):
        sysm = agent._system("СТАТИКА", "динамика")
        self.assertIsInstance(sysm, list)
        self.assertEqual(sysm[0]["cache_control"], {"type": "ephemeral"}, "статика не помечена кэшем")
        self.assertEqual(sysm[0]["text"], "СТАТИКА")
        self.assertEqual(sysm[1]["text"], "динамика")

    def test_string_when_cache_off(self):
        os.environ["PRAXIS_PROMPT_CACHE"] = "0"
        self.assertEqual(agent._system("СТАТИКА", "динамика"), "СТАТИКАдинамика")

    def test_parts_split_persona_and_dynamic(self):
        pe.set_telegram_id("егор", "Егор", "101")
        pe.append_fact("егор", "Егор", "любит горы")
        persona, dynamic, evidence = agent._build_prompt_parts(
            speaker="Егор", ctx=agent.ChannelContext(principal_id="101", known=True),
        )
        self.assertIn("Мой характер сейчас", persona, "персона — это душа")
        self.assertNotIn("любит горы", persona, "память не должна попасть в кэш-персону")
        self.assertNotIn("любит горы", dynamic, "портрет не должен стать system authority")
        self.assertIn("любит горы", evidence, "портрет остаётся полным контекстом")

    def test_build_system_prompt_still_string(self):
        sp = agent.build_system_prompt(speaker="Егор")
        self.assertIsInstance(sp, str)
        self.assertIn("Мой характер сейчас", sp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
