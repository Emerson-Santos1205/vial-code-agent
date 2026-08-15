from __future__ import annotations

import unittest

from vial_code_agent.size_utils import bytes_to_decimal_unit


class SizeUtilsTests(unittest.TestCase):
    def test_bytes_to_decimal_unit_formats_kilobytes_with_two_decimals(self) -> None:
        self.assertEqual(bytes_to_decimal_unit(1500), "1.50 KB")

    def test_bytes_to_decimal_unit_formats_gigabytes_with_two_decimals(self) -> None:
        self.assertEqual(bytes_to_decimal_unit(2500000000), "2.50 GB")
