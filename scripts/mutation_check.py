"""变异测试门禁（R-A5）：把"测试盲区"变成一条会红的门禁。

语义：**幸存的变异 = 测试盲区**。变异测试与 `tests/test_audit_regressions.py` 互补——
审计回归防已知的 P0 复活，变异测试探没人写过的分支。

为什么是"防倒退基线"而不是"追求某个绝对值"：
* 这三个模块里相当一部分路径由**子进程驱动**的崩溃矩阵与四系统评测覆盖（`experiments/`、
  `opsenv/`），它们不在 `pytest` 进程内，mutmut 看不见——因此幸存清单里必然包含
  "其实被更外层验证保护着"的变异。把幸存率当绝对质量分会得出错误结论。
* 真正要防的是：**新增代码把盲区扩大**。所以首版把当前幸存清单冻结成基线
  （`reports/mutation_baseline.json`），之后只判"有没有**新增**幸存变异"。

用法::

    .venv/bin/python scripts/mutation_check.py                     # 判定（CI nightly 用）
    .venv/bin/python scripts/mutation_check.py --update-baseline   # 显式刷新基线（PR 里说明）
    .venv/bin/python scripts/mutation_check.py --module harness/approval.py --timeout 300

纪律：

* **超时即失败**：跑不完不许当成功（"没跑完"和"没发现回归"是两件事）；
* 新增幸存变异 → 退出 1，并逐条打印 `mutmut show` 的 diff（失败必须可读）；
* 基线只能由 `--update-baseline` 改写，且必须在 PR 描述里写明为什么放宽。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = PROJECT_ROOT / "reports" / "mutation_baseline.json"
DEFAULT_MODULES = ("harness/loop.py", "harness/store/checkpoints.py", "harness/approval.py")


def _mutmut_argv(module: str | None) -> list[str]:
    argv = [sys.executable, "-m", "mutmut", "run"]
    if module:
        argv.append(module)
    return argv


def run_mutmut(*, module: str | None, timeout: float, max_children: int) -> None:
    argv = _mutmut_argv(module)
    print(f"[mutation] 运行: {' '.join(argv)} --max-children {max_children}（上限 {timeout:.0f}s）")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [*argv, "--max-children", str(max_children)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"[mutation] 超时失败：{timeout:.0f} 秒内没跑完（module={module or '全部'}）。\n"
            "  这是**失败**而不是跳过：跑不完就无法判定「有没有新增盲区」。\n"
            "  处置：缩小 --module 范围、提高 runner 并行度，或在 PR 里记录本次跳过及原因。\n"
            "  已产生的输出尾部："
            f"{(exc.stdout or '')[-200:] if isinstance(exc.stdout, str) else ''}"
        ) from exc
    print(f"[mutation] 用时 {time.perf_counter() - started:.1f}s")
    if proc.returncode != 0:
        # `mutmut run` 非 0 = 本轮没跑成功（配置没生效、collect error、被 OOM 杀…）。
        # 绝不能继续判定：mutmut 的结果读的是 `mutants/` 缓存，失败时残留的旧结果
        # 会被当成"这一轮的结果"（评审 P0-1）。
        raise SystemExit(
            f"[mutation] `mutmut run` 退出码 {proc.returncode}：本轮没有跑成功，判定作废。\n"
            f"  stdout 尾部：{proc.stdout.strip()[-300:]}\n"
            f"  stderr 尾部：{proc.stderr.strip()[-300:]}"
        )


def collect_status() -> dict[str, list[str]]:
    """读 mutmut 的结果表：``{status: [mutant 名]}``。"""
    proc = subprocess.run(
        # mutmut 3.x 的 --all 是**带值**选项（default=False 且没有 is_flag），
        # 必须写 --all=true，否则报 "Option '--all' requires an argument"。
        # 要全量是因为 survivor_rate 需要 killed 数作分母（只看非 killed 会把比率算成 1.0）。
        [sys.executable, "-m", "mutmut", "results", "--all=true"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise SystemExit(f"[mutation] 读结果失败：{proc.stderr[-400:]}")
    buckets: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, status = line.rpartition(":")
        buckets.setdefault(status.strip(), []).append(name.strip())
    return buckets


def mutation_summary(buckets: dict[str, list[str]]) -> dict[str, Any]:
    killed = len(buckets.get("killed", []))
    survived = sorted(buckets.get("survived", []))
    no_tests = sorted(buckets.get("no tests", []))
    decided = killed + len(survived)
    return {
        "killed": killed,
        "survived": survived,
        "no_tests": no_tests,
        "total": killed + len(survived) + len(no_tests),
        # 幸存率只在"被判定的变异体"上算：no tests 是另一类盲区（连覆盖都没有），
        # 混进分母会让这个数看起来更好看。
        "survivor_rate": (len(survived) / decided) if decided else 0.0,
    }


def show_mutant(name: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mutmut", "show", name],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout.strip()[-600:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/mutation_check.py")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--json-out", default="", help="{out} 占位符的落点（claim 对账用）")
    parser.add_argument("--module", default="", help="只跑一个模块（默认跑配置里的三个）")
    parser.add_argument("--timeout", type=float, default=780.0, help="整体墙钟上限（秒）")
    parser.add_argument("--max-children", type=int, default=4)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--max-report", type=int, default=10, help="最多展示几条新增幸存变异")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="只读基线并输出 JSON 摘要（不跑 mutmut）：给 claim 门禁做对账用",
    )
    args = parser.parse_args(argv)

    if args.summary_only:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"没有基线文件 {baseline_path}")
            return 1
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        summary_payload = {
            "counts": payload["counts"],
            "survivor_rate": payload["survivor_rate"],
            "survivors": len(payload["survivors"]),
            "modules": payload["modules"],
        }
        print(json.dumps(summary_payload, ensure_ascii=False))
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 0

    run_mutmut(
        module=args.module or None, timeout=args.timeout, max_children=args.max_children
    )
    buckets = collect_status()
    summary = mutation_summary(buckets)
    print(
        f"[mutation] killed={summary['killed']} survived={len(summary['survived'])} "
        f"no_tests={len(summary['no_tests'])} survivor_rate={summary['survivor_rate']:.3f}"
    )

    baseline_path = Path(args.baseline)
    previous: set[str] = set()
    if baseline_path.exists():
        previous = set(json.loads(baseline_path.read_text(encoding="utf-8"))["survivors"])

    if summary["total"] == 0:
        # 评审 P0-1：`mutmut results` 在没有结果时**退出 0 且无输出**，于是
        # survived=[] ⇒ new_survivors=[] ⇒ 打印"没有新增幸存变异" ⇒ 退出 0（绿）。
        # 那意味着"配置错 / 缓存被清 / collect error"都会让 nightly 静静变绿。
        print(
            "[mutation] 本轮**没有产出任何变异体**（total=0）：这不是「没有盲区」，"
            "而是「没跑起来」，判定失败。\n"
            "  常见原因：mutmut 配置未生效、`mutants/` 被清空、collect error、runner 崩溃。"
        )
        return 1

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "schema": "mutation-baseline/v1",
                    "tool": "mutmut 3.x（见 pyproject.toml [tool.mutmut]）",
                    "modules": list(DEFAULT_MODULES),
                    "command": "mutmut run --max-children 4（nightly job `mutation`）",
                    "counts": {
                        "killed": summary["killed"],
                        "survived": len(summary["survived"]),
                        "no_tests": len(summary["no_tests"]),
                        "total": summary["total"],
                    },
                    "survivor_rate": round(summary["survivor_rate"], 4),
                    "survivors": summary["survived"],
                    "no_tests": summary["no_tests"],
                    "note": (
                        "幸存变异 = 测试盲区。基线只用来防「新增盲区」，不是质量分："
                        "这三个模块的部分路径由子进程驱动的崩溃矩阵/四系统评测覆盖，"
                        "mutmut 看不见，因此清单里包含「其实被外层验证保护着」的变异。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[mutation] 基线已更新：{baseline_path}")
        return 0

    if not previous:
        print(f"[mutation] 没有基线文件 {baseline_path}：请先跑 --update-baseline 并入库")
        return 1

    new_survivors = [name for name in summary["survived"] if name not in previous]
    fixed = [name for name in previous if name not in set(summary["survived"])]
    if fixed:
        print(f"[mutation] 有 {len(fixed)} 条基线里的幸存变异已被杀死（好事，不判失败）")
    if not new_survivors:
        print("[mutation] 没有新增幸存变异：测试盲区没有扩大")
        return 0

    print(f"\n[mutation] 新增幸存变异 {len(new_survivors)} 条（= 新增的测试盲区）：")
    for name in new_survivors[: args.max_report]:
        print(f"\n--- {name}")
        print(show_mutant(name))
    if len(new_survivors) > args.max_report:
        print(f"\n（其余 {len(new_survivors) - args.max_report} 条省略，用 mutmut results 查看）")
    print(
        "\n处置：给这些分支补用例（docs/testing.md §3：新增机制三件套），"
        "或者——如果放宽是有意的——在 PR 里说明并跑 --update-baseline。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
