"""Версия оболочки её аппа не может разойтись с самой оболочкой.

⚠ 03.08.2026 номер жил в ТРЁХ копиях и разошёлся: `praxisapp.html` просил `?v=31`,
`sw.js` прекэшировал `?v=30`, а маршрут `/px` переписывал 31→32. Следствий два, и оба
тихие: прекэш складывал URL, которых страница не запрашивает, а её реальные `?v=31` шли
по stale-while-revalidate — то есть после обновления апп ПЕРВЫЙ РАЗ показывал старую
сборку. Плюс имя кэша `praxis-shell-pass30-v2` было постоянным, поэтому `activate` не
находил что удалять, и старые версии жили в браузере бессрочно.

Ассеты лежат в чекауте, а `mailroom_bot.BASE` под стендом уведён в песочницу — поэтому
здесь база подменяется на каталог модуля явно.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

import mailroom_bot as mb

REPO = Path(mb.__file__).resolve().parent


class ShellStampIsDerivedNotWritten(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mb, "BASE", REPO)
        patcher.start()
        self.addCleanup(patcher.stop)
        mb._ASSET_STAMP.clear()
        self.addCleanup(mb._ASSET_STAMP.clear)

    def _stamped(self, rel: str) -> str:
        return mb._stamped((REPO / rel).read_text(encoding="utf-8"))

    def test_stamp_follows_the_bytes(self):
        first = mb._asset_stamp()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(first, mb._asset_stamp(), "стабилен, пока байты те же")

    def test_page_and_worker_agree_on_one_stamp(self):
        stamp = mb._asset_stamp()
        page = set(re.findall(r"\?v=([0-9a-zA-Z]+)", self._stamped("praxisapp.html")))
        worker = set(re.findall(r"\?v=([0-9a-zA-Z]+)", self._stamped("praxis_static/sw.js")))
        self.assertTrue(page, "страница должна ссылаться на версионированные ассеты")
        self.assertEqual(page, {stamp}, "страница просит не тот отпечаток")
        self.assertEqual(worker, {stamp},
                         "прекэш складывал бы URL, которых страница не запрашивает")

    def test_cache_name_carries_the_stamp(self):
        worker = self._stamped("praxis_static/sw.js")
        names = set(re.findall(r"praxis-shell-[A-Za-z0-9._-]+", worker))
        self.assertEqual(names, {f"praxis-shell-{mb._asset_stamp()}"},
                         "постоянное имя кэша значит, что activate не чистит старые версии")

    def test_a_changed_asset_changes_the_stamp(self):
        """Иначе клиент остался бы на старой оболочке навсегда.

        Проверяется на ВРЕМЕННЫХ ассетах: первая редакция этого теста дописывала пробу в
        живой `praxis_static/app.css` и убирала её в `finally` — но прерванный прогон
        оставил бы её в дереве. Механизм от этого не страдает: он и так считает байты.
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="shell_stamp_") as tmp:
            root = Path(tmp)
            (root / "praxis_static").mkdir()
            for name in ("app.js", "app.css", "ambient.js"):
                (root / "praxis_static" / name).write_text("// v1", encoding="utf-8")
            with mock.patch.object(mb, "BASE", root):
                mb._ASSET_STAMP.clear()
                before = mb._asset_stamp()
                (root / "praxis_static" / "app.css").write_text("// v2", encoding="utf-8")
                mb._ASSET_STAMP.clear()
                after = mb._asset_stamp()
        self.assertNotEqual(after, before, "правка ассета обязана менять версию оболочки")
        self.assertRegex(after, r"^[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()
