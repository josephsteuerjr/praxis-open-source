"""Её конституция не превращается в дамп питоновского объекта.

02.08. `_model_call` кладёт снимок входа модели через `_scrub_critical_text`, а тот делает
`str(text or "")`. Для списка anthropic-блоков это питоновский repr:

    [{'type': 'text', 'text': '# Конституция Praxis\\n\\nЯ — Praxis…

Сам вызов уходил чистым — в модель передаётся исходный `system`. Но
`continue_tool_response` берёт системный промпт ИМЕННО ИЗ ЭТОЙ ЗАПИСИ, поэтому со второй
итерации тул-цикла и до конца прогона модель получала конституцию в скобках, с
литеральными \\n и с маркерами `'cache_control'` как обычным текстом.

На живом дереве это сотни прогонов: `0001-model-input.log` — 1864 файла, `0005` — 535,
`0009` — 419, `0013` — 326.
"""
from __future__ import annotations

import json
import unittest

import agent

BLOCKS = [
    {"type": "text", "text": "# Конституция Praxis\n\nЯ — Praxis."},
    {"type": "text", "text": "Второй блок.", "cache_control": {"type": "ephemeral"}},
]


class SystemStaysStructural(unittest.TestCase):
    def test_scrubber_keeps_blocks_as_blocks(self):
        out = agent._scrub_critical_value(BLOCKS, ())
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["text"], "# Конституция Praxis\n\nЯ — Praxis.")
        self.assertEqual(out[1]["cache_control"], {"type": "ephemeral"})

    def test_text_scrubber_is_the_one_that_flattens(self):
        """Граница: сам `_scrub_critical_text` по-прежнему строковый — он для строк."""
        flat = agent._scrub_critical_text(BLOCKS, ())
        self.assertIn("{'type': 'text'", flat, "именно это и попадало в снимок входа")

    def test_snapshot_of_model_input_round_trips_without_repr(self):
        """То, что кладётся в снимок, должно вернуться списком блоков, а не строкой."""
        payload = json.dumps(
            {"system": agent._scrub_critical_value(BLOCKS, ()),
             "messages": [], "tools": agent._scrub_critical_value([], ())},
            ensure_ascii=False, default=str)
        back = json.loads(payload)
        self.assertIsInstance(back["system"], list)
        self.assertNotIn("{'type':", payload)
        self.assertNotIn("\\\\n", payload, "переносы не должны стать литеральными")

    def test_secrets_are_still_scrubbed_inside_blocks(self):
        secret = "sk-очень-секретно-1234"
        blocks = [{"type": "text", "text": f"ключ {secret} внутри"}]
        out = agent._scrub_critical_value(blocks, (secret,))
        self.assertNotIn(secret, json.dumps(out, ensure_ascii=False))
        self.assertIn(agent._CRITICAL_PARAM_REPLACEMENT, out[0]["text"])

    def test_both_durable_writers_keep_system_structural(self):
        """Оба места, откуда системный промпт ЧИТАЮТ ОБРАТНО и шлют модели.

        `_model_call` — снимок входа, из него берёт `continue_tool_response` (итерации
        тул-цикла). `_persist_tool_loop_checkpoint` — чекпойнт, из него берёт
        `run_executor` при возобновлении ПОСЛЕ ПЕРЕЗАПУСКА. Достаточно было забыть один,
        чтобы конституция всё равно приезжала дампом объекта.
        """
        import inspect
        for fn in (agent._model_call, agent._persist_tool_loop_checkpoint):
            src = inspect.getsource(fn)
            self.assertNotIn('"system": _scrub_critical_text', src,
                             f"{fn.__name__}: строковый scrubber на структурном поле")
            self.assertIn('"system": _scrub_critical_value', src, fn.__name__)

    def test_plain_string_system_still_works(self):
        """Совместимость: строковый system остаётся строкой и не оборачивается."""
        out = agent._scrub_critical_value("просто строка", ())
        self.assertEqual(out, "просто строка")

    def test_llm_flattens_blocks_for_openai_without_python_syntax(self):
        import llm
        text = llm.system_text(BLOCKS)
        self.assertIn("# Конституция Praxis", text)
        self.assertNotIn("{'type'", text)
        self.assertNotIn("cache_control", text)


if __name__ == "__main__":
    unittest.main()
