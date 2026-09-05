import time
from pathlib import Path

import pytest

from vial_code_agent.core import VialCoreReference
from vial_code_agent.vial_runtime import (
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    VialRuntime,
)


def _reference() -> VialCoreReference:
    root = Path(__file__).resolve().parents[1]
    reference = VialCoreReference(root / "vendor" / "vial-core")
    if not reference.exists():
        pytest.skip("VIAL submodule is not initialized")
    return reference


@pytest.fixture
def runtime(tmp_path):
    return VialRuntime(_reference(), tmp_path / "vial-state")


def test_default_decision_ttl_calculation(runtime):
    assert runtime.compute_decision_ttl(RISK_CRITICAL) == 300.0
    assert runtime.compute_decision_ttl(RISK_HIGH) == 300.0
    assert runtime.compute_decision_ttl(RISK_MEDIUM) == 1800.0
    assert runtime.compute_decision_ttl(RISK_LOW) == 86400.0

    # Cost tier restriction takes minimum
    assert runtime.compute_decision_ttl(RISK_LOW, cost_tier="advanced") == 300.0
    assert runtime.compute_decision_ttl(RISK_MEDIUM, cost_tier="deterministic") == 1800.0


def test_propose_decision_populates_expires_at(runtime):
    now = time.time()
    decision_high = runtime.propose_decision("high risk op", risk=RISK_HIGH)
    assert decision_high.expires_at is not None
    assert abs(decision_high.expires_at - (now + 300.0)) < 2.0

    decision_ttl = runtime.propose_decision("custom ttl op", ttl=60.0)
    assert decision_ttl.expires_at is not None
    assert abs(decision_ttl.expires_at - (now + 60.0)) < 2.0

    target_ts = now + 999.0
    decision_fixed = runtime.propose_decision("fixed op", expires_at=target_ts)
    assert decision_fixed.expires_at == target_ts


def test_authorization_gate_rejects_expired_decision(runtime):
    expired_ts = time.time() - 10.0
    tool = runtime.tools.get("TOOL-READ-FILE")
    policy = tool.security_policy.get("required_policy", "development")
    capability = tool.security_policy.get("required_capability", tool.capability)

    decision = runtime.propose_decision("expired op", type=capability, policy=policy, expires_at=expired_ts)

    # Direct validation through AuthorizationGate raises DECISION_EXPIRED
    gate = runtime._authorization.AuthorizationGate()
    with pytest.raises(runtime._errors.VIALAuthorizationError) as exc_info:
        gate.validate(
            tool, decision, actor=runtime.authority, organization_id=runtime.org_id
        )

    assert exc_info.value.code == "DECISION_EXPIRED"
    assert "expired" in str(exc_info.value).lower()

    # Governed invoke_tool rejects invocation cleanly
    res = runtime.invoke_tool("TOOL-READ-FILE", {"path": "README.md"}, decision=decision)
    assert res.status.lower() == "rejected"
    assert "expired" in res.error.lower()


def test_decision_engine_expire_method(runtime):
    expired_ts = time.time() - 5.0
    decision = runtime.propose_decision("expired op", expires_at=expired_ts)

    assert decision.validity == "VALID"
    expired_decision = runtime.decision_engine.expire(decision.id)
    assert expired_decision.validity == "EXPIRED"
