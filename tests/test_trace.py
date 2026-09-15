"""Trace 投影：事件日志 → span 树（事件是权威，trace 是投影）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
from fakeworld.world import World
from harness.chaos import Chaos
from harness.ids import new_id
from harness.llm import ScriptedLLMClient
from harness.loop import Loop
from harness.store import SqliteCheckpointSaver, SqliteStore
from harness.trace import Trace, build_trace, render_text, summary_of, trace_to_json

TASK = "支付服务 P99 告警，请定位并处置。"


def _run(tmp_path: Path) -> tuple[SqliteStore, str]:
    """跑一次"审批 + 执行"的完整流程，产出可投影的事件日志。"""
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    world = World(tmp_path / "world.db")
    registry = build_registry(world, idempotent_impl=False)
    llm = ScriptedLLMClient(ScriptedModel(SCENARIO_POOL_EXHAUSTION))
    loop = Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=llm,
        registry=registry,
        chaos=Chaos.disabled(),
    )
    run_id, thread_id, branch_id = new_id("run"), new_id("thr"), new_id("br")
    store.create_run(run_id, thread_id=thread_id)
    store.create_branch(branch_id, run_id)
    loop.start(run_id=run_id, thread_id=thread_id, branch_id=branch_id, task=TASK)
    loop.approve(run_id=run_id, thread_id=thread_id, branch_id=branch_id)
    loop.resume(run_id=run_id, thread_id=thread_id, branch_id=branch_id)
    world.close()
    return store, branch_id


def test_trace_has_expected_span_shape(tmp_path: Path) -> None:
    store, branch_id = _run(tmp_path)
    try:
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()
    assert isinstance(trace, Trace)
    names = [span.name.split(" ")[0] for span in trace.flatten()]
    assert names[0] == "invoke_agent"
    assert names.count("chat") == 3  # 取证 / 结论+提议 / 终局
    assert names.count("execute_tool") == 2  # 读 + 写
    assert names.count("human_approval") == 1


def test_tool_span_carries_idempotency_and_effect(tmp_path: Path) -> None:
    store, branch_id = _run(tmp_path)
    try:
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()
    write_span = next(s for s in trace.flatten() if s.name == "execute_tool scale_pool")
    assert write_span.attributes["harness.effect"] == "write_nonidempotent"
    assert write_span.attributes["harness.idempotency_key"].startswith("idem_")
    assert write_span.attributes["harness.result_status"] == "executed"
    assert write_span.end is not None and write_span.end >= write_span.start


def test_approval_span_records_the_decision(tmp_path: Path) -> None:
    store, branch_id = _run(tmp_path)
    try:
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()
    approval = next(s for s in trace.flatten() if s.name == "human_approval")
    assert approval.attributes["harness.approval.decision"] == "approved"
    assert approval.attributes["harness.approval.pending"] is False
    assert approval.attributes["harness.tool.name"] == "scale_pool"
    # 审批事实挂在被审批的那次调用上，而不是根 span 上
    assert "harness.approved_args_sha256" in approval.attributes
    assert "harness.approved_args_sha256" not in trace.root.attributes


def test_cost_is_aggregated_on_the_root(tmp_path: Path) -> None:
    store, branch_id = _run(tmp_path)
    try:
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()
    assert trace.root.attributes["harness.budget.spent_usd"] > 0


def test_render_and_summary_are_readable(tmp_path: Path) -> None:
    store, branch_id = _run(tmp_path)
    try:
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()
    text = render_text(trace)
    assert "invoke_agent" in text and "human_approval" in text
    summary = summary_of(trace)
    assert summary["spans"] == trace.span_count
    assert summary["by_kind"]["chat"] == 3


def test_otel_export_has_required_fields(tmp_path: Path) -> None:
    import json

    store, branch_id = _run(tmp_path)
    try:
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()
    payload = json.loads(trace_to_json(trace, otel=True))
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == trace.span_count
    for span in spans:
        assert span["traceId"] and span["spanId"]
        assert span["startTimeUnixNano"] <= span["endTimeUnixNano"]
        assert span["kind"] in (1, 3)


def test_projection_is_pure_and_repeatable(tmp_path: Path) -> None:
    store, branch_id = _run(tmp_path)
    try:
        events = store.effective_events(branch_id)
        first = build_trace(events)
        second = build_trace(events)
    finally:
        store.close()
    assert [s.attributes for s in first.flatten()] == [s.attributes for s in second.flatten()]
    assert first.root.attributes == second.root.attributes


def test_empty_log_projects_to_a_single_root_span(tmp_path: Path) -> None:
    """空日志（新分支）也要能投影：只得到根 span，不能抛异常。"""
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        run_id, branch_id = new_id("run"), new_id("br")
        store.create_run(run_id, thread_id=new_id("thr"))
        store.create_branch(branch_id, run_id)
        assert store.effective_events(branch_id) == []
        trace = build_trace([])
        assert trace.span_count == 1
        assert trace.root.name.startswith("invoke_agent")
    finally:
        store.close()


def test_unknown_branch_is_an_error_not_an_empty_log(tmp_path: Path) -> None:
    """未知分支必须报错：静默返回空日志会把"读错分支"伪装成"还没开始"。"""
    from harness.store import StoreError

    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        with pytest.raises(StoreError, match="unknown branch"):
            store.effective_events("br_missing")
    finally:
        store.close()
