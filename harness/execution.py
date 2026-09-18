"""工具执行管线（七步）+ 审批决策 + 恢复段——loop 与外部驱动方共用的**唯一**实现。

为什么单独成模块（而不是留在 ``harness/loop.py``）：治理型 MCP 工具服务要在
**零模型**的前提下走同一条管线（审批门、幂等键、outbox 三态、ArgPolicy、TOCTOU 复核、
事件日志 append-only）。把管线抽出来共用，好过让 ``Loop`` 承担第二种驱动模式，
也好过在集成层复制一份语义会分叉的"轻量执行器"。
裁决与证明方式见 ``docs/design/2026-09-18-open-questions-answered.md`` §一。

**边界纪律**（改动前先读）：

* 本模块只做"把一次工具调用走完七步"，不建视图、不调模型、不提交 checkpoint——
  那些是 ``Loop`` 的边界。checkpoint 提交与模型调用**不得**搬进来。
* 崩溃窗口（``pre_tool_exec`` 等四个）的命中点属于管线，必须留在这里；
  ``after_resume`` 属于 loop 的恢复流程，留在 ``Loop.resume``。
* 状态一律从事件日志折叠；本模块不缓存任何跨调用的结论。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .approval import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    SCOPE_ONCE,
    SCOPE_SESSION,
    ApprovalBinding,
    ApprovalError,
)
from .chaos import Chaos
from .events import ArtifactEventType, Event, NewEvent, Source, TreeEventType
from .ids import new_id
from .state import DerivedState, reduce_events
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

# 审批有效期默认值。loop 与 MCP 服务共用同一个默认（客户端拿到的 TTL 提示值以此为准）。
APPROVAL_TTL_SECONDS = 3600.0


class InterruptRequest(BaseModel):
    """一次待审批调用的可读描述（interrupt 事件的 payload 形状）。"""

    interrupt_id: str
    interrupt_index: int
    tool_call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    args_sha256: str
    reason: str = ""


class InterruptSignal(Exception):
    """控制流信号：不是错误，但绝不能被业务代码吞掉。

    对齐 LangGraph 的 ``interrupt()`` 语义——恢复时当前步骤从头重跑，
    因此 interrupt 之前的副作用必须幂等。
    """

    def __init__(self, request: InterruptRequest) -> None:
        super().__init__(f"interrupt: {request.reason}")
        self.request = request


@dataclass(frozen=True)
class RunContext:
    """一次 run 的三种身份（原 ``harness/loop.py::_Ctx``）。"""

    run_id: str
    thread_id: str
    branch_id: str


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具执行的结论。

    它**不是**权威——权威是事件日志里那条 ``tool_result``；这里只是把同一份结论
    按可读形状返回给调用方（loop 靠它累计计数，外部驱动方靠它回话）。
    ``replayed=True`` 表示本次没有执行 ``tool.fn``（答案来自日志或探针对账）。
    """

    tool_call_id: str
    status: str
    result: dict[str, Any] | None = None
    error_class: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class ResumeReport:
    """恢复段的结果：执行了哪几条、以及是否被某一条的审批门拦下。

    为什么要有这个类型（而不是让 ``InterruptSignal`` 直接穿透）：恢复段常常
    **执行了几条之后**才被下一条的审批门拦住。若用异常表达，前面那几条的结论会随栈一起丢掉，
    调用方只能看到一个 interrupt——"已经执行了但没被告知"正是最危险的形态。
    """

    outcomes: tuple[ToolOutcome, ...] = ()
    pending: InterruptRequest | None = None


@runtime_checkable
class CounterSink(Protocol):
    """管线会累加的 7 个计数（鸭子类型：``Loop._Counters`` 与集成层各自实现）。

    刻意不 import ``Loop._Counters``——那会造成 ``loop`` ↔ ``execution`` 循环 import，
    也会把模型侧的 15 个字段绑进工具路径。
    """

    replayed: int
    unknown: int
    rejected: int
    probes: int
    reconciled: int
    executed: int
    tool_failures: int


