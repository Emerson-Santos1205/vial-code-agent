"""Immutable, non-governing representation of a SWE-bench instance."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InstanceSpec:
    """Benchmark metadata; authorization remains owned by VialRuntime."""

    id: str
    repo: str
    base_commit: str
    problem_statement: str = ""
    patch: str = ""
    test_patch: str = ""
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstanceSpec":
        def tests(key: str) -> tuple[str, ...]:
            raw = value.get(key, ())
            if isinstance(raw, str):
                return tuple(line.strip() for line in raw.splitlines() if line.strip())
            return tuple(str(item) for item in raw or ())

        return cls(
            id=str(value.get("id", "")),
            repo=str(value.get("repo", "")),
            base_commit=str(value.get("base_commit", "")),
            problem_statement=str(value.get("problem_statement", "")),
            patch=str(value.get("patch", "")),
            test_patch=str(value.get("test_patch", "")),
            fail_to_pass=tests("fail_to_pass"),
            pass_to_pass=tests("pass_to_pass"),
        )
