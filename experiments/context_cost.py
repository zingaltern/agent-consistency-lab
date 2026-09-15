"""W4 成本实验：缓存纪律、卸载、压缩三个杠杆的对照。

三个问题，各用一种对照回答：

* **E1 缓存纪律**：动态信息放尾部 vs 塞进前缀 → 命中率与净成本差多少？
* **E2 卸载**：大工具结果内联 vs 卸载成 artifact → 溢不溢出、省多少 token？
* **E3 压缩**：小窗口下无压缩 vs 有压缩 → 任务能不能跑完、代价多少？

度量取自运行日志本身（每条 ``agent_message`` 都带 context_tokens / cache_read /
cache_write / cost_usd），因此报告的数字可以被任何人在同一 run dir 上复算。
所有结论都限定在本项目的缓存模型与脚本模型下——不是真实 API 的实测。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.store.sqlite_store import SqliteStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 每个常量都是"一组 CLI 参数"，组合时直接展开
LONG = (("--scenario", "long_incident"),)
BIG_INLINE = (("--max-inline-tokens", "100000"),)
SMALL_WINDOW = (("--window-tokens", "10000"),)
SMALL_OUTPUT = (("--max-output-tokens", "1024"),)
NO_COMPACTION = (("--compaction", "off"),)
DYNAMIC_AT_HEAD = (("--dynamic-at-head", "on"),)


@dataclass(frozen=True)
class Config:
    name: str
    question: str
    flags: tuple[tuple[str, str], ...] = ()

    def argv(self) -> list[str]:
        out: list[str] = []
        for flag, value in self.flags:
            out.extend([flag, value])
        return out


CONFIGS: tuple[Config, ...] = (
    Config(
        name="E1a-纪律-动态在尾部",
        question="E1",
        flags=(*LONG,),
    ),
    Config(
        name="E1b-反面-动态进前缀",
        question="E1",
        flags=(*LONG, *DYNAMIC_AT_HEAD),
    ),
    Config(
        name="E2a-大结果内联-小窗口-无压缩",
        question="E2/E3",
        flags=(*LONG, *BIG_INLINE, *SMALL_WINDOW, *SMALL_OUTPUT, *NO_COMPACTION),
    ),
    Config(
        name="E2b-大结果内联-小窗口-有压缩",
        question="E2/E3",
        flags=(*LONG, *BIG_INLINE, *SMALL_WINDOW, *SMALL_OUTPUT),
    ),
    Config(
        name="E2c-结果卸载-小窗口-无压缩",
        question="E2",
        flags=(*LONG, *SMALL_WINDOW, *SMALL_OUTPUT, *NO_COMPACTION),
    ),
)


@dataclass
class Metrics:
    name: str
    status: str = "?"
    calls: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    compactions: int = 0
    overflows: int = 0
    view_violations: int = 0
    effects: int = 0
    per_call_read_ratio: list[float] = field(default_factory=list)
    view_sizes: list[int] = field(default_factory=list)

    @property
    def read_ratio(self) -> float:
        return self.cache_read / self.input_tokens if self.input_tokens else 0.0

    @property
    def cost_per_call(self) -> float:
        return self.cost_usd / self.calls if self.calls else 0.0

    @property
    def tokens_per_call(self) -> float:
        return self.input_tokens / self.calls if self.calls else 0.0


def run_config(config: Config, workroot: Path) -> Metrics:
    run_dir = workroot / config.name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    summary: dict[str, Any] = {}
    for mode in ("run", "approve", "resume"):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "experiments.worker",
                "--run-dir",
                str(run_dir),
                "--mode",
                mode,
                "--tool-idem",
                "off",
                *config.argv(),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.stdout.strip():
            summary = json.loads(proc.stdout.strip().splitlines()[-1])
    return collect(config.name, run_dir, summary)


def collect(name: str, run_dir: Path, summary: dict[str, Any]) -> Metrics:
    """指标全部从事件日志推导（阶段无关），因此可以被任何人复算。

    每次模型调用的 context_tokens / cache_read / cache_write / cost 都写在
    ``agent_message`` 事件里——这是"trace 即数据源"的最小演示。
    """
    from fakeworld.world import World
    from harness.state import reduce_events

    metrics = Metrics(name=name)
    ids_path = run_dir / "ids.json"
    if not ids_path.exists():
        metrics.status = str(summary.get("status", "?"))
        return metrics
    ids = json.loads(ids_path.read_text(encoding="utf-8"))

    store = SqliteStore(run_dir / "runtime.db")
    try:
        store.setup()
        events = store.effective_events(ids["branch_id"])
        for event in events:
            if event.type == "agent_message":
                context_tokens = int(event.payload.get("context_tokens", 0) or 0)
                read = int(event.payload.get("cache_read_tokens", 0) or 0)
                write = int(event.payload.get("cache_write_tokens", 0) or 0)
                metrics.calls += 1
                metrics.input_tokens += context_tokens
                metrics.cache_read += read
                metrics.cache_write += write
                metrics.cost_usd += float(event.payload.get("cost_usd", 0.0) or 0.0)
                metrics.view_sizes.append(context_tokens)
                metrics.per_call_read_ratio.append(read / context_tokens if context_tokens else 0.0)
            elif event.type == "compaction":
                metrics.compactions += 1
            elif event.type == "error" and event.payload.get("error_class") == "context_overflow":
                metrics.overflows += 1
        state, _ = reduce_events(events)
        metrics.status = state.status.value
    finally:
        store.close()

    world_path = run_dir / "world.db"
    if world_path.exists():
        with World(world_path) as world:
            metrics.effects = world.total_effects()
    return metrics


def render(metrics: list[Metrics]) -> str:
    lines = [
        "| 配置 | 状态 | 调用数 | 输入 token | 命中读 | 缓存写 | 命中率 | 总成本 | 成本/调用 |"
        " token/调用 | 压缩次数 | 溢出 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in metrics:
        lines.append(
            f"| {item.name} | {item.status} | {item.calls} | {item.input_tokens} |"
            f" {item.cache_read} | {item.cache_write} | {item.read_ratio:.1%} |"
            f" ${item.cost_usd:.5f} | ${item.cost_per_call:.5f} | {item.tokens_per_call:.0f} |"
            f" {item.compactions} | {item.overflows} |"
        )
    return "\n".join(lines)


def findings(metrics: list[Metrics]) -> str:
    by_name = {item.name: item for item in metrics}
    lines: list[str] = []

    tail = by_name.get("E1a-纪律-动态在尾部")
    head = by_name.get("E1b-反面-动态进前缀")
    if tail and head and tail.calls and head.calls:
        delta = (head.cost_per_call - tail.cost_per_call) / tail.cost_per_call
        lines.append(
            f"* **E1 缓存纪律**：动态信息塞进前缀后，命中率 "
            f"{tail.read_ratio:.1%} → {head.read_ratio:.1%}，成本/调用 "
            f"${tail.cost_per_call:.5f} → ${head.cost_per_call:.5f}（{delta:+.1%}）。"
            "前缀一旦被每步变化的内容污染，整个上下文都不再是缓存命中。"
        )

    inline = by_name.get("E2a-大结果内联-小窗口-无压缩")
    compaction = by_name.get("E2b-大结果内联-小窗口-有压缩")
    offload = by_name.get("E2c-结果卸载-小窗口-无压缩")
    if inline:
        lines.append(
            f"* **E2 卸载**：大结果内联、小窗口且不做压缩时任务以 "
            f"`{inline.status}` 结束（溢出 {inline.overflows} 次）；"
            + (
                f"改为卸载后同窗口下 `{offload.status}`、溢出 {offload.overflows} 次（token/调用 "
                f"{offload.tokens_per_call:.0f} vs {inline.tokens_per_call:.0f}）。"
                if offload
                else ""
            )
        )
    if inline and compaction:
        lines.append(
            f"* **E3 压缩**：同样内联、同样小窗口，开压缩后 `{compaction.status}`"
            f"（压缩 {compaction.compactions} 次，溢出 {compaction.overflows} 次），"
            f"但成本/调用 ${compaction.cost_per_call:.5f}"
            f" 高于不压缩的 ${inline.cost_per_call:.5f}，"
            f"命中率 {compaction.read_ratio:.1%} 低于 {inline.read_ratio:.1%}——"
            "**压缩是用可用性换成本：它降低 token 消耗，却因为击穿前缀缓存而抬高净成本**。"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.context_cost")
    parser.add_argument("--workroot", default="")
    parser.add_argument("--md-out", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    root = Path(args.workroot) if args.workroot else Path(tempfile.mkdtemp(prefix="w4-cost-"))
    metrics = [run_config(config, root) for config in CONFIGS]
    table = render(metrics)
    print(table)
    print()
    print(findings(metrics))
    if args.md_out:
        Path(args.md_out).write_text(table + "\n\n" + findings(metrics) + "\n", encoding="utf-8")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([item.__dict__ for item in metrics], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
