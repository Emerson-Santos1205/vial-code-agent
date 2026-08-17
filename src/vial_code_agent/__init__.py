"""Public package boundary for the VIAL coding agent."""

from .api import (PersistenceError, VialAgent, VialRuntime, apply_patch,
                  create_runtime)

__all__ = ["PersistenceError", "VialAgent", "VialRuntime", "apply_patch",
           "create_runtime", "__version__"]
__version__ = "0.3.0"