def append_error_artifact(
    store: SqliteStore,
    ctx: RunContext,
    error_class: str,
    message: str,
    *,
    fatal: bool = False,
) -> None:
    """写 ``error`` artifact。工具路径与模型路径共用这一处（避免两份实现分叉）。"""
    store.append(
        NewEvent.artifact(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=ArtifactEventType.ERROR,
            payload={"error_class": error_class, "message": message, "fatal": fatal},
        )
    )


class ToolExecutor:
    """七步管线 + 审批决策 + 恢复段。``Loop`` 与 MCP 工具服务共用本类。"""

    def __init__(
        self,
        store: SqliteStore,
        *,
        registry: ToolRegistry,
        tool_calls: ToolCallStore | None = None,
        dedup: bool = True,
        outbox: bool = True,
        chaos: Chaos | None = None,
        tamper: Callable[[ToolCallRequest], ToolCallRequest] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._tool_calls = tool_calls or ToolCallStore(store)
        self._dedup = dedup
        self._outbox = outbox
        self._chaos = chaos or Chaos.disabled()
        self._tamper = tamper

    # ------------------------------------------------------------------ 读取面

    def log(self, ctx: RunContext) -> list[Event]:
        return self._store.effective_events(ctx.branch_id)

    def derived_state(self, ctx: RunContext) -> DerivedState:
        state, _ = reduce_events(self.log(ctx))
        return state

    def tool_request_from_log(self, ctx: RunContext, tool_call_id: str) -> ToolCallRequest | None:
        return self._tool_request_from_log(ctx, tool_call_id)

    def open_calls_in_log_order(self, ctx: RunContext, state: DerivedState) -> list[str]:
        """未闭合的调用 id，按**它们在日志里出现的先后**排序（不按 ``tool_call_id``）。

        为什么不能用 ``sorted(state.open_tool_calls)``：``tool_call_id`` 由 ``new_id``
        生成——3.14 上是 ``uuid7``（时间有序），3.11/3.12 上没有 uuid7、回退成 ``uuid4``
        （随机）。于是"按 id 排序"在开发机与 CI runner 上给出**不同的顺序**，并且同一台
        机器每次运行也可能不同。两处调用点都因此出过问题（MCP 的待审批队列顺序、
        恢复段先驱动哪一条），根因是同一个。

        顺序的权威来源是日志：先来的那条正在审批门等，后来的排在它后面。
        日志里找不到 id 的开放调用（理论上不该出现）按 id 追加到末尾——
        "不丢项"优先于"顺序好看"。
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for event in self.log(ctx):
            if event.type != TreeEventType.TOOL_CALL.value:
                continue
            call_id = str(event.payload.get("tool_call_id") or "")
            if call_id and call_id not in seen:
                seen.add(call_id)
                ordered.append(call_id)
        ordered.extend(sorted(set(state.open_tool_calls) - seen))
        return [call_id for call_id in ordered if call_id in state.open_tool_calls]

    def pending_approvals(self, ctx: RunContext) -> list[dict[str, Any]]:
        """日志里有、但还没闭合的**需审批**调用（按调用在日志里的先后排序）。

        返回的是描述不是权威：权威仍是 interrupt / tool_result 事件本身。
        同时暴露"哪一条正卡在审批门"（``is_current``），因为 INV-004 保证同一时刻
        至多一条 interrupt——其余调用是**排在前一条之后**等待，不是被丢弃。
        """
        state = self.derived_state(ctx)
        current = (
            self._interrupt_request(ctx, state.pending_interrupt_id)
            if state.pending_interrupt_id is not None
            else None
        )
        out: list[dict[str, Any]] = []
        for call_id in self.open_calls_in_log_order(ctx, state):
            request = self._tool_request_from_log(ctx, call_id)
            if request is None:
                continue
            try:
                tool = self._registry.get(request.tool)
            except KeyError:
                continue
            if not tool.requires_approval:
                continue
            is_current = current is not None and current.tool_call_id == call_id
            out.append(
                {
                    "tool_call_id": call_id,
                    "tool": request.tool,
                    "args": request.args,
                    "args_sha256": canonical_args_sha256(request.args),
                    "is_current": is_current,
                    "interrupt_id": current.interrupt_id if is_current else None,
                    "interrupt_index": current.interrupt_index if is_current else None,
                    "reason": current.reason if is_current else "等待前一条审批先落地",
                }
            )
        return out

    # ------------------------------------------------------------------ 管线

    def execute(
        self,
        ctx: RunContext,
        request: ToolCallRequest,
        counters: CounterSink,
    ) -> ToolOutcome:
        """走完七步。需要人工审批时抛 ``InterruptSignal``（不返回结论）。"""
        key = idempotency_key(ctx.run_id, ctx.branch_id, request.tool_call_id)
        args_sha256 = canonical_args_sha256(request.args)

        # 1) 日志权威：已有结论的调用只重放，不再产生副作用
        if self._has_result(ctx, request.tool_call_id):
            counters.replayed += 1
            return self._replay_outcome(ctx, request.tool_call_id)

        # 2) 工具解析：模型幻觉出的工具名也是一次失败调用，必须被记录并闭合，
        #    而不是让 KeyError 穿透 loop（否则 run 永远停在 RUNNING、调用悬挂）。
        try:
            tool = self._registry.get(request.tool)
        except KeyError:
            self._append_tool_call_event(ctx, request, None, None)
            counters.tool_failures += 1
            append_error_artifact(
                self._store, ctx, "unknown_tool", f"{request.tool!r} is not registered"
            )
            self._append_tool_result(ctx, request.tool_call_id, "failed", None, "unknown_tool")
            return ToolOutcome(
                request.tool_call_id, "failed", None, "unknown_tool", replayed=False
            )

        # 3) 补记决策（幂等）
        self._ensure_tool_call_event(ctx, request, tool.effect, key, args_sha256)

        # 3) 既有意图行处置
        existing = self._tool_calls.lookup(key) if (self._dedup or self._outbox) else None
        if existing is not None and existing.args_sha256 != args_sha256:
            # 同键不同参：旁路校验字段在这里起作用——绝不允许把另一次参数的执行结果
            # 当成本次调用的重放（那是静默的错误答案，比报错危险得多）。
            counters.rejected += 1
            append_error_artifact(
                self._store,
                ctx,
                "args_sha256_mismatch",
                f"key {key} recorded args {existing.args_sha256[:12]} != actual {args_sha256[:12]}",
            )
            self._append_tool_result(
                ctx, request.tool_call_id, "rejected", None, "args_sha256_mismatch"
            )
            return ToolOutcome(
                request.tool_call_id, "rejected", None, "args_sha256_mismatch", replayed=False
            )
        if existing is not None:
            if existing.status == "executed":
                counters.replayed += 1
                self._append_tool_result(ctx, request.tool_call_id, "executed", existing.result)
                return ToolOutcome(
                    request.tool_call_id, "executed", existing.result, None, replayed=True
                )
            if existing.status == "pending":
                resolved = self._resolve_pending(ctx, existing, tool, request, counters)
                if resolved is not None:
                    return resolved
            elif existing.status == "unknown":
                counters.unknown += 1
                self._append_tool_result(
                    ctx,
                    request.tool_call_id,
                    "unknown",
                    None,
                    "unknown_effect_unresolved",
                )
                return ToolOutcome(
                    request.tool_call_id,
                    "unknown",
                    None,
                    "unknown_effect_unresolved",
                    replayed=False,
                )

        # 4) 审批门
        binding = self.find_approval(ctx, request.tool_call_id, tool.name)
        if binding is not None:
            ok, reason = binding.validates(
                tool_call_id=request.tool_call_id, tool=tool.name, args_sha256=args_sha256
            )
            if not ok:
                counters.rejected += 1
                append_error_artifact(self._store, ctx, reason, f"审批校验未通过: {reason}")
                self._append_tool_result(ctx, request.tool_call_id, "rejected", None, reason)
                return ToolOutcome(request.tool_call_id, "rejected", None, reason, replayed=False)
            self._chaos.hit("post_approval_pre_exec")
        elif tool.requires_approval:
            self._raise_interrupt(ctx, request, args_sha256)
            # 防御而不是"不可能的代码"：_raise_interrupt 的契约是永远抛，
            # 若哪天它被改成会返回，这里必须**吵**——否则流程会静默滑进第 5 步去执行一个
            # 未经审批的写操作（W3 的教训：分支不可达 + 静默放行是同一类事故）。
            raise AssertionError("unreachable: _raise_interrupt 必须抛 InterruptSignal")

        # 5) TOCTOU：执行前用实际参数复核批准的 hash
        if self._tamper is not None:
            request = self._tamper(request)
        actual_sha = canonical_args_sha256(request.args)
        if binding is not None and actual_sha != binding.approved_args_sha256:
            counters.rejected += 1
            append_error_artifact(
                self._store, ctx, "args_sha256_mismatch", "执行前参数与批准参数不一致（TOCTOU）"
            )
            self._append_tool_result(
                ctx, request.tool_call_id, "rejected", None, "args_sha256_mismatch"
            )
            return ToolOutcome(
                request.tool_call_id, "rejected", None, "args_sha256_mismatch", replayed=False
            )

        # 5b) 参数级安全域（R-B2）：闸门在**执行 handler 前的最后一刻**，
        # 输入是实际 request.args（不是审批记录里的 hash）——否则会出现
        # "审批时看不懂结构化参数、批准了被禁止值"的缝隙。
        # 位置刻意放在 outbox 预写之前：被拒的调用不该留下意图行。
        if tool.arg_policy is not None:
            allowed, reason = tool.arg_policy.evaluate(request.args)
            if not allowed:
                counters.rejected += 1
                append_error_artifact(
                    self._store,
                    ctx,
                    "arg_policy_violation",
                    f"{request.tool} 参数越出工具自述的安全域（{reason}；"
                    f"策略 {tool.arg_policy.describe()}）",
                )
                self._append_tool_result(
                    ctx, request.tool_call_id, "rejected", None, "arg_policy_violation"
                )
                return ToolOutcome(
                    request.tool_call_id, "rejected", None, "arg_policy_violation", replayed=False
                )

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
            return self._handle_tool_failure(
                ctx, request, tool, exc, counters, outbox_path=outbox_path
            )
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
        return ToolOutcome(request.tool_call_id, "executed", result, None, replayed=False)

    def resume_open_calls(self, ctx: RunContext, counters: CounterSink) -> ResumeReport:
        """恢复段：把日志里**未闭合**的调用按**日志先后**重新驱动一次。

        这是 ``Loop.resume`` 与"外部驱动方重启后续跑"共用的那段。
        是否需要先探针对账由管线第 3 步决定；本方法不做任何自己的判断。
        未获批的调用会被审批门拦下，此时**已经执行的那几条仍然随报告返回**。

        顺序为什么重要：撞上未获批的调用会抛 ``InterruptSignal`` 并**提前返回**。
        若顺序把"排队那条"排在前面（按随机 ``tool_call_id`` 排序时会发生，见
        :meth:`open_calls_in_log_order`），刚被批准、本该本轮执行的那条就轮不到——
        MCP 侧实测过：批准第一条后回执里的 ``executed`` 是空的（3.11/3.12 runner）。
        按日志先后驱动则"先来的先执行，后面的仍停在门里"，与审批语义一致。
        """
        state = self.derived_state(ctx)
        outcomes: list[ToolOutcome] = []
        for call_id in self.open_calls_in_log_order(ctx, state):
            request = self._tool_request_from_log(ctx, call_id)
            if request is None:
                continue
            try:
                outcomes.append(self.execute(ctx, request, counters))
            except InterruptSignal as signal:
                return ResumeReport(tuple(outcomes), signal.request)
        return ResumeReport(tuple(outcomes))

    # ------------------------------------------------------------------ 审批

    def decide(
        self,
        ctx: RunContext,
        *,
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
        state = self.derived_state(ctx)
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
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=ArtifactEventType.APPROVAL,
                payload=binding.model_dump(),
            )
        )
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
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

    # ------------------------------------------------------------- 管线私有件

    def _replay_outcome(self, ctx: RunContext, tool_call_id: str) -> ToolOutcome:
        """日志权威路径：把已闭合调用在日志里的结论读回来（不产生任何新事件）。"""
        for event in self.log(ctx):
            if (
                event.type == TreeEventType.TOOL_RESULT.value
                and event.payload.get("tool_call_id") == tool_call_id
            ):
                return ToolOutcome(
                    tool_call_id,
                    str(event.payload.get("status", "executed")),
                    event.payload.get("result"),
                    event.payload.get("error_class"),
                    replayed=True,
                )
        # closed_tool_calls 里有、日志里却找不到 result：不可能，除非日志被外部改过。
        return ToolOutcome(tool_call_id, "unknown", None, "result_missing_from_log", replayed=True)

    def _resolve_pending(
        self,
        ctx: RunContext,
        existing: ToolCallRecord,
        tool: Tool,
        request: ToolCallRequest,
        counters: CounterSink,
    ) -> ToolOutcome | None:
        """处置 pending 意图。返回 ``None`` 表示"确认未生效、可安全执行"。"""
        if tool.probe is None:
            self._tool_calls.mark_unknown(existing.idempotency_key, "no_probe_available")
            counters.unknown += 1
            append_error_artifact(
                self._store, ctx, "unknown_effect", "pending 意图无法对账：下游不支持按键读回"
            )
            self._append_tool_result(ctx, request.tool_call_id, "unknown", None, "unknown_effect")
            return ToolOutcome(
                request.tool_call_id, "unknown", None, "unknown_effect", replayed=False
            )
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
            return ToolOutcome(request.tool_call_id, "executed", result, None, replayed=True)
        if probe.outcome is ProbeOutcome.NOT_APPLIED:
            return None
        self._tool_calls.mark_unknown(existing.idempotency_key, "probe_inconclusive")
        counters.unknown += 1
        append_error_artifact(self._store, ctx, "unknown_effect", "探针结论不确定，转人工对账")
        self._append_tool_result(ctx, request.tool_call_id, "unknown", None, "unknown_effect")
        return ToolOutcome(request.tool_call_id, "unknown", None, "unknown_effect", replayed=False)

    def _handle_tool_failure(
        self,
        ctx: RunContext,
        request: ToolCallRequest,
        tool: Tool,
        exc: Exception,
        counters: CounterSink,
        *,
        outbox_path: bool,
    ) -> ToolOutcome:
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
        append_error_artifact(self._store, ctx, error_class, f"{request.tool}: {exc}")
        self._append_tool_result(ctx, request.tool_call_id, status, None, error_class)
        return ToolOutcome(request.tool_call_id, status, None, error_class, replayed=False)

    def _raise_interrupt(
        self, ctx: RunContext, request: ToolCallRequest, args_sha256: str
    ) -> NoReturn:
        """发 interrupt 事件并抛 ``InterruptSignal``；**永不返回**。"""
        state = self.derived_state(ctx)
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

    def _interrupt_request(self, ctx: RunContext, interrupt_id: str) -> InterruptRequest:
        for event in self.log(ctx):
            if (
                event.type == TreeEventType.INTERRUPT.value
                and event.payload.get("interrupt_id") == interrupt_id
            ):
                return InterruptRequest.model_validate(event.payload)
        raise ApprovalError(f"interrupt {interrupt_id!r} not found in log")

    def _interrupt_count(self, ctx: RunContext) -> int:
        return sum(1 for e in self.log(ctx) if e.type == TreeEventType.INTERRUPT.value)

    def find_approval(
        self, ctx: RunContext, tool_call_id: str, tool: str
    ) -> ApprovalBinding | None:
        """最近一条可用于该调用的批准（once 绑定调用；session 绑定工具+参数）。"""
        candidates: list[tuple[tuple[int, int, float], ApprovalBinding]] = []
        for event in self.log(ctx):
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

    def _has_result(self, ctx: RunContext, tool_call_id: str) -> bool:
        return tool_call_id in self.derived_state(ctx).closed_tool_calls

    def _ensure_tool_call_event(
        self,
        ctx: RunContext,
        request: ToolCallRequest,
        effect: Effect,
        key: str,
        args_sha256: str,
    ) -> None:
        state = self.derived_state(ctx)
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
        ctx: RunContext,
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
        ctx: RunContext,
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

    def _tool_request_from_log(self, ctx: RunContext, tool_call_id: str) -> ToolCallRequest | None:
        for event in self.log(ctx):
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
    "CounterSink",
    "InterruptRequest",
    "InterruptSignal",
    "ResumeReport",
    "RunContext",
    "ToolExecutor",
    "ToolOutcome",
    "append_error_artifact",
]
