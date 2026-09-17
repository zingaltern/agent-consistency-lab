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

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .approval import DECISION_APPROVED, SCOPE_ONCE, ApprovalBinding
from .budget import BudgetExceeded
from .chaos import Chaos
from .context import ViewBuilder
from .events import Event, NewEvent, Source, TreeEventType
from .execution import (
    APPROVAL_TTL_SECONDS,
    InterruptRequest,
    InterruptSignal,
    RunContext,
    ToolExecutor,
    ToolOutcome,
    append_error_artifact,
)
from .ids import new_id
from .llm import ContextOverflow, LLMClient, ModelResponse
from .model import Model, ModelTurn
from .prompts import DEFAULT_OPS_SYSTEM_PROMPT
from .state import DerivedState, RunStatus, reduce_events
from .store.checkpoints import SqliteCheckpointSaver, Write
from .store.sqlite_store import SqliteStore
from .tools import ToolCallRequest, ToolRegistry

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


class LoopError(RuntimeError):
    """调用方误用（例如在非空分支上 start、在终态上 resume 且期望有副作用）。"""


# 三身份上下文与 interrupt 信号住在 harness/execution.py（与工具管线同处一个模块，
# 因为外部驱动方也要用同一套）。这里保留名字，既有导入与文档引用不受影响。
_Ctx = RunContext


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
        self._llm = llm
        self._registry = registry
        self._chaos = chaos or Chaos.disabled()
        # 七步管线 + 审批决策 + 恢复段住在 harness/execution.py：loop 与外部驱动方
        # （MCP 工具服务）共用同一实现，loop 这边只负责"模型怎么驱动它"。
        self._exec = ToolExecutor(
            store,
            registry=registry,
            dedup=dedup,
            outbox=outbox,
            chaos=self._chaos,
            tamper=tamper,
        )
        self._builder = builder or ViewBuilder(
            system_prompt=DEFAULT_SYSTEM_PROMPT, registry=registry
        )
        self._compactor = compactor
        self._budget = budget
        self._max_steps = max_steps
        self._ns = checkpoint_ns
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
        report = self._exec.resume_open_calls(ctx, counters)
        if report.pending is not None:
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
        实现住在 ``harness/execution.py::ToolExecutor.decide``（与外部驱动方共用）。
        """
        return self._exec.decide(
            RunContext(run_id, thread_id, branch_id),
            decision=decision,
            actor=actor,
            ttl_seconds=ttl_seconds,
            approved_args=approved_args,
            scope=scope,
        )

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
                    self._execute_tool(ctx, request, counters)
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
                    append_error_artifact(
                        self._store, ctx, "context_overflow", str(exc), fatal=True
                    )
                    return None
                record = self._compactor.compact(
                    run_id=ctx.run_id,
                    branch_id=ctx.branch_id,
                    events=self._log(ctx),
                    reason="overflow",
                )
                if record is None:
                    append_error_artifact(
                        self._store, ctx, "context_overflow", "无可压缩内容", fatal=True
                    )
                    return None
                counters.compactions += 1
                view = self._build_view(ctx, counters)
            except BudgetExceeded as exc:
                append_error_artifact(self._store, ctx, "budget_exceeded", str(exc), fatal=True)
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
        counters: _Counters,
    ) -> ToolOutcome:
        """七步管线的**唯一**实现在 ``harness/execution.py::ToolExecutor.execute``。

        这里只做委托：loop 侧不得出现第二条执行路径（有结构断言守着）。
        registry 不再逐调用传入——executor 持有它，避免"两个 registry 来源"。
        需要审批时抛 ``InterruptSignal``，与从前逐字一致。
        """
        return self._exec.execute(ctx, request, counters)

    def _find_approval(
        self, ctx: _Ctx, tool_call_id: str, tool: str
    ) -> ApprovalBinding | None:
        """保留符号的薄委托（审计回归用例直接调用它，那个 P0 不该因为搬家而失守）。"""
        return self._exec.find_approval(ctx, tool_call_id, tool)

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
        append_error_artifact(
            self._store,
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
