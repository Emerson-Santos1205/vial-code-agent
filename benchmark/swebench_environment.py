"""Resolve reproducible test environments from SWE-bench instance metadata."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnvironmentSpec:
    """Immutable environment contract consumed by one benchmark instance."""

    python_version: str
    image: str
    dependencies: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    timeout_seconds: int = 900
    metadata: tuple[tuple[str, str], ...] = ()


class EnvironmentResolver:
    """Map instance metadata to a reusable Python image family."""

    REPOSITORY_PYTHON = {
        "astropy/astropy": "3.9",
        "django/django": "3.8",
    }
    REPOSITORY_DEPENDENCIES = {
        "astropy/astropy": (
            "pytest==7.4.4", "Cython<3", "pytest-astropy==0.9.0",
            "pytest-astropy-header==0.1.2",
        ),
    }

    def resolve(self, instance: dict[str, Any], override: str | None = None) -> EnvironmentSpec:
        repo = str(instance.get("repo", ""))
        python = str(instance.get("python_version") or
                      instance.get("test_python") or
                      self.REPOSITORY_PYTHON.get(repo, "3.12"))
        compact = python.replace(".", "")
        image = (override or instance.get("test_image") or
                 f"vial-code-agent-swebench-python{compact}:local")
        dependencies = tuple(dict.fromkeys(
            self.REPOSITORY_DEPENDENCIES.get(repo, ()) +
            tuple(str(item) for item in instance.get("dependencies", ()))))
        command = instance.get("test_command", ())
        if isinstance(command, str):
            command = (command,)
        else:
            command = tuple(str(item) for item in command)
        metadata = tuple(sorted(
            (str(key), str(value))
            for key, value in (instance.get("environment_metadata") or {}).items()))
        try:
            timeout = int(instance.get("timeout_seconds") or
                          instance.get("timeout") or 900)
        except (TypeError, ValueError):
            timeout = 900
        timeout = max(timeout, 1)
        return EnvironmentSpec(
            python_version=python,
            image=str(image),
            dependencies=dependencies,
            test_command=command,
            timeout_seconds=timeout,
            metadata=metadata,
        )
