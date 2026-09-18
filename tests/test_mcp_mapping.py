"""MCP 工具服务的**无 SDK** 用例：映射、审批状态机、失败语义、恢复。

为什么值得单独一层：``[mcp]`` extra 只提供传输，治理语义全在业务层
（``handle_tools_call`` 调 ``ToolExecutor``）。把这一层放进默认 pytest，
CI 即使没装 extra 也能看住"审批/拒绝/改参/未注册工具"这些真正的承诺面。

不 spawn 子进程、不联网：每个用例都在进程内建一个 run 目录。
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from fakeworld.world import World
from harness.events import Source, TreeEventType
from harness.state import RunStatus
from harness.tools import Effect, Tool, ToolRegistry
from integrations import mcp_server
from integrations.mcp_server import (
    APPROVE_TOOL,
    GOVERNANCE_TOOL_NAMES,
    LIST_TOOL,
    McpUnavailableError,
    build_environment,
    handle_tools_call,
    list_all_tool_specs,
    list_tool_specs,
)

WRITE_ARGS = {"service": "payment", "size": 64}


@pytest.fixture()
def env(tmp_path: Path):
    environment = build_environment(tmp_path / "run")
    try:
        yield environment
    finally:
        environment.close()


def _log(env) -> list:
    return env.store.effective_events(env.ctx.branch_id)


def _types(env) -> list[str]:
    return [event.type for event in _log(env)]


# ------------------------------------------------------------------ 工具映射


def test_tool_specs_mirror_the_registry(env) -> None:
    """tools/list 由注册表直接映射：名字、描述、schema 都来自工具自述。"""
    specs = list_tool_specs(env.registry)
    assert [spec["name"] for spec in specs] == [
        "create_ticket",
        "fetch_logs",
        "query_metrics",
        "scale_pool",
    ]
    by_name = {spec["name"]: spec for spec in specs}
    scale = env.registry.get("scale_pool")
    assert by_name["scale_pool"]["inputSchema"] is scale.parameters, (
        "schema 必须与下发给真实模型的那份同源（同一对象）"
    )
    for spec in specs:
        assert spec["inputSchema"]["type"] == "object"


def test_undeclared_schema_falls_back_to_the_shared_empty_table() -> None:
    """未声明 schema 的工具回退形状只有一处定义，MCP 与 live 路径同源。"""
    bare = Tool(name="noop", effect=Effect.READ, fn=lambda args, key: {}, description="空的")
    specs = list_tool_specs(ToolRegistry({"noop": bare}))
    assert specs[0]["inputSchema"] == {"type": "object", "properties": {}}


def test_all_specs_include_governance_tools_sorted(env) -> None:
    """治理工具对在列，且整体按名字排序（列表稳定，客户端可缓存）。"""
    specs = list_all_tool_specs(env.registry)
    names = [spec["name"] for spec in specs]
    assert names == sorted(names)
    assert set(GOVERNANCE_TOOL_NAMES) <= set(names)
    approve = next(spec for spec in specs if spec["name"] == APPROVE_TOOL)
    assert approve["inputSchema"]["required"] == ["tool_call_id"]
    assert approve["inputSchema"]["properties"]["decision"]["enum"] == ["approve", "reject"]


def test_governance_tool_name_collision_is_refused(tmp_path: Path) -> None:
    """注册表里若出现治理工具同名工具，必须在建环境时就炸——不能静默顶替审批接口。"""
    registry = ToolRegistry(
        {LIST_TOOL: Tool(name=LIST_TOOL, effect=Effect.READ, fn=lambda args, key: {})}
    )
    with pytest.raises(ValueError, match="重名"):
        mcp_server._reject_governance_name_collision(registry)


# ------------------------------------------------------------------ run 来源


def test_run_is_created_with_an_explicit_provenance_event(env) -> None:
    """服务创建的 run 必须写明来源：第一条树节点说清"谁建的这个 run"。"""
    assert env.created_run is True
    events = _log(env)
    assert len(events) == 1
    assert events[0].type == TreeEventType.USER_MESSAGE.value
    assert events[0].source is Source.SYSTEM, "不假装有人类说话"
    assert "integrations.mcp_server" in events[0].payload["text"]


def test_existing_run_is_reused_and_ids_json_is_shared(tmp_path: Path) -> None:
    """已有 run 目录直接接续（ids.json 与 experiments.worker 同格式）。"""
    root = tmp_path / "run"
    first = build_environment(root)
    ids = json.loads((root / "ids.json").read_text(encoding="utf-8"))
    first.close()

    second = build_environment(root)
    try:
        assert second.created_run is False
        assert second.ctx.run_id == ids["run_id"]
        assert second.ctx.branch_id == ids["branch_id"]
        assert len(_log(second)) == 1, "接续不该再写一条来源事件"
    finally:
        second.close()


# ------------------------------------------------------------------ 审批形态


def test_read_call_executes_and_reports_the_outcome(env) -> None:
    payload = handle_tools_call(env, "query_metrics", {"service": "payment"})
    assert payload["status"] == "executed"
    assert payload["replayed"] is False
    assert payload["result"]["service"] == "payment"
    assert mcp_server.is_error_status(payload["status"]) is False
    assert env.world.total_effects() == 0
    assert env.executor.derived_state(env.ctx).status is RunStatus.RUNNING


def test_write_call_stops_at_the_gate_then_runs_after_approval(env) -> None:
    """写操作停在审批门 → 可读待审批 → approve 后执行；副作用恰好一次。"""
    gated = handle_tools_call(env, "scale_pool", WRITE_ARGS)
    assert gated["status"] == "pending_approval"
    assert gated["tool"] == "scale_pool"
    assert gated["args"] == WRITE_ARGS
    assert gated["args_sha256"]
    assert gated["interrupt_index"] == 0
    assert gated["approval_ttl_seconds"] > 0
    assert env.world.total_effects() == 0, "停在审批门时绝不产生副作用"
    assert env.executor.derived_state(env.ctx).status is RunStatus.WAITING_HUMAN

    listed = handle_tools_call(env, LIST_TOOL, {})
    assert [item["tool_call_id"] for item in listed["pending"]] == [gated["tool_call_id"]]
    assert listed["pending"][0]["is_current"] is True

    approved = handle_tools_call(
        env, APPROVE_TOOL, {"tool_call_id": gated["tool_call_id"], "decision": "approve"}
    )
    assert approved["status"] == "approved"
    assert approved["edited"] is False
    assert [item["status"] for item in approved["executed"]] == ["executed"]
    assert env.world.total_effects() == 1
    assert handle_tools_call(env, LIST_TOOL, {})["pending"] == []


def test_reject_closes_the_call_without_side_effect(env) -> None:
    gated = handle_tools_call(env, "scale_pool", WRITE_ARGS)
    rejected = handle_tools_call(
        env, APPROVE_TOOL, {"tool_call_id": gated["tool_call_id"], "decision": "reject"}
    )
    assert rejected["status"] == "rejected"
    assert rejected["executed"] == []
    assert env.world.total_effects() == 0
    statuses = [
        event.payload["status"] for event in _log(env) if event.type == "tool_result"
    ]
    assert statuses == ["rejected"]


def test_edit_approval_supersedes_and_binds_to_the_new_call(env) -> None:
    """改参批准 = 原调用 superseded + 新 tool_call_id（因此新幂等键）+ 新参数执行。"""
    gated = handle_tools_call(env, "scale_pool", WRITE_ARGS)
    edited = handle_tools_call(
        env,
        APPROVE_TOOL,
        {
            "tool_call_id": gated["tool_call_id"],
            "decision": "approve",
            "approved_args": {"service": "payment", "size": 32},
        },
    )
    assert edited["edited"] is True
    assert edited["tool_call_id"] == f"{gated['tool_call_id']}__edit1"
    assert [item["status"] for item in edited["executed"]] == ["executed"]

    results = {
        event.payload["tool_call_id"]: event.payload["status"]
        for event in _log(env)
        if event.type == "tool_result"
    }
    assert results[gated["tool_call_id"]] == "superseded"
    assert results[edited["tool_call_id"]] == "executed"
    assert env.world.total_effects() == 1


def test_approve_with_a_mismatched_tool_call_id_is_refused(env) -> None:
    gated = handle_tools_call(env, "scale_pool", WRITE_ARGS)
    payload = handle_tools_call(env, APPROVE_TOOL, {"tool_call_id": "tc_someone_else"})
    assert payload["status"] == "rejected"
    assert payload["error_class"] == "tool_call_id_mismatch"
    assert env.world.total_effects() == 0
    assert handle_tools_call(env, LIST_TOOL, {})["pending"][0]["tool_call_id"] == (
        gated["tool_call_id"]
    )


def test_approve_without_pending_call_is_readable(env) -> None:
    payload = handle_tools_call(env, APPROVE_TOOL, {"tool_call_id": "tc_none"})
    assert payload["error_class"] == "no_pending_interrupt"
    assert handles_readably(payload)


def test_pending_order_follows_the_log_not_the_generated_ids(env, monkeypatch) -> None:
    """回归：待审批队列的顺序必须按"谁先来"（日志先后），**不能**按 tool_call_id。

    修复前 ``pending_approvals`` 用 ``sorted(state.open_tool_calls)`` 排序，而
    ``tool_call_id`` 由 ``new_id`` 生成、每次运行都不同 —— 队列顺序随运行而变。
    CI 上实测过：同一条用例在开发机与 runner 上得到相反顺序（``[True, False]`` vs
    ``[False, True]``），也就是"当前这条排第一"这条断言本身在不稳定地抛硬币。

    本用例把 id 固定成**后到的那条字典序更小**（``tc_zzz_first`` / ``tc_aaa_second``），
    于是"按 id 排序"必然得到相反顺序 —— 修复前这条用例必红，修复后按日志先后必绿。
    """
    ids = iter(["tc_zzz_first", "tc_aaa_second"])
    monkeypatch.setattr(mcp_server, "new_id", lambda _prefix: next(ids))

    first = handle_tools_call(env, "scale_pool", {"service": "payment", "size": 64})
    second = handle_tools_call(env, "scale_pool", {"service": "search", "size": 8})
    assert first["tool_call_id"] == "tc_zzz_first"
    assert second["queued_tool_call_id"] == "tc_aaa_second"

    pending = handle_tools_call(env, LIST_TOOL, {})["pending"]
    assert [item["tool_call_id"] for item in pending] == ["tc_zzz_first", "tc_aaa_second"]
    assert [item["is_current"] for item in pending] == [True, False]


def handles_readably(payload: dict) -> bool:
    return isinstance(payload.get("message"), str) and bool(payload["message"])


def test_second_write_queues_behind_the_current_interrupt(env) -> None:
    """INV-004 保证同一时刻至多一条 interrupt：第二条调用要排队，不能被丢掉。"""
    first = handle_tools_call(env, "scale_pool", {"service": "payment", "size": 64})
    second = handle_tools_call(env, "scale_pool", {"service": "search", "size": 8})
    assert second["status"] == "pending_approval"
    assert second["tool_call_id"] == first["tool_call_id"], "interrupt 仍属于第一条"
    assert "note" in second and first["tool_call_id"] in second["note"]

    # 排队的那条要能自报家门：否则客户端不知道"我这次调用"在日志里叫什么
    assert second["queued_tool_call_id"] != first["tool_call_id"]
    pending = handle_tools_call(env, LIST_TOOL, {})["pending"]
    assert [item["is_current"] for item in pending] == [True, False]
    assert [item["tool_call_id"] for item in pending] == [
        first["tool_call_id"],
        second["queued_tool_call_id"],
    ]
    assert pending[1]["interrupt_id"] is None
    assert "等待" in pending[1]["reason"]

    # 先批第一条：批准那一刻就把第二条推进审批门，并在同一份回执里告知
    first_round = handle_tools_call(
        env, APPROVE_TOOL, {"tool_call_id": first["tool_call_id"], "decision": "approve"}
    )
    assert [item["status"] for item in first_round["executed"]] == ["executed"]
    assert first_round["next_pending"]["tool_call_id"] == second["queued_tool_call_id"]
    assert env.world.total_effects() == 1, "第二条还没批，不该有副作用"

    # 再批第二条：各自恰好一次副作用
    second_round = handle_tools_call(
        env, APPROVE_TOOL, {"tool_call_id": second["queued_tool_call_id"]}
    )
    assert [item["status"] for item in second_round["executed"]] == ["executed"]
    assert env.world.total_effects() == 2


# ------------------------------------------------------------------ 失败语义


def test_unregistered_tool_is_recorded_and_closed(env) -> None:
    """未注册工具：可读错误 + 日志里留下并闭合（"未注册"不等于"没发生"）。"""
    payload = handle_tools_call(env, "no_such_tool", {"x": 1})
    assert payload["status"] == "failed"
    assert payload["error_class"] == "unknown_tool"
    assert mcp_server.is_error_status(payload["status"]) is True
    results = [
        event.payload["status"] for event in _log(env) if event.type == "tool_result"
    ]
    assert results == ["failed"]
    assert env.executor.derived_state(env.ctx).open_tool_calls == {}


def test_arg_policy_violation_after_approval_leaves_no_intent_row(env) -> None:
    """参数越出安全域：执行前被拒，不留意图行、不产生副作用。"""
    gated = handle_tools_call(env, "scale_pool", {"service": "payment", "size": 9999})
    payload = handle_tools_call(
        env, APPROVE_TOOL, {"tool_call_id": gated["tool_call_id"], "decision": "approve"}
    )
    assert [item["status"] for item in payload["executed"]] == ["rejected"]
    assert payload["executed"][0]["error_class"] == "arg_policy_violation"
    assert env.world.total_effects() == 0
    assert env.store._conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0


def test_tool_exception_on_a_read_tool_becomes_failed(env) -> None:
    """异常绝不穿透：只读工具按 failed 处置，run 不会永久挂在 RUNNING。"""

    def boom(args: dict, key: str) -> dict:
        raise RuntimeError("下游炸了")

    env.registry.register(
        Tool(name="fragile", effect=Effect.READ, fn=boom, description="会抛的只读工具")
    )
    payload = handle_tools_call(env, "fragile", {})
    assert payload["status"] == "failed"
    assert payload["error_class"] == "tool_error:RuntimeError"
    assert mcp_server.is_error_status(payload["status"]) is True


# ------------------------------------------------------------------ 恢复


def test_recover_on_start_returns_the_approved_call(tmp_path: Path) -> None:
    """启动即恢复：审批过但还没执行的调用，重启后被续跑且不重跑已执行的。"""
    root = tmp_path / "run"
    first = build_environment(root)
    gated = handle_tools_call(first, "scale_pool", WRITE_ARGS)
    assert gated["status"] == "pending_approval"
    first.executor.decide(first.ctx, actor="human:test")  # 批准但**不**续跑
    assert first.world.total_effects() == 0
    first.close()

    second = build_environment(root)
    try:
        report = mcp_server.recover_on_start(second)
        assert [outcome.status for outcome in report.outcomes] == ["executed"]
        assert report.pending is None
        assert second.world.total_effects() == 1
        assert second.executor.derived_state(second.ctx).open_tool_calls == {}
        # 再恢复一次：日志里已有结论 → 重放，不产生第二个副作用
        again = mcp_server.recover_on_start(second)
        assert again.outcomes == () and again.pending is None
        assert second.world.total_effects() == 1
    finally:
        second.close()


def test_recover_on_start_reports_a_pending_interrupt_instead_of_duplicating(
    tmp_path: Path,
) -> None:
    """未获批的调用在重启后仍是"待审批"，不会多发一条 interrupt（INV-004）。"""
    root = tmp_path / "run"
    first = build_environment(root)
    handle_tools_call(first, "scale_pool", WRITE_ARGS)
    interrupts_before = sum(1 for event in _log(first) if event.type == "interrupt")
    first.close()

    second = build_environment(root)
    try:
        recovered = mcp_server.recover_on_start(second)
        assert recovered.pending is not None
        assert recovered.pending.tool == "scale_pool"
        assert recovered.outcomes == ()
        interrupts_after = sum(1 for event in _log(second) if event.type == "interrupt")
        assert interrupts_after == interrupts_before == 1
        assert second.world.total_effects() == 0
    finally:
        second.close()


def test_run_status_projection_follows_the_fold(env) -> None:
    """``runs.status`` 是看板摘要：每次操作后按日志折叠刷新，不是新权威。"""
    assert env.store.get_run(env.ctx.run_id).status == "running"
    handle_tools_call(env, "scale_pool", WRITE_ARGS)
    state = mcp_server.refresh_run_status(env)
    assert state.status is RunStatus.WAITING_HUMAN
    assert env.store.get_run(env.ctx.run_id).status == "waiting_human"


# ------------------------------------------------------------------ 缺 extra


def test_import_does_not_require_the_extra() -> None:
    """缺 [mcp] extra 时 import 本模块必须照常成功（SDK 的 import 在函数体内）。"""
    source = (Path(mcp_server.__file__)).read_text(encoding="utf-8")
    top = source.split("def _load_sdk")[0]
    assert "import mcp" not in top, "MCP SDK 的 import 必须在函数体内（照 harness/otel.py）"


def test_missing_extra_gives_a_readable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 extra 时给的是可读提示（含 extra 名），不是 ImportError 栈。"""
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(McpUnavailableError) as excinfo:
        mcp_server._load_sdk()
    assert "[mcp]" in str(excinfo.value)


def test_list_tools_cli_needs_no_sdk(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """``--list-tools`` 是纯映射路径：不装 extra 也能跑（claim 命令用的就是它）。"""
    out = tmp_path / "tools.json"
    code = mcp_server.main(
        ["--run-dir", str(tmp_path / "run"), "--list-tools", "--json-out", str(out)]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    names = [spec["name"] for spec in payload["tools"]]
    assert names == ["approve", "create_ticket", "fetch_logs", "list_pending_approvals",
                     "query_metrics", "scale_pool"]
    assert json.loads(capsys.readouterr().out)["tools"] == payload["tools"]


def test_environment_builds_world_under_the_run_dir(tmp_path: Path) -> None:
    """服务把外部账本放在 run 目录里——判分读的就是它（连 -wal 一起快照）。"""
    root = tmp_path / "run"
    env = build_environment(root)
    try:
        assert isinstance(env.world, World)
        assert (root / "world.db").exists()
        assert (root / "runtime.db").exists()
    finally:
        env.close()
