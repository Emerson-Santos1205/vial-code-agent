"""Public environment contract for benchmark orchestration.

This module only resolves metadata. It does not create containers, install
dependencies, or mutate a workspace.
"""
try:
    from .swebench_environment import EnvironmentResolver, EnvironmentSpec
except ImportError:
    from swebench_environment import EnvironmentResolver, EnvironmentSpec

__all__ = ["EnvironmentResolver", "EnvironmentSpec"]
