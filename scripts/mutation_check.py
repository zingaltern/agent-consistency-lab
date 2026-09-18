"""变异测试门禁（R-A5）：把"测试盲区"变成一条会红的门禁。

语义：**幸存的变异 = 测试盲区**。变异测试与 `tests/test_audit_regressions.py` 互补——
审计回归防已知的 P0 复活，变异测试探没人写过的分支。

为什么是"防倒退基线"而不是"追求某个绝对值"：
* 这几个模块里相当一部分路径由**子进程驱动**的崩溃矩阵与四系统评测覆盖（`experiments/`、
  `opsenv/`），它们不在 `pytest` 进程内，mutmut 看不见——因此幸存清单里必然包含
  "其实被更外层验证保护着"的变异。把幸存率当绝对质量分会得出错误结论。
* 真正要防的是：**新增代码把盲区扩大**。所以首版把当前幸存清单冻结成基线
  （`reports/mutation_baseline.json`），之后只判"有没有**新增**幸存变异"。

## 全状态记账（2026-09-18 修，来源：独立验证报告 P0-1 / P0-2）

首版判据只看本轮 `survived` 里有没有新名字。这有两个后果，都被独立验证的**反例**证伪过：

* **P0-1｜"缺项"被当成好消息。** 任何**从本轮结果里消失**的基线幸存变异
  （未评估 / `segfault` / `timeout` / `no tests`）都会落进 `fixed`，
  被打印成"已被杀死（好事）"并**退出 0**。判据结构上缺项**不可能**导致红灯。
  活体场景：`harness/store/checkpoints.py` 逐字未改，76 条基线幸存变异在某轮里
  被 mutmut 改判成 `segfault`——`segfault` 原先是"不计入任何计数"的垃圾桶。
* **P0-2｜抓不到"新增的、完全没被测的代码"。** 新增的变异体被判为 `no tests`，
  而判据只看 `survived` ⇒ 覆盖率为零的新代码**不会变红**，与 AGENTS.md 红线 5 冲突。

因此本脚本现在把 mutmut 的**全部状态**分三类记账，**不再静默丢弃任何一个**：

| 类 | 状态 | 含义 |
|---|---|---|
| 判定类 | `killed` / `survived` | 真的得到了判决 |
| 未覆盖类 | `no tests` | 连覆盖都没有（另一类盲区） |
| 无结论类 | 见下 | 跑了但**判决不可信**，或根本没判决 |

无结论类 = `segfault` / `timeout` / `suspicious` / `skipped` / `not checked` /
`caught by type check` / `check was interrupted by user`
（名单在代码里是 `INCONCLUSIVE_STATUSES`，来自 `mutmut/stats.py::status_by_exit_code`）。

后两类合起来是**不可见空间**：它们不参与 `survivor_rate`，每次运行都打印其规模——
`survivor_rate` **不是覆盖率**，别读成覆盖率。

### 六条红灯条件

1. 新增幸存变异（保留首版行为；基线里 `killed` 的变异体变成 `survived` 也算新增）；
2. 基线里是**判定类**的变异体，本轮变成**非判定类**（P0-1 的活体场景）；
3. 基线里的变异体本轮**完全缺失**（未评估 / 改名 / 缓存缺项）；
4. 新增 `no tests`（P0-2：对"新增的、完全没被测的代码"不敏感）；
5. 本轮**没有任何变异体**、或**没有任何一条被判定的变异体**（"跑不起来"≠"没有盲区"）；
6. **无结论集合较基线增长**（有新名字进入不可见空间）。

外加一条 fail-closed：出现**本脚本不认识的状态名** ⇒ 判失败（mutmut 升版带来的新分类
必须先被归类，否则就是新一轮"静默丢数据"）。

用法::

    .venv/bin/python scripts/mutation_check.py                     # 判定（CI nightly 用）
    .venv/bin/python scripts/mutation_check.py --update-baseline   # 显式刷新基线（PR 里说明）
    .venv/bin/python scripts/mutation_check.py --module harness/approval.py --timeout 300
    .venv/bin/python scripts/mutation_check.py --module "harness.approval.*" --timeout 300

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
from typing import Any, NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = PROJECT_ROOT / "reports" / "mutation_baseline.json"
# 顺序与 pyproject.toml [tool.mutmut].only_mutate 一致（harness/execution.py 是七步管线的
# 实现所在：设计文档 C 抽取后必须一并纳入，否则管线失去变异覆盖而门禁不会变红）。
DEFAULT_MODULES = (
    "harness/execution.py",
    "harness/loop.py",
    "harness/store/checkpoints.py",
    "harness/approval.py",
)

# 三类划分。mutmut 的编号在 mutmut/stats.py::status_by_exit_code；这里按**状态名**归类，
# 不按退出码——名称是 mutmut 的公开面，退出码是实现细节。
DECIDED_STATUSES = ("killed", "survived")
UNCOVERED_STATUSES = ("no tests",)
INCONCLUSIVE_STATUSES = (
    "segfault",
    "timeout",
    "suspicious",
    "skipped",
    "not checked",
    "caught by type check",
    "check was interrupted by user",
)
KNOWN_STATUSES = DECIDED_STATUSES + UNCOVERED_STATUSES + INCONCLUSIVE_STATUSES


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
) -> None:
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
        )
    print(f"[mutation] 用时 {time.perf_counter() - started:.1f}s")
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
        )


def collect_status(*, json_out: str = "") -> dict[str, list[str]]:
    """读 mutmut 的结果表：``{status: [mutant 名]}``。**全状态**，不做任何过滤。"""
    proc = subprocess.run(
        # mutmut 3.x 的 --all 是**带值**选项（default=False 且没有 is_flag），
        # 必须写 --all=true，否则报 "Option '--all' requires an argument"。
        # 要全量是因为 survivor_rate 需要 killed 数作分母（只看非 killed 会把比率算成 1.0），
        # 也因为"无结论类"必须被看见（P0-1 的教训）。
        [sys.executable, "-m", "mutmut", "results", "--all=true"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        _abort(
            "results_unreadable",
            f"[mutation] 读结果失败：{proc.stderr[-400:]}",
            json_out=json_out,
        )
    buckets: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, status = line.rpartition(":")
        buckets.setdefault(status.strip(), []).append(name.strip())
    return buckets


def mutation_summary(buckets: dict[str, list[str]]) -> dict[str, Any]:
    """把 mutmut 的状态表折算成"全状态记账"的摘要。**不丢弃任何状态**。"""
    by_status = {status: sorted(names) for status, names in buckets.items()}
    status_by_mutant = {name: status for status, names in buckets.items() for name in names}
    counts = {status: len(buckets.get(status, [])) for status in KNOWN_STATUSES}
    unknown_statuses = sorted(status for status in buckets if status not in KNOWN_STATUSES)
    decided = counts["killed"] + counts["survived"]
    total = len(status_by_mutant)
    # 不认识的状态名一律计入"不可见空间"（fail-closed）：宁可让它显得更差，
    # 也不能让它悄悄消失。
    inconclusive = sorted(
        name
        for name, status in status_by_mutant.items()
        if status in INCONCLUSIVE_STATUSES or status in unknown_statuses
    )
    return {
        "killed": counts["killed"],
        "survived": by_status.get("survived", []),
        "no_tests": by_status.get("no tests", []),
        "inconclusive": inconclusive,
        "status_counts": counts,
        "unknown_statuses": unknown_statuses,
        "by_status": by_status,
        "status_by_mutant": status_by_mutant,
        "total": total,
        "decided": decided,
        "uncovered_count": counts["no tests"],
        "invisible_count": total - decided,
        "invisible_share": (total - decided) / total if total else 0.0,
        # 幸存率只在"被判定的变异体"上算：no tests 与无结论类是另一类盲区，
        # 混进分母会让这个数看起来更好看。**它不是覆盖率**。
        "survivor_rate": (counts["survived"] / decided) if decided else 0.0,
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


def load_baseline(path: Path) -> dict[str, Any]:
    """读基线。**兼容 v1**：v1 只有 `survivors`/`no_tests` 两个列表。

    v1 的 `killed` 只有计数没有名字，所以条件 2/3 在 v1 上只能覆盖
    "基线幸存者" 与 "基线 no tests"——这一点会被打印出来，免得被读成全覆盖。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    full = isinstance(payload.get("status_by_mutant"), dict) and bool(payload["status_by_mutant"])
    if full:
        status_by_mutant = {str(k): str(v) for k, v in payload["status_by_mutant"].items()}
    else:
        status_by_mutant = {}
        for name in payload.get("survivors", []):
            status_by_mutant[str(name)] = "survived"
        for name in payload.get("no_tests", []):
            status_by_mutant[str(name)] = "no tests"
    return {
        "schema": str(payload.get("schema", "mutation-baseline/v1")),
        "status_by_mutant": status_by_mutant,
        "full_status_coverage": full,
        "inconclusive": set(
            name for name, status in status_by_mutant.items() if status in INCONCLUSIVE_STATUSES
        ),
    }


