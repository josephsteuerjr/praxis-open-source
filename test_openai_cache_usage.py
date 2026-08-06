"""Кэшированный префикс на openai-пути перестаёт быть невидимым.

02.08.2026. Кэш здесь АВТОМАТИЧЕСКИЙ: провайдер сам кэширует совпадающий префикс, его
нельзя включить — его зарабатывают стабильностью байтов. Отчитывается он единственным
полем `usage.prompt_tokens_details.cached_tokens`. Мы читали только антропиковские имена
(`cache_read`/`cache_creation`), а реле вдобавок схлопывало usage до `{in, out}`.

Из-за этого в учёте по всем ходам через gpt стояли нули — и это читалось как «кэш не
работает», хотя означало «не сообщено». На этом я построил неверный вывод для Егора.
Тест охраняет именно различение: отсутствие поля не превращается в ноль-как-факт.
"""
from __future__ import annotations

import unittest

import llm


class _Details:
    def __init__(self, cached):
        self.cached_tokens = cached


class _Usage:
    def __init__(self, prompt=0, completion=0, details=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        if details is not None:
            self.prompt_tokens_details = details


class CachedPrefixIsVisible(unittest.TestCase):
    def test_reads_the_openai_field(self):
        self.assertEqual(llm._openai_cached_tokens(_Usage(42000, 300, _Details(18800))), 18800)

    def test_reads_a_plain_dict_too(self):
        self.assertEqual(
            llm._openai_cached_tokens({"prompt_tokens_details": {"cached_tokens": 7}}), 7)

    def test_absent_details_are_zero_not_a_crash(self):
        self.assertEqual(llm._openai_cached_tokens(_Usage(10, 1)), 0)
        self.assertEqual(llm._openai_cached_tokens(None), 0)
        self.assertEqual(llm._openai_cached_tokens({}), 0)

    def test_garbage_does_not_poison_accounting(self):
        self.assertEqual(llm._openai_cached_tokens({"prompt_tokens_details": "нет"}), 0)
        self.assertEqual(
            llm._openai_cached_tokens({"prompt_tokens_details": {"cached_tokens": "х"}}), 0)
        self.assertEqual(
            llm._openai_cached_tokens({"prompt_tokens_details": {"cached_tokens": -5}}), 0)

    def test_zero_cache_is_not_recorded_as_a_hit(self):
        """Ноль кэша и отсутствие кэша одинаково не должны попадать в учёт как попадание."""
        self.assertEqual(llm._openai_cached_tokens(_Usage(10, 1, _Details(0))), 0)


class UsageAccountingCarriesIt(unittest.TestCase):
    def test_usage_add_accumulates_cache_read(self):
        import json
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory(prefix="praxis_usage_")
        self.addCleanup(tmp.cleanup)
        orig = llm.USAGE_PATH
        llm.USAGE_PATH = Path(tmp.name) / "usage.json"
        try:
            llm._usage_add("voice", {"in": 42000, "out": 300, "cache_read": 18800},
                           model="gpt-5.6-sol")
            data = json.loads(llm.USAGE_PATH.read_text(encoding="utf-8"))
            day = next(iter(data.values()))
            self.assertEqual(day["voice"]["cache_read"], 18800)
            self.assertEqual(day["voice"]["models"]["gpt-5.6-sol"]["cache_read"], 18800)
        finally:
            llm.USAGE_PATH = orig


if __name__ == "__main__":
    unittest.main()
