"""审批绑定：把"人批准了什么"变成可校验的持久事实。

四条约束（评审对原设计的修正，全部体现在 validates() 里）：

1. **绑定调用与参数 hash**：批准的是"某次调用 + 某组参数"，不是"某类操作"的模糊授权。
   执行前用实际参数重新计算 hash 并比对，堵住 TOCTOU（批准 A、执行 B）。
2. **改参即换调用**：参数被改写时，原调用闭合为 superseded，另起新 tool_call_id
   与新幂等键（否则下游按键去重会返回旧参数的结果，静默错误）。见 loop.approve()。
3. **nonce 不是一次性闩锁**：它是这次批准的审计标识；"单次性"由
   (tool_call_id, args_sha256) 绑定 + 调用闭合隐式保证。因此"批准后崩溃、恢复后重跑"
   不会因为 nonce 已消费而卡死——重复执行由 outbox/幂等层拦截。
4. **过期与拒绝是默认方向**：expires_at 必须显式给；auto-approve 绝不默认开。

scope 语义：
* ``once``：只授权绑定到的那一次调用。
* ``session``：同一工具 + 同一参数 hash 的其它调用可复用该批准（"记住这类批准"），
  不同参数一律拒绝。
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from .ids import new_id

APPROVAL_POLICY_VERSION = "policy-v1"

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

SCOPE_ONCE = "once"
SCOPE_SESSION = "session"


class ApprovalError(RuntimeError):
    pass


class ApprovalBinding(BaseModel):
    approval_id: str = Field(default_factory=lambda: new_id("apr"))
    tool_call_id: str
    tool: str
    requested_args_sha256: str
    approved_args_sha256: str
    decision: str = DECISION_APPROVED
    actor: str = "human:unknown"
    nonce: str = Field(default_factory=lambda: new_id("nonce"))
    issued_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0
    scope: str = SCOPE_ONCE
    policy_version: str = APPROVAL_POLICY_VERSION
    edited: bool = False
    requested_args: dict = Field(default_factory=dict)
    approved_args: dict = Field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def validates(
        self,
        *,
        tool_call_id: str,
        tool: str,
        args_sha256: str,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """返回 (是否放行, 原因)；原因用于审计与错误分类。"""
        if self.decision != DECISION_APPROVED:
            return False, "approval_rejected"
        if self.is_expired(now):
            return False, "approval_expired"
        if args_sha256 != self.approved_args_sha256:
            return False, "args_sha256_mismatch"
        if tool_call_id == self.tool_call_id:
            return True, "ok"
        if self.scope == SCOPE_SESSION and tool == self.tool:
            return True, "session_scoped_reuse"
        return False, "approval_not_for_this_call"