def gate_verdict(summary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """六条红灯条件 + 未知状态 fail-closed。返回 ``{"failures": [...], ...}``。

    纯函数：不吃 mutmut、不读文件——判定逻辑本身要能被单元测试与退化注入直接钉住。
    """
    base = baseline["status_by_mutant"]
    base_decided = {name for name, status in base.items() if status in DECIDED_STATUSES}
    current = summary["status_by_mutant"]

    # 1. 新增幸存变异（基线里 killed 的变成 survived 也算：那是新增的盲区）。
    new_survivors = [name for name in summary["survived"] if base.get(name) != "survived"]
    # 2. 基线里是判定类的变异体，本轮变成非判定类（含 no tests / segfault / timeout…）。
    regressed_from_decided = sorted(
        name for name in base_decided if name in current and current[name] not in DECIDED_STATUSES
    )
    # 3. 基线里存在的变异体本轮完全缺失（未评估 / 改名 / 缓存缺项）。
    vanished = sorted(name for name in base if name not in current)
    # 4. 新增 no tests（P0-2）。
    new_no_tests = [name for name in summary["no_tests"] if base.get(name) != "no tests"]
    # 6. 无结论集合较基线增长。
    new_inconclusive = sorted(set(summary["inconclusive"]) - baseline["inconclusive"])

    failures: list[dict[str, Any]] = []

    def fail(key: str, label: str, names: list[str], hint: str = "") -> None:
        failures.append(
            {"key": key, "label": label, "count": len(names), "names": names, "hint": hint}
        )

    if summary["total"] == 0:
        fail(
            "empty-result-set",
            "本轮**没有产出任何变异体**（total=0）",
            [],
            "这不是「没有盲区」，而是「没跑起来」。常见原因：mutmut 配置未生效、"
            "`mutants/` 被清空、collect error、runner 崩溃。",
        )
    elif summary["decided"] == 0:
        fail(
            "no-decided-mutants",
            "本轮**没有任何一条变异体得到判定**（killed=survived=0）",
            summary["inconclusive"],
            "全是无结论类 / 未覆盖类：这种情况下的 survivor_rate=0 毫无意义。",
        )
    if new_survivors:
        fail("new-survivors", "新增幸存变异（= 新增的测试盲区）", new_survivors)
    if regressed_from_decided:
        fail(
            "decided-to-inconclusive",
            "基线里**有判定**的变异体，本轮变成无结论 / 未覆盖",
            regressed_from_decided,
            "它们从门禁视野里消失了——只有「确实被 killed」才算好消息。",
        )
    if vanished:
        fail(
            "vanished-from-results",
            "基线里的变异体本轮**完全缺失**（未评估 / 改名 / 缓存缺项）",
            vanished,
        )
    if new_no_tests:
        fail(
            "new-no-tests",
            "新增 `no tests` 变异体（= 新增的、完全没有用例覆盖的代码）",
            new_no_tests,
        )
    if new_inconclusive:
        fail(
            "inconclusive-grew",
            "无结论集合较基线**增长**（有新名字进入不可见空间）",
            new_inconclusive,
        )
    if summary["unknown_statuses"]:
        fail(
            "unknown-status",
            "出现本脚本不知道的 mutmut 状态名（全状态记账的兜底）",
            summary["unknown_statuses"],
            "先把它归类进判定类 / 未覆盖类 / 无结论类，再判定；不要让它漏过去。",
        )

    return {
        "failures": failures,
        "new_survivors": new_survivors,
        "regressed_from_decided": regressed_from_decided,
        "vanished": vanished,
        "new_no_tests": new_no_tests,
        "new_inconclusive": new_inconclusive,
        "baseline_full_status_coverage": baseline["full_status_coverage"],
        "ok": not failures,
    }


def _report_line(summary: dict[str, Any]) -> str:
    sc = summary["status_counts"]
    parts = [f"{status}={count}" for status, count in sc.items() if count]
    unknown = summary["unknown_statuses"]
    if unknown:
        parts.append("未知状态=" + ",".join(unknown))
    return "[mutation] 全状态记账：" + " ".join(parts) if parts else "[mutation] 全状态记账：（空）"


def _invisible_space_lines(summary: dict[str, Any]) -> list[str]:
    total = summary["total"]
    decided = summary["decided"]
    invisible = summary["invisible_count"]
    share = summary["invisible_share"]
    rate = (
        f"{summary['survivor_rate']:.3f}"
        if decided
        else "n/a（没有任何一条变异体得到判定）"
    )
    return [
        _report_line(summary),
        f"[mutation] 判定类={decided} survivor_rate={rate}（分母只含 killed+survived）",
        f"[mutation] 不可见空间（无结论类 + 未覆盖类）= {invisible}/{total} "
        f"= {share:.1%}：这一部分既不进 survivor_rate，也不等于「测试没问题」；"
        "**survivor_rate 不是覆盖率**。",
    ]


def _write_json_out(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _abort(reason: str, message: str, *, json_out: str) -> NoReturn:
    """判定**作废**时的退出路径：先落产物，再退出。

    为什么要有这个 helper：nightly 上传的就是 `--json-out` 指向的文件，而"跑不起来"
    （超时 / `mutmut run` 非零退出 / 结果读不出）恰恰是最需要产物的那条路径——
    先前这三条路都是裸 `SystemExit`，夜里出问题只留一行 stderr，artifact 是空的
    （独立验证报告 P1-1）。

    ``reason`` 是给机器读的稳定标识（timeout / mutmut_run_failed / results_unreadable），
    ``message`` 是给人读的原文——两者都不做截断，产物里要能直接定位原因。
    """
    _write_json_out(
        json_out,
        {
            "schema": "mutation-run/v2",
            "verdict": "aborted",
            "reason": reason,
            "message": message,
        },
    )
    raise SystemExit(message)


def _summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """`--summary-only` / `--json-out` 的公开字段。

    `survivors` 一律是**列表**（v1 曾在这里写个数，而主判定路径写列表——
    同一个字段名两种类型，claim 对账会踩空）；个数是 `survivor_count`。
    """
    counts = payload.get("counts", {})
    status_by_mutant = payload.get("status_by_mutant") or {}
    # 老基线可能只写了计数没写名单：能从 `status_by_mutant` 推的就推出来，
    # 推不出来才回落到 0——但绝不假装"没有不可见空间"。
    if "inconclusive" in payload:
        inconclusive = list(payload["inconclusive"])
    else:
        inconclusive = sorted(
            name for name, status in status_by_mutant.items() if status in INCONCLUSIVE_STATUSES
        )
    invisible = counts.get("invisible")
    if invisible is None:
        decided = counts.get("decided")
        if decided is None:
            decided = counts.get("killed", 0) + counts.get("survived", 0)
        total = counts.get("total", len(status_by_mutant))
        invisible = total - decided
    return {
        "schema": payload.get("schema", "mutation-baseline/v1"),
        "counts": counts,
        "status_counts": payload.get("status_counts", counts),
        "survivor_rate": payload["survivor_rate"],
        "survivors": list(payload.get("survivors", [])),
        "survivor_count": len(payload.get("survivors", [])),
        "no_tests": list(payload.get("no_tests", [])),
        "no_tests_count": len(payload.get("no_tests", [])),
        "inconclusive": inconclusive,
        "inconclusive_count": len(inconclusive),
        "invisible_count": invisible,
        "invisible_share": payload.get("invisible_share", 0.0),
        "modules": payload.get("modules", []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/mutation_check.py")
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
    run_mutmut(
        module=module_filter or None,
        timeout=args.timeout,
        max_children=args.max_children,
        json_out=args.json_out,
    )
    buckets = collect_status(json_out=args.json_out)
    summary = mutation_summary(buckets)
    print(
        f"[mutation] killed={summary['killed']} survived={len(summary['survived'])} "
        f"no_tests={len(summary['no_tests'])} survivor_rate={summary['survivor_rate']:.3f}"
    )
    for line in _invisible_space_lines(summary):
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
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "schema": "mutation-baseline/v2",
                    "tool": "mutmut 3.x（见 pyproject.toml [tool.mutmut]）",
                    "modules": list(DEFAULT_MODULES),
                    "command": "mutmut run --max-children 4（nightly job `mutation`）",
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
                        "`inconclusive` 是「已知无结论集合」，门禁只判它的增长、不判它本身——"
                        "**它不得被读成「不是盲区」**：2026-09-18 之前这个集合里装着 906 条 "
                        "`segfault`（macOS 上 fork 出的子进程碰系统代理解析导致的误判），"
                        "其中 is_expired__mutmut_9 经手工应用后整套用例全绿，是一条真幸存变异；"
                        "根因修掉后该集合为空，调查见 "
                        "docs/design/2026-09-18-mutation-segfault-investigation.md。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[mutation] 基线已更新：{baseline_path}（schema v2，含每条变异体的状态）")
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
            "条件 2/3 因此只能覆盖基线幸存者与 no tests；跑一次 --update-baseline 可升到 v2。"
        )

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
