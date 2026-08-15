from __future__ import annotations

import unittest

from vial_code_agent.csv_parser import parse_csv_line


class CsvParserTests(unittest.TestCase):
    def test_parses_unquoted_and_quoted_comma_fields(self) -> None:
        self.assertEqual(
            parse_csv_line('alpha,"bravo,charlie",delta'),
            ["alpha", "bravo,charlie", "delta"],
        )

    def test_parses_escaped_quotes_inside_quoted_fields(self) -> None:
        self.assertEqual(
            parse_csv_line('"alpha ""quoted""",bravo'),
            ['alpha "quoted"', "bravo"],
        )
