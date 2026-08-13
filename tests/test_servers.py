from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from vial_code_agent.servers import ServerRegistry


class ServerRegistryTests(unittest.TestCase):
    def _registry(self, tmp: str) -> ServerRegistry:
        return ServerRegistry(Path(tmp))

    def test_add_and_list_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(directory)
            server = registry.add_server("my-server", "https://api.example.com/v1", "MY_KEY")
            self.assertEqual(server.name, "my-server")
            self.assertEqual(server.base_url, "https://api.example.com/v1")
            self.assertEqual(len(registry.list_servers()), 1)

    def test_persisted_in_vial_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ServerRegistry(root)
            registry.add_server("srv", "https://x.example/v1", models=["m1"])
            registry.pool_add("srv/m1")
            data = (root / ".vial.json").read_text(encoding="utf-8")
            self.assertIn("servers", data)
            self.assertIn("pool", data)
            reloaded = ServerRegistry(root)
            self.assertEqual(reloaded.servers["srv"].base_url, "https://x.example/v1")
            self.assertEqual(reloaded.pool, ["srv/m1"])

    def test_preserves_existing_config_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vial.json").write_text(
                '{"model": "keep-me", "test_timeout": 42}\n', encoding="utf-8")
            registry = ServerRegistry(root)
            registry.add_server("srv", "https://x.example/v1")
            data = (root / ".vial.json").read_text(encoding="utf-8")
            self.assertIn('"model": "keep-me"', data)
            self.assertIn("42", data)

    def test_remove_server_drops_its_pool_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(directory)
            registry.add_server("srv", "https://x.example/v1", models=["m1"])
            registry.pool_add("srv/m1")
            registry.remove_server("srv")
            self.assertEqual(registry.pool, [])
            self.assertEqual(registry.list_servers(), [])

    def test_pool_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(directory)
            registry.pool_add("a/model")
            registry.pool_add("b/model")
            registry.pool_remove("a/model")
            self.assertEqual(registry.pool, ["b/model"])

    def test_provider_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(directory)
            registry.add_server("local", "https://local.example/v1")
            self.assertEqual(registry.provider_kind("local/m1"), "http")
            self.assertEqual(registry.provider_kind("openai/gpt-5.6-luna"), "opencode")

    def test_server_and_model_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(directory)
            self.assertEqual(registry.server_and_model("srv/m1"), ("srv", "m1"))
            with self.assertRaises(ValueError):
                registry.server_and_model("no-slash")


if __name__ == "__main__":
    unittest.main()
