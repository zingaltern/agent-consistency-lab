"""Trace 投影：把事件日志投影成 span 树（事件是权威，trace 是投影）。

为什么不"边跑边打 span"：那样会得到两套事实（事件日志与 span 各写一份），
崩溃时两者的差异无从对账。这里反过来——**只从事件日志投影**，
因此 trace 与语义永远一致，且可以离线重放、可以对历史 run 补投影。

属性命名向 OpenTelemetry GenAI 语义约定靠拢（`gen_ai.*`），但不假装实现了该规范：
真正接入 OTLP 时，把 ``Trace.to_otel`` 的映射换成官方 SDK 即可——
注意当前导出的 ID 不是 32/16 位十六进制、缺 `status`、属性统一字符串化，
**是"OTel 形状"而不是规范兼容**。

Span 树形状：

```
invoke_agent <run>                     ← 整个 run
├── chat <model>                       ← 每次模型调用（含 usage / 缓存 / 成本）
├── execute_tool <tool>                ← 从 tool_call 到 tool_result 的**墙钟窗口**
├── human_approval                     ← 每次人工审批（含决策与参数 hash）
├── compact                            ← 每次压缩（含前后 token）
└── error <class>                      ← 每个错误事实（零时长，便于检索）
```

两条必须知道的语义（审计指出早期注释与实现不符，已更正）：

* **`execute_tool` 的时长包含人工审批等待与恢复停机**：span 覆盖 `tool_call → … → tool_result`，
  而审批（`human_approval` 子窗口）与"崩溃→恢复"都落在这段窗口里。
  要纯执行时长，请减去 `human_approval` 的时长或按 `resume` 事件切分。
* **`chat` 的起点是"上一个事件之后"**，因此它覆盖"视图构建 + 模型计算 + 记账簿记"的整段墙钟；
  属性里的 `harness.duration_is_derived=true` 就是在说"这是推导值，不是模型自报的耗时"。

未知事件类型不会被静默吞掉：`build_trace` 会把它记进根 span 的
`harness.trace.unhandled_event_types`，便于发现"投影漏了东西"。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from .events import Event, EventKind
from .ids import new_id

# 事件类型 → span 名（未列出的事件类型不进 span 树，只在属性里汇总）
TREE_SPAN_NAMES = {
    "agent_message": "chat",
    "tool_call": "execute_tool",
    "tool_result": "tool_result",
    "interrupt": "human_approval",
    "resume": "resume",
    "compaction": "compact",
    "error": "error",
    "budget_update": "budget",
}


# 渲染时的短名：不短名会出现 "input_tokens=0 input_tokens=0 input_tokens=828" 这种重名
ATTRIBUTE_ALIASES = {
    "gen_ai.usage.input_tokens": "tokens_in",
    "gen_ai.usage.output_tokens": "tokens_out",
    "gen_ai.usage.cache_read.input_tokens": "cache_read",
    "gen_ai.usage.cache_write.input_tokens": "cache_write",
    "harness.cost_usd": "cost_usd",
    "harness.result_status": "result",
    "harness.approval.decision": "decision",
    "harness.approval.pending": "pending",
    "gen_ai.tool.name": "tool",
    "gen_ai.tool.call.id": "call_id",
    "harness.effect": "effect",
    "harness.idempotency_key": "idem_key",
    "harness.compaction.round": "round",
    "harness.compaction.reason": "reason",
    "harness.error.fatal": "fatal",
    "harness.duration_is_derived": "dur_derived",
    "harness.budget.spent_usd": "spent_usd",
    "harness.interrupt.id": "interrupt_id",
    "harness.interrupt.index": "index",
    "harness.approval.id": "approval_id",
    "harness.compaction.id": "compaction_id",
    "harness.compaction.replacements": "replaced",
    "harness.compaction.tokens_before": "tokens_before",
    "harness.compaction.tokens_after": "tokens_after",
    "harness.approval.actor": "actor",
    "harness.step_is_final": "final",
    "run.id": "run_id",
    "harness.budget.last_bucket": "last_bucket",
    "gen_ai.agent.name": "agent",
    "gen_ai.operation.name": "op",
    "gen_ai.response.finish_reasons": "finish",
}


class Span(BaseModel):
    span_id: str = Field(default_factory=lambda: new_id("span"))
    parent_id: str | None = None
    name: str
    kind: str = "internal"  # internal | client（模型/工具调用算 client）
    start: float
    end: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    children: list[Span] = Field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end is None:
            return 0.0
        return round((self.end - self.start) * 1000, 3)


class Trace(BaseModel):
    run_id: str
    branch_id: str
    root: Span
    span_count: int = 0

    def flatten(self) -> list[Span]:
        out: list[Span] = []

        def walk(span: Span) -> None:
            out.append(span)
            for child in span.children:
                walk(child)

        walk(self.root)
        return out

    def to_otel(self) -> dict[str, Any]:
        """OTel 味的导出结构（resourceSpans/scopeSpans/spans），便于接真实后端。"""
        spans = []
        for span in self.flatten():
            spans.append(
                {
                    "traceId": self.run_id,
                    "spanId": span.span_id,
                    "parentSpanId": span.parent_id or "",
                    "name": span.name,
                    "kind": 3 if span.kind == "client" else 1,
                    "startTimeUnixNano": int(span.start * 1e9),
                    "endTimeUnixNano": int((span.end or span.start) * 1e9),
                    "attributes": [
                        {"key": key, "value": {"stringValue": str(value)}}
                        for key, value in sorted(span.attributes.items())
                    ],
                }
            )
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "agent-consistency-lab"},
                            }
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": "harness.trace"}, "spans": spans}],
                }
            ]
        }


def build_trace(events: Sequence[Event]) -> Trace:
    """从事件日志投影出 span 树（纯函数，可重复调用）。"""
    unhandled: dict[str, int] = {}
    run_id = events[0].run_id if events else "unknown"
    branch_id = events[0].branch_id if events else "unknown"
    root = Span(
        name=f"invoke_agent {run_id}", kind="client", start=events[0].created_at if events else 0.0
    )
    root.attributes["gen_ai.agent.name"] = "ops-harness"
    root.attributes["run.id"] = run_id

    open_spans: dict[str, Span] = {}  # 工具调用：tool_call_id → span
    approval_spans: dict[str, Span] = {}  # 审批：tool_call_id → span
    interrupt_spans: dict[str, Span] = {}  # 审批：interrupt_id → span
    last_end = root.start
    previous_at = root.start

    for event in events:
        last_end = max(last_end, event.created_at)
        if event.kind is EventKind.TREE_NODE and event.type == "agent_message":
            span = Span(
                name="chat scripted-model",
                kind="client",
                # 模型调用没有独立起止事件：起点取"上一个事件之后"，即模型真正运行时的那段窗口。
                # 这段窗口同时包含框架自身的开销，因此标注 duration_is_derived 供审计识别。
                start=previous_at,
                end=event.created_at,
                parent_id=root.span_id,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.response.finish_reasons": ["stop"],
                    "gen_ai.usage.input_tokens": event.payload.get("context_tokens", 0),
                    "gen_ai.usage.cache_read.input_tokens": event.payload.get(
                        "cache_read_tokens", 0
                    ),
                    "gen_ai.usage.cache_write.input_tokens": event.payload.get(
                        "cache_write_tokens", 0
                    ),
                    "gen_ai.usage.output_tokens": (event.payload.get("usage") or {}).get(
                        "completion_tokens", 0
                    ),
                    "harness.cost_usd": event.payload.get("cost_usd", 0.0),
                    "harness.view_fingerprint": event.payload.get("view_fingerprint", ""),
                    "harness.step_is_final": bool(event.payload.get("final")),
                    "harness.duration_is_derived": True,
                },
            )
            root.children.append(span)
        elif event.kind is EventKind.TREE_NODE and event.type == "tool_call":
            call_id = str(event.payload.get("tool_call_id"))
            span = Span(
                name=f"execute_tool {event.payload.get('tool')}",
                kind="client",
                start=event.created_at,
                parent_id=root.span_id,
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": str(event.payload.get("tool")),
                    "gen_ai.tool.call.id": call_id,
                    "harness.effect": str(event.payload.get("effect")),
                    "harness.args_sha256": str(event.payload.get("args_sha256"))[:16],
                    "harness.idempotency_key": str(event.payload.get("idempotency_key")),
                },
            )
            root.children.append(span)
            open_spans[call_id] = span
        elif event.kind is EventKind.TREE_NODE and event.type == "tool_result":
            call_id = str(event.payload.get("tool_call_id"))
            span = open_spans.get(call_id)
            if span is not None:
                span.end = event.created_at
                span.attributes["harness.result_status"] = str(event.payload.get("status"))
                if event.payload.get("error_class"):
                    span.attributes["error.type"] = str(event.payload["error_class"])
        elif event.kind is EventKind.ARTIFACT and event.type == "interrupt":
            pass  # interrupt 是树事件，见下
        elif event.type == "interrupt":
            span = Span(
                name="human_approval",
                kind="internal",
                start=event.created_at,
                parent_id=root.span_id,
                attributes={
                    "harness.interrupt.id": str(event.payload.get("interrupt_id")),
                    "harness.interrupt.index": event.payload.get("interrupt_index", 0),
                    "harness.tool.name": str(event.payload.get("tool")),
                    "harness.tool.call.id": str(event.payload.get("tool_call_id")),
                    "harness.approval.pending": True,
                },
            )
            root.children.append(span)
            interrupt_spans[str(event.payload.get("interrupt_id"))] = span
            approval_spans[str(event.payload.get("tool_call_id"))] = span
        elif event.type == "resume":
            span = interrupt_spans.get(str(event.payload.get("interrupt_id")))
            if span is not None:
                span.end = event.created_at
                span.attributes["harness.approval.pending"] = False
                span.attributes["harness.approval.decision"] = str(event.payload.get("decision"))
                span.attributes["harness.approval.id"] = str(event.payload.get("approval_id"))
        elif event.kind is EventKind.ARTIFACT and event.type == "approval":
            # 审批事实按 tool_call_id 找到它对应的中断 span（审批对象是"某次调用"）
            target = approval_spans.get(str(event.payload.get("tool_call_id")), root)
            target.attributes["harness.approved_args_sha256"] = str(
                event.payload.get("approved_args_sha256")
            )[:16]
            target.attributes["harness.approval.actor"] = str(event.payload.get("actor"))
        elif event.kind is EventKind.ARTIFACT and event.type == "compaction":
            span = Span(
                name="compact",
                kind="internal",
                start=event.created_at,
                end=event.created_at,
                parent_id=root.span_id,
                attributes={
                    "harness.compaction.id": str(event.payload.get("compaction_id")),
                    "harness.compaction.replacements": len(
                        event.payload.get("replaces_event_ids", [])
                    ),
                    "harness.compaction.tokens_before": event.payload.get("tokens_before", 0),
                    "harness.compaction.tokens_after": event.payload.get("tokens_after", 0),
                    "harness.compaction.reason": str(event.payload.get("reason")),
                    "harness.compaction.round": event.payload.get("round", 0),
                },
            )
            root.children.append(span)
        elif event.kind is EventKind.ARTIFACT and event.type == "error":
            root.children.append(
                Span(
                    name=f"error {event.payload.get('error_class')}",
                    kind="internal",
                    start=event.created_at,
                    end=event.created_at,
                    parent_id=root.span_id,
                    attributes={
                        "error.type": str(event.payload.get("error_class")),
                        "error.message": str(event.payload.get("message"))[:200],
                        "harness.error.fatal": bool(event.payload.get("fatal")),
                    },
                )
            )
        elif event.kind is EventKind.ARTIFACT and event.type == "budget_update":
            root.attributes["harness.budget.last_bucket"] = str(event.payload.get("bucket"))
            root.attributes["harness.budget.spent_usd"] = round(
                float(root.attributes.get("harness.budget.spent_usd", 0.0))
                + float(event.payload.get("cost_usd", 0.0)),
                8,
            )
        else:
            key = f"{event.kind.value}:{event.type}"
            unhandled[key] = unhandled.get(key, 0) + 1
        previous_at = event.created_at

    if unhandled:
        # 不静默丢弃：投影漏掉的事件类型会出现在根 span 上（审计指出早期版本什么都不记）
        root.attributes["harness.trace.unhandled_event_types"] = ", ".join(
            f"{key}×{count}" for key, count in sorted(unhandled.items())
        )
    root.end = last_end
    if "harness.budget.spent_usd" not in root.attributes:
        # 没有接预算账本时（评测里常见），成本从 chat span 汇总——两处口径一致：
        # 账本记的是 provider 口径的 usage，chat span 记的是同一次调用的 cost_usd。
        root.attributes["harness.budget.spent_usd"] = round(
            sum(float(s.attributes.get("harness.cost_usd", 0.0)) for s in root.children), 8
        )
        root.attributes["harness.budget.source"] = "derived_from_chat_spans"
    trace = Trace(run_id=run_id, branch_id=branch_id, root=root)
    trace.span_count = len(trace.flatten())
    return trace


def render_text(trace: Trace) -> str:
    """把 span 树渲染成可读文本（排障时看这个，比翻事件日志快）。"""
    lines: list[str] = [
        f"trace run={trace.run_id} branch={trace.branch_id} spans={trace.span_count}"
    ]

    def walk(span: Span, depth: int) -> None:
        indent = "  " * depth
        duration = f"{span.duration_ms:.1f}ms" if span.end else "open"
        detail = " ".join(
            f"{ATTRIBUTE_ALIASES.get(key, key.split('.')[-1])}={value}"
            for key, value in sorted(span.attributes.items())
            if not key.endswith(("fingerprint", "args_sha256"))
        )
        lines.append(f"{indent}- {span.name} [{duration}] {detail}".rstrip())
        for child in span.children:
            walk(child, depth + 1)

    walk(trace.root, 0)
    return "\n".join(lines)


def trace_to_json(trace: Trace, *, otel: bool = False) -> str:
    payload = trace.to_otel() if otel else json.loads(trace.model_dump_json())
    return json.dumps(payload, ensure_ascii=False, indent=2)


def summary_of(trace: Trace) -> dict[str, Any]:
    """一眼能看的汇总（也用于把 trace 指标接进评测报告）。"""
    spans = trace.flatten()
    by_name: dict[str, int] = {}
    for span in spans:
        key = span.name.split(" ")[0]
        by_name[key] = by_name.get(key, 0) + 1
    return {
        "run_id": trace.run_id,
        "spans": trace.span_count,
        "by_kind": by_name,
        "errors": [s.name for s in spans if s.name.startswith("error")],
        "approvals": [s for s in spans if s.name == "human_approval"],
        "cost_usd": trace.root.attributes.get("harness.budget.spent_usd", 0.0),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI：`python -m harness.trace --run-dir <dir> [--json|--otel]`。"""
    import argparse
    from pathlib import Path

    from .store.sqlite_store import SqliteStore

    parser = argparse.ArgumentParser(prog="harness.trace")
    parser.add_argument("--run-dir", required=True, help="worker 的 run 目录（含 runtime.db）")
    parser.add_argument("--branch", default="", help="缺省取该 run 目录里唯一的分支")
    parser.add_argument("--json", action="store_true", help="输出 span 树 JSON")
    parser.add_argument("--otel", action="store_true", help="输出 OTel 形状的 JSON")
    parser.add_argument(
        "--otlp-endpoint",
        default="",
        help="显式启用真实 OTLP 导出（如 http://localhost:4318）；不传则行为与今天完全一致",
    )
    parser.add_argument(
        "--otlp-json",
        action="store_true",
        help="不装 [otel] extra 时走 OTLP/JSON over HTTP（少了 SDK 的批处理，本仓库不做重试）",
    )
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    try:
        ids_path = run_dir / "ids.json"
        branch_id = args.branch
        if not branch_id:
            if ids_path.exists():
                branch_id = json.loads(ids_path.read_text(encoding="utf-8"))["branch_id"]
            else:
                raise SystemExit("需要 --branch（该目录没有 ids.json）")
        trace = build_trace(store.effective_events(branch_id))
    finally:
        store.close()

    if args.otlp_endpoint:
        # 显式启用才导出：默认路径（--json/--otel/文本）一个字节都不变
        if args.otlp_json:
            from .otel import post_otlp_json

            print(
                json.dumps(
                    post_otlp_json(trace, endpoint=args.otlp_endpoint),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            from .otel import export_otlp

            print(
                json.dumps(
                    export_otlp(trace, endpoint=args.otlp_endpoint),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.otel:
        print(trace_to_json(trace, otel=True))
    elif args.json:
        print(trace_to_json(trace))
    else:
        print(render_text(trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
