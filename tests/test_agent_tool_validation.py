from __future__ import annotations

import unittest

from agent import execute_tool_call
from tools import function_call, git_history_query, header_lookup


class AgentToolValidationTest(unittest.TestCase):
    def test_missing_required_tool_argument_is_returned_to_model(self) -> None:
        cases = [
            ("header_lookup", header_lookup, "name"),
            ("function_call", function_call, "call"),
            ("git_history_query", git_history_query, "ref"),
        ]

        for name, tool, required in cases:
            with self.subTest(tool=name):
                args, result = execute_tool_call(name, tool, "{}")
                self.assertEqual(args, {})
                self.assertEqual(
                    result["error"]["type"], "invalid_tool_arguments"
                )
                self.assertEqual(result["error"]["tool"], name)
                self.assertIn(required, result["error"]["required"])
                self.assertTrue(result["retryable"])

    def test_malformed_tool_arguments_are_returned_to_model(self) -> None:
        args, result = execute_tool_call("header_lookup", header_lookup, "{")

        self.assertEqual(args, {})
        self.assertEqual(result["error"]["type"], "invalid_tool_arguments")
        self.assertTrue(result["retryable"])

    def test_unexpected_tool_argument_is_returned_to_model(self) -> None:
        args, result = execute_tool_call(
            "header_lookup",
            header_lookup,
            '{"name": "stdio.h", "extra": true}',
        )

        self.assertEqual(args, {"name": "stdio.h", "extra": True})
        self.assertEqual(result["error"]["type"], "invalid_tool_arguments")
        self.assertIn("extra", result["error"]["received"])

    def test_valid_tool_call_still_executes(self) -> None:
        args, result = execute_tool_call(
            "header_lookup", header_lookup, '{"name": "stdio.h"}'
        )

        self.assertEqual(args, {"name": "stdio.h"})
        self.assertEqual(result["name"], "stdio.h")
        self.assertTrue(result["exists"])


if __name__ == "__main__":
    unittest.main()
