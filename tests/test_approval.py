"""审批绑定语义：过期、拒绝、改参、TOCTOU、scope 复用。"""

from __future__ import annotations

import time

from harness.approval import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    SCOPE_ONCE,
    SCOPE_SESSION,
    ApprovalBinding,
)
from harness.tools import canonical_args_sha256

ARGS = {"service": "payment", "size": 64}
OTHER_ARGS = {"service": "payment", "size": 32}


def _binding(**overrides) -> ApprovalBinding:
    base = dict(
        tool_call_id="tc_1",
        tool="scale_pool",
        requested_args_sha256=canonical_args_sha256(ARGS),
        approved_args_sha256=canonical_args_sha256(ARGS),
        decision=DECISION_APPROVED,
        actor="human:oncall",
        expires_at=time.time() + 600,
        scope=SCOPE_ONCE,
    )
    base.update(overrides)
    return ApprovalBinding(**base)


def test_valid_for_bound_call() -> None:
    ok, reason = _binding().validates(
        tool_call_id="tc_1", tool="scale_pool", args_sha256=canonical_args_sha256(ARGS)
    )
    assert ok and reason == "ok"


def test_rejected_decision_never_validates() -> None:
    binding = _binding(decision=DECISION_REJECTED)
    ok, reason = binding.validates(
        tool_call_id="tc_1", tool="scale_pool", args_sha256=canonical_args_sha256(ARGS)
    )
    assert not ok and reason == "approval_rejected"


def test_expired_approval_never_validates() -> None:
    binding = _binding(expires_at=time.time() - 1)
    ok, reason = binding.validates(
        tool_call_id="tc_1", tool="scale_pool", args_sha256=canonical_args_sha256(ARGS)
    )
    assert not ok and reason == "approval_expired"


def test_args_hash_mismatch_is_rejected() -> None:
    ok, reason = _binding().validates(
        tool_call_id="tc_1", tool="scale_pool", args_sha256=canonical_args_sha256(OTHER_ARGS)
    )
    assert not ok and reason == "args_sha256_mismatch"


def test_once_scope_does_not_apply_to_another_call() -> None:
    ok, reason = _binding().validates(
        tool_call_id="tc_2", tool="scale_pool", args_sha256=canonical_args_sha256(ARGS)
    )
    assert not ok and reason == "approval_not_for_this_call"


def test_session_scope_reuses_for_same_tool_and_args() -> None:
    binding = _binding(scope=SCOPE_SESSION)
    ok, reason = binding.validates(
        tool_call_id="tc_2", tool="scale_pool", args_sha256=canonical_args_sha256(ARGS)
    )
    assert ok and reason == "session_scoped_reuse"


def test_session_scope_still_requires_same_tool() -> None:
    binding = _binding(scope=SCOPE_SESSION)
    ok, reason = binding.validates(
        tool_call_id="tc_2", tool="restart_service", args_sha256=canonical_args_sha256(ARGS)
    )
    assert not ok and reason == "approval_not_for_this_call"


def test_session_scope_still_requires_same_args() -> None:
    binding = _binding(scope=SCOPE_SESSION)
    ok, reason = binding.validates(
        tool_call_id="tc_2", tool="scale_pool", args_sha256=canonical_args_sha256(OTHER_ARGS)
    )
    assert not ok and reason == "args_sha256_mismatch"
