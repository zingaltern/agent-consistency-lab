"""最小 agent loop：被测对象。

每一轮 = 一个 super-step：构建视图 → 模型响应 → 执行工具 → 提交边界 checkpoint。
命名崩溃窗口嵌在工具执行路径上（见 harness/chaos.py）。

工具执行的完整顺序（每一步都是"日志权威"的具体体现）：

1. 日志里已有结论 → 重放，不重跑
2. 补记 ``tool_call`` 事件（幂等）
3. 查既有意图行（outbox / 去重表）：executed → 重放；pending → 探针对账；
   unknown → 不自动重试
4. 审批门：未获批的需审批调用 → 发 interrupt 并停止（WAITING_HUMAN）
5. 审批校验：拒绝 / 过期 / 参数 hash 不一致（TOCTOU）→ 拒绝执行并记录原因
6. outbox 预写 intent(pending)  ← 这一步之后崩溃才可能留下可对账的痕迹
7. 执行副作用 → 记录 result 事件 → 闭合意图行 → 边界 checkpoint

恢复语义（与 docs/semantics.md 一致）：

* 权威是事件日志；日志里已有结论的调用一律重放。
* 未闭合调用按 at-least-once 重新执行；但**有 pending 意图行**时先探针，绝不盲目重跑。
* checkpoint 只在 super-step 边界提交；writes 先于 checkpoint 落盘，孤儿 writes 被恢复忽略。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .approval import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    SCOPE_ONCE,
    SCOPE_SESSION,
    ApprovalBinding,
    ApprovalError,
)
from .budget import BudgetExceeded
from .chaos import Chaos
from .context import ViewBuilder
from .events import ArtifactEventType, Event, NewEvent, Source, TreeEventType
from .ids import new_id
from .llm import ContextOverflow, LLMClient, ModelResponse
from .model import Model, ModelTurn
from .prompts import DEFAULT_OPS_SYSTEM_PROMPT
from .state import DerivedState, RunStatus, reduce_events
from .store.checkpoints import SqliteCheckpointSaver, Write
from .store.sqlite_store import SqliteStore
from .store.tool_calls import ToolCallRecord, ToolCallStore
from .tools import (
    Effect,
    ProbeOutcome,
    ProbeResult,
    Tool,
    ToolCallRequest,
    ToolRegistry,
    canonical_args_sha256,
    idempotency_key,
)

APPROVAL_TTL_SECONDS = 3600.0

# 刻意写成接近真实的运维处置规范长度：系统提示是"稳定前缀"的主体，
# 它必须足够大才会进入供应商的最小可缓存长度（本模型设 1024 token）。
DEFAULT_SYSTEM_PROMPT = DEFAULT_OPS_SYSTEM_PROMPT


class RunOutcome(BaseModel):
    status: str
    step: int
    executed: int
    replayed: int
    checkpoints: int
    reconciled: int = 0
    unknown: int = 0
    rejected: int = 0
    probes: int = 0
    compactions: int = 0
    overflows: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    view_violations: int = 0
    tool_failures: int = 0
    checkpoint_mismatches: int = 0


class InterruptRequest(BaseModel):
    interrupt_id: str
    interrupt_index: int
    tool_call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    args_sha256: str
    reason: str = ""


class LoopError(RuntimeError):
    """调用方误用（例如在非空分支上 start、在终态上 resume 且期望有副作用）。"""


class InterruptSignal(Exception):
    """控制流信号：不是错误，但绝不能被业务代码吞掉。

    对齐 LangGraph 的 ``interrupt()`` 语义——恢复时当前步骤从头重跑，
    因此 interrupt 之前的副作用必须幂等。
    """

    def __init__(self, request: InterruptRequest) -> None:
        super().__init__(f"interrupt: {request.reason}")
        self.request = request


@dataclass
class _Ctx:
    run_id: str
    thread_id: str
    branch_id: str


@dataclass
class _Counters:
    executed: int = 0
    replayed: int = 0
    checkpoints: int = 0
    reconciled: int = 0
    unknown: int = 0
    rejected: int = 0
    probes: int = 0
    compactions: int = 0
    overflows: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    view_violations: int = 0
    tool_failures: int = 0
    checkpoint_mismatches: int = 0

    def to_outcome(self, status: str, step: int) -> RunOutcome:
        return RunOutcome(
            status=status,
            step=step,
            executed=self.executed,
            replayed=self.replayed,
            checkpoints=self.checkpoints,
            reconciled=self.reconciled,
            unknown=self.unknown,
            rejected=self.rejected,
            probes=self.probes,
            compactions=self.compactions,
            overflows=self.overflows,
            input_tokens=self.input_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cost_usd=round(self.cost_usd, 8),
            view_violations=self.view_violations,
            tool_failures=self.tool_failures,
            checkpoint_mismatches=self.checkpoint_mismatches,
        )


class Loop:
    def __init__(
        self,
        store: SqliteStore,
        saver: SqliteCheckpointSaver,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        builder: ViewBuilder | None = None,
        compactor: object | None = None,
        budget: object | None = None,
        dedup: bool = True,
        outbox: bool = True,
        chaos: Chaos | None = None,
        max_steps: int = 12,
        checkpoint_ns: str = "",
        tamper: Callable[[ToolCallRequest], ToolCallRequest] | None = None,
    ) -> None:
        self._store = store
        self._saver = saver
        self._tool_calls = ToolCallStore(store)
        self._llm = llm
        self._registry = registry
        self._builder = builder or ViewBuilder(
            system_prompt=DEFAULT_SYSTEM_PROMPT, registry=registry
        )
        self._compactor = compactor
        self._budget = budget
        self._dedup = dedup
        self._outbox = outbox
        self._chaos = chaos or Chaos.disabled()
        self._max_steps = max_steps
        self._ns = checkpoint_ns
        self._tamper = tamper
        self._last_view_violations = 0

    # ------------------------------------------------------------------ public

    def start(
        self,
        *,
        run_id: str,
        thread_id: str,
        branch_id: str,
        task: str,
    ) -> RunOutcome:
        ctx = _Ctx(run_id, thread_id, branch_id)
        if self._log(ctx):
            raise LoopError(
                f"branch {branch_id} already has events; use resume() or fork a new branch"
            )
        self._store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.USER_MESSAGE,
                source=Source.USER,
                payload={"text": task},
            )
        )
        return self._drive(ctx, _Counters())

    def resume(
        self,
        *,
        run_id: str,
        thread_id: str,
        branch_id: str,
    ) -> RunOutcome:
        ctx = _Ctx(run_id, thread_id, branch_id)
        counters = _Counters()
        state = self._state(ctx)
        if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            # 终态是吸收态：不允许通过 resume 追加树节点把 run "复活"
            # （否则 fatal 之后还能执行工具、甚至再开审批门，日志会累积 INV-006）。
            counters.checkpoint_mismatches = self._checkpoint_cross_check(ctx)
            return counters.to_outcome(state.status.value, state.step)
        counters.checkpoint_mismatches = self._checkpoint_cross_check(ctx)
        if state.pending_interrupt_id is not None:
            return counters.to_outcome(state.status.value, state.step)
        try:
            for call_id in sorted(state.open_tool_calls):
                request = self._tool_request_from_log(ctx, call_id)
                if request is not None:
                    self._execute_tool(ctx, request, self._registry, counters)
        except InterruptSignal:
            self._commit(ctx, f"model:{state.step}", [], step=state.step, counters=counters)
            final = self._state(ctx)
            return counters.to_outcome(final.status.value, final.step)
        self._chaos.hit("after_resume")
        return self._drive(ctx, counters)

    def approve(
        self,
        *,
        run_id: str,
        thread_id: str,
        branch_id: str,
        decision: str = DECISION_APPROVED,
        actor: str = "human:oncall",
        ttl_seconds: float = APPROVAL_TTL_SECONDS,
        approved_args: dict[str, Any] | None = None,
        scope: str = SCOPE_ONCE,
    ) -> ApprovalBinding:
        """人工审批：写入 approval 事实 + resume 事件（关闭 interrupt）。

        改参时：原调用闭合为 ``superseded``，另起新 tool_call_id（因此是新幂等键），
        批准绑定到新调用与新参数——旧批准无法复用到新参数上。
        """
        ctx = _Ctx(run_id, thread_id, branch_id)
        state = self._state(ctx)
        if state.pending_interrupt_id is None:
            raise ApprovalError("no open interrupt to approve")
        request = self._interrupt_request(ctx, state.pending_interrupt_id)

        edited = (
            approved_args is not None
            and canonical_args_sha256(approved_args) != request.args_sha256
        )
        target_call_id = request.tool_call_id
        if decision == DECISION_REJECTED:
            if edited:
                raise ApprovalError("rejection cannot carry edited args")
            self._append_tool_result(
                ctx, request.tool_call_id, "rejected", None, "approval_rejected"
            )
        elif edited:
            target_call_id = f"{request.tool_call_id}__edit1"
            self._append_tool_result(
                ctx, request.tool_call_id, "superseded", None, "superseded_by_edit"
            )
            self._append_tool_call_event(
                ctx,
                ToolCallRequest(
                    tool_call_id=target_call_id,
                    tool=request.tool,
                    args=dict(approved_args or {}),
                ),
                effect=None,
                idempotency_key_value=None,
            )

        effective_args = dict(approved_args) if edited else request.args
        binding = ApprovalBinding(
            tool_call_id=target_call_id,
            tool=request.tool,
            requested_args_sha256=request.args_sha256,
            approved_args_sha256=canonical_args_sha256(effective_args),
            decision=decision,
            actor=actor,
            issued_at=time.time(),
            expires_at=time.time() + ttl_seconds,
            scope=scope,
            edited=edited,
            requested_args=request.args,
            approved_args=effective_args,
        )
        self._store.append(
            NewEvent.artifact(
                run_id=run_id,
                branch_id=branch_id,
                type=ArtifactEventType.APPROVAL,
                payload=binding.model_dump(),
            )
        )
        self._store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.RESUME,
                source=Source.USER,
                payload={
                    "interrupt_id": request.interrupt_id,
                    "interrupt_index": request.interrupt_index,
                    "approval_id": binding.approval_id,
                    "decision": decision,
                    "tool_call_id": target_call_id,
                },
            )
        )
        return binding

    # ------------------------------------------------------------------- drive

    def _drive(self, ctx: _Ctx, counters: _Counters) -> RunOutcome:
        for _ in range(self._max_steps):
            state = self._state(ctx)
            if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                break
            response = self._call_model(ctx, state.step, counters)
            if response is None:
                break
            turn = response.turn
            self._append_agent_message(ctx, turn, response=response, final=not turn.tool_calls)
            task_id = f"model:{state.step}"
            writes = [
                Write(
                    task_id=task_id,
                    idx=0,
                    channel="messages",
                    payload={
                        "text": turn.text,
                        "tool_calls": [c.tool_call_id for c in turn.tool_calls],
                    },
                )
            ]
            interrupted = False
            try:
                for request in turn.tool_calls:
                    self._execute_tool(ctx, request, self._registry, counters)
                    writes.append(
                        Write(
                            task_id=f"tool:{request.tool_call_id}",
                            idx=0,
                            channel="tool_output",
                            payload={"tool": request.tool, "tool_call_id": request.tool_call_id},
                        )
                    )
            except InterruptSignal:
                interrupted = True
            self._commit(ctx, task_id, writes, step=state.step, counters=counters)
            if interrupted or not turn.tool_calls:
                break
        final = self._state(ctx)
        counters.view_violations = self._last_view_violations
        # runs.status 是给外部查询/看板用的运行摘要（权威仍是事件日志的折叠结果）
        self._store.set_run_status(ctx.run_id, final.status.value)
        return counters.to_outcome(final.status.value, final.step)

    # ------------------------------------------------------------------- model

    def _call_model(self, ctx: _Ctx, step: int, counters: _Counters) -> ModelResponse | None:
        """构建视图、（必要时）压缩、调模型；溢出只重试一次，之后判失败。"""
        view = self._build_view(ctx, counters)
        if self._compactor is not None and self._compactor.should_compact(
            view=view, window=self._llm.window
        ):
            record = self._compactor.compact(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                events=self._log(ctx),
                reason="threshold",
            )
            if record is not None:
                counters.compactions += 1
                view = self._build_view(ctx, counters)
        for attempt in (1, 2):
            try:
                response = self._llm.complete(step=step, view=view)
            except ContextOverflow as exc:
                counters.overflows += 1
                if self._compactor is None or attempt == 2:
                    self._append_error_artifact(ctx, "context_overflow", str(exc), fatal=True)
                    return None
                record = self._compactor.compact(
                    run_id=ctx.run_id,
                    branch_id=ctx.branch_id,
                    events=self._log(ctx),
                    reason="overflow",
                )
                if record is None:
                    self._append_error_artifact(ctx, "context_overflow", "无可压缩内容", fatal=True)
                    return None
                counters.compactions += 1
                view = self._build_view(ctx, counters)
            except BudgetExceeded as exc:
                self._append_error_artifact(ctx, "budget_exceeded", str(exc), fatal=True)
                return None
            else:
                counters.input_tokens += response.usage.input_tokens
                counters.cache_read_tokens += response.usage.cache_read_tokens
                counters.cache_write_tokens += response.usage.cache_write_tokens
                counters.cost_usd += response.cost_usd
                return response
        return None

    def _dynamic_snapshot(self, ctx: _Ctx) -> dict[str, Any]:
        """每步变化的运行时信息——只能进 tail；进了 prefix 就是每步击穿缓存。"""
        state = self._state(ctx)
        snapshot: dict[str, Any] = {"step": state.step, "status": state.status.value}
        if self._budget is not None:
            budget = self._budget.snapshot()
            snapshot["spent_usd"] = budget.spent_usd
            snapshot["remaining_usd"] = round(budget.remaining_usd, 4)
        return snapshot

    def _build_view(self, ctx: _Ctx, counters: _Counters):
        view = self._builder.build(events=self._log(ctx), dynamic=self._dynamic_snapshot(ctx))
        self._last_view_violations = len(view.violations)
        return view

    # --------------------------------------------------------------- tool path

    def _execute_tool(
        self,
        ctx: _Ctx,
        request: ToolCallRequest,
        registry: ToolRegistry,
        counters: _Counters,
    ) -> None:
        key = idempotency_key(ctx.run_id, ctx.branch_id, request.tool_call_id)
        args_sha256 = canonical_args_sha256(request.args)

        # 1) 日志权威：已有结论的调用只重放，不再产生副作用
        if self._has_result(ctx, request.tool_call_id):
            counters.replayed += 1
            return

        # 2) 工具解析：模型幻觉出的工具名也是一次失败调用，必须被记录并闭合，
        #    而不是让 KeyError 穿透 loop（否则 run 永远停在 RUNNING、调用悬挂）。
        try:
            tool = registry.get(request.tool)
        except KeyError:
            self._append_tool_call_event(ctx, request, None, None)
            counters.tool_failures += 1
            self._append_error_artifact(ctx, "unknown_tool", f"{request.tool!r} is not registered")
            self._append_tool_result(ctx, request.tool_call_id, "failed", None, "unknown_tool")
            return

        # 3) 补记决策（幂等）
        self._ensure_tool_call_event(ctx, request, tool.effect, key, args_sha256)

        # 3) 既有意图行处置
        existing = self._tool_calls.lookup(key) if (self._dedup or self._outbox) else None
        if existing is not None and existing.args_sha256 != args_sha256:
            # 同键不同参：旁路校验字段在这里起作用——绝不允许把另一次参数的执行结果
            # 当成本次调用的重放（那是静默的错误答案，比报错危险得多）。
            counters.rejected += 1
            self._append_error_artifact(
                ctx,
                "args_sha256_mismatch",
                f"key {key} recorded args {existing.args_sha256[:12]} != actual {args_sha256[:12]}",
            )
            self._append_tool_result(
                ctx, request.tool_call_id, "rejected", None, "args_sha256_mismatch"
            )
            return
        if existing is not None:
            if existing.status == "executed":
                counters.replayed += 1
                self._append_tool_result(ctx, request.tool_call_id, "executed", existing.result)
                return
            if existing.status == "pending":
                if not self._resolve_pending(ctx, existing, tool, request, counters):
                    return
            elif existing.status == "unknown":
                counters.unknown += 1
                self._append_tool_result(
                    ctx,
                    request.tool_call_id,
                    "unknown",
                    None,
                    "unknown_effect_unresolved",
                )
                return

        # 4) 审批门
        binding = self._find_approval(ctx, request.tool_call_id, tool.name)
        if binding is not None:
            ok, reason = binding.validates(
                tool_call_id=request.tool_call_id, tool=tool.name, args_sha256=args_sha256
            )
            if not ok:
                counters.rejected += 1
                self._append_error_artifact(ctx, reason, f"审批校验未通过: {reason}")
                self._append_tool_result(ctx, request.tool_call_id, "rejected", None, reason)
                return
            self._chaos.hit("post_approval_pre_exec")
        elif tool.requires_approval:
            self._raise_interrupt(ctx, request, args_sha256)
            return

        # 5) TOCTOU：执行前用实际参数复核批准的 hash
        if self._tamper is not None:
            request = self._tamper(request)
        actual_sha = canonical_args_sha256(request.args)
        if binding is not None and actual_sha != binding.approved_args_sha256:
            counters.rejected += 1
            self._append_error_artifact(
                ctx, "args_sha256_mismatch", "执行前参数与批准参数不一致（TOCTOU）"
            )
            self._append_tool_result(
                ctx, request.tool_call_id, "rejected", None, "args_sha256_mismatch"
            )
            return

        # 5b) 参数级安全域（R-B2）：闸门在**执行 handler 前的最后一刻**，
        # 输入是实际 request.args（不是审批记录里的 hash）——否则会出现
        # "审批时看不懂结构化参数、批准了被禁止值"的缝隙。
        # 位置刻意放在 outbox 预写之前：被拒的调用不该留下意图行。
        if tool.arg_policy is not None:
            allowed, reason = tool.arg_policy.evaluate(request.args)
            if not allowed:
                counters.rejected += 1
                self._append_error_artifact(
                    ctx,
                    "arg_policy_violation",
                    f"{request.tool} 参数越出工具自述的安全域（{reason}；"
                    f"策略 {tool.arg_policy.describe()}）",
                )
                self._append_tool_result(
                    ctx, request.tool_call_id, "rejected", None, "arg_policy_violation"
                )
                return

        # 6) outbox 预写意图（仅非幂等写、且尚无意图行）
        outbox_path = self._outbox and tool.effect is Effect.WRITE_NONIDEMPOTENT
        if outbox_path and existing is None:
            self._tool_calls.begin(
                tool_call_id=request.tool_call_id,
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                tool=request.tool,
                args=request.args,
                args_sha256=actual_sha,
                idempotency_key=key,
                effect=tool.effect.value,
            )

        # 7) 执行与记录：权威记录（事件）先落，意图行后闭合
        self._chaos.hit("pre_tool_exec")
        try:
            result = tool.fn(request.args, key)
        except Exception as exc:
            self._handle_tool_failure(ctx, request, tool, exc, counters, outbox_path=outbox_path)
            return
        self._chaos.hit("post_tool_effect_pre_record")
        self._append_tool_result(ctx, request.tool_call_id, "executed", result)
        if outbox_path:
            self._tool_calls.complete(key, result)
        else:
            self._tool_calls.record(
                tool_call_id=request.tool_call_id,
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                tool=request.tool,
                args=request.args,
                args_sha256=actual_sha,
                idempotency_key=key,
                effect=tool.effect.value,
                status="executed",
                result=result,
            )
        counters.executed += 1
        self._chaos.hit("post_record_pre_commit")

    def _handle_tool_failure(
        self,
        ctx: _Ctx,
        request: ToolCallRequest,
        tool: Tool,
        exc: Exception,
        counters: _Counters,
        *,
        outbox_path: bool,
    ) -> None:
        """工具抛异常时的分类处置。

        关键判断：异常**不代表效果未发生**（可能写完才抛）。因此
        非幂等写（有 outbox 意图行）一律按 ``unknown`` 处置、等探针对账；
        只读/幂等工具按 ``failed`` 处置。两种情况都记录 error artifact，
        并且绝不把异常抛给调用方——否则 run 会永久停在 RUNNING、调用悬挂、
        每次 resume 重放同一副作用（审计实测的重试风暴）。
        """
        error_class = f"tool_error:{type(exc).__name__}"
        key = idempotency_key(ctx.run_id, ctx.branch_id, request.tool_call_id)
        if outbox_path:
            self._tool_calls.mark_unknown(key, error_class)
            status = "unknown"
        else:
            self._tool_calls.record(
                tool_call_id=request.tool_call_id,
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                tool=request.tool,
                args=request.args,
                args_sha256=canonical_args_sha256(request.args),
                idempotency_key=key,
                effect=tool.effect.value,
                status="failed",
                result=None,
                error_class=error_class,
            )
            status = "failed"
        counters.tool_failures += 1
        self._append_error_artifact(ctx, error_class, f"{request.tool}: {exc}")
        self._append_tool_result(ctx, request.tool_call_id, status, None, error_class)

    def _resolve_pending(
        self,
        ctx: _Ctx,
        existing: ToolCallRecord,
        tool: Tool,
        request: ToolCallRequest,
        counters: _Counters,
    ) -> bool:
        """处置 pending 意图。返回 True 表示"确认未生效、可安全执行"。"""
        if tool.probe is None:
            self._tool_calls.mark_unknown(existing.idempotency_key, "no_probe_available")
            counters.unknown += 1
            self._append_error_artifact(
                ctx, "unknown_effect", "pending 意图无法对账：下游不支持按键读回"
            )
            self._append_tool_result(ctx, request.tool_call_id, "unknown", None, "unknown_effect")
            return False
        try:
            probe = tool.probe(request.args, existing.idempotency_key)
        except Exception as exc:
            probe = ProbeResult(
                outcome=ProbeOutcome.UNKNOWN,
                detail={"probe_error": f"{type(exc).__name__}: {exc}"},
            )
        counters.probes += 1
        if probe.outcome is ProbeOutcome.APPLIED:
            result = {
                "operation": request.tool,
                "reconstructed_from_probe": True,
                **probe.detail,
            }
            self._tool_calls.complete(existing.idempotency_key, result)
            self._append_tool_result(ctx, request.tool_call_id, "executed", result)
            counters.reconciled += 1
            return False
        if probe.outcome is ProbeOutcome.NOT_APPLIED:
            return True
        self._tool_calls.mark_unknown(existing.idempotency_key, "probe_inconclusive")
        counters.unknown += 1
        self._append_error_artifact(ctx, "unknown_effect", "探针结论不确定，转人工对账")
        self._append_tool_result(ctx, request.tool_call_id, "unknown", None, "unknown_effect")
        return False

    # ----------------------------------------------------------------- approval

    def _raise_interrupt(self, ctx: _Ctx, request: ToolCallRequest, args_sha256: str) -> None:
        state = self._state(ctx)
        if state.pending_interrupt_id is not None:
            raise InterruptSignal(self._interrupt_request(ctx, state.pending_interrupt_id))
        payload = InterruptRequest(
            interrupt_id=new_id("int"),
            interrupt_index=self._interrupt_count(ctx),
            tool_call_id=request.tool_call_id,
            tool=request.tool,
            args=request.args,
            args_sha256=args_sha256,
            reason=f"{request.tool} 是高危写操作，需要人工审批",
        )
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.INTERRUPT,
                source=Source.AGENT,
                payload=payload.model_dump(),
            )
        )
        raise InterruptSignal(payload)

    def _interrupt_request(self, ctx: _Ctx, interrupt_id: str) -> InterruptRequest:
        for event in self._log(ctx):
            if (
                event.type == TreeEventType.INTERRUPT.value
                and event.payload.get("interrupt_id") == interrupt_id
            ):
                return InterruptRequest.model_validate(event.payload)
        raise ApprovalError(f"interrupt {interrupt_id!r} not found in log")

    def _interrupt_count(self, ctx: _Ctx) -> int:
        return sum(1 for e in self._log(ctx) if e.type == TreeEventType.INTERRUPT.value)

    def _find_approval(self, ctx: _Ctx, tool_call_id: str, tool: str) -> ApprovalBinding | None:
        """最近一条可用于该调用的批准（once 绑定调用；session 绑定工具+参数）。"""
        candidates: list[tuple[tuple[int, int, float], ApprovalBinding]] = []
        for event in self._log(ctx):
            if event.type != ArtifactEventType.APPROVAL.value:
                continue
            binding = ApprovalBinding.model_validate(event.payload)
            exact = binding.tool_call_id == tool_call_id
            reusable = binding.scope == SCOPE_SESSION and binding.tool == tool
            if not (exact or reusable):
                continue
            approved = 1 if binding.decision == DECISION_APPROVED else 0
            # 排序优先级：精确绑定 > 可复用；批准 > 拒绝；同档取最新。
            # 否则一条更晚的 session 拒绝会否决更早的、精确绑定本调用的批准。
            candidates.append(((int(exact), approved, binding.issued_at), binding))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    # ------------------------------------------------------------------ helpers

    def _checkpoint_cross_check(self, ctx: _Ctx) -> int:
        """把 checkpoint 当作**交叉校验**而不是权威：权威永远是事件日志。

        唯一能发现的真实不一致是"checkpoint 指向了一个不存在的事件"
        （例如数据库被手工改过、或 checkpoint 与日志被分开恢复）。
        分叉场景下 checkpoint 属于父分支，跳过检查（thread 级 checkpoint 不跨分支复用）。
        """
        latest = self._saver.get_latest(ctx.thread_id, self._ns)
        if latest is None or latest.checkpoint.branch_id != ctx.branch_id:
            return 0
        last_event_id = latest.checkpoint.channel_values.get("last_event_id")
        if not last_event_id:
            return 0
        if any(event.event_id == last_event_id for event in self._log(ctx)):
            return 0
        self._append_error_artifact(
            ctx,
            "checkpoint_log_mismatch",
            f"checkpoint {latest.checkpoint.id} references missing event {last_event_id}",
        )
        return 1

    def _commit(
        self,
        ctx: _Ctx,
        task_id: str,
        writes: list[Write],
        *,
        step: int,
        counters: _Counters,
    ) -> None:
        """writes 先落盘、checkpoint 后提交：崩溃可能留下孤儿 writes（被恢复忽略）。"""
        checkpoint_id = new_id("ckpt")
        if writes:
            self._saver.put_writes(
                thread_id=ctx.thread_id,
                checkpoint_id=checkpoint_id,
                writes=writes,
                checkpoint_ns=self._ns,
            )
        latest = self._saver.get_latest(ctx.thread_id, self._ns)
        state = self._state(ctx)
        self._saver.put(
            thread_id=ctx.thread_id,
            branch_id=ctx.branch_id,
            channel_values={
                "step": state.step,
                "status": state.status.value,
                "last_event_id": state.last_event_id,
            },
            source="loop",
            parent_checkpoint_id=latest.checkpoint.id if latest else None,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=self._ns,
            step=step,
            metadata={"task_id": task_id, "last_seq": state.last_seq},
        )
        counters.checkpoints += 1

    def _state(self, ctx: _Ctx) -> DerivedState:
        state, _ = reduce_events(self._log(ctx))
        return state

    def _log(self, ctx: _Ctx) -> list[Event]:
        return self._store.effective_events(ctx.branch_id)

    def _has_result(self, ctx: _Ctx, tool_call_id: str) -> bool:
        return tool_call_id in self._state(ctx).closed_tool_calls

    def _ensure_tool_call_event(
        self,
        ctx: _Ctx,
        request: ToolCallRequest,
        effect: Effect,
        key: str,
        args_sha256: str,
    ) -> None:
        state = self._state(ctx)
        if (
            request.tool_call_id in state.open_tool_calls
            or request.tool_call_id in state.closed_tool_calls
        ):
            return
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_CALL,
                source=Source.AGENT,
                payload={
                    "tool_call_id": request.tool_call_id,
                    "tool": request.tool,
                    "args": request.args,
                    "args_sha256": args_sha256,
                    "idempotency_key": key,
                    "effect": effect.value,
                },
            )
        )

    def _append_tool_call_event(
        self,
        ctx: _Ctx,
        request: ToolCallRequest,
        effect: Effect | None,
        idempotency_key_value: str | None,
    ) -> None:
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_CALL,
                source=Source.AGENT,
                payload={
                    "tool_call_id": request.tool_call_id,
                    "tool": request.tool,
                    "args": request.args,
                    "args_sha256": canonical_args_sha256(request.args),
                    "idempotency_key": idempotency_key_value
                    or idempotency_key(ctx.run_id, ctx.branch_id, request.tool_call_id),
                    "effect": effect.value if effect is not None else None,
                },
            )
        )

    def _append_tool_result(
        self,
        ctx: _Ctx,
        tool_call_id: str,
        status: str,
        result: dict[str, Any] | None,
        error_class: str | None = None,
    ) -> None:
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_RESULT,
                source=Source.TOOL,
                payload={
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "result": result,
                    "error_class": error_class,
                },
            )
        )

    def _append_error_artifact(
        self, ctx: _Ctx, error_class: str, message: str, *, fatal: bool = False
    ) -> None:
        self._store.append(
            NewEvent.artifact(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=ArtifactEventType.ERROR,
                payload={"error_class": error_class, "message": message, "fatal": fatal},
            )
        )

    def _append_agent_message(
        self, ctx: _Ctx, turn: ModelTurn, *, response: ModelResponse, final: bool
    ) -> None:
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={
                    "text": turn.text,
                    "final": final,
                    "usage": turn.usage,
                    "view_fingerprint": response.view_fingerprint,
                    "context_tokens": response.context_tokens,
                    "cache_read_tokens": response.usage.cache_read_tokens,
                    "cache_write_tokens": response.usage.cache_write_tokens,
                    "cost_usd": response.cost_usd,
                    "cache_config_version": response.cache_config_version,
                    "price_version": response.price_version,
                },
            )
        )

    def _tool_request_from_log(self, ctx: _Ctx, tool_call_id: str) -> ToolCallRequest | None:
        for event in self._log(ctx):
            if (
                event.type == TreeEventType.TOOL_CALL.value
                and event.payload.get("tool_call_id") == tool_call_id
            ):
                return ToolCallRequest(
                    tool_call_id=tool_call_id,
                    tool=event.payload["tool"],
                    args=event.payload.get("args", {}),
                )
        return None


__all__ = [
    "APPROVAL_TTL_SECONDS",
    "DEFAULT_SYSTEM_PROMPT",
    "InterruptRequest",
    "InterruptSignal",
    "Loop",
    "Model",
    "ModelTurn",
    "RunOutcome",
]
