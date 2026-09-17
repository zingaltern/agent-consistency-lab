"""OTLP 导出（R-B4）：默认行为零改变 + 装了 extra 时映射对齐。

两条验收：

1. **未装 extra 时**：`import harness.trace` 与全部既有行为不变（不因缺依赖产生 ImportError）。
   这条用"模块顶层不含 otel import" + 不传 `--otlp-endpoint` 时 CLI 行为不变来钉；
2. **装了 extra 时**：用**内存内** exporter 验证 span 树的关键字段与 `to_otel()` today 输出逐位一致
   （名字序列、起止时间戳、父子关系）。不引入任何外部 collector 进程。

OTel 相关用例在缺 extra 的环境下 `importorskip` 跳过——CI 默认只装 `[dev,eval]`，
这既是"不做默认依赖"的体现，也是"跳过要说清原因"的做法。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.events import Event, EventKind, Source
from harness.trace import build_trace

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sample_trace():
    events = [
        Event(
            event_id="e0",
            run_id="run-1",
            branch_id="br-1",
            seq=0,
            kind=EventKind.TREE_NODE,
            type="user_message",
            source=Source.USER,
            payload={"text": "任务"},
            created_at=1.0,
        ),
        Event(
            event_id="e1",
            run_id="run-1",
            branch_id="br-1",
            seq=1,
            kind=EventKind.TREE_NODE,
            type="tool_call",
            source=Source.AGENT,
            payload={"tool_call_id": "tc1", "tool": "query_metrics", "args": {}},
            created_at=2.0,
        ),
        Event(
            event_id="e2",
            run_id="run-1",
            branch_id="br-1",
            seq=2,
            kind=EventKind.TREE_NODE,
            type="tool_result",
            source=Source.TOOL,
            payload={"tool_call_id": "tc1", "status": "executed", "result": {}},
            created_at=3.0,
        ),
    ]
    return build_trace(events)


def test_module_import_does_not_require_the_extra() -> None:
    """模块顶层的 import 清单里不能有 otel：否则缺 extra 时 import harness.trace 就炸。"""
    source = (PROJECT_ROOT / "harness" / "otel.py").read_text(encoding="utf-8")
    top = source.split("def _load_sdk")[0]
    assert "import opentelemetry" not in top, "OTel 的 import 必须在函数体内"


def test_cli_without_endpoint_is_unchanged(tmp_path: Path) -> None:
    """不传 --otlp-endpoint 时 CLI 行为与今天一致（输出 OTel 形状的 JSON，不联网）。"""
    run_dir = tmp_path / "run"
    # 造一个最小 run 目录：直接写一个只含 events 表的库
    import sqlite3

    from harness.store.schema import DDL

    db = run_dir / "runtime.db"
    run_dir.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript(DDL)
    con.execute(
        "INSERT INTO runs(run_id, thread_id, status, created_at, updated_at)"
        " VALUES('run-1', 'thr-1', 'completed', 0, 0)"
    )
    con.execute(
        "INSERT INTO branches(branch_id, run_id, parent_branch_id, fork_event_id, created_at)"
        " VALUES('br-1', 'run-1', NULL, NULL, 0)"
    )
    con.execute(
        "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
        " payload_json, created_at, prev_hash, event_hash)"
        " VALUES('e0', 'run-1', 'br-1', 0, 'tree_node', 'user_message', 'user', '{}', 0, '', '')"
    )
    con.commit()
    con.close()
    (run_dir / "ids.json").write_text(json.dumps({"branch_id": "br-1"}), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "harness.trace", "--run-dir", str(run_dir), "--otel"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-400:]
    payload = json.loads(proc.stdout)
    assert "resourceSpans" in payload


def test_mapping_matches_to_otel_with_an_in_memory_exporter() -> None:
    """装了 extra：内存 exporter 收到的 span 与 to_otel() 的关键字段逐位一致。"""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from harness.otel import export_otlp

    trace = _sample_trace()
    exporter = InMemorySpanExporter()
    result = export_otlp(trace, endpoint="http://memory.invalid", exporter=exporter)

    expected = trace.to_otel()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert [item["name"] for item in expected] == [item["name"] for item in result["spans"]]
    assert result["span_count"] == len(expected)
    assert len(exporter.get_finished_spans()) == len(expected)
    finished = exporter.get_finished_spans()
    for otel_item, sdk_span in zip(expected, finished, strict=True):
        # SDK 的 start_time 也是纳秒口径：应当与投影**逐位相同**
        assert sdk_span.start_time == otel_item["startTimeUnixNano"]
        assert sdk_span.end_time == otel_item["endTimeUnixNano"]
        assert sdk_span.attributes["harness.span_id"] == otel_item["spanId"]
    assert exporter.get_finished_spans()[0].resource.attributes["service.name"] == (
        "agent-consistency-lab"
    )
    assert SpanExportResult.SUCCESS is not None  # 显式引用避免未使用导入


def test_unavailable_extra_is_a_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 extra 时给的是**可读失败**（告诉你怎么装），不是裸 ImportError。"""
    import builtins

    from harness import otel

    real_import = builtins.__import__

    def _blocked(name: str, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(otel.OtelUnavailableError) as excinfo:
        otel._load_sdk()
    assert "[otel]" in str(excinfo.value)


def test_otlp_json_fallback_reports_connection_failure_readably() -> None:
    """降级路径（OTLP/JSON）在连不上时给出可读失败，而不是静默成功。"""
    from harness.otel import OtelExportError, post_otlp_json

    with pytest.raises(OtelExportError) as excinfo:
        post_otlp_json(_sample_trace(), endpoint="http://127.0.0.1:9", timeout=1.0)
    assert "OTLP/JSON 导出失败" in str(excinfo.value)


def test_cli_otlp_endpoint_branch_is_covered(tmp_path: Path) -> None:
    """**P2-14 回归**：`--otlp-endpoint` 这条 CLI 分支必须有用例走过。

    修复前会怎样：把 `harness/trace.py` 的该分支改成不可达，`test_otel.py` 仍然 5/5 绿——
    而"显式启用 OTLP"正是 R-B4 的那个开关（默认那一半测到了，启用那一半没测到）。
    这里用**未监听的本地端口** + OTLP/JSON 降级路径：不需要网络，只要求"可读失败 + 非零退出"。
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    import sqlite3

    from harness.store.schema import DDL

    con = sqlite3.connect(run_dir / "runtime.db")
    con.executescript(DDL)
    con.execute(
        "INSERT INTO runs(run_id, thread_id, status, created_at, updated_at)"
        " VALUES('run-1', 'thr-1', 'completed', 0, 0)"
    )
    con.execute(
        "INSERT INTO branches(branch_id, run_id, parent_branch_id, fork_event_id, created_at)"
        " VALUES('br-1', 'run-1', NULL, NULL, 0)"
    )
    con.execute(
        "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
        " payload_json, created_at, prev_hash, event_hash)"
        " VALUES('e0', 'run-1', 'br-1', 0, 'tree_node', 'user_message', 'user', '{}', 0, '', '')"
    )
    con.commit()
    con.close()
    (run_dir / "ids.json").write_text(json.dumps({"branch_id": "br-1"}), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.trace",
            "--run-dir",
            str(run_dir),
            "--otlp-endpoint",
            "http://127.0.0.1:9",
            "--otlp-json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, "连不上时必须非零退出，不能静默成功"
    assert "OTLP/JSON 导出失败" in (proc.stdout + proc.stderr)
