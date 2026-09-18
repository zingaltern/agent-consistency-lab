"""可观测导出的可执行口径：本地接收器 + 三条口径的**正向与反向**断言。

这一组不需要 `[otel]` extra、不联网、不需要 Docker：走的是
``harness/otel.py::post_otlp_json``（OTLP/JSON over HTTP）这条降级路径。
正因为它离线可跑，"三条口径"才不是文档里的一句声明，而是会被断言的东西：

* 时长口径：`harness.duration_is_derived` **恰好**标在模型 span 上，不多不少；
* 事后导入：恰好一次 POST 到 `/v1/traces`（不是流）；
* trace 里没有账本：反向断言"找不到账本属性"。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from integrations.otlp_local_sink import (
    _SinkHandler,
    build_demo_run,
    round_trip,
    summarize_otlp,
)


@pytest.fixture(scope="module")
def model_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一个最小的**模型驱动** run（只有它会产出模型 span）。"""
    return build_demo_run(tmp_path_factory.mktemp("otel-demo"))


@pytest.fixture(scope="module")
def exported(model_run: Path) -> dict:
    return round_trip(model_run)


def test_round_trip_posts_exactly_once_to_the_otlp_http_path(
    model_run: Path, exported: dict
) -> None:
    """事后导入：一次 POST 到 /v1/traces，200；不是流式推送。"""
    assert exported["request_paths"] == ["/v1/traces"], exported["request_paths"]
    assert exported["http_status"] == 200
    assert exported["span_count"] == len(exported["spans"])
    assert exported["span_count"] >= 5
    assert exported["tool_span_count"] >= 2
    assert exported["model_span_count"] >= 1


def test_derived_duration_flag_lands_exactly_on_model_spans(
    model_run: Path, exported: dict
) -> None:
    """时长口径：模型 span 是事件间隔推导的（带标记）；工具/审批 span 不带。"""
    assert exported["derived_flag_matches_model_spans"] is True
    assert exported["derived_duration_span_count"] == exported["model_span_count"] >= 1
    for span in exported["spans"]:
        if span["operation"] == "chat":
            assert str(span["duration_is_derived"]).lower() == "true", span["name"]
        else:
            assert span["duration_is_derived"] is None, span["name"]


def test_trace_carries_no_ledger_attributes(model_run: Path, exported: dict) -> None:
    """反向断言：trace 里**没有**账本。

    "副作用发生了几次"只在事件日志与外部账本（`world.db`）里；
    如果哪天有人往 trace 里塞了一个效果计数，这条会红——那正是要防的事。
    """
    assert exported["has_ledger_attributes"] is False
    keys = {key for span in exported["spans"] for key in span["attribute_keys"]}
    keys |= set(exported["resource_attribute_keys"])
    # `harness.effect` 是工具的**效果类别**（read / write_*），不是"发生了几次"——
    # 要防的是**计数类**字段：账本前缀、effects/occurrences/count 这类名字。
    ledger_like = [
        key
        for key in keys
        if key.startswith("harness.ledger")
        or "effects" in key
        or "occurrences" in key
        or key.endswith("_count")
    ]
    assert not ledger_like, f"trace 里出现了账本类字段：{sorted(ledger_like)}"
    # 审批"结论"在 trace 里体现为决策字段（谁批的、批没批），而不是账本
    assert any(key.startswith("harness.approval") for key in keys)


def test_mcp_driven_run_has_no_model_spans(tmp_path: Path) -> None:
    """口径要写准：MCP 驱动的 run 进程内没有模型，所以既没有模型 span 也没有推导时长。"""
    from integrations.mcp_server import build_environment, handle_tools_call

    run_dir = tmp_path / "mcp-run"
    env = build_environment(run_dir)
    try:
        handle_tools_call(env, "query_metrics", {"service": "payment"})
    finally:
        env.close()

    exported = round_trip(run_dir)
    assert exported["tool_span_count"] == 1
    assert exported["model_span_count"] == 0
    assert exported["derived_duration_span_count"] == 0
    assert exported["derived_flag_matches_model_spans"] is True
    assert exported["has_ledger_attributes"] is False


def test_receiver_rejects_a_non_json_body(tmp_path: Path) -> None:
    """接收器的**正对照**：它真的在解析请求体，而不是"收到什么都算成功"。"""
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SinkHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/traces",
            data=b"not json at all",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_summary_reports_structure_not_performance() -> None:
    """汇总里只有结构性字段：没有任何可以被读成性能的量（耗时、百分位、吞吐）。"""
    spans = [
        {
            "name": "execute_tool t",
            "span_id": "s",
            "parent_span_id": "",
            "start_ns": 1,
            "end_ns": 2,
            "attribute_keys": ["gen_ai.operation.name"],
            "operation": "execute_tool",
            "duration_is_derived": None,
            "idempotency_key": None,
            "approval_decision": None,
        }
    ]
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "x"}}]
                },
                "scopeSpans": [{"scope": {"name": "s"}, "spans": []}],
            }
        ]
    }
    summary = summarize_otlp(payload)
    forbidden = {"duration_ms", "latency", "p50", "p95", "throughput", "elapsed_ms"}
    assert not (forbidden & set(summary)), sorted(summary)
    assert summary["has_ledger_attributes"] is False
    assert spans[0]["start_ns"] < spans[0]["end_ns"]
    assert json.dumps(summary, ensure_ascii=False)  # 可序列化（claim 要读它）
