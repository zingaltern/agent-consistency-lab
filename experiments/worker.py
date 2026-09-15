"""子进程 worker：一次 run 或一次 resume，运行在独立进程里。

必须独立进程的原因：崩溃注入用 SIGKILL，进程内的任何状态（连接、锁、内存）
都必须真实地随进程消失，才能验证"恢复只能依赖持久化的事实"。

用法::

    CHAOS_WINDOWS=pre_tool_exec:2 python -m experiments.worker \
        --run-dir /tmp/cell0 --mode run --dedup on --tool-idem off

崩溃时进程被 SIGKILL（退出码 -9），并留下 ``crash_marker.json``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
from fakeworld.world import World
from harness.chaos import Chaos
from harness.ids import new_id
from harness.loop import Loop
from harness.store.checkpoints import SqliteCheckpointSaver
from harness.store.sqlite_store import SqliteStore

TASK = "支付服务 P99 告警，请定位并处置。"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="experiments.worker")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=("run", "resume"), default="run")
    parser.add_argument("--scenario", default=SCENARIO_POOL_EXHAUSTION)
    parser.add_argument("--dedup", choices=("on", "off"), default="on")
    parser.add_argument("--tool-idem", choices=("on", "off"), default="on")
    return parser.parse_args(argv)


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
    registry = build_registry(world, idempotent_impl=args.tool_idem == "on")
    loop = Loop(store, saver, dedup=args.dedup == "on", chaos=chaos)
    model = ScriptedModel(args.scenario)

    try:
        if args.mode == "run":
            ids = {
                "run_id": new_id("run"),
                "thread_id": new_id("thr"),
                "branch_id": new_id("br"),
            }
            ids_path.write_text(json.dumps(ids), encoding="utf-8")
            store.create_run(ids["run_id"], thread_id=ids["thread_id"])
            store.create_branch(ids["branch_id"], ids["run_id"])
            outcome = loop.start(
                run_id=ids["run_id"],
                thread_id=ids["thread_id"],
                branch_id=ids["branch_id"],
                model=model,
                registry=registry,
                task=TASK,
            )
        else:
            ids = json.loads(ids_path.read_text(encoding="utf-8"))
            outcome = loop.resume(
                run_id=ids["run_id"],
                thread_id=ids["thread_id"],
                branch_id=ids["branch_id"],
                model=model,
                registry=registry,
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
                "checkpoints": outcome.checkpoints,
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
