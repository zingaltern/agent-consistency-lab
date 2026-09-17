"""子进程 worker：一次 run / approve / resume，运行在独立进程里。

必须独立进程的原因：崩溃注入用 SIGKILL，进程内的任何状态（连接、锁、内存）都必须
真实地随进程消失，才能验证"恢复只能依赖持久化的事实"。

三个阶段刻意分开，对应真实世界的三个动作：

* ``run``     ：agent 开始处理任务（可能在审批门处停下，status=waiting_human）
* ``approve`` ：人在外部批准/拒绝（只写审批事实与 resume 事件，不执行任何副作用）
* ``resume``  ：agent 继续（可能在任何命名窗口被 SIGKILL）

用法::

    python -m experiments.worker --run-dir /tmp/cell0 --mode run
    python -m experiments.worker --run-dir /tmp/cell0 --mode approve
    CHAOS_WINDOWS=post_tool_effect_pre_record:1 \\
        python -m experiments.worker --run-dir /tmp/cell0 --mode resume
    # 墙钟定时注入（R-A2 的新注入族；与 CHAOS_WINDOWS 语义独立、可同时给出）
    python -m experiments.worker --run-dir /tmp/cell0 --mode resume --kill-after-ms 120

崩溃时进程被 SIGKILL（退出码 137/-9），并留下 ``crash_marker.json``
（``injection_kind`` 区分 window / time_hit 两个注入族）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
from fakeworld.world import World
from harness.approval import DECISION_APPROVED, DECISION_REJECTED
from harness.artifacts import ArtifactStore
from harness.budget import BudgetLedger, BudgetLimits
from harness.cache import CacheConfig, PrefixCacheModel
from harness.cassette import CassetteMeta, CassetteStore
from harness.chaos import Chaos
from harness.compaction import CompactionPolicy, Compactor
from harness.context import DEFAULT_MAX_INLINE_TOKENS, ViewBuilder
from harness.ids import new_id
from harness.llm import (
    ModelWindow,
    RecordingLLMClient,
    ReplayLLMClient,
    ScriptedLLMClient,
    ScriptedTransport,
)
from harness.loop import DEFAULT_SYSTEM_PROMPT, Loop
from harness.store.checkpoints import SqliteCheckpointSaver
from harness.store.sqlite_store import SqliteStore
from harness.tools import ToolCallRequest

TASK = "支付服务 P99 告警，请定位并处置。"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="experiments.worker")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=("run", "approve", "resume"), default="run")
    parser.add_argument("--scenario", default=SCENARIO_POOL_EXHAUSTION)
    parser.add_argument("--long-steps", type=int, default=8, help="长任务场景的取证轮数")
    parser.add_argument("--long-lines", type=int, default=60, help="长任务场景每次日志行数")
    parser.add_argument("--dedup", choices=("on", "off"), default="on")
    parser.add_argument("--outbox", choices=("on", "off"), default="on")
    parser.add_argument("--tool-idem", choices=("on", "off"), default="on")
    parser.add_argument("--probe", choices=("on", "off"), default="on")
    parser.add_argument("--tamper", choices=("on", "off"), default="off")
    parser.add_argument(
        "--kill-after-ms",
        type=int,
        default=None,
        help="墙钟定时注入：启动后 N 毫秒 SIGKILL 自身（与 CHAOS_WINDOWS 独立）",
    )
    parser.add_argument("--approve-mode", choices=("approve", "reject", "edit"), default="approve")
    parser.add_argument("--compaction", choices=("on", "off"), default="on")
    parser.add_argument(
        "--compaction-trigger",
        type=float,
        default=None,
        help="压缩触发阈值（占可用窗口的比例）；缺省用库默认值",
    )
    parser.add_argument("--keep-recent-groups", type=int, default=None)
    parser.add_argument("--window-tokens", type=int, default=ModelWindow().context_limit_tokens)
    parser.add_argument("--max-output-tokens", type=int, default=ModelWindow().max_output_tokens)
    parser.add_argument("--dynamic-at-head", choices=("on", "off"), default="off")
    parser.add_argument("--budget-usd", type=float, default=BudgetLimits().total_usd)
    parser.add_argument("--max-inline-tokens", type=int, default=DEFAULT_MAX_INLINE_TOKENS)
    parser.add_argument(
        "--model",
        choices=("scripted", "record", "replay"),
        default="scripted",
        help="模型模式：scripted=现状（默认）；record=调真实 API 并录制；replay=按录制回放",
    )
    parser.add_argument("--record-dir", default="", help="录制目录（record/replay 必需）")
    parser.add_argument(
        "--transport",
        choices=("live", "scripted"),
        default="live",
        help="record 模式的传输：live=真实 HTTP（需要 key 与 --budget-usd）；"
        "scripted=用脚本模型当供应商（无网络，供端到端测试与录制演示）",
    )
    parser.add_argument("--model-name", default="gpt-4o-mini", help="record 模式下记录的模型名")
    parser.add_argument(
        "--model-key-env", default="OPENAI_API_KEY", help="key 所在的环境变量名（绝不写入仓库）"
    )
    parser.add_argument("--model-base-url", default="https://api.openai.com/v1")
    return parser.parse_args(argv)


def _policy_from_args(args: argparse.Namespace) -> CompactionPolicy:
    """把 CLI 参数变成压缩策略；缺省时用库默认值（不要在 CLI 里复制默认值）。"""
    policy = CompactionPolicy()
    if args.compaction_trigger is not None:
        policy = policy.model_copy(update={"trigger_fraction": args.compaction_trigger})
    if args.keep_recent_groups is not None:
        policy = policy.model_copy(update={"keep_recent_groups": args.keep_recent_groups})
    return policy


def _tamper_hook(request: ToolCallRequest) -> ToolCallRequest:
    """测试用对手：模拟"批准之后、执行之前"参数被改写。"""
    if request.tool == "scale_pool" and isinstance(request.args.get("size"), int):
        tampered = {**request.args, "size": request.args["size"] + 8}
        return request.model_copy(update={"args": tampered})
    return request


def build_llm(args: argparse.Namespace, *, budget: BudgetLedger, run_dir: Path, registry: object):
    """按 ``--model`` 造客户端。三条模式的边界在这里收口：

    * ``scripted``：默认路径，与既有实现**逐字节相同**（本函数只是把它挪进来）；
    * ``record``：必须显式给 ``--record-dir``；``--transport live`` 还必须给
      ``--budget-usd``（真实调用会花钱，无预算拒绝启动）与环境变量里的 key；
    * ``replay``：只读录制目录，未命中给可读错误。
    """
    window = ModelWindow(
        context_limit_tokens=args.window_tokens, max_output_tokens=args.max_output_tokens
    )
    if args.model == "scripted":
        return ScriptedLLMClient(
            ScriptedModel(args.scenario, steps=args.long_steps, lines=args.long_lines),
            cache=PrefixCacheModel(CacheConfig()),
            window=window,
            budget=budget,
        )
    if not args.record_dir:
        raise SystemExit(
            f"--model {args.model} 需要 --record-dir（录制物落在这里，默认不要写仓库）"
        )
    store = CassetteStore(args.record_dir)
    if args.model == "replay":
        return ReplayLLMClient(store=store, window=window, budget=budget)
    if args.transport == "live" and args.budget_usd <= 0:
        raise SystemExit(
            "live 录制必须显式给 --budget-usd（正数）：真实调用会花钱，无预算拒绝启动"
        )
    scripted = ScriptedModel(args.scenario, steps=args.long_steps, lines=args.long_lines)
    transport = (
        ScriptedTransport(scripted)
        if args.transport == "scripted"
        else _live_transport(args, registry)
    )
    meta = CassetteMeta(
        model=args.model_name if args.transport == "live" else f"scripted/{args.scenario}",
        provider="live" if args.transport == "live" else "scripted-transport",
        temperature=0.0,
        note=f"mode=record transport={args.transport} run_dir={run_dir.name}",
    )
    return RecordingLLMClient(transport, store=store, window=window, budget=budget, meta=meta)


def _live_transport(args: argparse.Namespace, registry: object):
    """真实 HTTP transport（延迟 import：CI 路径不会碰它，也不会因为缺配置而炸）。

    补一条 live 专用的系统消息（`LIVE_TOOL_USE_SYSTEM_PROMPT`）：实测真实模型在
    只看到脚本模型那份提示词时会用文字描述工具调用而不真正调用。该消息是**追加**的
    system 角色消息，不改动视图前缀，因此 scripted 路径与既有结论零影响。
    """
    from harness.live_transport import LiveChatTransport, tools_schema_from_registry
    from harness.prompts import LIVE_TOOL_USE_SYSTEM_PROMPT

    return LiveChatTransport(
        model=args.model_name,
        base_url=args.model_base_url,
        key_env=args.model_key_env,
        tools=tools_schema_from_registry(registry),
        system_prompt=LIVE_TOOL_USE_SYSTEM_PROMPT,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    ids_path = run_dir / "ids.json"

    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    world = World(run_dir / "world.db")
    saver = SqliteCheckpointSaver(store)
    chaos = Chaos.from_env(run_dir / "crash_marker.json", args.kill_after_ms)
    artifacts = ArtifactStore(run_dir / "artifacts")
    registry = build_registry(
        world,
        idempotent_impl=args.tool_idem == "on",
        probe_enabled=args.probe == "on",
        artifacts=artifacts,
    )
    builder = ViewBuilder(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        registry=registry,
        artifacts=artifacts,
        max_inline_tokens=args.max_inline_tokens,
        dynamic_at_head=args.dynamic_at_head == "on",
    )
    budget: BudgetLedger | None = None
    compactor: Compactor | None = None
    if args.mode == "run":
        ids = {
            "run_id": new_id("run"),
            "thread_id": new_id("thr"),
            "branch_id": new_id("br"),
        }
        ids_path.write_text(json.dumps(ids), encoding="utf-8")
        budget = BudgetLedger(
            store,
            run_id=ids["run_id"],
            branch_id=ids["branch_id"],
            limits=BudgetLimits(total_usd=args.budget_usd),
        )
        if args.compaction == "on":
            compactor = Compactor(
                store,
                artifacts=artifacts,
                policy=_policy_from_args(args),
                chaos=chaos,
                budget=budget,
            )
    else:
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
        budget = BudgetLedger(
            store,
            run_id=ids["run_id"],
            branch_id=ids["branch_id"],
            limits=BudgetLimits(total_usd=args.budget_usd),
        )
        if args.compaction == "on":
            compactor = Compactor(
                store,
                artifacts=artifacts,
                policy=_policy_from_args(args),
                chaos=chaos,
                budget=budget,
            )
    llm = build_llm(args, budget=budget, run_dir=run_dir, registry=registry)
    loop = Loop(
        store,
        saver,
        llm=llm,
        registry=registry,
        builder=builder,
        compactor=compactor,
        budget=budget,
        dedup=args.dedup == "on",
        outbox=args.outbox == "on",
        chaos=chaos,
        tamper=_tamper_hook if args.tamper == "on" else None,
    )

    try:
        if args.mode == "run":
            store.create_run(ids["run_id"], thread_id=ids["thread_id"])
            store.create_branch(ids["branch_id"], ids["run_id"])
            outcome = loop.start(
                run_id=ids["run_id"],
                thread_id=ids["thread_id"],
                branch_id=ids["branch_id"],
                task=TASK,
            )
        elif args.mode == "approve":
            ids = json.loads(ids_path.read_text(encoding="utf-8"))
            decision = DECISION_REJECTED if args.approve_mode == "reject" else DECISION_APPROVED
            approved_args = (
                {"service": "payment", "size": 32} if args.approve_mode == "edit" else None
            )
            binding = loop.approve(
                run_id=ids["run_id"],
                thread_id=ids["thread_id"],
                branch_id=ids["branch_id"],
                decision=decision,
                actor="human:oncall",
                approved_args=approved_args,
            )
            print(
                json.dumps(
                    {
                        "mode": "approve",
                        "decision": binding.decision,
                        "edited": binding.edited,
                        "tool_call_id": binding.tool_call_id,
                        "approved_args_sha256": binding.approved_args_sha256[:16],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        else:
            outcome = loop.resume(
                run_id=ids["run_id"],
                thread_id=ids["thread_id"],
                branch_id=ids["branch_id"],
            )
    finally:
        world.close()

    # 独立测试 P2-6：`replay_warnings` 原先只在 LLM client 里 append，worker 不打印、
    # 不落盘——"提示词与录制不一致"这条告警在端到端**没有出口**。
    # 这里把它写进 outcome JSON（机器可读）与 stdout（人可读），但**不判失败**：
    # 崩溃恢复会合法改变视图，判失败会让崩溃轨迹永远无法回放。
    replay_warnings = list(getattr(llm, "replay_warnings", []) or [])
    outcome_payload = json.loads(outcome.model_dump_json())
    outcome_payload["replay_warnings"] = replay_warnings
    (run_dir / f"outcome_{args.mode}.json").write_text(
        json.dumps(outcome_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": args.mode,
                "status": outcome.status,
                "step": outcome.step,
                "executed": outcome.executed,
                "replayed": outcome.replayed,
                "reconciled": outcome.reconciled,
                "unknown": outcome.unknown,
                "rejected": outcome.rejected,
                "probes": outcome.probes,
                "checkpoints": outcome.checkpoints,
                "compactions": outcome.compactions,
                "overflows": outcome.overflows,
                "input_tokens": outcome.input_tokens,
                "cache_read_tokens": outcome.cache_read_tokens,
                "cache_write_tokens": outcome.cache_write_tokens,
                "cost_usd": outcome.cost_usd,
                "view_violations": outcome.view_violations,
                "effects": world_total_effects(run_dir),
                "replay_warnings": replay_warnings,
            },
            ensure_ascii=False,
        )
    )
    if replay_warnings:
        # 独立测试 P2-6：告警原先只在 LLM client 里躺着，端到端没有任何出口。
        # 这里同时给人（stderr）与机器（stdout 的 JSON 字段 + outcome 文件）两条路；
        # 但**不判失败**：崩溃恢复会合法改变视图，把它当错误会让崩溃轨迹无法回放。
        print(
            f"[replay] {len(replay_warnings)} 条警告（不判失败，见 docs/model-modes.md §4）：",
            file=sys.stderr,
        )
        for warning in replay_warnings[:5]:
            print(f"  - {json.dumps(warning, ensure_ascii=False)}", file=sys.stderr)
    store.close()
    return 0


def world_total_effects(run_dir: Path) -> int:
    with World(run_dir / "world.db") as world:
        return world.total_effects()


if __name__ == "__main__":
    sys.exit(main())
