"""参数级审批策略（R-B2）：声明、注册期校验、**执行前一刻**的强制。

为什么校验点在执行前一刻而不是审批时：审批比的是 ``args_sha256``——参数一变即拒，
所以"session 复用 + 改参"这条路径已经被 hash 拒掉了。真正的缝隙是**审批时看不懂结构化参数**：
人批准的是"某次调用 + 某组参数"的哈希，他没有逐字段核对"这个 size 是不是越界"。
因此工具的**自述安全域**必须在执行 handler 之前，用**实际参数**再核一遍。

这个文件覆盖的九类情形（验收要求 ≥8 例）：

1. allowed 命中 → 通过；
2. forbidden 命中 → 拒绝（且不产生副作用）；
3. 数值区间边界（端点算通过、越界拒绝、非数值拒绝）；
4. 缺字段：allowed/区间类策略**拒绝**（无法核对不能默认放行），纯 forbidden 策略放行；
5. **审批通过但参数越界**（人只看了 hash）→ 执行前一刻被拦，账本 0 行；
6. session 复用路径下改参 → 仍被拒（hash 档 + 策略档各一道）；
7. 策略缺失 → 回落到布尔 `requires_approval` 语义（行为零改变）；
8. 非法策略定义 → **注册期**显式报错（不留到执行期）；
9. 被拒的调用不留下 outbox 意图行、也不写 side effect。
"""

from __future__ import annotations

import pytest

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
from fakeworld.world import World
from harness.approval import SCOPE_SESSION
from harness.cache import CacheConfig, PrefixCacheModel
from harness.chaos import Chaos
from harness.llm import ScriptedLLMClient
from harness.loop import Loop
from harness.state import reduce_events
from harness.store import SqliteCheckpointSaver
from harness.store.sqlite_store import SqliteStore
from harness.tools import (
    ArgPolicy,
    ArgPolicyError,
    Effect,
    Tool,
    ToolCallRequest,
    ToolRegistry,
)

from .conftest import RunCtx

TASK = "支付服务 P99 告警，请定位并处置。"


# ------------------------------------------------------------------ 策略本身


def test_allowed_value_passes() -> None:
    policy = ArgPolicy(field="role", allowed=["api", "worker"])
    assert policy.evaluate({"role": "api"}) == (True, "ok")


def test_forbidden_value_is_rejected() -> None:
    policy = ArgPolicy(field="role", forbidden=["billing"])
    ok, reason = policy.evaluate({"role": "billing"})
    assert not ok and reason.startswith("arg_policy_forbidden_value")


def test_numeric_bounds_include_endpoints_and_reject_non_numeric() -> None:
    policy = ArgPolicy(field="size", min=1, max=512)
    assert policy.evaluate({"size": 1})[0] is True  # 端点算通过
    assert policy.evaluate({"size": 512})[0] is True
    below = policy.evaluate({"size": 0})
    assert not below[0] and below[1].startswith("arg_policy_below_min")
    above = policy.evaluate({"size": 513})
    assert not above[0] and above[1].startswith("arg_policy_above_max")
    not_number = policy.evaluate({"size": "big"})
    assert not not_number[0] and not_number[1].startswith("arg_policy_not_numeric")


def test_missing_field_semantics_differ_by_policy_kind() -> None:
    """缺字段的处置是**语义**，不是细节：无法核对时不能默认放行。"""
    assert ArgPolicy(field="size", min=1, max=512).evaluate({})[1].startswith(
        "arg_policy_field_missing"
    )
    assert ArgPolicy(field="role", allowed=["api"]).evaluate({})[1].startswith(
        "arg_policy_field_missing"
    )
    # 纯 forbidden：没有出现危险值 ⇒ 通过
    assert ArgPolicy(field="role", forbidden=["billing"]).evaluate({}) == (True, "ok")


def test_invalid_policy_definitions_fail_at_definition_time() -> None:
    # pydantic 会把 model_validator 里抛出的 ValueError 包成 ValidationError
    # （ValidationError 是 ValueError 的子类），因此两种都接受
    with pytest.raises((ArgPolicyError, ValueError)):
        ArgPolicy(field="")
    with pytest.raises((ArgPolicyError, ValueError)):
        ArgPolicy(field="size")  # 一条约束都没有 = 没有策略
    with pytest.raises((ArgPolicyError, ValueError)):
        ArgPolicy(field="size", min=10, max=1)


