from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.cache import JsonCache, content_digest


class CacheTests(unittest.TestCase):
    def test_cache_round_trip_and_digest_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("one", encoding="utf-8")
            first = content_digest([source])
            cache = JsonCache(root / "cache")
            cache.put(first, {"passed": True})
            source.write_text("two", encoding="utf-8")

            self.assertEqual(cache.get(first), {"passed": True})
            self.assertNotEqual(first, content_digest([source]))
