"""Server and model registry for the chat runtime.

Custom servers are OpenAI-compatible HTTP endpoints configured by the user
directly from the terminal. The registry persists servers and the routing
pool in ``.vial.json`` under the ``servers`` and ``pool`` keys, preserving
every other configuration key already present in the file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModelServer:
    """An OpenAI-compatible HTTP endpoint with its exposed model ids."""

    name: str
    base_url: str
    api_key_env: str = ""
    models: list[str] = field(default_factory=list)

    def model_refs(self) -> list[str]:
        return [f"{self.name}/{model}" for model in self.models]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "models": list(self.models),
        }


class ServerRegistry:
    """Loads, mutates and persists servers and the routing pool in .vial.json."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / ".vial.json"
        self.servers: dict[str, ModelServer] = {}
        self.pool: list[str] = []
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for server in data.get("servers", []):
            self.servers[str(server.get("name", ""))] = ModelServer(
                name=str(server.get("name", "")),
                base_url=str(server.get("base_url", "")),
                api_key_env=str(server.get("api_key_env", "")),
                models=[str(model) for model in server.get("models", [])],
            )
        self.pool = [str(model) for model in data.get("pool", [])]

    def save(self) -> None:
        """Merge servers and pool into .vial.json preserving other keys."""
        data: dict[str, object] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        data["servers"] = [server.to_dict() for server in self.servers.values()]
        data["pool"] = list(self.pool)
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ #
    # Servers
    # ------------------------------------------------------------------ #
    def add_server(
        self,
        name: str,
        base_url: str,
        api_key_env: str = "",
        models: list[str] | None = None,
    ) -> ModelServer:
        if not name or not base_url:
            raise ValueError("server name and base_url are required")
        if name in self.servers:
            raise ValueError(f"server already registered: {name}")
        server = ModelServer(
            name=name,
            base_url=base_url.rstrip("/"),
            api_key_env=api_key_env,
            models=list(models or []),
        )
        self.servers[name] = server
        self.save()
        return server

    def remove_server(self, name: str) -> None:
        if name not in self.servers:
            raise ValueError(f"unknown server: {name}")
        del self.servers[name]
        self.pool = [
            model for model in self.pool if not model.startswith(f"{name}/")
        ]
        self.save()

    def add_model(self, server_name: str, model: str) -> None:
        server = self.servers.get(server_name)
        if server is None:
            raise ValueError(f"unknown server: {server_name}")
        if model not in server.models:
            server.models.append(model)
            self.save()

    def remove_model(self, server_name: str, model: str) -> None:
        server = self.servers.get(server_name)
        if server is None:
            raise ValueError(f"unknown server: {server_name}")
        if model in server.models:
            server.models.remove(model)
            ref = f"{server_name}/{model}"
            if ref in self.pool:
                self.pool.remove(ref)
            self.save()

    def list_servers(self) -> list[ModelServer]:
        return sorted(self.servers.values(), key=lambda server: server.name)

    # ------------------------------------------------------------------ #
    # Routing pool
    # ------------------------------------------------------------------ #
    def pool_add(self, model_ref: str) -> None:
        if model_ref in self.pool:
            return
        self.pool.append(model_ref)
        self.save()

    def pool_remove(self, model_ref: str) -> None:
        if model_ref in self.pool:
            self.pool.remove(model_ref)
            self.save()

    def pool_set(self, model_refs: list[str]) -> None:
        """Replace the routing pool with exactly the given model refs."""
        self.pool = [ref for ref in model_refs if ref]
        self.save()

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def all_models(self) -> list[str]:
        """Every model ref the user can route to (pool first, then servers)."""
        refs: list[str] = []
        for ref in self.pool:
            if ref not in refs:
                refs.append(ref)
        for server in self.list_servers():
            for ref in server.model_refs():
                if ref not in refs:
                    refs.append(ref)
        return refs

    def provider_kind(self, model_ref: str) -> str:
        """Return 'http' when the model belongs to a registered server."""
        server_name = model_ref.split("/", 1)[0]
        return "http" if server_name in self.servers else "opencode"

    @staticmethod
    def server_and_model(model_ref: str) -> tuple[str, str]:
        parts = model_ref.split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"model must use server/model format: {model_ref}")
        return parts[0], parts[1]

    @staticmethod
    def available_servers_models(config_root: Path) -> list[str]:
        """Models from .vial.json servers without touching the registry object."""
        registry = ServerRegistry(config_root)
        return registry.all_models()
