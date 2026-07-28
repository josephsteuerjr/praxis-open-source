from __future__ import annotations

import unittest

import tool_offerings


class ToolOfferingTests(unittest.TestCase):
    def test_provider_search_shapes_are_not_local_functions(self):
        for descriptor in (
            {"type": "web_search", "search_context_size": "medium",
             "external_web_access": True},
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        ):
            self.assertEqual(tool_offerings.local_function_names([descriptor]), frozenset())

    def test_hybrid_and_unknown_provider_shapes_fail_closed(self):
        invalid = (
            {"type": "web_search", "name": "web_search", "input_schema": {}},
            {"type": "web_search_20250305", "name": "web_search", "input_schema": {}},
            {"type": "future_server_tool", "name": "probe"},
            {"type": "", "name": "probe"},
        )
        for descriptor in invalid:
            with self.subTest(descriptor=descriptor), self.assertRaises(
                tool_offerings.ToolOfferingError
            ):
                tool_offerings.local_function_names([descriptor])

    def test_local_names_are_exact_and_cannot_alias_hosted_search(self):
        invalid_lists = (
            [{"name": " probe"}],
            [{"name": "probe "}],
            [{"type": "web_search"}, {"name": "web_search"}],
        )
        for offered in invalid_lists:
            with self.subTest(offered=offered), self.assertRaises(
                tool_offerings.ToolOfferingError
            ):
                tool_offerings.local_function_names(offered)


if __name__ == "__main__":
    unittest.main()
