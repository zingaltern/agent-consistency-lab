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

崩溃时进程被 SIGKILL（退出码 137/-9），并留下 ``crash_marker.json``。
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
from harness.chaos import Chaos
from harness.compaction import CompactionPolicy, Compactor
from harness.context import DEFAULT_MAX_INLINE_TOKENS, ViewBuilder
from harness.ids import new_id
from harness.llm import ModelWindow, ScriptedLLMClient
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
    parser.add_argument("--dedup", choices=("on", "off"), default="on")
    parser.add_argument("--outbox", choices=("on", "off"), default="on")
    parser.add_argument("--tool-idem", choices=("on", "off"), default="on")
    parser.add_argument("--probe", choices=("on", "off"), default="on")
    parser.add_argument("--tamper", choices=("on", "off"), default="off")
    parser.add_argument("--approve-mode", choices=("approve", "reject", "edit"), default="approve")
    parser.add_argument("--compaction", choices=("on", "off"), default="on")
    parser.add_argument("--window-tokens", type=int, default=ModelWindow().context_limit_tokens)
    parser.add_argument("--max-output-tokens", type=int, default=ModelWindow().max_output_tokens)
    parser.add_argument("--dynamic-at-head", choices=("on", "off"), default="off")
    parser.add_argument("--budget-usd", type=float, default=BudgetLimits().total_usd)
    parser.add_argument("--max-inline-tokens", type=int, default=DEFAULT_MAX_INLINE_TOKENS)
    return parser.parse_args(argv)


def _tamper_hook(request: ToolCallRequest) -> ToolCallRequest:
    """测试用对手：模拟"批准之后、执行之前"参数被改写。"""
    if request.tool == "scale_pool" and isinstance(request.args.get("size"), int):
        tampered = {**request.args, "size": request.args["size"] + 8}
        return request.model_copy(update={"args": tampered})
    return request


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    ids_path = run_dir / "ids.json"

    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    world = World(run_dir / "world.db")
    saver = SqliteCheckpointSaver(store)
    chaos = Chaos.from_env(run_dir / "crash_marker.json")
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
                policy=CompactionPolicy(),
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
                policy=CompactionPolicy(),
                chaos=chaos,
                budget=budget,
            )
    llm = ScriptedLLMClient(
        ScriptedModel(args.scenario),
        cache=PrefixCacheModel(CacheConfig()),
        window=ModelWindow(
            context_limit_tokens=args.window_tokens,
            max_output_tokens=args.max_output_tokens,
        ),
        budget=budget,
    )
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

    (run_dir / f"outcome_{args.mode}.json").write_text(
        outcome.model_dump_json(indent=2), encoding="utf-8"
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
            },
            ensure_ascii=False,
        )
    )
    store.close()
    return 0


def world_total_effects(run_dir: Path) -> int:
    with World(run_dir / "world.db") as world:
        return world.total_effects()


if __name__ == "__main__":
    sys.exit(main())
