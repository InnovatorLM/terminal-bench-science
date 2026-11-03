"""Classify Harbor/ACS trial outcomes without treating missing rewards as zero.

This module is deliberately provider-agnostic and does not read credentials or
contact a cluster.  A missing verifier result is an infrastructure/diagnostic
state until the verifier evidence proves otherwise.
"""

from __future__ import annotations

from typing import Any
import math


def classify_trial(result: dict[str, Any], *, finite_rewards: bool = False) -> dict[str, Any]:
    """Return a stable, non-scoring diagnostic for one Harbor result."""
    if not isinstance(result, dict):
        return {'status':'infrastructure_error','diagnostic':'trial_result_parse_error','scored':False,'reward':None}
    verifier = result.get("verifier_result")
    exception = result.get("exception_info") or {}
    if not isinstance(exception, dict):
        return {'status':'infrastructure_error','diagnostic':'trial_exception_parse_error','scored':False,'reward':None}
    exception_type = exception.get("exception_type")
    exception_message = str(exception.get("exception_message") or "").lower()

    if result.get('cleanup_exception_info'):
        return {'status':'infrastructure_error','diagnostic':'cleanup_failed','scored':False,'reward':None}
    if exception_type in ('AgentTimeoutError','AgentExecutionTimeoutError'):
        return {'status':'incomplete','diagnostic':'agent_timeout_requires_cause_review','scored':False,'reward':None}
    if exception_type and verifier is not None:
        # A stale/partial reward cannot hide a failed agent or verifier. A
        # confirmed difficulty timeout is adjudicated separately, never guessed
        # from the mere presence of an exception or a zero reward.
        return {'status':'infrastructure_error','diagnostic':'trial_exception_with_reward','scored':False,'reward':None}

    if verifier is None:
        if exception_type == "RewardFileNotFoundError":
            # Missing verifier output needs cause review; it does not by
            # itself establish task, platform, or network responsibility.
            kind = "verifier_reward_missing"
        elif exception_type == "DownloadVerifierDirError":
            # Old adapters raised missing reward inside download_dir, so
            # Harbor wrapped it as a transfer error. Preserve that distinction
            # without changing the historical result or fabricating a score.
            exception_traceback = str(exception.get("exception_traceback") or "").lower()
            if "missing verifier reward" in exception_message or "missing verifier reward" in exception_traceback:
                kind = "legacy_verifier_reward_missing"
            else:
                kind = "verifier_evidence_transfer_failed"
        elif exception_type == "ValueError" and "network policy" in exception_message:
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
    if finite_rewards:
        valid=bool(rewards) and all(type(v) in (int,float) and math.isfinite(v) for v in rewards.values())
        return {'status':'scored' if valid else 'infrastructure_error',
                'diagnostic':'ok' if valid else 'verifier_reward_invalid',
                'scored':valid,'reward':rewards if valid else None}
    reward=rewards.get('reward')
    if type(reward) not in (int,float) or not math.isfinite(reward) or reward not in (0,1):
        return {'status':'infrastructure_error','diagnostic':'verifier_reward_invalid','scored':False,'reward':None}
    return {"status": "scored", "diagnostic": "ok", "scored": True,
            "reward": rewards}


def classify_trials(results: list[dict[str, Any]]) -> dict[str, Any]:
    items = [classify_trial(item) for item in results]
    return {"total": len(items),
            "scored": sum(item["scored"] for item in items),
            "infrastructure_errors": sum(not item["scored"] for item in items),
            "diagnostics": items}