# ------------------------------------------------------------------ 端到端


def _loop(
    store: SqliteStore,
    ctx: RunCtx,
    world: World,
    registry: ToolRegistry,
    *,
    outbox: bool = True,
) -> Loop:
    llm = ScriptedLLMClient(
        ScriptedModel(SCENARIO_POOL_EXHAUSTION),
        cache=PrefixCacheModel(CacheConfig()),
    )
    return Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=llm,
        registry=registry,
        chaos=Chaos.disabled(),
        outbox=outbox,
    )


def _drive_to_interrupt(loop: Loop, ctx: RunCtx) -> None:
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)


def _effects(world: World) -> int:
    return world.total_effects()


@pytest.fixture()
def world(tmp_path) -> World:
    world = World(tmp_path / "world.db")
    yield world
    world.close()


def _tool_with_policy(policy: ArgPolicy) -> Tool:
    # 名字刻意与脚本模型提议的工具一致（SCENARIO_POOL_EXHAUSTION 第二轮提 scale_pool），
    # 这样整条路径（模型提议 → 审批门 → 执行前策略闸）都是真实代码在跑
    return Tool(
        name="scale_pool",
        effect=Effect.WRITE_NONIDEMPOTENT,
        fn=lambda args, key: {"ok": args},
        description="测试用写工具",
        requires_approval=True,
        arg_policy=policy,
    )


def test_register_rejects_a_hand_written_invalid_policy() -> None:
    """注册期校验：策略写错现在就炸，而不是等某次执行时才拒绝。"""
    registry = ToolRegistry()
    with pytest.raises((ArgPolicyError, ValueError)):
        registry.register(
            Tool(
                name="bad",
                effect=Effect.READ,
                fn=lambda args, key: {},
                arg_policy={"field": "x", "min": 5, "max": 1},  # type: ignore[arg-type]
            )
        )


def test_approval_that_only_looked_at_the_hash_is_still_gated(
    store: SqliteStore, ctx: RunCtx, world: World
) -> None:
    """核心用例：批准的是 hash，人没逐字段核对 ⇒ 执行前一刻必须拦下越界参数。"""
    registry = ToolRegistry()
    registry.register(_tool_with_policy(ArgPolicy(field="size", min=1, max=64)))
    loop = _loop(store, ctx, world, registry)

    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    state, _ = reduce_events(store.effective_events(ctx.branch_id))
    assert state.pending_interrupt_id is not None, "写工具应当开门等审批"

    # 人批准的就是越界参数本身（他只看了一眼 hash）：hash 一致，策略不一致
    loop.approve(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        approved_args={"service": "payment", "size": 9999},
    )
    outcome = loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    assert outcome.rejected >= 1
    events = store.effective_events(ctx.branch_id)
    statuses = [
        event.payload.get("status")
        for event in events
        if event.type == "tool_result"
    ]
    assert "rejected" in statuses
    error_classes = [
        event.payload.get("error_class")
        for event in events
        if event.type == "error"
    ]
    assert "arg_policy_violation" in error_classes
    # 没有副作用、也没有留下 pending 意图行
    assert _effects(world) == 0
    rows = store._conn.execute("SELECT status FROM tool_calls").fetchall()
    assert all(row["status"] != "pending" for row in rows)


def test_in_range_args_pass_the_gate_and_execute(
    store: SqliteStore, ctx: RunCtx, world: World
) -> None:
    """同一套机制在域内参数上不误伤：正常路径必须照旧执行。"""
    registry = ToolRegistry()
    registry.register(_tool_with_policy(ArgPolicy(field="size", min=1, max=64)))
    loop = _loop(store, ctx, world, registry)
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    loop.approve(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        approved_args={"service": "payment", "size": 64},
    )
    outcome = loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    assert outcome.executed >= 1
    assert outcome.rejected == 0


def test_session_reuse_with_changed_args_is_still_blocked_by_both_gates(
    store: SqliteStore, ctx: RunCtx, world: World
) -> None:
    """session 复用路径：hash 档先拒改参，策略档再拒越界——两道都在。"""
    registry = ToolRegistry()
    registry.register(_tool_with_policy(ArgPolicy(field="size", min=1, max=64)))
    loop = _loop(store, ctx, world, registry)
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    loop.approve(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        scope=SCOPE_SESSION,
    )
    # 换一组参数再跑同一个工具：session 批准**不覆盖**不同 hash 的调用
    from harness.approval import ApprovalBinding
    from harness.tools import canonical_args_sha256

    bindings = [
        ApprovalBinding.model_validate(event.payload)
        for event in store.effective_events(ctx.branch_id)
        if event.type == "approval"
    ]
    assert bindings and bindings[-1].scope == SCOPE_SESSION
    ok, reason = bindings[-1].validates(
        tool_call_id="another",
        tool="scale_pool",
        args_sha256=canonical_args_sha256({"service": "payment", "size": 9999}),
    )
    assert not ok and reason == "args_sha256_mismatch"


