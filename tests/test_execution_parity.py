"""管线复用的一致性证明：`Loop` 与外部驱动方走的是**同一条**实现。

设计文档 C 的技术裁决选了"把七步管线抽成 `harness/execution.py::ToolExecutor`"
（见 `docs/design/2026-09-18-open-questions-answered.md` §一），并要求给出
"两处调用点行为一致"的**可执行证明**，而不是"我看了一遍"。本文件就是那个证明，三条：

1. 差分：同一 `run_id`/`branch_id`、同一批 `tool_call`，分别由 `Loop`（脚本模型驱动）
   与 `ToolExecutor`（零模型直接驱动）跑完整条链，比较**工具路径事件子序列**与外部账本。
   两边 run/branch 相同 ⇒ 幂等键、`args_sha256` 等派生值必须逐字相等，逃不掉。
2. 委托：把 `ToolExecutor.execute` 换成记录型包装后跑 `Loop`，断言包装真的被调用——
   loop 侧不存在第二条执行路径。
3. 结构 + 正对照：管线私有件（崩溃窗口字面量、outbox 状态机、`tool.fn(`）不得出现在
   驱动方模块里；并用一段**合成坏源码**证明扫描函数真的会报违规
   （否则"永远绿"的扫描器本身不可信）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fakeworld.tools import build_registry
from fakeworld.world import World
from harness.cache import CacheConfig, PrefixCacheModel
from harness.chaos import Chaos
from harness.execution import CounterSink, InterruptSignal, RunContext, ToolExecutor
from harness.llm import ScriptedLLMClient
from harness.loop import InterruptRequest, Loop, _Counters
from harness.model import ModelTurn
from harness.store import SqliteCheckpointSaver, SqliteStore
from harness.tools import ArgPolicy, Effect, Tool, ToolCallRequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 同一条 run 的三种身份：两条路径**故意用同一组 id**，这样幂等键、args_sha256、
# 事件 payload 里的派生值必须逐字相等——"两套实现"会在这里露馅。
RUN_ID = "run_parity"
THREAD_ID = "thr_parity"
BRANCH_ID = "br_parity"

CALLS: tuple[ToolCallRequest, ...] = (
    # 只读：不需要审批，直接执行
    ToolCallRequest(tool_call_id="tc_read", tool="query_metrics", args={"service": "payment"}),
    # 非幂等写：要过审批门
    ToolCallRequest(
        tool_call_id="tc_write", tool="scale_pool", args={"service": "payment", "size": 64}
    ),
    # 参数越出工具自述的安全域 → 执行前被拒。
    # 注意必须用**不需要审批**的工具：审批门（第 4 步）先于参数安全域（第 5b 步），
    # 用 scale_pool 只会停在审批门，测不到这道闸。
    ToolCallRequest(tool_call_id="tc_bad", tool="export_logs", args={"limit": 999}),
    # 未注册工具：必须被记录并闭合，而不是让 KeyError 穿透
    ToolCallRequest(tool_call_id="tc_ghost", tool="no_such_tool", args={}),
)

# 工具路径会产出的事件类型；模型侧的 agent_message 不属于管线输出，不参与比较。
PIPELINE_EVENT_TYPES = {"tool_call", "tool_result", "interrupt", "resume", "approval", "error"}

EXPECTED_EVENT_TYPES = [
    "tool_call",
    "tool_result",
    "tool_call",
    "interrupt",
    "approval",
    "resume",
    "tool_result",
    "tool_call",
    "error",
    "tool_result",
    "tool_call",
    "error",
    "tool_result",
]

# payload 里每次运行都不同的字段（随机 id 与墙钟）。它们不改变管线语义，比较前归一化；
# 派生值（tool_call_id / args_sha256 / idempotency_key）**不**归一化——那才是要盯的。
VOLATILE_KEYS = ("interrupt_id", "approval_id", "nonce")
VOLATILE_TIMES = ("issued_at", "expires_at")

TOOL_COUNTER_FIELDS = (
    "executed",
    "replayed",
    "unknown",
    "rejected",
    "probes",
    "reconciled",
    "tool_failures",
)


class _ParityModel:
    """按 step 取 turn：turn 0 提两个调用，turn 1 提两个坏调用，turn 2 收尾。"""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = turns

    def next_turn(self, *, step: int, view: Any = None) -> ModelTurn:
        return self._turns[min(step, len(self._turns) - 1)]


class _ExecutorCounters:
    """外部驱动方自己的计数器：只实现 ``CounterSink`` 的 7 个字段。

    刻意不复用 ``Loop._Counters``——它带着 15 个模型侧字段，外部驱动方不该继承它们。
    """

    def __init__(self) -> None:
        self.executed = 0
        self.replayed = 0
        self.unknown = 0
        self.rejected = 0
        self.probes = 0
        self.reconciled = 0
        self.tool_failures = 0

    def as_tuple(self) -> tuple[int, ...]:
        return tuple(getattr(self, field) for field in TOOL_COUNTER_FIELDS)


def _turns() -> list[ModelTurn]:
    return [
        ModelTurn(text="先取证再处置", tool_calls=list(CALLS[:2]), usage={"prompt_tokens": 128}),
        ModelTurn(text="再试两个坏调用", tool_calls=list(CALLS[2:]), usage={"prompt_tokens": 160}),
        ModelTurn(text="收尾", tool_calls=[], usage={"prompt_tokens": 192}),
    ]


def _new_store(root: Path) -> SqliteStore:
    root.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(root / "runtime.db")
    store.setup()
    store.create_run(RUN_ID, thread_id=THREAD_ID)
    store.create_branch(BRANCH_ID, RUN_ID)
    return store


def _registry(world: World):
    # 下游非幂等 + 探针可用：与崩溃矩阵里"固定格"同配置，让 outbox 真的走预写路径。
    registry = build_registry(world, idempotent_impl=False, probe_enabled=True)
    # 带参数安全域、**不需要审批**的只读工具：只有这样的工具才能走到执行前一刻的
    # ArgPolicy 闸门（需审批的工具会先停在审批门）。
    registry.register(
        Tool(
            name="export_logs",
            effect=Effect.READ,
            fn=lambda args, key: {"operation": "export_logs", "lines": 0},
            description="带条数上限的只读导出",
            arg_policy=ArgPolicy(field="limit", max=100),
        )
    )
    return registry


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in payload.items() if k not in VOLATILE_TIMES}
    for key in VOLATILE_KEYS:
        if key in out:
            out[key] = f"<{key}>"
    return out


def _pipeline_events(store: SqliteStore) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (event.kind.value, event.type, event.source.value, _normalize(event.payload))
        for event in store.effective_events(BRANCH_ID)
        if event.type in PIPELINE_EVENT_TYPES
    ]


def _effect_rows(world: World) -> list[tuple[str, str, str]]:
    """外部账本的形状（去掉自增主键与时间戳）。"""
    return [
        (str(row["idempotency_key"]), str(row["operation"]), str(row["payload_json"]))
        for row in world.all_effects()
    ]


def _drive_with_loop(store: SqliteStore, world: World) -> tuple[int, ...]:
    """路径 A：模型驱动的完整链（start → approve → resume），返回工具侧计数。"""
    loop = Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=ScriptedLLMClient(_ParityModel(_turns()), cache=PrefixCacheModel(CacheConfig())),
        registry=_registry(world),
        chaos=Chaos.disabled(),
        outbox=True,
    )
    first = loop.start(run_id=RUN_ID, thread_id=THREAD_ID, branch_id=BRANCH_ID, task="parity")
    loop.approve(run_id=RUN_ID, thread_id=THREAD_ID, branch_id=BRANCH_ID, actor="human:parity")
    second = loop.resume(run_id=RUN_ID, thread_id=THREAD_ID, branch_id=BRANCH_ID)
    return tuple(
        getattr(first, field) + getattr(second, field) for field in TOOL_COUNTER_FIELDS
    )


def _drive_with_executor(store: SqliteStore, world: World) -> tuple[int, ...]:
    """路径 B：零模型直接驱动同一条链（执行 → 审批 → 恢复段），返回工具侧计数。"""
    executor = ToolExecutor(store, registry=_registry(world), outbox=True)
    counters = _ExecutorCounters()
    ctx = RunContext(RUN_ID, THREAD_ID, BRANCH_ID)

    executor.execute(ctx, CALLS[0], counters)  # 只读
    with pytest.raises(InterruptSignal) as interrupted:
        executor.execute(ctx, CALLS[1], counters)  # 需审批 → 停在审批门
    assert interrupted.value.request.tool_call_id == "tc_write"

    executor.decide(ctx, actor="human:parity")
    executor.resume_open_calls(ctx, counters)  # 被批准的写在这里执行
    executor.execute(ctx, CALLS[2], counters)  # 参数越界 → 拒
    executor.execute(ctx, CALLS[3], counters)  # 未注册 → failed
    return counters.as_tuple()


def test_loop_and_executor_emit_identical_pipeline_events(tmp_path: Path) -> None:
    """两条驱动路径产出逐字相同的工具路径事件序列、外部账本形状与工具侧计数。"""
    store_a = _new_store(tmp_path / "a")
    world_a = World(tmp_path / "a" / "world.db")
    store_b = _new_store(tmp_path / "b")
    world_b = World(tmp_path / "b" / "world.db")
    try:
        counters_a = _drive_with_loop(store_a, world_a)
        counters_b = _drive_with_executor(store_b, world_b)

        events_a = _pipeline_events(store_a)
        events_b = _pipeline_events(store_b)
        assert [e[1] for e in events_a] == EXPECTED_EVENT_TYPES, (
            f"事件序列与预期形状不符：{[e[1] for e in events_a]}"
        )
        assert events_a == events_b, "loop 与 executor 的管线事件不一致"

        # 外部账本形状：同一条 run 的副作用集合（含 idempotency_key）必须一致
        assert _effect_rows(world_a) == _effect_rows(world_b)
        assert len(_effect_rows(world_a)) == 1, "只有被批准的那次写应该产生副作用"

        # 工具侧计数：执行 2 次（只读 + 被批准的写）、拒 1、失败 1
        assert counters_a == counters_b, (counters_a, counters_b)
        assert counters_a == (2, 0, 0, 1, 0, 0, 1), counters_a
    finally:
        for store, world in ((store_a, world_a), (store_b, world_b)):
            store.close()
            world.close()


def test_loop_delegates_to_the_shared_executor(tmp_path: Path, monkeypatch: Any) -> None:
    """loop 侧没有第二条执行路径：把 executor 的 execute 换成记录型包装，它必须被调用。"""
    seen: list[str] = []
    original = ToolExecutor.execute

    def spy(self: ToolExecutor, ctx: RunContext, request: ToolCallRequest, counters: Any):
        seen.append(request.tool_call_id)
        return original(self, ctx, request, counters)

    monkeypatch.setattr(ToolExecutor, "execute", spy)

    store = _new_store(tmp_path)
    world = World(tmp_path / "world.db")
    try:
        _drive_with_loop(store, world)
    finally:
        store.close()
        world.close()

    # tc_write 出现两次是**预期**的：第一次停在审批门（抛 InterruptSignal），
    # 第二次是审批之后由恢复段执行。
    assert seen == ["tc_read", "tc_write", "tc_write", "tc_bad", "tc_ghost"], seen


def test_both_counter_shapes_satisfy_the_protocol() -> None:
    """loop 的计数器和外部驱动方的计数器都满足 ``CounterSink``（鸭子类型成立）。"""
    assert isinstance(_Counters(), CounterSink)
    assert isinstance(_ExecutorCounters(), CounterSink)


# ------------------------------------------------- 返回值不是第二权威

def _results_for(store: SqliteStore, tool_call_id: str) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in store.effective_events(BRANCH_ID)
        if event.type == "tool_result" and event.payload.get("tool_call_id") == tool_call_id
    ]


def _assert_outcome_matches_the_log(
    store: SqliteStore, outcome: Any, *, expected_status: str, expected_replayed: bool
) -> None:
    """``ToolOutcome`` 是**可读形状**，不是第二权威：必须与日志里那条 tool_result 逐字一致。

    这条不变式是 M1 抽取时新增的返回通道的守门人：只断言事件日志的用例抓不到
    "返回值与日志不一致"的改动（变异门禁实测：新增幸存变异集中在这些 return 上）。
    """
    rows = _results_for(store, outcome.tool_call_id)
    assert rows, f"{outcome.tool_call_id} 只有返回值、日志里没有结论——返回值成了第二权威"
    last = rows[-1]
    assert outcome.status == expected_status
    assert outcome.status == last["status"], f"返回值 {outcome.status} != 日志 {last['status']}"
    assert outcome.result == last["result"], "返回的 result 与日志不一致"
    assert outcome.error_class == last["error_class"], "返回的 error_class 与日志不一致"
    assert outcome.replayed is expected_replayed
    # 一次调用至多一条结论（INV-003），重放不得追加新的事件
    assert len(rows) == 1


def test_every_outcome_matches_its_event_across_all_pipeline_branches(tmp_path: Path) -> None:
    """把管线的每条出口都走一遍，逐条核对返回值与日志一致。"""
    store = _new_store(tmp_path)
    world = World(tmp_path / "world.db")
    try:
        registry = _registry(world)

        def fragile_ok(args: dict, key: str) -> dict:
            return {"ok": True}

        def fragile_boom(args: dict, key: str) -> dict:
            raise RuntimeError("下游炸了")

        registry.register(
            Tool(name="ok_read", effect=Effect.READ, fn=fragile_ok, description="正常的只读")
        )
        registry.register(
            Tool(name="boom_read", effect=Effect.READ, fn=fragile_boom, description="会抛的只读")
        )
        registry.register(
            Tool(
                name="boom_write",
                effect=Effect.WRITE_NONIDEMPOTENT,
                fn=fragile_boom,
                description="会抛的非幂等写（outbox 开 ⇒ 只能判 unknown）",
            )
        )
        executor = ToolExecutor(store, registry=registry, outbox=True)
        counters = _ExecutorCounters()
        ctx = RunContext(RUN_ID, THREAD_ID, BRANCH_ID)

        cases = [
            ("tc_ok", "ok_read", {}, "executed", False),
            ("tc_bad_policy", "export_logs", {"limit": 999}, "rejected", False),
            ("tc_ghost", "no_such_tool", {}, "failed", False),
            ("tc_boom_read", "boom_read", {}, "failed", False),
            ("tc_boom_write", "boom_write", {}, "unknown", False),
        ]
        for call_id, tool, args, status, replayed in cases:
            outcome = executor.execute(
                ctx, ToolCallRequest(tool_call_id=call_id, tool=tool, args=args), counters
            )
            _assert_outcome_matches_the_log(
                store, outcome, expected_status=status, expected_replayed=replayed
            )

        # 重放：日志里已有结论 → 不追加事件，返回值必须复述日志里的那条
        replay = executor.execute(
            ctx, ToolCallRequest(tool_call_id="tc_ok", tool="ok_read", args={}), counters
        )
        _assert_outcome_matches_the_log(
            store, replay, expected_status="executed", expected_replayed=True
        )

        # 审批后执行：返回值同样是日志的复述
        gated = ToolCallRequest(
            tool_call_id="tc_gate", tool="scale_pool", args={"service": "payment", "size": 8}
        )
        with pytest.raises(InterruptSignal):
            executor.execute(ctx, gated, counters)
        executor.decide(ctx, actor="human:parity")
        report = executor.resume_open_calls(ctx, counters)
        assert [outcome.status for outcome in report.outcomes] == ["executed"]
        assert report.pending is None
        _assert_outcome_matches_the_log(
            store, report.outcomes[0], expected_status="executed", expected_replayed=False
        )

        # 同键不同参：旁路校验字段否决，返回值仍是日志的复述
        from harness.store import ToolCallStore
        from harness.tools import canonical_args_sha256, idempotency_key

        key = idempotency_key(RUN_ID, BRANCH_ID, "tc_conflict")
        ToolCallStore(store).record(
            tool_call_id="tc_conflict_other",
            run_id=RUN_ID,
            branch_id=BRANCH_ID,
            tool="ok_read",
            args={"other": True},
            args_sha256=canonical_args_sha256({"other": True}),
            idempotency_key=key,
            effect=Effect.READ.value,
            status="executed",
            result={"ok": True},
        )
        conflict = executor.execute(
            ctx, ToolCallRequest(tool_call_id="tc_conflict", tool="ok_read", args={}), counters
        )
        _assert_outcome_matches_the_log(
            store, conflict, expected_status="rejected", expected_replayed=False
        )
        assert conflict.error_class == "args_sha256_mismatch"

        # 既有意图行 pending + 探针说"已生效"：**不重跑**，结论来自对账
        pending_call = ToolCallRequest(
            tool_call_id="tc_pending", tool="scale_pool", args={"service": "payment", "size": 4}
        )
        pending_key = idempotency_key(RUN_ID, BRANCH_ID, pending_call.tool_call_id)
        ToolCallStore(store).begin(
            tool_call_id=pending_call.tool_call_id,
            run_id=RUN_ID,
            branch_id=BRANCH_ID,
            tool=pending_call.tool,
            args=pending_call.args,
            args_sha256=canonical_args_sha256(pending_call.args),
            idempotency_key=pending_key,
            effect=Effect.WRITE_NONIDEMPOTENT.value,
        )
        # 下游那边效果已经发生了（崩溃窗口：效果在、runtime 记录没落）
        world.apply(
            operation=pending_call.tool,
            payload=dict(pending_call.args),
            idempotency_key=pending_key,
            idempotent_impl=False,
        )
        effects_before_reconcile = world.total_effects()
        reconciled = executor.execute(ctx, pending_call, counters)
        _assert_outcome_matches_the_log(
            store, reconciled, expected_status="executed", expected_replayed=True
        )
        assert reconciled.result is not None
        assert reconciled.result["reconstructed_from_probe"] is True
        assert world.total_effects() == effects_before_reconcile, (
            "对账路径绝不能再写一次副作用（结论来自探针，不是重跑）"
        )

        # 防御分支：去重表已有 executed 行、但日志里那条调用还没闭合（例如结果事件缺失）
        # ——必须按既有行的结论重放，不允许再执行一次。
        orphan_call = ToolCallRequest(tool_call_id="tc_orphan", tool="ok_read", args={})
        ToolCallStore(store).record(
            tool_call_id=orphan_call.tool_call_id,
            run_id=RUN_ID,
            branch_id=BRANCH_ID,
            tool=orphan_call.tool,
            args=orphan_call.args,
            args_sha256=canonical_args_sha256(orphan_call.args),
            idempotency_key=idempotency_key(RUN_ID, BRANCH_ID, orphan_call.tool_call_id),
            effect=Effect.READ.value,
            status="executed",
            result={"from": "dedup_row"},
        )
        orphan = executor.execute(ctx, orphan_call, counters)
        _assert_outcome_matches_the_log(
            store, orphan, expected_status="executed", expected_replayed=True
        )
        assert orphan.result == {"from": "dedup_row"}, "重放必须复述去重行里的结论"
    finally:
        store.close()
        world.close()


# ---------------------------------------------------------------- 结构证明

# 管线私有件：只允许出现在 harness/execution.py。驱动方（loop 与 integrations/）
# 一旦出现其中之一，就说明"共用一条管线"被破坏了。
#
# 注意判据是**调用形状**而不是窗口名：驱动方可以（也必须）**命名**窗口来配置注入
# （`integrations/mcp_crash_demo.py` 的 `CRASH_WINDOW` 就是），但不得自己**命中**它。
# loop 侧的 `.hit("after_resume")` 是合法的（恢复流程归 loop），不在下面这张表里。
PIPELINE_PATTERNS = (
    'hit("pre_tool_exec")',
    'hit("post_tool_effect_pre_record")',
    'hit("post_record_pre_commit")',
    'hit("post_approval_pre_exec")',
    "tool.fn(",
    "_tool_calls.begin(",
    "_tool_calls.complete(",
    "_tool_calls.mark_unknown(",
    "_tool_calls.record(",
)

DRIVER_MODULES = ("harness/loop.py",)
INTEGRATION_DIR = PROJECT_ROOT / "integrations"


def scan_for_pipeline_private_parts(source: str) -> list[str]:
    """返回源码里出现的管线私有件。纯函数：正对照可以直接喂合成源码。"""
    return [pattern for pattern in PIPELINE_PATTERNS if pattern in source]


def test_pipeline_lives_only_in_the_execution_module() -> None:
    """四个崩溃窗口与 outbox 状态机都在 execution.py，且驱动方一处都没有。"""
    execution_source = (PROJECT_ROOT / "harness" / "execution.py").read_text(encoding="utf-8")
    missing = [
        f'hit("{window}")'
        for window in ("pre_tool_exec", "post_tool_effect_pre_record", "post_record_pre_commit")
        if f'hit("{window}")' not in execution_source
    ]
    assert not missing, f"execution.py 里找不到这些命中点（扫描会失去意义）：{missing}"

    for relative in DRIVER_MODULES:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        hits = scan_for_pipeline_private_parts(source)
        assert not hits, f"{relative} 里出现了管线私有件：{hits}"

    for path in sorted(INTEGRATION_DIR.glob("*.py")):
        hits = scan_for_pipeline_private_parts(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.relative_to(PROJECT_ROOT)} 里出现了管线私有件：{hits}"


def test_pipeline_scan_flags_a_synthetic_violation() -> None:
    """正对照：扫描函数对合成坏源码必须报违规，否则"全绿"毫无意义。"""
    sneaky = "def handle(self, tool):\n    return tool.fn(args, key)  # 绕过七步\n"
    assert scan_for_pipeline_private_parts(sneaky) == ["tool.fn("]
    window_hit = 'def drive(self):\n    self._chaos.hit("post_tool_effect_pre_record")\n'
    assert scan_for_pipeline_private_parts(window_hit) == [
        'hit("post_tool_effect_pre_record")'
    ], "在驱动方自己命中管线窗口，扫描器必须报"
    # 只**命名**窗口（配置注入）不算违规：崩溃演示必须能写出要杀哪个窗口
    naming = 'CRASH_WINDOW = "post_tool_effect_pre_record"\n'
    assert scan_for_pipeline_private_parts(naming) == []
    assert scan_for_pipeline_private_parts("def ok(self):\n    return 1\n") == []


def test_interrupt_request_is_reexported_from_loop() -> None:
    """搬走的是实现，不是契约：既有导入路径必须仍然可用（文档与回归用例都引用它）。"""
    from harness import InterruptRequest as package_level
    from harness.loop import InterruptRequest as loop_level

    assert package_level is InterruptRequest
    assert loop_level is InterruptRequest
