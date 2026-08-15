import unittest

from vial_code_agent.bytes_utils import format_bytes


class BytesUtilsTests(unittest.TestCase):
    def test_format_bytes_converts_to_kilobytes(self) -> None:
        self.assertEqual(format_bytes(1500), "1.50 KB")

    def test_format_bytes_converts_to_gigabytes(self) -> None:
        self.assertEqual(format_bytes(1_500_000_000), "1.50 GB")
