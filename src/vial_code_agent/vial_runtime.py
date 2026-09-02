"""Full VIAL runtime integration for the code-agent application.

Every official prototype surface in ``vendor/vial-core/prototype/`` is
composed into the code-generation workflow instead of remaining dead code:

    state          Organization owns persistent cognitive State (RFC-003 §53).
    context        selective vs full Context projection (RFC-004, RFC-007,
                   SDK-004) with the official lifecycle.
    tokenizer      token budgeting for Context and prompts (RFC-007).
    decision       propose -> approve -> authorize -> execute (SDK-005,
                   RUNTIME-006).
    authorization  AuthorizationGate separates capability from authority
                   (SDK-005 §34, TOOLS-007).
    tool          Tool contract, ToolResult and audit records (TOOLS-001).
    resource      Resources + capabilities with cost tiers (SDK-003, RFC-010).
    identity      Authenticator + Principal (SDK-001 §30).
    persistence   atomic JsonRepository persistence (reference deployments).
    coordinator   intent log + atomic transitions + recovery (RFC-009).
    reuse         cognitive reuse with stale invalidation (RFC-004, RFC-008).
    cost          total-cost model + Deterministic-First selector (RFC-010).
    executor      DeterministicExecutor + Evaluator scoring (RFC-007 §2.2).
    errors        structured VIALError handling (SDK-001 §30-31).
"""
from __future__ import annotations

import hashlib
import importlib
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import VialCoreReference
from .events import EventStore, VialEvent
from .persistence import TransactionalJsonRepository
from .project import ProjectDelta, ProjectSnapshot, ProjectStateStore


class PersistenceError(RuntimeError):
    """Persistence failed and the runtime cannot claim durable state."""

# Organizational identity defaults (SDK-002 §4).
ORG_ID = "ORG-VIAL-CODE-AGENT"
AUTHORITY = "org-root"
ACTOR = "vial-code-agent"

# Cost tiers ordered from cheapest to most expensive (RFC-010 §2.4).
RESOURCE_TIERS = {"deterministic": 1.0, "light": 3.0, "advanced": 10.0}
RESOURCE_ORDER = ["deterministic", "light", "advanced"]
TIER_MODEL = {
    "deterministic": None,
    # Internal routing tiers are resolved by the provider boundary.
    "light": "fast",
    "advanced": "reasoning",
}

# Workload price table for the economic cost model (RFC-010 §2.2).
DEFAULT_PRICE_TABLE = {
    "tokens_per_1k": 0.002,
    "inference_input_per_1k": 0.0,
    "inference_output_per_1k": 0.01,
    "latency_per_second": 0.001,
    "retrieval_per_op": 0.0005,
    "construction_per_context": 0.0002,
    "validation_per_op": 0.001,
}

PATCH_TOOL_ID = "TOOL-PATCH-APPLY"
WORKSPACE_FIELD = "workspace"

# TOOLS-003 / RUNTIME-006 §20 risk classification levels.
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Policies bound to Decision authority (SDK-005 policy field).
POLICY_INSPECT = "inspect"
POLICY_DEVELOPMENT = "development"
POLICY_CODE_APPLY = "code-apply"
CONSENSUS_MIN_AGREEMENT = 0.6

_LOCAL_SECRET = "local-vial-dev-secret"


@dataclass
class ApprovalRecord:
    """A recorded human/administrative approval (SDK-005, RUNTIME-006 §8).

    Approval remains semantically distinct from Decision and Authorization:
    a Decision may be authorized yet still require an Approval before
    invocation when policy demands it.
    """
    decision_id: str
    approver: str
    note: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsensusRecord:
    """A recorded cross-model consensus outcome for a Decision.

    Stores every raw per-model answer (not just the winner) so a divergence
    can be handed to a human reviewer with full context. ``agreed`` is the
    verdict used by the consensus gate; ``agreement_ratio`` is the textual
    similarity that produced it (``router._agreement_ratio``).
    """
    decision_id: str
    agreed: bool
    agreement_ratio: float = 0.0
    models: list[str] = field(default_factory=list)
    responses: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, dict[str, object]] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    note: str = ""


def file_field_key(relative: str) -> str:
    return f"file:{relative}"


