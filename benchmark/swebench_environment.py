"""Resolve reproducible test environments from SWE-bench instance metadata."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

ENVIRONMENT_CATALOG_VERSION = "2026-08-30"


@dataclass(frozen=True)
class EnvironmentSpec:
    """Immutable environment contract consumed by one benchmark instance."""

    python_version: str
    image: str
    dependencies: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    timeout_seconds: int = 900
    metadata: tuple[tuple[str, str], ...] = ()

    @property
    def fingerprint(self) -> str:
        """Stable identity for the effective environment contract."""
        payload = {
            "python_version": self.python_version,
            "image": self.image,
            "dependencies": self.dependencies,
            "test_command": self.test_command,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()


class EnvironmentResolver:
    """Map instance metadata to a reusable Python image family."""

    REPOSITORY_PYTHON = {
        "astropy/astropy": "3.9",
        "django/django": "3.8",
        "matplotlib/matplotlib": "3.7",
        "mwaskom/seaborn": "3.8",
        "pallets/flask": "3.8",
        "psf/requests": "2.7",
        "pydata/xarray": "3.7",
        "pylint-dev/pylint": "3.7",
        "pytest-dev/pytest": "3.7",
        "scikit-learn/scikit-learn": "3.7",
    }
    REPOSITORY_DEPENDENCIES = {
        "astropy/astropy": (
            "pytest==7.4.4", "Cython<3", "pytest-astropy==0.9.0",
            "pytest-astropy-header==0.1.2",
        ),
        "django/django": (
            "pytz",
            "asgiref",
            "sqlparse",
        ),
    }

    @staticmethod
    def _catalog_key(repo: str, revision: str) -> str:
        """Identify the catalog contract without relying on dict ordering."""
        return f"{repo}@{revision or 'unspecified'}"

    def resolve(self, instance: dict[str, Any], override: str | None = None,
                official_images: bool = False) -> EnvironmentSpec:
        repo = str(instance.get("repo", ""))
        python = str(instance.get("python_version") or
                      instance.get("test_python") or
                      self.REPOSITORY_PYTHON.get(repo, "3.12"))
        if not re.fullmatch(r"\d+\.\d+", python):
            raise ValueError(f"invalid Python version: {python}")
        compact = python.replace(".", "")
        official_image = "swebench/sweb.eval.x86_64." + str(
            instance.get("id", "")).lower().replace("__", "_1776_") + ":latest"
        image = (override or instance.get("test_image") or
                 (official_image if official_images else
                  f"vial-code-agent-swebench-python{compact}:local"))
        declared_dependencies = instance.get("dependencies", ())
        if isinstance(declared_dependencies, str):
            declared_dependencies = (declared_dependencies,)
        dependencies = tuple(sorted(set(
            self.REPOSITORY_DEPENDENCIES.get(repo, ()) +
            tuple(str(item) for item in declared_dependencies))))
        command = instance.get("test_command", ())
        if isinstance(command, str):
            command = tuple(shlex.split(command))
        else:
            command = tuple(str(item) for item in command)
        revision = str(instance.get("base_commit") or "")
        metadata = tuple(sorted(
            (str(key), str(value))
            for key, value in (instance.get("environment_metadata") or {}).items()))
        metadata = tuple(sorted((*metadata, (
            "catalog_version", ENVIRONMENT_CATALOG_VERSION),
            ("catalog_key", self._catalog_key(repo, revision)),
            ("base_commit", revision))))
        default_timeout = 1800 if repo == "astropy/astropy" else 900
        try:
            timeout = int(instance.get("timeout_seconds") or
                          instance.get("timeout") or default_timeout)
        except (TypeError, ValueError):
            timeout = default_timeout
        timeout = max(timeout, 1)
        if official_images:
            metadata = tuple(sorted((*metadata, ("official_image", "true"))))
        return EnvironmentSpec(
            python_version=python,
            image=str(image),
            dependencies=dependencies,
            test_command=command,
            timeout_seconds=timeout,
            metadata=metadata,
        )
