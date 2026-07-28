"""
Ориентация в ветке: промпт перестаёт утверждать форум там, где форума нет.

Правка ФРАЗЫ, а не маршрута — ключ разговора не меняется. Но фраза была фактом,
который она читала о своём положении, и в обычной супергруппе он был ложен целиком.

Запуск:  python praxis_test.py test_topic_orientation -v
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

import telegram_routes as tr


def _orient_source(*, code_only: bool = False) -> str:
    """Кусок раннера, который собирает ориентацию. Читаем с диска: импорт раннера
    тянет telethon и живой .env, а проверяем мы ветвление по вердикту реестра.

    `code_only` выбрасывает строки-комментарии. Без этого тест ловил бы собственный
    комментарий, цитирующий снятую фразу, — ровно та хрупкость грепа по исходнику,
    из-за которой такие тесты и врут."""
    src = (Path(tr.__file__).parent / "mtproto_runner.py").read_text(
        encoding="utf-8", errors="replace")
    i = src.index("topic_orient = \"\"")
    j = src.index("orientation_bundle", i)
    block = src[i:j]
    if not code_only:
        return block
    return "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))


class TestBranchesOnEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_orient_"))
        self._orig = tr.DIR
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"

    def tearDown(self):
        tr.DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_verdict_says_something_different_and_true(self):
        """Поведением, а не грепом: греп по исходнику ломался на переносе строки
        три раза подряд — это не тест, это проверка форматирования."""
        args = ("-1001240718803", 93707, "-1001240718803#topic:93707")
        forum = tr.orientation_line(*args, tr.TRUE)
        plain = tr.orientation_line(*args, tr.FALSE)
        unknown = tr.orientation_line(*args, tr.UNKNOWN)

        self.assertIn("Telegram forum topic", forum)
        self.assertIn("isolated from every other topic", forum)

        self.assertIn("NOT a Telegram forum topic", plain)
        self.assertIn("PART of a room", plain, "она обязана знать, что видит не всё")
        self.assertNotIn("isolated from every other topic", plain)

        self.assertIn("unverified", unknown)
        self.assertIn("unconfirmed", unknown)
        self.assertNotIn("Telegram forum topic:", unknown)

        for line in (forum, plain, unknown):
            self.assertIn("top_msg_id=93707", line,
                          "адресация одинакова во всех случаях — меняется утверждение")
            self.assertIn("selector=-1001240718803#topic:93707", line)

    def test_runner_asks_the_registry_instead_of_assuming(self):
        block = _orient_source(code_only=True)
        self.assertIn("telegram_routes.status_at", block,
                      "ориентация обязана спрашивать реестр, а не догадываться")
        self.assertIn("telegram_routes.orientation_line", block)

    def test_registry_answers_for_the_measured_rooms(self):
        # AbstractDL: прямой отказ Telegram — не форум.
        tr.observe("-1001240718803", kind="channel_forum_missing", message_id=1)
        self.assertEqual(tr.status_at("-1001240718803", 93707)[0], tr.FALSE)
        # Грибница: настоящие openers — форум.
        tr.observe("-1004301095307", kind="topic_opener_seen", message_id=5)
        self.assertEqual(tr.status_at("-1004301095307", 9)[0], tr.TRUE)
        # Незнакомая комната: молчим, а не выдумываем.
        self.assertEqual(tr.status_at("-100999", 7)[0], tr.UNKNOWN)


class TestItIsStillOnlyAPhrase(unittest.TestCase):
    def test_routing_is_untouched(self):
        """Ключ разговора не зависит от реестра: это следующий шаг и отдельное решение."""
        import telegram_topics
        src = Path(telegram_topics.__file__).read_text(encoding="utf-8")
        self.assertNotIn("telegram_routes", src)

    def test_orientation_block_does_not_change_the_route(self):
        block = _orient_source(code_only=True)
        self.assertFalse(re.search(r"\broute\s*=", block),
                         "ориентация не имеет права переписывать маршрут")


if __name__ == "__main__":
    unittest.main()
