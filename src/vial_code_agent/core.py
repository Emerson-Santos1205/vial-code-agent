from __future__ import annotations

import importlib
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
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

    def get_current_commit(self) -> str:
        """Retorna o commit SHA atualmente pinado no checkout do VIAL Core."""
        if not self.exists():
            return ""
        import subprocess
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True, capture_output=True, check=False
        )
        return res.stdout.strip() if res.returncode == 0 else ""

    def get_upstream_commit(self) -> str:
        """Obtém o commit SHA mais recente do repositório remoto do VIAL Core."""
        if not self.exists():
            return ""
        import subprocess
        res = subprocess.run(
            ["git", "ls-remote", "origin", "HEAD"], cwd=self.root, text=True, capture_output=True, check=False
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().split()[0]
        return ""

    def check_drift(self) -> dict[str, Any]:
        """Inspeciona o alinhamento e desfasamento entre o pino local e o repositório remoto."""
        current = self.get_current_commit()
        upstream = self.get_upstream_commit()
        synced = (current == upstream) if (current and upstream) else True
        return {
            "exists": self.exists(),
            "current_commit": current,
            "upstream_commit": upstream,
            "synced": synced,
        }

