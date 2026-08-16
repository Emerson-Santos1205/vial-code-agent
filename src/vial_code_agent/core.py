from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib
import sys
import uuid
from typing import Any


@dataclass(frozen=True)
class VialCoreReference:
    """Local reference to a VIAL checkout until the core is packaged."""

    root: Path

    def exists(self) -> bool:
        return self.root.is_dir()

    def prototype(self, module: str) -> Any:
        """Load an official prototype module from the pinned VIAL checkout."""
        if not self.exists():
            raise RuntimeError(f"VIAL core is unavailable: {self.root}")
        parent = str(self.root)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        return importlib.import_module(f"prototype.{module}")

    def build_context(self, task: str, root: Path, files: list[Path]) -> Any:
        """Build the official selective Context lifecycle for code generation."""
        state = self.prototype("state")
        context_module = self.prototype("context")
        organization = state.Organization("ORG-VIAL-CODE-AGENT")
        for path in files:
            relative = path.relative_to(root).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            organization.add_field(relative, content, [relative, path.suffix, "source"])
        required = [path.relative_to(root).as_posix() for path in files]
        vial_task = context_module.Task(
            id=f"TASK-{uuid.uuid4().hex[:12]}", prompt=task, required=required,
            expected=None, op="code_generation",
        )
        return context_module.ContextBuilder(organization).build_selective(vial_task)

    def execute_patch(self, applier: Any, patch: str, context_id: str = "") -> Any:
        """Authorize and audit patch application through official Decision/Tool APIs."""
        decision_module = self.prototype("decision")
        tool_module = self.prototype("tool")
        engine = decision_module.DecisionEngine("ORG-VIAL-CODE-AGENT")
        authority = decision_module.Authority(actor="org-root", scope="organization")
        decision = engine.propose(
            objective="apply generated code patch", actor="vial-code-agent",
            authority=authority, context_id=context_id,
        )
        engine.approve(decision.id, "vial-code-agent")
        engine.authorize(decision.id, "org-root")
        tool = tool_module.Tool(
            "TOOL-PATCH-APPLY", "patch_apply", "Apply validated code patch",
            "1.0", "workspace.write", "ORG-VIAL-CODE-AGENT",
            risk_classification="medium", side_effect_classification="mutation",
            invocation=lambda value: applier.apply(value["patch"]),
        )
        result = tool.invoke(
            {"patch": patch}, actor="org-root", organization_id="ORG-VIAL-CODE-AGENT",
            context_id=context_id, decision=decision,
        )
        return result
