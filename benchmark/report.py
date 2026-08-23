"""Pure benchmark report metrics."""
from __future__ import annotations


def _candidate_failure_category(outcome: dict) -> str | None:
    """Classify the first observable failure in a candidate pipeline."""
    if outcome.get("tests_passed") is False:
        return "behavioral_failure"
    if outcome.get("returned_patch") is False:
        return "no_response" if outcome.get("response_received") is False else "no_patch"
    if outcome.get("patch_valid") is False:
        detail = str(outcome.get("failure_detail", "")).lower()
        if any(token in detail for token in ("does not apply", "patch failed",
                                             "hunk", "corrupt patch")):
            return "patch_not_applicable"
        return "invalid_patch"
    return None


def candidate_metrics(results: list[dict]) -> dict[str, object]:
    """Measure candidate quality independently from consensus outcomes."""
    candidates: list[dict] = []
    agreement = 0
    both_valid = 0
    candidate_a_valid = 0
    candidate_b_valid = 0
    candidate_a_patch_valid = 0
    candidate_b_patch_valid = 0
    consensus_success = 0
    failure_breakdown = {"A": {}, "B": {}}
    protocol_parity_tasks = 0
    protocol_parity = 0
    prompt_hash_equal = 0
    protocol_hash_equal = 0
    workspace_hash_equal = 0
    hash_comparison_tasks = 0
    for row in results:
        raw = row.get("candidates", row.get("candidate_outcomes"))
        if raw is None:
            raw = (row.get("consensus") or {}).get("candidate_outcomes", ())
        raw = raw or ()
        if isinstance(raw, dict):
            raw = list(raw.values())
        row_candidates = [item for item in raw if isinstance(item, dict)]
        candidates.extend(row_candidates)
        valid = [item for item in row_candidates if item.get("patch_valid") is True]
        behaviorally_valid = [
            item for item in row_candidates
            if item.get("patch_valid") is True
            and item.get("tests_passed") is True
        ]
        if len(behaviorally_valid) >= 2:
            both_valid += 1
            consensus = row.get("consensus") or {}
            agreed = consensus.get("agreed", row.get("agreement", False))
            agreement += bool(agreed)
        consensus = row.get("consensus") or {}
        consensus_success += bool(
            consensus.get("agreed")
            or row.get("result_code") == "CONSENSUS_SUCCEEDED"
        )
        for index, item in enumerate(row_candidates):
            label = str(item.get("candidate_id", "")).upper()
            if not label:
                label = "A" if index == 0 else "B" if index == 1 else ""
            patch_valid = item.get("patch_valid") is True
            behaviorally_valid = patch_valid and item.get("tests_passed") is True
            if label == "A":
                candidate_a_patch_valid += patch_valid
                candidate_a_valid += behaviorally_valid
            elif label == "B":
                candidate_b_patch_valid += patch_valid
                candidate_b_valid += behaviorally_valid
            category = _candidate_failure_category(item)
            if label in failure_breakdown and category:
                counts = failure_breakdown[label]
                counts[category] = int(counts.get(category, 0)) + 1
        labeled = {
            str(item.get("candidate_id", "")).upper(): item
            for item in row_candidates
        }
        if "A" in labeled and "B" in labeled:
            protocol_parity_tasks += 1
            prompt_equal = (labeled["A"].get("prompt_sha256") is not None
                            and labeled["B"].get("prompt_sha256") is not None
                            and labeled["A"].get("prompt_sha256")
                            == labeled["B"].get("prompt_sha256"))
            protocol_equal = (labeled["A"].get("protocol_sha256") is not None
                              and labeled["B"].get("protocol_sha256") is not None
                              and labeled["A"].get("protocol_sha256")
                              == labeled["B"].get("protocol_sha256"))
            workspace_equal = (labeled["A"].get("workspace_sha256") is not None
                              and labeled["B"].get("workspace_sha256") is not None
                              and labeled["A"].get("workspace_sha256")
                              == labeled["B"].get("workspace_sha256"))
            if prompt_equal:
                prompt_hash_equal += 1
            if protocol_equal:
                protocol_hash_equal += 1
            if workspace_equal:
                workspace_hash_equal += 1
            if prompt_equal and protocol_equal and workspace_equal:
                protocol_parity += 1
            if prompt_equal or protocol_equal or workspace_equal:
                hash_comparison_tasks += 1

    attempts = sum(int(item.get("attempts", 1) or 1) for item in candidates)
    retries = sum(int(item.get("retries", 0) or 0) for item in candidates)
    returned = sum(int(item.get("patch_returns", bool(
        item.get("returned_patch"))) or 0) for item in candidates)
    valid = sum(item.get("patch_valid") is True for item in candidates)
    tests_passed = sum(item.get("tests_passed") is True for item in candidates)
    static_evidence = sum(
        isinstance(item.get("patch_valid"), bool) for item in candidates)
    behavioral_evidence = sum(
        item.get("tests_passed") is not None for item in candidates)
    complete_evidence = sum(
        isinstance(item.get("patch_valid"), bool)
        and item.get("tests_passed") is not None
        for item in candidates)
    reliable_candidates = sum(
        item.get("patch_valid") is True
        and item.get("tests_passed") is True
        for item in candidates)
    return {
        "candidate_attempts": attempts,
        "candidate_retries": retries,
        "candidate_returned_patch": returned,
        "valid_patch": valid,
        "tests_passed": tests_passed,
        "static_evidence": static_evidence,
        "behavioral_evidence": behavioral_evidence,
        "complete_evidence": complete_evidence,
        "reliable_candidates": reliable_candidates,
        "both_valid": both_valid,
        "agreement": agreement,
        "candidate_a_patch_valid": candidate_a_patch_valid,
        "candidate_b_patch_valid": candidate_b_patch_valid,
        "candidate_a_valid": candidate_a_valid,
        "candidate_b_valid": candidate_b_valid,
        "consensus_success": consensus_success,
        "candidate_failure_breakdown": failure_breakdown,
        "protocol_parity_tasks": protocol_parity_tasks,
        "protocol_parity": protocol_parity,
        "protocol_parity_rate": (
            protocol_parity / protocol_parity_tasks
            if protocol_parity_tasks else 0.0),
        "diagnostic_table": {
            "no_response": {label: counts.get("no_response", 0)
                            for label, counts in failure_breakdown.items()},
            "no_patch": {label: counts.get("no_patch", 0)
                         for label, counts in failure_breakdown.items()},
            "invalid_patch": {label: counts.get("invalid_patch", 0)
                              for label, counts in failure_breakdown.items()},
            "patch_not_applicable": {
                label: counts.get("patch_not_applicable", 0)
                for label, counts in failure_breakdown.items()},
            "behavior_fail": {label: counts.get("behavioral_failure", 0)
                              for label, counts in failure_breakdown.items()},
            "valid": {"A": candidate_a_valid, "B": candidate_b_valid},
        },
        "hash_parity": {
            "tasks": protocol_parity_tasks,
            "prompt_equal": prompt_hash_equal,
            "protocol_equal": protocol_hash_equal,
            "workspace_equal": workspace_hash_equal,
            "all_equal": protocol_parity,
            "known": hash_comparison_tasks,
        },
        "candidate_completion_rate": returned / attempts if attempts else 0.0,
        "candidate_patch_validity": valid / returned if returned else 0.0,
        "candidate_behavioral_success": tests_passed / valid if valid else 0.0,
        "swebench_evidence_rate": (complete_evidence / len(candidates)
                                    if candidates else 0.0),
        "candidate_behavioral_evidence_rate": (
            behavioral_evidence / valid if valid else 0.0),
        "candidate_reliability_rate": (
            reliable_candidates / attempts if attempts else 0.0),
        "candidate_agreement": agreement / both_valid if both_valid else 0.0,
        "candidate_a_success_rate": candidate_a_valid / len(results) if results else 0.0,
        "candidate_b_success_rate": candidate_b_valid / len(results) if results else 0.0,
        "candidate_a_patch_validity_rate": (
            candidate_a_patch_valid / len(results) if results else 0.0),
        "candidate_b_patch_validity_rate": (
            candidate_b_patch_valid / len(results) if results else 0.0),
        "both_valid_rate": both_valid / len(results) if results else 0.0,
        "consensus_success_rate": (
            consensus_success / len(results) if results else 0.0),
    }


def success_metrics(results: list[dict]) -> dict[str, float | int]:
    """Calculate agent and end-to-end rates without changing execution state."""
    total = len(results)
    passed = sum(bool(row.get("passed")) for row in results)
    environment_valid = sum(
        row.get("failure_class") != "environment" for row in results)
    metrics = {
        "tasks": total,
        "environment_valid": environment_valid,
        "environment_valid_rate": environment_valid / total if total else 0.0,
        "agent_solved": passed,
        "agent_success_rate": passed / environment_valid if environment_valid else 0.0,
        "end_to_end_success_rate": passed / total if total else 0.0,
    }
    if any(row.get("candidates") or row.get("candidate_outcomes") or
           (row.get("consensus") or {}).get("candidate_outcomes")
           for row in results):
        metrics.update(candidate_metrics(results))
    return metrics
