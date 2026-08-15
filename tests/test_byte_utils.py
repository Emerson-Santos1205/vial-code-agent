from __future__ import annotations

import unittest

from vial_code_agent.byte_utils import format_bytes_decimal


class ByteUtilsTests(unittest.TestCase):
    def test_format_bytes_decimal_formats_kb_with_two_decimals(self) -> None:
        self.assertEqual(format_bytes_decimal(1_536), "1.54 KB")

    def test_format_bytes_decimal_formats_gb_with_two_decimals(self) -> None:
        self.assertEqual(format_bytes_decimal(2_500_000_000), "2.50 GB")
