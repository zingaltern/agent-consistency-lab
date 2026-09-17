"""MCP 工具服务（stdio）：把治理型工具注册表暴露给外部 agent。

**这是什么**：一个**本地、单用户、stdio** 的进程——一个进程对应一个 run 目录。
外部 agent（MCP 客户端）通过它调用本仓库的工具；每次调用都走
``harness/execution.py::ToolExecutor`` 的七步管线（审批门、幂等键含 branch 维度、
outbox 三态、ArgPolicy、TOCTOU 复核、事件日志 append-only 全部生效）。

**这不是什么**（设计文档 C §1 非目标）：不做多租户、不做鉴权、不监听非本地地址、
不是守护进程、没有实时流。集成层不产生结论，也不新增权威状态——
服务端**不**缓存审批结论、**不**在内存里放 run 的权威副本、**不**记会话数据库；
状态一律从事件日志折叠（客户端可断可续，服务被强杀也不产生新语义）。

**边界纪律**（改动前先读）：

* 服务端**不得**自己调用工具：所有执行都必须经过 ``ToolExecutor``。
  有结构用例守着（``tests/test_execution_parity.py`` 扫描 ``integrations/``）。
* SDK 的 import **全在函数体内**（照 ``harness/otel.py``）：缺 ``[mcp]`` extra 时
  ``import integrations.mcp_server`` 照常工作，``--list-tools`` 也照常工作。
* 工具的参数 schema 与下发给真实模型的那份**同源**（``harness/tools.py::tool_param_schema``），
  不在这里另写一份回退形状。
* 本模块**不实现 elicitation**：审批的默认形态（待审批作为工具结果返回 +
  ``list_pending_approvals``/``approve`` 工具对）就是唯一形态，因此"客户端不支持时自动退回"
  是恒等路径——没有可退的东西。有测试断言服务端从不向客户端发 elicitation 请求。
* 服务端**不做**第二套参数校验：执行前唯一的闸门是管线里的 ArgPolicy 与工具自身。
  另加一个校验器必然与管线分叉（同一个参数在两处得到不同判决）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fakeworld.tools import build_registry
from fakeworld.world import World
from harness.approval import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    SCOPE_ONCE,
    SCOPE_SESSION,
    ApprovalError,
)
from harness.events import NewEvent, Source, TreeEventType
from harness.execution import (
    APPROVAL_TTL_SECONDS,
    InterruptRequest,
    InterruptSignal,
    ResumeReport,
    RunContext,
    ToolExecutor,
    ToolOutcome,
)
from harness.ids import new_id
from harness.state import DerivedState
from harness.store.sqlite_store import SqliteStore
from harness.tools import ToolCallRequest, ToolRegistry, tool_param_schema

MCP_EXTRA_HINT = (
    "需要 MCP SDK：pip install -e '.[mcp]'（mcp）。"
    "本仓库不把 MCP 放进内核依赖：不加这个 extra 时 import integrations.mcp_server 与"
    "全部既有行为不变（SDK 的 import 全在函数体内）。"
)

# run 由本服务创建时写下的第一条树节点：写明来源，不假装有人类说话。
PROVENANCE_TEXT = (
    "此 run 由 integrations.mcp_server（stdio）创建：外部客户端驱动，服务进程内没有模型"
)

# 治理工具（不是被治理的对象）：它们是审批门的**显式接口**，不属于 ToolRegistry。
LIST_TOOL = "list_pending_approvals"
APPROVE_TOOL = "approve"
GOVERNANCE_TOOL_NAMES = (LIST_TOOL, APPROVE_TOOL)

GOVERNANCE_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": LIST_TOOL,
        "description": (
            "列出当前 run 里等待人工审批的调用（含 tool_call_id、参数、参数哈希、"
            "以及哪一条正卡在审批门）。没有待审批时返回空列表。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": APPROVE_TOOL,
        "description": (
            "对待审批的调用作出决定：approve / reject；带 approved_args 表示**改参批准**"
            "（原调用闭合为 superseded，另起新调用与新幂等键——沿用 runtime 既有语义）。"
            "批准后立即尝试执行该调用并把结果返回。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_call_id": {
                    "type": "string",
                    "description": "要决定的调用（取自待审批结果或 list_pending_approvals）",
                },
                "decision": {
                    "type": "string",
                    "enum": ["approve", "reject"],
                    "default": "approve",
                },
                "approved_args": {
                    "type": "object",
                    "description": "改参批准：替换后的完整参数（不传 = 按原参数批准）",
                },
                "actor": {"type": "string", "default": "human:mcp"},
                "ttl_seconds": {"type": "number", "default": APPROVAL_TTL_SECONDS},
                "scope": {"type": "string", "enum": [SCOPE_ONCE, SCOPE_SESSION]},
            },
            "required": ["tool_call_id"],
        },
    },
]

# 业务失败（isError=True）的结论集：被拒绝/失败/未知都该让客户端看见"这次没成功"。
ERROR_STATUSES = frozenset({"failed", "unknown", "rejected"})


class McpUnavailableError(RuntimeError):
    """缺 ``[mcp]`` extra 时的可读失败。"""


@dataclass
class ToolCounters:
    """管线累加的 7 个计数（``CounterSink`` 的最小实现）。

    这些数字**不是权威**，只是一次进程内的观测量；"发生了几次"永远以事件日志与
    外部账本为准。
    """

    replayed: int = 0
    unknown: int = 0
    rejected: int = 0
    probes: int = 0
    reconciled: int = 0
    executed: int = 0
    tool_failures: int = 0


@dataclass
class RunEnvironment:
    """一个 run 目录对应的全部依赖。服务进程一份，不共享给别的 run。"""

    run_dir: Path
    store: SqliteStore
    world: World
    registry: ToolRegistry
    executor: ToolExecutor
    ctx: RunContext
    counters: ToolCounters = field(default_factory=ToolCounters)
    created_run: bool = False

    def close(self) -> None:
        self.store.close()
        self.world.close()


# --------------------------------------------------------------------- 环境


def load_or_create_ids(run_dir: Path) -> dict[str, str]:
    """复用 ``experiments/worker.py`` 的 ids.json 形状：同一个 run 目录可以被两边接续。"""
    ids_path = run_dir / "ids.json"
    if ids_path.exists():
        payload = json.loads(ids_path.read_text(encoding="utf-8"))
        return {
            "run_id": str(payload["run_id"]),
            "thread_id": str(payload["thread_id"]),
            "branch_id": str(payload["branch_id"]),
        }
    ids = {"run_id": new_id("run"), "thread_id": new_id("thr"), "branch_id": new_id("br")}
    run_dir.mkdir(parents=True, exist_ok=True)
    ids_path.write_text(json.dumps(ids), encoding="utf-8")
    return ids


def ensure_run(store: SqliteStore, ids: dict[str, str]) -> bool:
    """run 不存在就建：run 行 + 分支 + 一条写明来源的 user_message。

    返回 True 表示这次真的创建了。已有内容的 run 一律不动（状态从日志折叠）。
    """
    if store.get_run(ids["run_id"]) is None:
        store.create_run(ids["run_id"], thread_id=ids["thread_id"])
    if store.get_branch(ids["branch_id"]) is None:
        store.create_branch(ids["branch_id"], ids["run_id"])
    ctx = RunContext(ids["run_id"], ids["thread_id"], ids["branch_id"])
    if store.effective_events(ctx.branch_id):
        return False
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.USER_MESSAGE,
            # 来源写 SYSTEM 而不是 USER：这条不是人类说的话，是"run 的来源登记"。
            source=Source.SYSTEM,
            payload={"text": PROVENANCE_TEXT},
        )
    )
    return True


def build_environment(
    run_dir: str | Path,
    *,
    tool_idem: bool = True,
    probe: bool = True,
    outbox: bool = True,
    dedup: bool = True,
) -> RunEnvironment:
    """打开（必要时创建）一个 run 目录，并把它接上共用的执行管线。"""
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    ids = load_or_create_ids(path)
    store = SqliteStore(path / "runtime.db")
    store.setup()
    created = ensure_run(store, ids)
    world = World(path / "world.db")
    registry = build_registry(world, idempotent_impl=tool_idem, probe_enabled=probe)
    _reject_governance_name_collision(registry)
    ctx = RunContext(ids["run_id"], ids["thread_id"], ids["branch_id"])
    executor = ToolExecutor(
        store,
        registry=registry,
        dedup=dedup,
        outbox=outbox,
    )
    return RunEnvironment(
        run_dir=path,
        store=store,
        world=world,
        registry=registry,
        executor=executor,
        ctx=ctx,
        created_run=created,
    )


def _reject_governance_name_collision(registry: ToolRegistry) -> None:
    """治理工具名不能被注册表里的工具覆盖——那会让"审批接口"被静默顶替。"""
    clashes = sorted(set(GOVERNANCE_TOOL_NAMES) & set(registry.names()))
    if clashes:
        raise ValueError(f"工具注册表与治理工具重名：{clashes}")


def recover_on_start(env: RunEnvironment) -> ResumeReport:
    """启动即恢复：把日志里未闭合的调用重新驱动一次（**不驱动模型**）。

    这是"服务被强杀 → 重启 → 同一 run 续跑"的实现：是否需要探针对账、是否允许重跑，
    全部由管线第 3 步按日志与 outbox 意图行决定，本函数不做自己的判断。
    报告里同时带"执行了哪几条"与"是否被某一条的审批门拦下"。
    """
    return env.executor.resume_open_calls(env.ctx, env.counters)


def refresh_run_status(env: RunEnvironment) -> DerivedState:
    """把 ``runs.status`` 刷新成日志折叠的结果（它是看板摘要，不是权威）。"""
    state = env.executor.derived_state(env.ctx)
    env.store.set_run_status(env.ctx.run_id, state.status.value)
    return state


# --------------------------------------------------------------------- 映射


def list_tool_specs(registry: ToolRegistry) -> list[dict[str, Any]]:
    """注册表 → MCP 的 ``tools/list`` 形状。schema 与下发给模型的那份同源。"""
    return [
        {
            "name": tool.name,
            "description": tool.description or tool.name,
            "inputSchema": tool_param_schema(tool),
        }
        for tool in sorted(registry, key=lambda item: item.name)
    ]


def list_all_tool_specs(registry: ToolRegistry) -> list[dict[str, Any]]:
    """注册表工具 + 两个治理工具。名字排序保证列表稳定（客户端可缓存）。"""
    return sorted(
        [*list_tool_specs(registry), *GOVERNANCE_TOOL_SPECS],
        key=lambda spec: spec["name"],
    )


def is_error_status(status: str) -> bool:
    """哪些结论该让客户端看到 ``isError``：被拒/失败/未知。**

    ``pending_approval`` 不是错误——它是治理层的**正常中间态**，审批之后同一条调用会继续。
    """
    return status in ERROR_STATUSES


# --------------------------------------------------------------------- 调用


def pending_payload(env: RunEnvironment, request: InterruptRequest) -> dict[str, Any]:
    """把"停在审批门"变成可读结果：调用方据此调 ``approve``。"""
    return {
        "status": "pending_approval",
        "tool_call_id": request.tool_call_id,
        "tool": request.tool,
        "args": request.args,
        "args_sha256": request.args_sha256,
        "interrupt_id": request.interrupt_id,
        "interrupt_index": request.interrupt_index,
        "reason": request.reason,
        # 提示值：批准那一刻才会真正生成 binding，有效期从那时起算
        "approval_ttl_seconds": APPROVAL_TTL_SECONDS,
        "next": (
            f"调用 {APPROVE_TOOL}，参数 {{'tool_call_id': {request.tool_call_id!r}, "
            "'decision': 'approve'|'reject'}}；带 approved_args 表示改参批准"
        ),
    }


def outcome_payload(env: RunEnvironment, outcome: ToolOutcome) -> dict[str, Any]:
    """工具结论 → 可读结果（业务失败也是可读结果，由 ``isError`` 区分）。"""
    return {
        "status": outcome.status,
        "tool_call_id": outcome.tool_call_id,
        "result": outcome.result,
        "error_class": outcome.error_class,
        "replayed": outcome.replayed,
    }


def handle_tools_call(env: RunEnvironment, name: str, arguments: dict[str, Any] | None) -> dict:
    """``tools/call`` 的业务层（与传输无关，因此无 SDK 也能测）。

    除两个治理工具外，**一律**走 ``ToolExecutor``：这里没有第二条执行路径。
    """
    args = dict(arguments or {})
    if name == LIST_TOOL:
        return {"status": "ok", "pending": env.executor.pending_approvals(env.ctx)}
    if name == APPROVE_TOOL:
        return _handle_approve(env, args)

    # 未知工具**不**提前拦截：管线第 2 步会把这次尝试记录并闭合
    # （"未注册"不等于"没发生"——它必须在日志里留下痕迹）。
    request = ToolCallRequest(tool_call_id=new_id("tc"), tool=name, args=args)
    try:
        outcome = env.executor.execute(env.ctx, request, env.counters)
    except InterruptSignal as signal:
        state = refresh_run_status(env)
        payload = pending_payload(env, signal.request)
        if signal.request.tool_call_id != request.tool_call_id:
            # INV-004：同一时刻至多一条 interrupt。本次调用是**排在它之后**等待，
            # 不是被丢弃——两个 id 都要报出来，否则客户端既不知道自己在等谁，
            # 也不知道"我这次调用"在日志里叫什么。
            payload["queued_tool_call_id"] = request.tool_call_id
            payload["note"] = (
                f"另有调用 {signal.request.tool_call_id} 正卡在审批门；"
                f"本次调用 {request.tool_call_id} 已记入日志并排队等待"
                "（先处理 tool_call_id 那一条，再用 list_pending_approvals 取本次的 id）"
            )
        payload["run_status"] = state.status.value
        return payload
    state = refresh_run_status(env)
    payload = outcome_payload(env, outcome)
    payload["run_status"] = state.status.value
    return payload


def _handle_approve(env: RunEnvironment, args: dict[str, Any]) -> dict[str, Any]:
    """审批 + 立即续跑。改参沿用既有语义（`decide` 里实现，不在这里另写一套）。"""
    raw_decision = str(args.get("decision") or "approve").strip().lower()
    decision_map = {
        "approve": DECISION_APPROVED,
        "approved": DECISION_APPROVED,
        "reject": DECISION_REJECTED,
        "rejected": DECISION_REJECTED,
    }
    if raw_decision not in decision_map:
        return {
            "status": "rejected",
            "error_class": "bad_decision",
            "message": f"decision 只能是 approve / reject，收到 {raw_decision!r}",
        }

    pending = env.executor.pending_approvals(env.ctx)
    current = next((item for item in pending if item["is_current"]), None)
    if current is None:
        return {
            "status": "rejected",
            "error_class": "no_pending_interrupt",
            "message": "当前 run 没有待审批的调用",
        }
    requested = str(args.get("tool_call_id") or "")
    if requested and requested != current["tool_call_id"]:
        # 显式核对：批准的是"某次调用 + 某组参数"，接错调用会把批准给到别处。
        return {
            "status": "rejected",
            "error_class": "tool_call_id_mismatch",
            "message": (
                f"待审批的调用是 {current['tool_call_id']}，收到 {requested}；"
                "请用 list_pending_approvals 核对后再提交"
            ),
        }

    approved_args = args.get("approved_args")
    if approved_args is not None and not isinstance(approved_args, dict):
        return {
            "status": "rejected",
            "error_class": "bad_approved_args",
            "message": "approved_args 必须是对象（把完整参数给全，不是打补丁）",
        }
    ttl = args.get("ttl_seconds")
    scope = str(args.get("scope") or SCOPE_ONCE)
    try:
        binding = env.executor.decide(
            env.ctx,
            decision=decision_map[raw_decision],
            actor=str(args.get("actor") or "human:mcp"),
            ttl_seconds=float(ttl) if ttl is not None else APPROVAL_TTL_SECONDS,
            approved_args=approved_args,
            scope=scope,
        )
    except ApprovalError as exc:
        return {"status": "rejected", "error_class": "approval_error", "message": str(exc)}

    report = env.executor.resume_open_calls(env.ctx, env.counters)
    executed = [outcome_payload(env, outcome) for outcome in report.outcomes]
    interrupted = pending_payload(env, report.pending) if report.pending is not None else None
    state = refresh_run_status(env)
    payload: dict[str, Any] = {
        "status": "approved" if decision_map[raw_decision] == DECISION_APPROVED else "rejected",
        "decision": binding.decision,
        "edited": binding.edited,
        "tool_call_id": binding.tool_call_id,
        "approved_args_sha256": binding.approved_args_sha256,
        "executed": executed,
        "run_status": state.status.value,
    }
    if interrupted is not None:
        payload["next_pending"] = interrupted
    return payload


# --------------------------------------------------------------------- 传输
#
# 以下三件是唯一需要 SDK 的部分：其余函数在没有 [mcp] extra 时也能 import 与测试。


def _load_sdk() -> tuple[Any, Any, Any]:
    """延迟 import：顶层不引入 mcp，缺 extra 时给出可读失败。

    返回 ``(Server, stdio_server, mcp_types)``——只需要模块与两个入口，
    其余类型从 ``mcp_types`` 上取，避免一长串别名 import。
    """
    try:
        import mcp.types as mcp_types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - 取决于环境是否装了 extra
        raise McpUnavailableError(f"{MCP_EXTRA_HINT}（原始错误：{exc}）") from exc
    return Server, stdio_server, mcp_types


def _result_payload(payload: dict[str, Any], *, is_error: bool, mcp_types: Any) -> Any:
    """把业务结果包成 MCP 的 ``CallToolResult``：文本块给人看，structuredContent 给机器读。"""
    return mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)
            )
        ],
        structured_content=payload,
        is_error=is_error,
    )


async def _serve_stdio(run_dir: str | Path, **options: Any) -> None:  # pragma: no cover
    """stdio 主循环：一个进程一个连接，一个连接一个 run。"""
    Server, stdio_server, mcp_types = _load_sdk()
    ListToolsResult = mcp_types.ListToolsResult
    McpTool = mcp_types.Tool

    env = build_environment(run_dir, **options)
    recovered = recover_on_start(env)
    refresh_run_status(env)
    # 恢复结论不打印到 stdout（那是协议线），只留一行到 stderr。
    print(
        f"[mcp_server] run={env.ctx.run_id} created={env.created_run} "
        f"recovered={type(recovered).__name__}",
        file=sys.stderr,
    )

    server = Server("agent-consistency-lab")

    async def handle_list(_context: Any, _params: Any) -> Any:
        return ListToolsResult(
            tools=[
                McpTool(
                    name=spec["name"],
                    description=spec["description"],
                    inputSchema=spec["inputSchema"],
                )
                for spec in list_all_tool_specs(env.registry)
            ]
        )

    async def handle_call(_context: Any, params: Any) -> Any:
        payload = handle_tools_call(env, params.name, params.arguments or {})
        return _result_payload(
            payload,
            is_error=is_error_status(str(payload.get("status", ""))),
            mcp_types=mcp_types,
        )

    # params_type 取 SDK 自己的方法表（`mcp/server/lowlevel/server.py` 的 _spec_requests）：
    # 传请求信封（*Request）会被参数校验挡下——第一次实测就踩到了这个坑。
    server.add_request_handler("tools/list", mcp_types.PaginatedRequestParams, handle_list)
    server.add_request_handler("tools/call", mcp_types.CallToolRequestParams, handle_call)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        env.close()


# --------------------------------------------------------------------- CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="integrations.mcp_server",
        description="治理型 MCP 工具服务（stdio，本地单用户；一个进程一个 run 目录）",
    )
    parser.add_argument("--run-dir", required=True, help="run 目录（不存在则由本服务创建）")
    parser.add_argument(
        "--tool-idem",
        choices=("on", "off"),
        default="on",
        help="下游写实现是否幂等（off = 非幂等，outbox 才会走预写路径）",
    )
    parser.add_argument("--probe", choices=("on", "off"), default="on", help="下游是否支持按键读回")
    parser.add_argument("--outbox", choices=("on", "off"), default="on", help="outbox 预写意图行")
    parser.add_argument("--dedup", choices=("on", "off"), default="on", help="去重表查询")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="只列出会下发的工具与 schema（不需要 [mcp] extra），然后退出",
    )
    parser.add_argument("--json-out", default="", help="把 --list-tools 的结果写到文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_tools:
        # 纯映射路径：不需要 SDK，因此在没装 extra 的环境里也能跑（claim 用的就是它）。
        env = build_environment(
            args.run_dir,
            tool_idem=args.tool_idem == "on",
            probe=args.probe == "on",
            outbox=args.outbox == "on",
            dedup=args.dedup == "on",
        )
        try:
            specs = list_all_tool_specs(env.registry)
            # tool_count 是给"下发了几个工具"这类对外数字用的稳定路径
            # （claim `mcp-tools-declared` 绑的就是它）。
            payload = {
                "tool_count": len(specs),
                "governance_tool_count": len(GOVERNANCE_TOOL_SPECS),
                "tools": specs,
            }
        finally:
            env.close()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json_out:
            Path(args.json_out).write_text(text, encoding="utf-8")
        print(text)
        return 0

    try:
        import asyncio

        asyncio.run(
            _serve_stdio(
                args.run_dir,
                tool_idem=args.tool_idem == "on",
                probe=args.probe == "on",
                outbox=args.outbox == "on",
                dedup=args.dedup == "on",
            )
        )
    except McpUnavailableError as exc:
        print(f"mcp_server: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
