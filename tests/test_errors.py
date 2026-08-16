from __future__ import annotations

import unittest

from vial_code_agent.errors import (
    ERR_INVALID_CONFIG,
    VialRuntimeError,
    wrap,
)


class VialRuntimeErrorTests(unittest.TestCase):
    def test_carries_code_message_details(self) -> None:
        error = VialRuntimeError(
            ERR_INVALID_CONFIG, "bad config", details={"field": "root"})
        self.assertEqual(error.code, ERR_INVALID_CONFIG)
        self.assertEqual(error.message, "bad config")
        self.assertEqual(error.details, {"field": "root"})
        self.assertEqual(str(error), "bad config")

    def test_defaults_details_to_empty(self) -> None:
        error = VialRuntimeError("CODE", "message")
        self.assertEqual(error.details, {})


class WrapTests(unittest.TestCase):
    def test_wraps_raw_exception(self) -> None:
        error = wrap(ValueError("boom"))
        self.assertIsInstance(error, VialRuntimeError)
        self.assertEqual(error.message, "boom")
        self.assertEqual(error.code, "RUNTIME_ERROR")

    def test_returns_structured_error_unchanged(self) -> None:
        original = VialRuntimeError("TOOL_ERROR", "already structured")
        self.assertIs(wrap(original), original)

    def test_wraps_with_details(self) -> None:
        error = wrap(OSError("io"), code="TOOL_ERROR", details={"op": "write"})
        self.assertEqual(error.code, "TOOL_ERROR")
        self.assertEqual(error.details, {"op": "write"})


if __name__ == "__main__":
    unittest.main()
