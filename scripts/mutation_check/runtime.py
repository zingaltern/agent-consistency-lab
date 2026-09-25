"""runner 与命令行：调 mutmut、算墙钟、组装产物、判定并决定退出码。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .baseline import (
    _summary_payload,
    apply_baseline_refresh_plan,
    baseline_refresh_plan,
    baseline_widening,
    load_baseline,
)
from .config import DEFAULT_BASELINE, DEFAULT_MODULES, MUTANTS_DIR, PROJECT_ROOT
from .fingerprint import (
    FINGERPRINT_ALGORITHM,
    compute_mutant_fingerprints,
    fingerprint_comparison_lines,
    fingerprint_report,
)
from .gate import _invisible_space_lines, gate_verdict
from .reporting import _abort, _write_json_out
from .status import (
    all_mutant_names,
    collect_status,
    mutation_summary,
    show_mutant,
)


def normalize_module_filter(value: str) -> str:
    """把 ``--module`` 的参数翻译成 `mutmut run` 的位置参数。

    `mutmut run` 的位置参数是**变异体名**的 fnmatch 模式（`harness.approval.xǁ…__mutmut_1`），
    不是文件路径。首版把 `DEFAULT_MODULES` 里的**路径**直接传进去，于是
    `--module harness/approval.py` 恒失败（`AssertionError: Filtered for specific mutants,
    but nothing matches`）——独立验证报告 P1-1。

    这里接受两种写法：文件路径（`harness/approval.py` → `harness.approval.*`）与
    变异体名 glob（原样传下去，例如 `harness.approval.*` 或某一条完整的变异体名）。
    """
    value = value.strip()
    if not value:
        return value
    if value.endswith(".py") or "/" in value:
        # 只按"以 .py 结尾"或"含路径分隔符"判定为路径；其余（含 `.` 的 glob）原样透传。
        parts = list(Path(value).with_suffix("").parts)
        return ".".join(parts) + ".*"
    return value


def _mutmut_argv(module: str | None) -> list[str]:
    argv = [sys.executable, "-m", "mutmut", "run"]
    if module:
        argv.append(module)
    return argv



def run_mutmut(
    *, module: str | None, timeout: float, max_children: int, json_out: str = ""
) -> float:
    """跑 mutmut，返回这一轮的**墙钟秒数**（写进 `--json-out` 的 `elapsed_s`）。

    为什么要返回值：预算够不够是唯一一个**只有 CI 能回答**的定量问题（独立验证
    2026-09-18 · 报告 §5-1），而先前只有 print 一行用时、不落盘 ⇒ 夜里跑完也留不下数。
    返回的区间是"这一条 mutmut 子进程的墙钟"，与 nightly 的 job 墙钟
    （`gh run view <id> --json jobs`，含 checkout/install）对照即可判断余量。
    """
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
        _abort(
            "timeout",
            f"[mutation] 超时失败：{timeout:.0f} 秒内没跑完（module={module or '全部'}）。\n"
            "  这是**失败**而不是跳过：跑不完就无法判定「有没有新增盲区」。\n"
            "  处置：缩小 --module 范围、提高 runner 并行度，或在 PR 里记录本次跳过及原因。\n"
            "  已产生的输出尾部："
            f"{(exc.stdout or '')[-200:] if isinstance(exc.stdout, str) else ''}",
            json_out=json_out,
            extra={"elapsed_s": round(time.perf_counter() - started, 1)},
        )
    elapsed = time.perf_counter() - started
    print(f"[mutation] 用时 {elapsed:.1f}s")
    if proc.returncode != 0:
        # `mutmut run` 非 0 = 本轮没跑成功（配置没生效、collect error、被 OOM 杀…）。
        # 绝不能继续判定：mutmut 的结果读的是 `mutants/` 缓存，失败时残留的旧结果
        # 会被当成"这一轮的结果"（评审 P0-1）。
        _abort(
            "mutmut_run_failed",
            f"[mutation] `mutmut run` 退出码 {proc.returncode}：本轮没有跑成功，判定作废。\n"
            f"  stdout 尾部：{proc.stdout.strip()[-300:]}\n"
            f"  stderr 尾部：{proc.stderr.strip()[-300:]}",
            json_out=json_out,
            extra={"elapsed_s": round(elapsed, 1)},
        )
    return elapsed



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.mutation_check")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument(
        "--json-out",
        default="",
        help="把本轮摘要写成 JSON 到这里（判定失败时也会写：nightly 要能上传它）",
    )
    parser.add_argument(
        "--module",
        default="",
        help="只跑一部分变异体：接受文件路径（`harness/approval.py`，会翻译成 "
        "`harness.approval.*`）或变异体名 glob（`harness.approval.*`、某条完整变异体名）。"
        "默认跑 pyproject.toml [tool.mutmut].only_mutate 里的全部四个模块。"
        "**注意**：它只影响「哪些变异体被重新评估」，判定读的是 `mutmut results` 的**全量**结果表，"
        "所以 `--baseline` 必须仍然是那份全量基线（传切片基线会把所有没被切中的变异体"
        "误报成新增）。",
    )
    parser.add_argument("--timeout", type=float, default=780.0, help="整体墙钟上限（秒）")
    parser.add_argument("--max-children", type=int, default=4)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--allow-incremental-refresh",
        action="store_true",
        help="与 --update-baseline 连用：明知 mutants/ 缓存不干净仍然沿用旧判决"
        "（会打出警告，且写出的基线可能是新旧混合）。默认不允许——刷新基线必须全量重跑。",
    )
    parser.add_argument(
        "--allow-wider-baseline",
        action="store_true",
        help="与 --update-baseline 连用：候选基线比旧基线**更宽**时也写下去"
        "（新增幸存 / 新增 no tests / 无结论增长 / 旧条目整条不见）。默认拒绝写入——"
        "刷新基线不许顺手把这一轮的盲区冻进去；要放行必须显式，且条件会记进基线。",
    )
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
        summary_payload = _summary_payload(payload)
        print(json.dumps(summary_payload, ensure_ascii=False))
        _write_json_out(args.json_out, summary_payload)
        return 0

    module_filter = normalize_module_filter(args.module)
    if args.module and module_filter != args.module:
        print(f"[mutation] `--module {args.module}` → 变异体名 glob `{module_filter}`")
    # 刷新基线之前先处理增量缓存：这一步**必须**在 run_mutmut 之前（缓存干不干净
    # 决定了这一轮是全量还是增量）。计划是纯函数算的，副作用只在这里发生。
    refresh = apply_baseline_refresh_plan(
        baseline_refresh_plan(
            update_baseline=args.update_baseline,
            allow_incremental=args.allow_incremental_refresh,
            mutants_dir=MUTANTS_DIR,
            stamp=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        )
    )
    # 墙钟：优先用 `run_mutmut` 自己的计时（它量的正是那条子进程的生命周期）；
    # 若返回 None（测试替身 / 未来的其它 runner），退回在这里量这一段——
    # 拿不到就写这一段实测，不写一个"看起来像实测"的常数。
    run_started = time.perf_counter()
    elapsed = run_mutmut(
        module=module_filter or None,
        timeout=args.timeout,
        max_children=args.max_children,
        json_out=args.json_out,
    )
    elapsed_s = round(
        float(elapsed if elapsed is not None else time.perf_counter() - run_started), 1
    )
    buckets = collect_status(json_out=args.json_out)
    # 内容指纹（条件 7）：从这一轮刚生成的 `mutants/` 读，逐条算；
    # 取不到的条目如实记 None，条数与"是否真的比对了"由 fingerprint_report 打印。
    fingerprints = compute_mutant_fingerprints(
        all_mutant_names(buckets), mutants_dir=MUTANTS_DIR
    )
    summary = mutation_summary(buckets, fingerprints=fingerprints)
    print(
        f"[mutation] killed={summary['killed']} survived={len(summary['survived'])} "
        f"no_tests={len(summary['no_tests'])} survivor_rate={summary['survivor_rate']:.3f}"
    )
    for line in _invisible_space_lines(summary):
        print(line)
    for line in fingerprint_report(fingerprints):
        print(line)

    run_payload = {
        "schema": "mutation-run/v2",
        "modules": list(DEFAULT_MODULES) if not args.module else [args.module],
        "command": (
            f"mutmut run {module_filter or '（全部模块）'}"
            f" --max-children {args.max_children}"
        ),
        "counts": {
            "killed": summary["killed"],
            "survived": len(summary["survived"]),
            "no_tests": len(summary["no_tests"]),
            "inconclusive": len(summary["inconclusive"]),
            "invisible": summary["invisible_count"],
            "total": summary["total"],
            "decided": summary["decided"],
        },
        "status_counts": summary["status_counts"],
        "status_by_mutant": summary["status_by_mutant"],
        "survivor_rate": round(summary["survivor_rate"], 4),
        "survivors": summary["survived"],
        "no_tests": summary["no_tests"],
        "inconclusive": summary["inconclusive"],
        "unknown_statuses": summary["unknown_statuses"],
        # 这一轮 `mutmut run` 的**墙钟秒数**（2026-09-26 新加）：nightly 上传的就是这个文件，
        # 而"预算够不够"只有 CI 能回答（独立验证 2026-09-18 · 报告 §5-1）——先前只有 print，
        # 夜里跑完留不下数。对照 nightly job 的墙钟（`gh run view <id> --json jobs`，含
        # checkout/install）即可算余量。不按模块拆分：本脚本一次 `mutmut run` 跑完
        # `only_mutate` 的全部模块，模块级细分要改成串行多次运行（那是另一种预算形态）。
        "elapsed_s": elapsed_s,
        # 内容指纹（条件 7 的原料）：逐条写进产物，两次运行的产物可以直接 diff 出
        # "哪些同名变异换了内容"；基线里也存同一份（算法标识一并写，换口径必须改名）。
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "fingerprints": fingerprints,
        # 这一轮是"全量重跑"还是"沿用了旧缓存"：基线混合与否只能从这里读出来
        "refresh": refresh,
    }
    # **主判定路径也要写 --json-out**：nightly 上传的就是这个文件，先前只有
    # `--summary-only` 写，于是夜里跑的那条路根本不会生成 `/tmp/mutation.json`
    # （独立验证报告 P1-1 的同族问题）。放在判定之前写：判失败也有产物可看。
    run_payload["invisible_share"] = round(summary["invisible_share"], 4)
    _write_json_out(args.json_out, run_payload)

    baseline_path = Path(args.baseline)
    if summary["total"] == 0:
        # 评审 P0-1：`mutmut results` 在没有结果时**退出 0 且无输出**。
        print(
            "[mutation] 本轮**没有产出任何变异体**（total=0）：这不是「没有盲区」，"
            "而是「没跑起来」，判定失败。\n"
            "  常见原因：mutmut 配置未生效、`mutants/` 被清空、collect error、runner 崩溃。"
        )
        return 1

    if args.update_baseline:
        unreadable = sorted(name for name, value in fingerprints.items() if not value)
        if fingerprints and len(unreadable) == len(fingerprints):
            # fail-closed：写一份**一条指纹都没有**的基线，等于把条件 7 静默关掉
            # （门禁之后会一直"没有可比对的指纹"而全绿）。宁可判失败。
            print(
                "[mutation] 一条内容指纹都取不到（mutants/ 缺文件 / .meta 里没有这些名字）："
                "拒绝写基线——那会把「同名不同指纹」这条判据静默关掉。\n"
                f"  mutants_dir={MUTANTS_DIR}，本轮变异体 {len(fingerprints)} 条"
            )
            return 1
        candidate = {
            "schema": "mutation-baseline/v3",
            "tool": "mutmut 3.x（见 pyproject.toml [tool.mutmut]）",
            "modules": list(DEFAULT_MODULES),
            "command": "mutmut run --max-children 4（nightly job `mutation`）",
            # 这份基线是什么条件下冻的：全量重跑（`action=full-run`/`move-aside`）
            # 还是"沿用了旧缓存的增量结果"（`action=incremental`）。后者是
            # 混合基线，写在这里而不是靠人记。
            "refresh": refresh,
            "counts": {
                "killed": summary["killed"],
                "survived": len(summary["survived"]),
                "no_tests": len(summary["no_tests"]),
                "inconclusive": len(summary["inconclusive"]),
                "invisible": summary["invisible_count"],
                "total": summary["total"],
                "decided": summary["decided"],
            },
            "status_counts": summary["status_counts"],
            # 每个变异体的状态：条件 2/3 靠它才能看见"killed 变成 segfault"
            # 与"整条重命名"（v1 只有 survivors/no_tests 两个列表，看不见这些）。
            "status_by_mutant": summary["status_by_mutant"],
            # 每条变异体的**内容指纹**（v3 起）：条件 7 靠它才能看见"同名但已不是同一条"
            # ——等量改写（常数改值、语句换序）既不增删条数也不移位编号，只看名字会静默绿。
            "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
            "fingerprints": {name: value for name, value in fingerprints.items() if value},
            "survivor_rate": round(summary["survivor_rate"], 4),
            "invisible_share": round(summary["invisible_share"], 4),
            "survivors": summary["survived"],
            "no_tests": summary["no_tests"],
            # 已知无结论集合：门禁对它的**增长**敏感，对它本身不做断言。
            # 它不得被读成"不是盲区"——2026-09-18 之前这个集合里装着 906 条
            # `segfault`（macOS fork 子进程碰系统代理解析导致的误判），其中
            # `harness.approval.xǁApprovalBindingǁis_expired__mutmut_9` 经手工应用
            # 整套用例全绿 ⇒ 是一条**真幸存变异**。根因修掉后该集合为空。
            "inconclusive": summary["inconclusive"],
            "note": (
                "幸存变异 = 测试盲区。基线只用来防「新增盲区」，不是质量分："
                "部分路径由子进程驱动的崩溃矩阵/四系统评测覆盖，mutmut 看不见，"
                "因此清单里包含「其实被外层验证保护着」的变异。"
                "无结论类（segfault/timeout/not checked…）与未覆盖类（no tests）"
                "合起来是**不可见空间**，它们不进 survivor_rate——"
                "survivor_rate 不是覆盖率。"
                "基线的 `status_by_mutant` 记了每一条的状态：判定类变成非判定类、"
                "整条缺失、新增 no tests、无结论集合增长，都会让门禁变红。"
                "`fingerprints` 记了每一条的内容指纹：同名**不同指纹**同样变红"
                f"（算法 {FINGERPRINT_ALGORITHM}；换口径必须换标识名，否则会集体误报）。"
                "`inconclusive` 是「已知无结论集合」，门禁只判它的增长、不判它本身——"
                "**它不得被读成「不是盲区」**：2026-09-18 之前这个集合里装着 906 条 "
                "`segfault`（macOS 上 fork 出的子进程碰系统代理解析导致的误判），"
                "其中 is_expired__mutmut_9 经手工应用后整套用例全绿，是一条真幸存变异；"
                "根因修掉后该集合为空，调查见 "
                "docs/design/2026-09-18-mutation-segfault-investigation.md。"
            ),
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        # "更宽即拒绝"守卫（2026-09-26）：写完候选基线**先与旧基线比**，比旧基线宽就拒绝写入。
        # 依据：独立验证 2026-09-18 · 报告 §5-2——`--update-baseline` 是文档化的逃生门，
        # 但技术上没有任何"更宽就拒绝"的守卫，"顺手把这一轮的盲区冻进基线"没有阻力。
        old_payload: dict[str, Any] | None = None
        if baseline_path.exists():
            try:
                old_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[mutation] 读不出旧基线 {baseline_path}（{exc}）：按「没有旧基线」处理")
        widening = baseline_widening(old_payload, candidate)
        if widening and not args.allow_wider_baseline:
            print(
                f"\n[mutation] **拒绝写基线**：候选基线比 {baseline_path.name} 更宽"
                f"（{len(widening)} 类）。刷新基线不许顺手把这一轮的盲区冻进去。\n"
            )
            for item in widening:
                print(f"--- [{item['key']}] {item['label']}：{item['count']} 条")
                for name in item["names"][: args.max_report]:
                    print(f"      {name}")
                if len(item["names"]) > args.max_report:
                    print(f"      （其余 {len(item['names']) - args.max_report} 条省略）")
            print(
                "\n处置：先给这些分支补用例（docs/testing.md §3），或者——确认"
                "「这一轮的放宽是有意的/是改代码导致的重编号」之后——显式重跑："
                "\n  python -m scripts.mutation_check --update-baseline --allow-wider-baseline\n"
                "（会把放行的理由写进基线的 `refresh.widening_override`，评审看得见。）"
            )
            return 1
        if widening:
            refresh = {
                **refresh,
                "widening_override": {
                    "allowed": True,
                    "reason": "显式给了 --allow-wider-baseline",
                    "categories": [
                        {"key": item["key"], "count": item["count"]} for item in widening
                    ],
                },
            }
            print(
                f"\n[mutation] **警告**：显式放行了一份**比旧基线更宽**的基线"
                f"（{len(widening)} 类："
                + "、".join(f"{item['key']}×{item['count']}" for item in widening)
                + "）。这条放行已记进基线的 `refresh.widening_override`。"
            )
        candidate["refresh"] = refresh
        # `--json-out` 在判定之前就写了（判失败也要留产物），而"放行了一份更宽的基线"
        # 这件事要到候选基线算完才知道 ⇒ 这里回写一次，让产物与基线里的记录一致。
        _write_json_out(args.json_out, {**run_payload, "refresh": refresh})
        baseline_path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[mutation] 基线已更新：{baseline_path}"
            f"（schema v3，含状态与内容指纹 {len(candidate['fingerprints'])} 条）"
        )
        return 0

    if not baseline_path.exists():
        print(f"[mutation] 没有基线文件 {baseline_path}：请先跑 --update-baseline 并入库")
        return 1
    baseline = load_baseline(baseline_path)
    if not baseline["status_by_mutant"]:
        print(f"[mutation] 基线文件 {baseline_path} 里没有任何变异体条目：判定作废")
        return 1
    if not baseline["full_status_coverage"]:
        print(
            "[mutation] 注意：基线是 v1（只有 survivors/no_tests 两个列表，没有 killed 的名字）。"
            "条件 2/3 因此只能覆盖基线幸存者与 no tests；跑一次 --update-baseline 可升到 v3。"
        )
    for line in fingerprint_comparison_lines(fingerprints, baseline["fingerprints"]):
        print(line)

    verdict = gate_verdict(summary, baseline)
    if verdict["ok"]:
        print("[mutation] 没有新增幸存变异、没有判定类退化、没有新盲区：盲区没有扩大")
        return 0

    print(f"\n[mutation] 判定失败：{len(verdict['failures'])} 条红灯条件成立")
    for failure in verdict["failures"]:
        print(f"\n--- [{failure['key']}] {failure['label']}：{failure['count']} 条")
        if failure["hint"]:
            print(f"    {failure['hint']}")
        for name in failure["names"][: args.max_report]:
            print(f"      {name}")
        if len(failure["names"]) > args.max_report:
            print(f"      （其余 {len(failure['names']) - args.max_report} 条省略）")

    for name in verdict["new_survivors"][: args.max_report]:
        print(f"\n--- {name}")
        print(show_mutant(name))
    print(
        "\n处置：给这些分支补用例（docs/testing.md §3：新增机制三件套），"
        "或者——如果放宽是有意的——在 PR 里说明并跑 --update-baseline。\n"
        "「本轮没看到」不等于「修好了」：判定类变成无结论、整条缺失、新增 no tests "
        "都算盲区扩大，不许当成好消息。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
