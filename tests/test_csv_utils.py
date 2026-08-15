from __future__ import annotations

import unittest

from vial_code_agent.csv_utils import parse_csv_line


class CsvUtilsTests(unittest.TestCase):
    def test_parse_csv_line_splits_unquoted_fields(self) -> None:
        self.assertEqual(parse_csv_line("alpha,beta,gamma"), ["alpha", "beta", "gamma"])

    def test_parse_csv_line_preserves_commas_and_quotes_inside_quoted_fields(self) -> None:
        self.assertEqual(
            parse_csv_line('alpha,"beta,gamma","say ""hello"""'),
            ["alpha", "beta,gamma", 'say "hello"'],
        )
