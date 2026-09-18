"""本地 OTLP/HTTP 接收器：让"导出到真实后端"这件事在**没有 Docker、不联网**时也能端到端跑通。

**这不是产品、不是后端、不是 collector**：它只在一段测试/演示的时长里监听 `127.0.0.1`
的一个端口，把收到的 OTLP/JSON 记在本进程里，然后把"能看出什么"打印出来。
之所以要它：本机没有 Docker、外网对部分域名不稳定，而"可观测导出"这条路径如果不留一条
**可实测**的路，文档里的三条口径就只能靠读者相信——那正是本仓库最不想要的形态。

三条口径（与 `harness/trace.py` / `harness/otel.py` 的既有实现一致，**读之前先读这段**）：

1. **时长是推导值**：`harness.duration_is_derived=true`（每个 span 都带）。工具 span 是
   真实墙钟窗口，模型 span 是事件间隔推导——**不得用来讲性能**。
2. **事后导入**，不是实时流：本路径是"跑完 → 投影 → POST"，没有"边跑边看"。
3. **trace 里没有账本**：审批、outbox、"副作用发生了几次"都在事件日志与 `world.db` 里，
   trace 只回答"这次调用的时序形状长什么样"。

用法::

    python -m integrations.otlp_local_sink --run-dir /tmp/demo/with_outbox
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from harness.otel import post_otlp_json
from harness.store.sqlite_store import SqliteStore
from harness.trace import build_trace

HOST = "127.0.0.1"


class _SinkHandler(BaseHTTPRequestHandler):
    """只认 ``POST /v1/traces``；把 body 原样收下，回 200 + 空 JSON。"""

    received: ClassVar[list[dict[str, Any]]] = []
    paths: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
            return
        type(self).paths.append(self.path)
        type(self).received.append(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:  # 静音：stdout 留给结论
        return


def _is_true(value: Any) -> bool:
    """``to_otel()`` 把每个属性都编码成 ``stringValue``（``str(value)``），
    于是布尔是 ``"True"``/``"False"``。判真值要同时容忍这两种写法。"""
    return str(value).strip().lower() == "true"


def summarize_otlp(payload: dict[str, Any]) -> dict[str, Any]:
    """从收到的 OTLP/JSON 里抽出"能看出什么"的那几项。

    刻意只抽结构性的东西（span 名、父子关系、属性键、纳秒时间戳有没有），
    不抽任何可以被读成性能的聚合量。
    """
    spans: list[dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                attributes = {
                    item["key"]: item["value"].get("stringValue")
                    for item in span.get("attributes", [])
                }
                spans.append(
                    {
                        "name": span.get("name"),
                        "span_id": span.get("spanId"),
                        "parent_span_id": span.get("parentSpanId"),
                        "start_ns": span.get("startTimeUnixNano"),
                        "end_ns": span.get("endTimeUnixNano"),
                        "attribute_keys": sorted(attributes),
                        "operation": attributes.get("gen_ai.operation.name"),
                        "duration_is_derived": attributes.get("harness.duration_is_derived"),
                        "idempotency_key": attributes.get("harness.idempotency_key"),
                        "approval_decision": attributes.get("harness.approval.decision"),
                    }
                )
    resource_attrs = [
        item["key"]
        for resource_span in payload.get("resourceSpans", [])
        for item in resource_span.get("resource", {}).get("attributes", [])
    ]
    tool_spans = [span for span in spans if span["operation"] == "execute_tool"]
    model_spans = [span for span in spans if span["operation"] == "chat"]
    flagged = [span for span in spans if _is_true(span["duration_is_derived"])]
    return {
        "span_count": len(spans),
        "span_names": [span["name"] for span in spans],
        "tool_span_count": len(tool_spans),
        # 模型 span 的时长是**事件间隔推导**出来的（没有独立的起止事件），因此必须被标注。
        # MCP 驱动的 run 进程内没有模型 ⇒ 模型 span 数为 0，这是事实不是缺陷。
        "model_span_count": len(model_spans),
        "derived_duration_span_count": len(flagged),
        # 可审计的性质：`harness.duration_is_derived` 恰好标在模型 span 上，
        # 不多不少——工具/审批 span 的起止来自两个真实事件，不该被标成推导值。
        "derived_flag_matches_model_spans": len(flagged) == len(model_spans)
        and all(_is_true(span["duration_is_derived"]) for span in model_spans),
        "resource_attribute_keys": sorted(resource_attrs),
        # 账本（审批结论、效果计数）**不在** trace 里：这里显式验证"找不到"，
        # 免得读者以为接了后端就能在 trace 里查"发生了几次"。
        "has_ledger_attributes": any(
            key.startswith("harness.ledger") or "effects" in key for key in resource_attrs
        ),
        "spans": spans,
    }


def round_trip(run_dir: Path, *, branch: str = "") -> dict[str, Any]:
    """跑完导出：建 trace → POST 到本地接收器 → 汇总收到的东西。"""
    branch_id = branch or _branch_from_ids(run_dir)
    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    try:
        events = store.effective_events(branch_id)
    finally:
        store.close()
    if not events:
        raise SystemExit(f"{run_dir} 的分支 {branch_id!r} 没有事件，没有什么可导出的")

    trace = build_trace(events)
    _SinkHandler.received = []
    _SinkHandler.paths = []
    server = ThreadingHTTPServer((HOST, 0), _SinkHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://{HOST}:{port}"
        posted = post_otlp_json(trace, endpoint=endpoint)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(_SinkHandler.received) == 1, "本地接收器应当恰好收到一次导出（不是流式）"
    summary = summarize_otlp(_SinkHandler.received[0])
    return {
        "run_dir": str(run_dir),
        "branch_id": branch_id,
        "endpoint": endpoint,
        "request_paths": _SinkHandler.paths,
        "http_status": posted["status"],
        "span_count": summary["span_count"],
        "span_names": summary["span_names"],
        "tool_span_count": summary["tool_span_count"],
        "model_span_count": summary["model_span_count"],
        "derived_duration_span_count": summary["derived_duration_span_count"],
        "derived_flag_matches_model_spans": summary["derived_flag_matches_model_spans"],
        "resource_attribute_keys": summary["resource_attribute_keys"],
        "has_ledger_attributes": summary["has_ledger_attributes"],
        "spans": summary["spans"],
        "caliber": {
            "duration_is_derived": True,
            "post_hoc_import": True,
            "ledger_not_in_trace": True,
        },
    }


def _branch_from_ids(run_dir: Path) -> str:
    """分支身份来自 ``ids.json``（本仓库各驱动方都写它：worker 与 mcp_server 同格式）。"""
    ids_path = run_dir / "ids.json"
    if not ids_path.exists():
        raise SystemExit(f"{run_dir} 里没有 ids.json；请显式传 --branch")
    return str(json.loads(ids_path.read_text(encoding="utf-8"))["branch_id"])


def build_demo_run(workroot: Path) -> Path:
    """跑一个最小的**模型驱动** run（脚本模型 + fakeworld），返回 run 目录。

    为什么要模型驱动：只有它才会产出模型 span，而"模型 span 的时长是推导值"正是
    三条口径里最容易被误读的一条——演示里必须能真的看到那个标记。
    """
    from fakeworld.model import ScriptedModel
    from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
    from fakeworld.world import World
    from harness.cache import CacheConfig, PrefixCacheModel
    from harness.chaos import Chaos
    from harness.ids import new_id
    from harness.llm import ScriptedLLMClient
    from harness.loop import Loop
    from harness.store import SqliteCheckpointSaver

    run_dir = workroot / "otel-demo"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    ids = {"run_id": new_id("run"), "thread_id": new_id("thr"), "branch_id": new_id("br")}
    (run_dir / "ids.json").write_text(json.dumps(ids), encoding="utf-8")

    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    world = World(run_dir / "world.db")
    try:
        registry = build_registry(world, idempotent_impl=True, probe_enabled=True)
        loop = Loop(
            store,
            SqliteCheckpointSaver(store),
            llm=ScriptedLLMClient(
                ScriptedModel(SCENARIO_POOL_EXHAUSTION), cache=PrefixCacheModel(CacheConfig())
            ),
            registry=registry,
            chaos=Chaos.disabled(),
        )
        store.create_run(ids["run_id"], thread_id=ids["thread_id"])
        store.create_branch(ids["branch_id"], ids["run_id"])
        loop.start(
            run_id=ids["run_id"],
            thread_id=ids["thread_id"],
            branch_id=ids["branch_id"],
            task="OTLP 演示：跑一个最小的模型驱动 run",
        )
        loop.approve(
            run_id=ids["run_id"], thread_id=ids["thread_id"], branch_id=ids["branch_id"]
        )
        loop.resume(
            run_id=ids["run_id"], thread_id=ids["thread_id"], branch_id=ids["branch_id"]
        )
    finally:
        store.close()
        world.close()
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="integrations.otlp_local_sink",
        description="把某个 run 的 trace 导到本机接收器上，并打印'能看出什么'（离线、无需 Docker）",
    )
    parser.add_argument("--run-dir", default="", help="run 目录（含 runtime.db 与 ids.json）")
    parser.add_argument("--branch", default="", help="缺省取 ids.json 里的分支")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="自己造一个最小的模型驱动 run 再导出（一条命令跑通整条路，不需要先有 run）",
    )
    parser.add_argument("--work-root", default="/tmp/otel-demo", help="--selftest 的工作目录")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    if args.selftest:
        run_dir = build_demo_run(Path(args.work_root))
    elif args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        parser.error("要么给 --run-dir，要么用 --selftest")
    result = round_trip(run_dir, branch=args.branch)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    print(
        f"[otlp_local_sink] span={result['span_count']} 路径={result['request_paths']} "
        "（时长是推导值；这是事后导入；账本不在 trace 里）",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