class VialRuntime:
    """Composes the official VIAL prototype contracts into one runtime.

    Execution resources remain replaceable (SDK-003); the Organization,
    its State, Decisions, Reuse cache and audit trail are persistent and
    auditable (RFC-003, RFC-008, RFC-009, SDK-005).
    """

    def __init__(
        self,
        reference: VialCoreReference,
        state_root: Path,
        org_id: str = ORG_ID,
        authority: str = AUTHORITY,
        actor: str = ACTOR,
        price_table: dict[str, Any] | None = None,
        persist_state: bool = True,
        dev_secret: str = _LOCAL_SECRET,
    ) -> None:
        self.reference = reference
        self.state_root = Path(state_root)
        self.org_id = org_id
        self.authority = authority
        self.actor = actor
        self.persist_state = persist_state
        self.dev_secret = dev_secret

        # --- prototype modules (every surface is loaded and composed) ---
        self._state = reference.prototype("state")
        self._context = reference.prototype("context")
        self._decision = reference.prototype("decision")
        self._tool = reference.prototype("tool")
        self._authorization = reference.prototype("authorization")
        self._resource = reference.prototype("resource")
        self._identity = reference.prototype("identity")
        self._persistence = reference.prototype("persistence")
        self._coordinator = reference.prototype("coordinator")
        self._reuse = reference.prototype("reuse")
        self._cost = reference.prototype("cost")
        self._executor = reference.prototype("executor")
        self._errors = reference.prototype("errors")
        self.tokenizer = reference.prototype("tokenizer")

        # --- state / organization (RFC-003 §53) ---
        self.organization = self._state.Organization(self.org_id, authority=self.authority)

        # --- identity boundary (SDK-001 §30) ---
        self.authenticator = self._identity.Authenticator()
        self.authenticator.register(self.actor, self.org_id, self.dev_secret)
        self.authenticator.register(self.authority, self.org_id, self.dev_secret)

        # --- resource registry (SDK-003 §27-33) ---
        self.registry = self._resource.ResourceRegistry(self.org_id)

        # --- persistence boundary (reference deployments) ---
        self.repository = TransactionalJsonRepository(
            self.state_root, self._persistence.JsonRepository(self.state_root))

        # --- coordinator: intent log + recovery (RFC-009 §2.3) ---
        self.coordinator = self._coordinator.StateCoordinator(self.organization)

        # --- decision engine (SDK-005) ---
        self.decision_engine = self._decision.DecisionEngine(
            self.org_id, root_authority=self.authority)

        # --- cognitive reuse (RFC-008 §2.3) ---
        self.reuse_engine = self._reuse.ReuseEngine(self.organization)

        # --- economic cost model + Deterministic-First selector (RFC-010) ---
        self.price_table = dict(price_table or DEFAULT_PRICE_TABLE)
        self.cost_model = self._cost.CostModel(self.price_table)
        self.selector = self._cost.ResourceSelector(RESOURCE_TIERS, RESOURCE_ORDER)
        self._costs = self._cost.CostComponents()

        # --- deterministic executor (RFC-007 §2.2) ---
        self.deterministic_executor = self._executor.DeterministicExecutor()
        self.executions: list[dict[str, Any]] = []

        # --- tools + authorization gate (TOOLS-001, TOOLS-007) ---
        self.tools = self._tool.ToolRegistry(self.org_id)
        self.contexts: dict[str, Any] = {}
        self.approvals: dict[str, ApprovalRecord] = {}
        self.consensus_records: dict[str, ConsensusRecord] = {}
        self.workspace_root: Path | None = None

        # --- event/ΔState bus + materialized project state (agent coordination) ---
        self.events = EventStore()
        self.events.configure({self.actor, self.authority})
        self.project = ProjectStateStore()
        self.project.configure({self.actor, self.authority})

        self._register_default_resources()
        self._register_default_tools()

        # Restore persisted organizational cognition (RFC-003 continuity).
        self._load_persisted()
        if WORKSPACE_FIELD not in self.organization.fields:
            self.organization.add_field(
                WORKSPACE_FIELD, "", [WORKSPACE_FIELD], authority=self.authority)

    # ------------------------------------------------------------------ #
    # Resource and Tool registration
    # ------------------------------------------------------------------ #
    def _register_default_resources(self) -> None:
        deterministic = self._resource.Resource(
            "RESOURCE-DETERMINISTIC", "function", self.org_id,
            name="deterministic-code-transform",
            description="Mechanical source transforms resolved without a model",
            metadata={"tier": "deterministic"},
        )
        deterministic.add_capability(self._resource.Capability(
            "code_transform", "mechanical code transforms",
            constraints={"tier": "deterministic", "op": "mechanical"}))
        self.registry.register(deterministic)

        opencode = self._resource.Resource(
            "RESOURCE-OPENCODE", "model", self.org_id,
            name="opencode", description="opencode CLI execution resource",
            metadata={"tier": "advanced", "provider": "opencode"},
        )
        opencode.add_capability(self._resource.Capability(
            "code_transform", "LLM code transforms",
            constraints={"tier": "advanced"}))
        opencode.add_capability(self._resource.Capability(
            "cognition", "general reasoning over workspace context",
            constraints={"tier": "advanced"}))
        self.registry.register(opencode)

    def _register_default_tools(self) -> None:
        self.patch_tool = self._tool.Tool(
            PATCH_TOOL_ID, "patch_apply", "Apply a validated code patch", "1.0",
            "patch_apply", self.org_id,
            contract=self._tool.ToolContract(
                input_schema={
                    "type": "object",
                    "properties": {"patch": {"type": "string"},
                                   "_applier": {},
                                   "reverse": {"type": "boolean"}},
                },
                output_schema={"type": "object"},
                errors=["PatchError"],
                invocation_semantics=(
                    "git apply --check, then apply; rollback on test failure"),
            ),
            security_policy={
                "allowed_actors": [self.actor, self.authority],
                "required_capability": "patch_apply",
                "required_scope": "organization",
                "required_policy": POLICY_CODE_APPLY,
            },
            risk_classification=RISK_MEDIUM,
            side_effect_classification="mutation",
            invocation=lambda value: (
                value["_applier"].reverse(value["patch"])
                if value.get("reverse") else
                value["_applier"].apply(value["patch"])),
        )
        self.tools.register(self.patch_tool)

        self._define_tool(
            "TOOL-READ-FILE", "read_file",
            "Read a source file inside the workspace", "file_read",
            RISK_LOW, "none", self._invoke_read_file,
            policy=POLICY_INSPECT)
        self._define_tool(
            "TOOL-SEARCH", "search",
            "Search workspace files for a text pattern", "search",
            RISK_LOW, "none", self._invoke_search,
            policy=POLICY_INSPECT)
        self._define_tool(
            "TOOL-LIST-FILES", "list_files",
            "List workspace files matching a glob", "list_files",
            RISK_LOW, "none", self._invoke_list_files,
            policy=POLICY_INSPECT)
        self._define_tool(
            "TOOL-INSPECT-DEPENDENCY", "inspect_dependency",
            "Inspect a declared dependency (import / requirements entry)",
            "inspect_dependency", RISK_LOW, "none",
            self._invoke_inspect_dependency, policy=POLICY_INSPECT)
        self._define_tool(
            "TOOL-RUN-TEST", "run_test",
            "Run a workspace test command", "run_test",
            RISK_MEDIUM, "none", self._invoke_run_test,
            policy=POLICY_DEVELOPMENT)
        self._define_tool(
            "TOOL-RUN-BUILD", "run_build",
            "Run an allowlisted build/command inside the workspace",
            "run_build", RISK_MEDIUM, "none", self._invoke_run_command,
            policy=POLICY_DEVELOPMENT)
        self._define_tool(
            "TOOL-RUN-GIT", "run_git",
            "Run a git command inside the workspace", "run_git",
            RISK_HIGH, "mutation", self._invoke_run_git,
            policy=POLICY_DEVELOPMENT)
        self._define_tool(
            "TOOL-RUN-AUDIT", "run_audit",
            "Run the VIAL repository audit suite (AUDIT-000..015)",
            "run_audit", RISK_LOW, "none", self._invoke_run_audit,
            policy=POLICY_INSPECT)

    def _define_tool(self, tool_id: str, name: str, description: str,
                     capability: str, risk: str, side_effect: str,
                     invocation: Callable[[dict[str, Any]], Any],
                     policy: str = POLICY_DEVELOPMENT) -> Any:
        """Register a governed Tool with a canonical contract (TOOLS-001/007)."""
        tool = self._tool.Tool(
            tool_id, name, description, "1.0", capability, self.org_id,
            contract=self._tool.ToolContract(
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                errors=["ToolError"],
                invocation_semantics=(
                    f"capability '{capability}' requires an AUTHORIZED Decision"),
            ),
            security_policy={
                "allowed_actors": [self.actor, self.authority],
                "required_capability": capability,
                "required_scope": "organization",
                "required_policy": policy,
            },
            risk_classification=risk,
            side_effect_classification=side_effect,
            invocation=invocation,
        )
        self.tools.register(tool)
        return tool

    # ------------------------------------------------------------------ #
    # Governed Tool invocations (TOOLS-001, TOOLS-007)
    # ------------------------------------------------------------------ #
    def set_workspace_root(self, root: Path) -> None:
        self.workspace_root = Path(root).resolve()

    def _resolve_path(self, relative: str) -> Path:
        root = self.workspace_root or Path.cwd().resolve()
        path = (root / relative).resolve()
        if root not in path.parents and path != root:
            raise self._errors.VIALValidationError(
                "PATH_OUTSIDE_WORKSPACE",
                f"path escapes workspace: {relative}",
                details={"path": relative, "workspace": str(root)})
        return path

    def _invoke_read_file(self, value: dict[str, Any]) -> Any:
        path = self._resolve_path(str(value["path"]))
        try:
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}
        except OSError as exc:
            raise self._errors.VIALStateError(
                "FILE_READ_ERROR", str(exc), details={"path": str(path)}) from exc

    def _invoke_search(self, value: dict[str, Any]) -> Any:
        pattern = str(value["pattern"])
        root = self.workspace_root or Path.cwd().resolve()
        expression = re.compile(pattern)
        matches = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    matches.append({"path": str(path.relative_to(root)),
                                    "line": number,
                                    "content": line.strip()[:200]})
                    break
        return {"matches": matches}

    def _invoke_list_files(self, value: dict[str, Any]) -> Any:
        root = self.workspace_root or Path.cwd().resolve()
        includes = value.get("patterns") or ["*"]
        if isinstance(includes, str):
            includes = [includes]
        files = []
        for pattern in includes:
            files.extend(root.rglob(str(pattern)))
        return {"files": sorted(
            str(path.relative_to(root)) for path in files if path.is_file())}

    def _invoke_inspect_dependency(self, value: dict[str, Any]) -> Any:
        root = self.workspace_root or Path.cwd().resolve()
        name = str(value["dependency"])
        requirements = root / "requirements.txt"
        declared = None
        if requirements.is_file():
            for line in requirements.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.split("==", 1)[0] == name:
                    declared = line
                    break
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", None)
            available = True
        except Exception:
            module, version, available = None, None, False
        return {"dependency": name, "declared": declared,
                "available": available, "version": version}

    def _invoke_run_command(self, value: dict[str, Any]) -> Any:
        from .command_runner import CommandRunner
        root = self.workspace_root or Path.cwd().resolve()
        command = value.get("command")
        if isinstance(command, str):
            command = CommandRunner.parse(command)
        runner = CommandRunner(root, unsafe=bool(value.get("unsafe", False)))
        result = runner.run(list(command), int(value.get("timeout", 120)))
        return {"command": list(result.command), "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}

    def _invoke_run_test(self, value: dict[str, Any]) -> Any:
        from .test_runner import run_tests
        root = self.workspace_root or Path.cwd().resolve()
        command = value.get("command")
        if isinstance(command, str):
            from .command_runner import CommandRunner
            command = CommandRunner.parse(command)
        result = run_tests(root, list(command), int(value.get("timeout", 120)))
        return {"command": list(result.command), "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "duration": result.elapsed_seconds, "status": "SUCCESS" if result.passed else "FAILED"}

    def _invoke_run_git(self, value: dict[str, Any]) -> Any:
        from .git_ops import GitError, GitWorkspace
        root = self.workspace_root or Path.cwd().resolve()
        try:
            output = GitWorkspace(root).run(*[str(a) for a in value.get("args", [])])
        except GitError as exc:
            raise self._errors.VIALExecutionError("GIT_ERROR", str(exc)) from exc
        return {"stdout": output}

    def _invoke_run_audit(self, value: dict[str, Any]) -> Any:
        core_root = value.get("core_root")
        if core_root:
            core = Path(str(core_root)).resolve()
        elif self.reference.exists():
            core = self.reference.root
        else:
            raise self._errors.VIALUnavailableError(
                "CORE_UNAVAILABLE", "no VIAL core checkout to audit")
        script = core / "audit" / "run_all.py"
        if not script.is_file():
            raise self._errors.VIALStateError(
                "AUDIT_SCRIPT_NOT_FOUND", f"missing {script}")
        process = subprocess.run(
            [sys.executable, str(script), "--full"], cwd=core,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=int(value.get("timeout", 300)))
        return {"returncode": process.returncode, "stdout": process.stdout,
                "stderr": process.stderr}

    def register_resource(self, resource_id: str, resource_type: str,
                          capability: str) -> Any:
        """Register a new execution resource with one capability (SDK-003)."""
        resource = self._resource.Resource(
            resource_id, resource_type, self.org_id, name=resource_id)
        resource.add_capability(self._resource.Capability(capability))
        self.registry.register(resource)
        return resource

    # ------------------------------------------------------------------ #
    # Organizational State / workspace projection
    # ------------------------------------------------------------------ #
    def add_workspace_fields(self, root: Path, files: list[Path]) -> None:
        """Project workspace files onto organizational State fields.

        Field values are refreshed from disk so Reuse compatibility checks
        (RFC-008 §2.2) detect external changes without new transitions.
        """
        for path in files:
            relative = path.relative_to(root).as_posix()
            key = file_field_key(relative)
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if key in self.organization.fields:
                self.organization.fields[key].value = content
            else:
                self.organization.add_field(
                    key, content, [relative, path.suffix or "unknown", "source", key],
                    authority=self.authority)

    def workspace_digest(self, files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    # ------------------------------------------------------------------ #
    # Context lifecycle (RFC-007, SDK-004)
    # ------------------------------------------------------------------ #
    def build_task(self, task_text: str, files: list[Path], root: Path,
                   op: str = "code_generation", args: Any = None) -> Any:
        """Build an official Task with a deterministic relevance descriptor.

        ``args`` defaults to a descriptor that includes the operation text and
        the sorted set of files the operation acts on, so the RFC-008 reuse
        signature changes when the workspace file set changes (otherwise a
        cached patch would silently omit newly added files).
        """
        required = [file_field_key(path.relative_to(root).as_posix())
                    for path in files]
        if args is None:
            args = {
                "task": task_text,
                "files": sorted(required),
            }
        return self._context.Task(
            id=f"TASK-{uuid.uuid4().hex[:12]}",
            prompt=task_text,
            required=required,
            expected=None,
            op=op,
            args=args,
        )

    def build_context(self, task_text: str, root: Path,
                      files: list[Path], full: bool = False) -> Any:
        """Build the official Context (selective or full) and register it."""
        self.add_workspace_fields(root, files)
        task = self.build_task(task_text, files, root)
        builder = self._context.ContextBuilder(self.organization)
        context = builder.build_full(task) if full else builder.build_selective(task)
        self.contexts[context.context_id] = context
        return context

    def count_tokens(self, text: str) -> int:
        """Token counting for cognitive cost measurement (RFC-007)."""
        return self.tokenizer.count_tokens(text)

    # ------------------------------------------------------------------ #
    # Cognitive reuse (RFC-008)
    # ------------------------------------------------------------------ #
    def lookup_reuse(self, task: Any) -> tuple[Any | None, str]:
        """Return ``(cached_result, outcome)``; stale entries are invalidated.

        The reference ``ReuseEngine`` never bumps its own hit/rebuild
        counters (vendor behavior), so we account for observable hits here
        (RFC-008 §2.3) without modifying the vendored core.
        """
        entry, outcome = self.reuse_engine.lookup(task)
        if outcome == "hit":
            self.reuse_engine.reuse_hits += 1
        return entry, outcome

    def store_reuse(self, task: Any, outcome: Any, quality: float,
                    context: Any) -> Any:
        """Store validated cognition keyed by a deterministic signature."""
        self.reuse_engine.recomputes += 1
        return self.reuse_engine.store(
            task, outcome, quality, context,
            provenance=f"org:{self.org_id}:runtime",
        )

    def reuse_stats(self) -> dict[str, Any]:
        return self.reuse_engine.stats()

    # ------------------------------------------------------------------ #
    # Cost-aware, Deterministic-First routing (RFC-004, RFC-010)
    # ------------------------------------------------------------------ #
    def capable_tiers(self) -> list[str]:
        tiers = set()
        for resource in self.registry.list():
            for capability in resource.capabilities.values():
                tier = capability.constraints.get("tier")
                if tier:
                    tiers.add(tier)
        return [tier for tier in RESOURCE_ORDER if tier in tiers]

    def select_route(self, task_text: str, requested_model: str = "auto",
                     deterministic: bool = False) -> str | None:
        """Choose the cheapest capable tier; explicit model always wins.

        The deterministic tier is only capable when the task is mechanically
        solvable (RFC-010 §2.4); otherwise only model tiers are considered.
        """
        if requested_model != "auto":
            return requested_model
        tiers = self.capable_tiers()
        if not deterministic:
            tiers = [tier for tier in tiers if tier != "deterministic"]
        try:
            tier = self.selector.select(deterministic, tiers)
        except Exception:
            tier = "advanced"
        return TIER_MODEL.get(tier, "reasoning")

    # ------------------------------------------------------------------ #
    # Economic cost accounting (RFC-004 §21-23, RFC-010)
    # ------------------------------------------------------------------ #
    def record_inference(self, input_tokens: int, output_tokens: int,
                         tier: str = "advanced") -> None:
        multiplier = RESOURCE_TIERS.get(tier, RESOURCE_TIERS["advanced"])
        self._accumulate(self.cost_model.infer(
            input_tokens or 0, output_tokens or 0, tier_multiplier=multiplier))

    def record_retrieval(self, n_ops: int = 1) -> None:
        self._accumulate(self.cost_model.retrieval(n_ops))

    def record_construction(self, n_contexts: int = 1) -> None:
        self._accumulate(self.cost_model.construction(n_contexts))

    def record_validation(self, n_validations: int = 1) -> None:
        self._accumulate(self.cost_model.validation(n_validations))

    def _accumulate(self, components: Any) -> None:
        self._costs.tokens += components.tokens
        self._costs.inference += components.inference
        self._costs.latency += components.latency
        self._costs.retrieval += components.retrieval
        self._costs.construction += components.construction
        self._costs.validation += components.validation

    def costs(self) -> dict[str, float]:
        return self._costs.to_dict()

    # ------------------------------------------------------------------ #
    # Deterministic execution (RFC-007 §2.2, RFC-010 Deterministic First)
    # ------------------------------------------------------------------ #
    def run_deterministic_executor(self, task: Any, ctx: Any) -> Any:
        """Run the official DeterministicExecutor for numeric/bool tasks."""
        return self.deterministic_executor.execute(ctx, task)

    def record_deterministic(self, task: Any, ctx: Any, outcome: Any,
                             correct: bool = True, quality: float = 1.0) -> dict[str, Any]:
        """Score a deterministic code outcome through the official Evaluator."""
        result = self._executor.ExecutionResult(
            task_id=task.id, mode=ctx.mode, outcome=outcome,
            correct=correct, quality=quality)
        score = self._executor.Evaluator().score(result)
        record = {
            "task_id": task.id, "mode": ctx.mode, "correct": correct,
            "quality": score, "timestamp": time.time(),
        }
        self.executions.append(record)
        return record

    # ------------------------------------------------------------------ #
    # Identity (SDK-001 §30)
    # ------------------------------------------------------------------ #
    def authenticate(self, actor: str, secret: str) -> Any:
        """Return a Principal or raise a structured authentication error."""
        return self.authenticator.authenticate(actor, secret)

    # ------------------------------------------------------------------ #
    # Decision lifecycle + authorized Tool invocation (SDK-005, TOOLS-007)
    # ------------------------------------------------------------------ #
    def propose_decision(self, objective: str, type: str = "operation",
                         policy: str = POLICY_DEVELOPMENT,
                         context_id: str = "", risk: str = RISK_MEDIUM,
                         rationale: str = "", evidence: list[str] | None = None,
                         confidence: float = 0.95) -> Any:
        """propose -> approve -> authorize a Decision (SDK-005 §51, RUNTIME-006)."""
        authority = self._decision.Authority(
            actor=self.authority, role="org-root",
            scope="organization", policy=policy)
        decision = self.decision_engine.propose(
            objective=objective,
            actor=self.actor,
            authority=authority,
            type=type,
            context_id=context_id,
            alternatives=[],
            rationale=rationale or "authorized operation for the current task context",
            evidence=evidence or [f"context:{context_id}"],
            confidence=confidence,
            risk=risk,
            priority="high",
        )
        self.decision_engine.approve(decision.id, self.actor)
        self.decision_engine.authorize(decision.id, self.authority)
        return decision

    def propose_patch_decision(self, context_id: str = "") -> Any:
        """propose -> approve -> authorize a patch-apply Decision (SDK-005)."""
        return self.propose_decision(
            objective="apply generated code patch", type="patch_apply",
            policy=POLICY_CODE_APPLY, context_id=context_id,
            risk=RISK_MEDIUM,
            rationale="authorized code change for the current task context",
            evidence=[f"context:{context_id}"])

    def authorize_decision(self, decision_id: str, actor: str) -> Any:
        return self.decision_engine.authorize(decision_id, actor)

    def approve_decision(self, decision_id: str, approver: str,
                         note: str = "") -> ApprovalRecord:
        """Record an explicit Approval distinct from Decision/Authorization
        (SDK-005 §350, RUNTIME-006 §8)."""
        if decision_id not in self.decision_engine.decisions:
            raise KeyError(decision_id)
        if approver != self.authority:
            raise PermissionError(
                f"approval must be recorded by authority '{self.authority}'")
        record = ApprovalRecord(decision_id=decision_id,
                                approver=approver, note=note)
        self.approvals[decision_id] = record
        self.persist()
        return record

    @staticmethod
    def _verified_consensus(record: ConsensusRecord) -> bool:
        """Return whether a positive consensus carries independent evidence.

        A persisted boolean alone is not sufficient to authorize a mutation:
        the gate requires two distinct model responses, a qualifying agreement
        ratio, and static validation evidence for each reviewed response.
        """
        models = list(dict.fromkeys(record.models))
        if len(models) < 2 or not (CONSENSUS_MIN_AGREEMENT <= record.agreement_ratio <= 1.0):
            return False
        if any(not str(record.responses.get(model, "")).strip() for model in models):
            return False
        return all(
            record.evidence.get(model, {}).get("static_valid") is True
            and record.evidence.get(model, {}).get("behavioral_passed") is not False
            for model in models)

    # ------------------------------------------------------------------ #
    # Cross-model consensus gate (TOOLS-001 side-effect classification)
    #
    # ``dispatch_consensus`` in the router asks >=2 independent models the
    # same question and requires textual agreement above a threshold. Any
    # Tool classified as ``mutation`` (today TOOL-PATCH-APPLY and
    # TOOL-RUN-GIT) must carry a ConsensusRecord for its Decision before it
    # may run: a mutation whose content no model verified is not authorized
    # by a Decision alone. Divergent consensus escalates to the same human
    # approval gate high-risk Decisions already use, instead of opening a
    # second, parallel approval path.
    # ------------------------------------------------------------------ #
    def requires_consensus(self, tool: Any) -> bool:
        """Whether a Tool needs a cross-model consensus before invocation."""
        return getattr(tool, "side_effect_classification", "none") == "mutation"

    def record_consensus(
        self,
        decision_id: str,
        agreed: bool,
        agreement_ratio: float = 0.0,
        models: list[str] | None = None,
        responses: dict[str, str] | None = None,
        evidence: dict[str, dict[str, object]] | None = None,
        note: str = "",
    ) -> ConsensusRecord:
        """Record the outcome of a cross-model consensus for a Decision.

        The record is persisted immediately so a CLI invocation that records
        consensus and then stops (e.g. because a later gate rejected the
        patch) still leaves the consensus durable for ``--decisions``.
        """
        record = ConsensusRecord(
            decision_id=decision_id,
            agreed=agreed,
            agreement_ratio=agreement_ratio,
            models=list(models or []),
            responses=dict(responses or {}),
            evidence=dict(evidence or {}),
            note=note,
        )
        self.consensus_records[decision_id] = record
        self.persist()
        return record

    def _consensus_gate(self, tool: Any, decision: Any) -> Any | None:
        """Enforce the cross-model consensus gate ahead of the approval gate.

        Returns a rejected ``ToolResult`` when the gate blocks, or ``None``
        so the normal authorization/approval pipeline continues.
        """
        if not self.requires_consensus(tool):
            return None
        record = self.consensus_records.get(decision.id)
        if decision.id in self.approvals:
            return None
        if record is None:
            self.persist()
            return self._tool.ToolResult(
                status=self._tool.STATUS_REJECTED,
                error=f"Decision '{decision.id}' requires cross-model consensus "
                      "before invocation",
                metadata={"tool_id": tool.tool_id, "error_code": "CONSENSUS_REQUIRED",
                          "decision_id": decision.id})
        if record.agreed and self._verified_consensus(record):
            return None
        if record.agreed:
            self.persist()
            return self._tool.ToolResult(
                status=self._tool.STATUS_REJECTED,
                error=f"consensus for Decision '{decision.id}' lacks independent validation evidence",
                metadata={"tool_id": tool.tool_id,
                          "error_code": "CONSENSUS_EVIDENCE_REQUIRED",
                          "decision_id": decision.id,
                          "agreement_ratio": record.agreement_ratio})
        self.persist()
        return self._tool.ToolResult(
            status=self._tool.STATUS_REJECTED,
            error=f"models disagreed on Decision '{decision.id}'; "
                  "human approval required",
            metadata={"tool_id": tool.tool_id, "error_code": "APPROVAL_REQUIRED",
                      "decision_id": decision.id,
                      "agreement_ratio": record.agreement_ratio})

    def risk_rank(self, risk: str) -> int:
        return RISK_ORDER.get(risk, RISK_ORDER[RISK_MEDIUM])

    def decision_requires_approval(self, decision: Any) -> bool:
        """High/critical risk Decisions require an Approval before invocation
        (RUNTIME-006 §20, §46)."""
        return self.risk_rank(getattr(decision, "risk", RISK_MEDIUM)) >= RISK_ORDER[RISK_HIGH]

    def invoke_tool(self, tool_id: str, arguments: dict[str, Any],
                    objective: str = "operate", context_id: str = "",
                    decision: Any = None, require_approval: bool = False) -> Any:
        """Invoke a governed Tool through the full authorization pipeline.

        Derives the Decision type/policy from the Tool contract, so the
        AuthorizationGate must accept the request (TOOLS-007). High-risk Tools
        are rejected unless an Approval is recorded for the Decision.
        """
        tool = self.tools.get(tool_id)
        capability = tool.security_policy.get("required_capability", tool.capability)
        policy = tool.security_policy.get("required_policy", POLICY_DEVELOPMENT)
        decision = decision or self.propose_decision(
            objective, capability, policy, context_id, risk=tool.risk_classification)
        consensus = self._consensus_gate(tool, decision)
        if consensus is not None:
            return consensus
        if (require_approval or self.decision_requires_approval(decision)) \
                and decision.id not in self.approvals:
            self.persist()
            return self._tool.ToolResult(
                status=self._tool.STATUS_REJECTED,
                error=f"Decision '{decision.id}' requires approval before invocation",
                metadata={"tool_id": tool_id, "error_code": "APPROVAL_REQUIRED",
                          "decision_id": decision.id})
        result = tool.invoke(
            arguments, actor=self.authority, organization_id=self.org_id,
            context_id=context_id, decision=decision)
        self._record_decision_outcome(decision, {
            "tool_id": tool_id, "status": result.status,
            "invocation_id": result.invocation_id, "error": result.error,
        })
        return result

    def _record_decision_outcome(self, decision: Any, outcome: Any) -> None:
        """Attach an outcome to an authorized Decision after execution
        (SDK-005 conformance #4, RUNTIME-002 execution cycle)."""
        if decision is None:
            return
        try:
            self.decision_engine.execute(decision.id, self.authority,
                                         outcome=outcome)
        except Exception:
            pass

    def decision_history(self) -> list[Any]:
        return self.decision_engine.history(self.org_id)

    def decision_trace(self, decision_id: str) -> dict[str, Any]:
        """Reconstruct why an operation occurred and which authority/evidence
        allowed it (SDK-001 §46-51, RUNTIME-006 §55)."""
        decision = self.decision_engine.decisions.get(decision_id)
        if decision is None:
            return {"decision_id": decision_id, "found": False}
        trace = decision.to_dict()
        trace["found"] = True
        trace["approval"] = self.approvals.get(decision_id).__dict__ \
            if decision_id in self.approvals else None
        trace["consensus"] = self.consensus_records.get(decision_id).__dict__ \
            if decision_id in self.consensus_records else None
        trace["audit_records"] = [
            record.__dict__ for tool in self.tools.list()
            for record in tool.audit_records
            if record.decision_id == decision_id]
        trace["context"] = self.contexts.get(decision.context_id).to_row() \
            if decision.context_id in self.contexts else None
        return trace

    def pending_decisions(self) -> list[dict[str, Any]]:
        """Decisions awaiting consensus or human approval, with their status.

        Used by ``vial --decisions`` and the ``/decisions`` slash command so
        an operator can review which Decisions are blocked on the consensus
        gate or the approval gate and act on them.
        """
        pending: list[dict[str, Any]] = []
        for decision in self.decision_engine.decisions.values():
            if getattr(decision, "status", "") == "COMPLETED":
                continue
            approval = self.approvals.get(decision.id)
            consensus = self.consensus_records.get(decision.id)
            tool = None
            try:
                decision_type = getattr(decision, "type", "")
                tool = next(
                    (t for t in self.tools.list() if t.capability == decision_type),
                    None)
            except Exception:
                tool = None
            pending.append({
                "decision_id": decision.id,
                "objective": getattr(decision, "objective", ""),
                "status": getattr(decision, "status", ""),
                "risk": getattr(decision, "risk", RISK_MEDIUM),
                "requires_consensus": self.requires_consensus(tool)
                if tool is not None else False,
                "consensus": consensus.__dict__ if consensus is not None else None,
                "approval": approval.__dict__ if approval is not None else None,
            })
        return pending

    # ------------------------------------------------------------------ #
    # Coordinator: atomic, idempotent patch application (RFC-009)
    # ------------------------------------------------------------------ #
    def apply_patch(self, applier: Any, patch: str, context_id: str = "",
                    operation_id: str | None = None, decision: Any = None,
                    allowed_paths: set[str] | None = None,
                    reverse: bool = False) -> Any:
        """Apply a patch through the full governance pipeline.

        Flow: resolve from intent log (idempotency/recovery) -> scope
        validation -> intent log (before mutation) -> authorized Tool
        invocation -> atomic commit / abort -> file reconciliation ->
        persistence. Replayed operations are resolved before any mutation.
        """
        op_id = operation_id or (
            ("ROLLBACK-" if reverse else "") +
            hashlib.sha256(patch.encode("utf-8")).hexdigest())

        resolved = self.coordinator.resolve(op_id)
        if resolved is not None and resolved.status == self._coordinator.COMMITTED:
            self.persist()
            return self._tool.ToolResult(
                status=self._tool.STATUS_SUCCESS,
                output="patch already committed (idempotent replay)",
                metadata={"operation_id": op_id, "recovered": True,
                          "intent": "resolved"},
                provenance=f"intent:{op_id}",
            )
        if resolved is not None and resolved.status == self._coordinator.ABORTED:
            self.persist()
            return self._tool.ToolResult(
                status=self._tool.STATUS_FAILED,
                error=f"operation {op_id} was previously aborted",
                metadata={"operation_id": op_id, "recovered": True},
            )

        if allowed_paths is not None:
            applier.validate(patch, allowed_paths, reverse=reverse)
        else:
            applier.validate(patch, reverse=reverse)

        intent = resolved
        if intent is not None and intent.status == self._coordinator.PENDING:
            # Interrupted operation: resume from the intent log (RFC-009 §2.3.5).
            self.coordinator.interruptions += 1

        decision = decision or self.propose_patch_decision(context_id)
        patch_digest = op_id

        tool = self.tools.get(PATCH_TOOL_ID)
        consensus = self._consensus_gate(tool, decision)
        if consensus is not None:
            return consensus

        if intent is None:
            try:
                intent = self.coordinator.begin(
                    op_id, WORKSPACE_FIELD, patch_digest, self.authority)
            except Exception as exc:
                return self._tool.ToolResult(
                    status=self._tool.STATUS_FAILED,
                    error=str(exc), metadata={"operation_id": op_id})

        tool = self.tools.get(PATCH_TOOL_ID)
        result = tool.invoke(
            {"patch": patch, "_applier": applier, "reverse": reverse},
            actor=self.authority,
            organization_id=self.org_id,
            context_id=context_id,
            decision=decision,
        )

        if result.ok():
            try:
                self.coordinator.commit(op_id)
            except Exception as exc:
                rollback_error = ""
                try:
                    applier.reverse(patch)
                    self.coordinator.abort(op_id)
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
                self._record_decision_outcome(decision, {
                    "operation_id": op_id, "status": self._tool.STATUS_FAILED,
                    "error": f"commit failed after apply: {exc}"})
                return self._tool.ToolResult(
                    status=self._tool.STATUS_FAILED,
                    error=(f"commit failed after apply: {exc}; patch rollback "
                           f"{'failed: ' + rollback_error if rollback_error else 'completed'}"),
                    metadata={"operation_id": op_id,
                              "rollback_completed": not bool(rollback_error),
                              "rollback_error": rollback_error})
            self._reconcile_files(applier.root)
            self._record_decision_outcome(decision, {
                "operation_id": op_id, "status": result.status,
                "invocation_id": result.invocation_id})
            self.persist()
            return result
        self.coordinator.abort(op_id)
        self._record_decision_outcome(decision, {
            "operation_id": op_id, "status": result.status,
            "invocation_id": result.invocation_id, "error": result.error})
        self.persist()
        return result

    def resolve_operation(self, operation_id: str) -> Any | None:
        """Resolve an operation's outcome from the intent log (RFC-009)."""
        return self.coordinator.resolve(operation_id)

    def record_rollback(self, patch: str, operation_id: str | None = None) -> Any:
        """Record an auditable compensation transition for a rolled-back patch."""
        op_id = operation_id or hashlib.sha256(patch.encode("utf-8")).hexdigest()
        compensation_id = f"ROLLBACK-{op_id}"
        resolved = self.coordinator.resolve(compensation_id)
        if resolved is not None:
            return resolved
        self.coordinator.begin(
            compensation_id, WORKSPACE_FIELD, f"rollback:{op_id}", self.authority)
        committed = self.coordinator.commit(compensation_id)
        self.persist()
        return committed

    def _reconcile_files(self, root: Path) -> None:
        """Refresh file fields from disk after an applied patch."""
        for key, file_field in list(self.organization.fields.items()):
            if not key.startswith("file:"):
                continue
            path = root / key[len("file:"):]
            if not path.is_file():
                continue
            try:
                file_field.value = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

    # ------------------------------------------------------------------ #
    # Persistence (RFC-003 continuity)
    # ------------------------------------------------------------------ #
    def persist(self) -> None:
        """Atomically persist organizational cognition or fail explicitly."""
        if not self.persist_state:
            return
        try:
            records = {
                "organization.json": self._organization_to_dict(),
                "decisions.json": {
                did: self._decision_to_dict(d)
                for did, d in self.decision_engine.decisions.items()},
                "intents.json": {
                op_id: self._intent_to_dict(intent)
                for op_id, intent in self.coordinator.intents.items()},
                "reuse.json": {
                "_stats": {
                    "reuse_hits": self.reuse_engine.reuse_hits,
                    "recomputes": self.reuse_engine.recomputes,
                    "invalidations": self.reuse_engine.invalidations,
                },
                "cache": {
                    sig: self._reuse_to_dict(entry)
                    for sig, entry in self.reuse_engine.cache.items()},
                },
                "audit.json": [record.__dict__ for record in self.patch_tool.audit_records],
                "approvals.json": [record.__dict__ for record in self.approvals.values()],
                "consensus.json": [record.__dict__ for record in self.consensus_records.values()],
                "cost.json": self._costs.to_dict(),
                "executions.json": self.executions,
                "events.json": self.events.to_list(),
                "contexts.json": {
                context_id: self._context_to_dict(context)
                for context_id, context in self.contexts.items()},
            }
            if self.project.snapshot is not None:
                records["project.json"] = self.project.snapshot.to_dict()
            self.repository.save_snapshot(records)
            # Compatibility mirrors are written only after the authoritative
            # snapshot is published; recovery always prefers the manifest.
            for name, value in records.items():
                self.repository.save(name, value)
        except Exception as exc:
            raise PersistenceError(
                f"failed to persist VIAL runtime state in {self.state_root}") from exc

    def _load_persisted(self) -> None:
        if not self.persist_state:
            return
        try:
            snapshot = self.repository.load_snapshot()

            def has_record(name: str) -> bool:
                return (name in snapshot if snapshot is not None else
                        (self.state_root / name).is_file())

            def load_record(name: str) -> Any:
                return (snapshot[name] if snapshot is not None
                        else self.repository.load(name))

            if has_record("organization.json"):
                data = load_record("organization.json")
                self.organization.authority = data["authority"]
                self.organization.config_version = data["config_version"]
                self.organization.state_version = data["state_version"]
                self.organization.fields = {
                    key: self._state.StateField(
                        key, field["value"], field["relevance"], field["authority"])
                    for key, field in data["fields"].items()}
                self.organization.transitions = [
                    self._transition_from_dict(t) for t in data["transitions"]]
            if has_record("decisions.json"):
                data = load_record("decisions.json")
                self.decision_engine.decisions = {
                    did: self._decision_from_dict(d) for did, d in data.items()}
            if has_record("intents.json"):
                data = load_record("intents.json")
                self.coordinator.intents = {
                    op_id: self._coordinator.Intent(**intent)
                    for op_id, intent in data.items()}
            if has_record("reuse.json"):
                data = load_record("reuse.json")
                stats = data.get("_stats", {})
                self.reuse_engine.reuse_hits = stats.get("reuse_hits", 0)
                self.reuse_engine.recomputes = stats.get("recomputes", 0)
                self.reuse_engine.invalidations = stats.get("invalidations", 0)
                self.reuse_engine.cache = {
                    sig: self._reuse.CachedResult(**entry)
                    for sig, entry in data.get("cache", data).items()}
            if has_record("audit.json"):
                data = load_record("audit.json")
                self.patch_tool.audit_records = [
                    self._tool.AuditRecord(**record) for record in data]
            if has_record("approvals.json"):
                data = load_record("approvals.json")
                self.approvals = {
                    record["decision_id"]: ApprovalRecord(**record)
                    for record in data}
            if has_record("consensus.json"):
                data = load_record("consensus.json")
                self.consensus_records = {
                    record["decision_id"]: ConsensusRecord(**record)
                    for record in data}
            if has_record("cost.json"):
                data = load_record("cost.json")
                self._costs = self._cost.CostComponents(
                    tokens=data.get("tokens", 0.0),
                    inference=data.get("inference", 0.0),
                    latency=data.get("latency", 0.0),
                    retrieval=data.get("retrieval", 0.0),
                    construction=data.get("construction", 0.0),
                    validation=data.get("validation", 0.0),
                )
            if has_record("executions.json"):
                self.executions = list(load_record("executions.json"))
            if has_record("events.json"):
                self.events = EventStore.from_list(
                    load_record("events.json"))
                self.events.configure({self.actor, self.authority})
            if has_record("project.json"):
                self.project.restore(ProjectSnapshot.from_dict(
                    load_record("project.json")))
                self.project.configure({self.actor, self.authority})
            if has_record("contexts.json"):
                self.contexts = {
                    context_id: self._context_from_dict(data)
                    for context_id, data
                    in load_record("contexts.json").items()}
        except Exception as exc:
            raise PersistenceError(
                f"failed to restore VIAL runtime state from {self.state_root}") from exc

    def _organization_to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.organization.org_id,
            "authority": self.organization.authority,
            "config_version": self.organization.config_version,
            "state_version": self.organization.state_version,
            "fields": {
                key: {"value": field.value, "relevance": field.relevance,
                      "authority": field.authority}
                for key, field in self.organization.fields.items()},
            "transitions": [self._transition_to_dict(t)
                            for t in self.organization.transitions],
        }

    @staticmethod
    def _transition_to_dict(t: Any) -> dict[str, Any]:
        return {
            "transition_id": t.transition_id,
            "organization": t.organization,
            "previous_version": t.previous_version,
            "resulting_version": t.resulting_version,
            "operation": t.operation,
            "authority": t.authority,
            "provenance": t.provenance,
            "timestamp": t.timestamp,
        }

    def _transition_from_dict(self, d: dict[str, Any]) -> Any:
        return self._state.StateTransition(**d)

    @staticmethod
    def _decision_to_dict(d: Any) -> dict[str, Any]:
        return d.to_dict()

    def _decision_from_dict(self, d: dict[str, Any]) -> Any:
        authority = self._decision.Authority(**d["authority"])
        fields = {k: v for k, v in d.items() if k != "authority"}
        return self._decision.Decision(**fields, authority=authority)

    @staticmethod
    def _context_to_dict(context: Any) -> dict[str, Any]:
        return {
            "context_id": context.context_id,
            "task_id": context.task_id,
            "organization_id": context.organization_id,
            "body": context.body,
            "mode": context.mode,
            "state_version": context.state_version,
            "tokens": context.tokens,
            "references": list(context.references),
            "objective": context.objective,
            "scope": context.scope,
            "status": context.status,
            "version": context.version,
            "created_at": context.created_at,
        }

    def _context_from_dict(self, d: dict[str, Any]) -> Any:
        return self._context.Context(**d)

    @staticmethod
    def _intent_to_dict(intent: Any) -> dict[str, Any]:
        return {
            "operation_id": intent.operation_id,
            "key": intent.key,
            "value": intent.value,
            "actor": intent.actor,
            "previous_version": intent.previous_version,
            "status": intent.status,
            "resulting_version": intent.resulting_version,
            "created_at": intent.created_at,
        }

    @staticmethod
    def _reuse_to_dict(entry: Any) -> dict[str, Any]:
        return {
            "signature": entry.signature,
            "outcome": entry.outcome,
            "quality": entry.quality,
            "state_version": entry.state_version,
            "referenced_fields": entry.referenced_fields,
            "provenance": entry.provenance,
            "created_at": entry.created_at,
        }

    # ------------------------------------------------------------------ #
    # Auditability + telemetry (RFC-004, SDK-005, TOOLS-001)
    # ------------------------------------------------------------------ #
    def audit_records(self) -> list[dict[str, Any]]:
        """Audit records aggregated across every registered Tool (TOOLS-001)."""
        records = []
        for tool in self.tools.list():
            records.extend(record.__dict__ for record in tool.audit_records)
        return sorted(records, key=lambda record: record["timestamp"])

    def memory(self) -> dict[str, Any]:
        """Organizational memory surface (RUNTIME-005): validated cognition,
        consequential decisions and audit records persist across executors."""
        return {
            "reuse": self.reuse_stats(),
            "decisions": [
                {"id": d.id, "objective": d.objective, "status": d.status,
                 "outcome": d.outcome}
                for d in self.decision_engine.history(self.org_id)],
            "audit_records": len(self.patch_tool.audit_records),
            "approvals": [record.__dict__ for record in self.approvals.values()],
            "consensus_records": [
                record.__dict__ for record in self.consensus_records.values()],
            "state_root": str(self.state_root),
        }

    def snapshot(self) -> dict[str, Any]:
        """Full organizational telemetry for ``vial status``."""
        return {
            "organization_id": self.organization.org_id,
            "authority": self.organization.authority,
            "config_version": self.organization.config_version,
            "state_version": self.organization.state_version,
            "resources": [resource.to_dict() for resource in self.registry.list()],
            "tools": [tool.to_dict() for tool in self.tools.list()],
            "reuse": self.reuse_stats(),
            "coordinator": {
                "intents": len(self.coordinator.intents),
                "duplicate_commits": self.coordinator.duplicate_commits,
                "interruptions": self.coordinator.interruptions,
            },
            "decisions": len(self.decision_engine.decisions),
            "executions": len(self.executions),
            "audit_records": len(self.patch_tool.audit_records),
            "contexts": len(self.contexts),
            "costs": self.costs(),
            "memory": self.memory(),
            "events": self.events.stats(),
            "project": (self.project.snapshot.to_dict()
                        if self.project.snapshot is not None else None),
            "persisted": self.persist_state,
            "state_root": str(self.state_root),
        }

    # ------------------------------------------------------------------ #
    # Event/ΔState bus + materialized project state (agent coordination)
    #
    # These are organizational-state surfaces (like RFC-003 State), NOT
    # workspace-mutating Tools, so they do not require a Decision: publishing
    # is gated on an authorized actor only and stays fully deterministic.
    # ------------------------------------------------------------------ #
    def publish_event(self, event_type: str, resource: str, version: int,
                      data: dict[str, Any] | None = None,
                      actor: str | None = None) -> VialEvent:
        """Publish a small, versioned event to the hub (idempotent)."""
        event = self.events.publish(
            event_type, resource, version, actor or self.actor, data=data)
        self.persist()
        return event

    def event_delta(self, after_event_id: str = "") -> list[VialEvent]:
        """Events published since a cursor (or all when no cursor)."""
        return self.events.delta(after_event_id)

    def event_latest(self, resource: str | None = None,
                     event_type: str | None = None) -> VialEvent | None:
        return self.events.latest(resource, event_type)

    def capture_project(self, root: Path, files: list[Path]) -> ProjectSnapshot:
        """Materialize the project snapshot from deterministic file facts."""
        snapshot = self.project.capture(root, files)
        self.project.restore(snapshot)
        self.persist()
        return snapshot

    def project_delta(self, root: Path, files: list[Path]) -> ProjectDelta | None:
        """ΔState since the last capture (None on first/baseline capture)."""
        return self.project.delta_from(root, files)

    def set_project_status(self, module: str, value: str,
                           actor: str | None = None) -> None:
        """Record an authorized materialized status (e.g. backend: complete)."""
        self.project.set_status(module, value, actor or self.actor)
        self.persist()