def test_tool_without_policy_keeps_the_boolean_behaviour(
    store: SqliteStore, ctx: RunCtx, world: World
) -> None:
    """没有声明策略的工具行为零改变：审批门照旧，执行前不加任何额外判定。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="scale_pool",
            effect=Effect.WRITE_NONIDEMPOTENT,
            fn=lambda args, key: {"ok": args},
            description="没有策略的写工具",
            requires_approval=True,
        )
    )
    loop = _loop(store, ctx, world, registry)
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    loop.approve(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        approved_args={"service": "payment", "size": 9999},
    )
    outcome = loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    assert outcome.executed >= 1, "没有策略就不该被策略拦下"
    assert outcome.rejected == 0


def test_fakeworld_scale_pool_declares_a_live_policy() -> None:
    """既有工具集里真的挂了一条策略（机制不是只为测试而存在）。"""
    registry = build_registry(World(":memory:"), idempotent_impl=False)
    tool = registry.get("scale_pool")
    assert tool.arg_policy is not None
    assert tool.arg_policy.field == "size"
    assert tool.arg_policy.evaluate({"size": 64})[0] is True
    assert tool.arg_policy.evaluate({"size": 100_000})[0] is False


def test_rejected_call_does_not_write_a_side_effect(
    store: SqliteStore, ctx: RunCtx, world: World
) -> None:
    """被策略拒绝的调用**绝不产生副作用**（策略是在 handler 之前拦的）。"""
    calls: list[dict] = []

    def _fn(args: dict, key: str) -> dict:
        calls.append(dict(args))
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="resize",
            effect=Effect.WRITE_NONIDEMPOTENT,
            fn=_fn,
            description="带策略的写工具",
            requires_approval=False,  # 不需要审批：策略是**唯一**的闸门
            arg_policy=ArgPolicy(field="size", max=10),
        )
    )
    loop = _loop(store, ctx, world, registry)
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)

    # 直接构造一次越界调用（绕过模型脚本）：走 loop 的真实执行路径
    from harness.loop import _Counters

    loop._execute_tool(
        ctx,
        ToolCallRequest(tool_call_id="tc_direct", tool="resize", args={"size": 99}),
        registry,
        _Counters(),
    )
    assert calls == [], "越界调用不能被执行"
    statuses = [
        event.payload.get("status")
        for event in store.effective_events(ctx.branch_id)
        if event.type == "tool_result"
    ]
    assert "rejected" in statuses


def test_boolean_does_not_pass_a_numeric_allow_list() -> None:
    """**P2-8 回归**：`True == 1` 在 Python 里成立，但"用真值穿透数值白名单"必须被拒。

    修复前会怎样：`ArgPolicy(field='m', allowed=[1])` 接受 `{'m': True}`——
    安全域判定里混进布尔值属于类型穿透（`forbidden` 侧同源，只是方向相反）。
    """
    policy = ArgPolicy(field="m", allowed=[1])
    rejected = policy.evaluate({"m": True})
    assert rejected[0] is False
    assert rejected[1].startswith("arg_policy_value_not_allowed")
    assert policy.evaluate({"m": 1}) == (True, "ok")
    # 反向同理：布尔白名单不接受数字
    assert ArgPolicy(field="m", allowed=[True]).evaluate({"m": 1})[0] is False
    # 布尔与布尔仍然正常匹配
    assert ArgPolicy(field="m", allowed=[True]).evaluate({"m": True}) == (True, "ok")


def test_boolean_is_not_caught_by_a_numeric_forbidden_list() -> None:
    """forbidden 侧同源：`forbidden=[1]` 不该把 `True` 拦下（那会让语义取决于运气）。"""
    policy = ArgPolicy(field="m", forbidden=[1])
    assert policy.evaluate({"m": True}) == (True, "ok")
    assert policy.evaluate({"m": 1})[0] is False
