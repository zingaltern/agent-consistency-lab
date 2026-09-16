"""W8 demo：一条命令跑通"告警 → 取证 → 审批 → 执行 → trace 回放"。

```bash
.venv/bin/python -m examples.ops_demo            # 默认：一次称职值班人 + 一次橡皮图章（对比）
.venv/bin/python -m examples.ops_demo --scenario slow_query-00
```

它刻意做两件事：
1. 用**真实的 opsenv 场景**跑一遍完整流程（走 Loop 的公开 API：审批、outbox、事件日志）；
2. 把同一次运行投影成 trace，让人看到"每个 span 从哪来"——
   事件日志是权威、trace 只是投影。
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
from pathlib import Path

from harness.store import SqliteStore
from harness.trace import build_trace, render_text
from opsenv.scenario import build_catalog
from opsenv.suite import PROFILES
from opsenv.systems import LazyOperator, Operator, run_harness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="examples.ops_demo")
    parser.add_argument("--scenario", default="disk_full-00", help="场景 id（默认 disk_full-00）")
    parser.add_argument("--profile", default="competent-honest", help="推理器人格")
    args = parser.parse_args(argv)

    catalog = build_catalog(per_fault=8)
    scenario = next((s for s in catalog if s.id == args.scenario), None)
    if scenario is None:
        print(f"未知场景 {args.scenario!r}；可选例如: " + ", ".join(s.id for s in catalog[:3]))
        return 2
    profile = next(p for p in PROFILES if p.name == args.profile)

    workdir = Path(tempfile.mkdtemp(prefix="ops-demo-"))
    print(f"场景 {scenario.id}：{scenario.fault}（决定性证据在 {scenario.decisive_channel} 通道）")
    red_lines = list(scenario.forbidden_actions)
    if scenario.novel_forbidden:
        red_lines.append(scenario.novel_forbidden)
    print(f"  期望处置：{scenario.expected_action}｜红线动作：{red_lines}")
    print(
        f"  推理器：{profile.name}（证据充分时正确率 {profile.competence:.0%}）\n"
    )

    # 1) 称职值班人：走完整流程（含审批门）
    print("== ① 称职值班人（拒绝破坏性动作）==")
    result = run_harness(
        scenario=scenario,
        profile=profile,
        rng=random.Random(7),
        operator=Operator(),
        workdir=workdir / "oracle",
    )
    print(f"  诊断={result.diagnosis} 动作={result.action} 正确={result.correct}")
    print(f"  取证通道={result.channels_read} 步数={result.steps}")
    print(f"  走审批={result.gated} 被拦下={result.blocked} 执行={result.executed_actions}")
    print(f"  成本=${result.cost_usd:.5f} 红线={result.red_line}")

    # 2) 消融对照：扫到"模型出错"的那一次，同一个 seed 下比较两种值班人
    print("\n== ② 消融：gate 的价值来自人，还是来自架构？==")
    weak = next(p for p in PROFILES if p.name == "weak-guesser")
    for seed in range(12):
        oracle = run_harness(
            scenario=scenario,
            profile=weak,
            rng=random.Random(seed),
            operator=Operator(),
            workdir=workdir / f"ab-o-{seed}",
        )
        lazy = run_harness(
            scenario=scenario,
            profile=weak,
            rng=random.Random(seed),
            operator=LazyOperator(),
            workdir=workdir / f"ab-l-{seed}",
        )
        if lazy.red_line != oracle.red_line:
            print(f"  （同一个推理器 weak-guesser、同一个 seed={seed}、同一个场景）")
            print(
                f"  称职值班人：动作={oracle.action} 被拦={oracle.blocked} 红线={oracle.red_line}"
            )
            print(f"  橡皮图章　：动作={lazy.action} 被拦={lazy.blocked} 红线={lazy.red_line}")
            print("  ↑ 两条路线的 runtime 完全一样——差别只在'有没有人把关'")
            break
    else:
        print("  这 12 个 seed 下推理器都判断正确，没触发对照（换个场景试试）")

    # 3) trace 回放
    print("\n== ③ trace 投影（事件日志是权威，trace 只是投影）==")
    store = SqliteStore(workdir / "oracle" / "runtime.db")
    store.setup()
    try:
        trace = build_trace(store.effective_events(f"br-{scenario.id}"))
    finally:
        store.close()
    print(render_text(trace))

    print("\n提示：任意历史 run 都能这样回放：")
    print("  .venv/bin/python -m harness.trace --run-dir <run 目录> [--otel]")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
