"""Classify Harbor/ACS trial outcomes without treating missing rewards as zero.

This module is deliberately provider-agnostic and does not read credentials or
contact a cluster.  A missing verifier result is an infrastructure/diagnostic
state until the verifier evidence proves otherwise.
"""

from __future__ import annotations

from typing import Any


def classify_trial(result: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, non-scoring diagnostic for one Harbor result."""
    verifier = result.get("verifier_result")
    exception = result.get("exception_info") or {}
    exception_type = exception.get("exception_type")
    exception_message = str(exception.get("exception_message") or "").lower()

    if verifier is None:
        if exception_type == "ValueError" and "network policy" in exception_message:
            kind = "platform_dynamic_network_policy_mismatch"
        elif exception_type:
            kind = "trial_exception_before_verifier"
        else:
            kind = "verifier_result_missing"
        return {"status": "infrastructure_error", "diagnostic": kind,
                "scored": False, "reward": None}

    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    if rewards is None:
        return {"status": "infrastructure_error",
                "diagnostic": "verifier_reward_missing", "scored": False,
                "reward": None}
    if not isinstance(rewards, dict):
        return {"status": "infrastructure_error",
                "diagnostic": "verifier_reward_parse_error", "scored": False,
                "reward": None}
    return {"status": "scored", "diagnostic": "ok", "scored": True,
            "reward": rewards}


def classify_trials(results: list[dict[str, Any]]) -> dict[str, Any]:
    items = [classify_trial(item) for item in results]
    return {"total": len(items),
            "scored": sum(item["scored"] for item in items),
            "infrastructure_errors": sum(not item["scored"] for item in items),
            "diagnostics": items}
