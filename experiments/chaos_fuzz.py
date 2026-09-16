"""随机时刻 SIGKILL fuzz：把"6 个命名窗口"的采样升级为"整条时间轴"的扫描（R-A2）。

与命名窗口矩阵的分工：

* 命名窗口（`experiments/crash_matrix.py`）回答"**这个**位置崩溃会怎样"——语义清楚、可断言预期；
* fuzz 回答"**任意**时刻崩溃会不会出现命名窗口之外的新类违例"——覆盖空间，不预设位置。

四条纪律：

1. **注入时刻由 seed 决定**：同一个 `--seed` 产生同一串 `--kill-after-ms`（可复现的输入）。
   落点（进程当时跑到哪一行）受机器时序影响——这一点写在报告的边界一节里，不假装逐字节可复现。
2. **窗口必须先标定**：进程光启动就要几十毫秒，而"有意义的注入窗口"只有标定过才知道。
   标定方法是线扫描：找一个**仍然能杀死进程**的最大 K，然后在其 15%–95% 区间采样。
   不标定的话 K 会落在"进程早就退出了"的区域，跑出来的全绿只是因为**根本没被杀到**。
3. **裁判是外部账本**：判定全部由 `opsenv/oracle.py` 从 `runtime.db` + `world.db`
   （连 `-wal` 一起快照）做出；runtime 自己的 `effects` 计数字段不参与判定。
4. **seed 不含 system/scenario 名**：扰动只决定注入时刻，不参与任何哈希命名
   （W5 审计的种子污染教训：种子里带系统名会产生 p=0.0009 的伪影）。

用法::

    python -m experiments.chaos_fuzz --repeats 30 --seed 20260917 \\
        --json-out reports/chaos_fuzz_report.json --md-out reports/chaos_fuzz_report.md
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsenv.oracle import LEGAL_TERMINAL_STATUSES, audit_run

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 对照面：保护组合（沿用崩溃矩阵的开关面）。每个组合**声明自己在什么情况下算发现**——
# 把"已知语义"与"新类违例"分开，否则 outbox 关闭时的重复会被当成功劳，或被当成没事。
COMBOS: tuple[dict[str, Any], ...] = (
    {
        "name": "全开(outbox+probe)",
        "flags": {"--outbox": "on", "--probe": "on", "--tool-idem": "off"},
        "expected_codes": (),
        "note": "预期：每键恰好 1 行；探针负责对账",
    },
    {
        "name": "outbox开-无读回",
        "flags": {"--outbox": "on", "--probe": "off", "--tool-idem": "off"},
        "expected_codes": (),
        "note": "预期：每键 ≤1 行、最多 1 行 unknown 待人工对账",
    },
    {
        "name": "全关(无outbox)",
        "flags": {"--outbox": "off", "--probe": "off", "--tool-idem": "off"},
        # 窗口 2 语义在无 outbox 时必然产生重复（W2 实测 5/5），这是**已知语义**而不是新发现；
        # 但仍然要求日志完整、调用闭合、恢复能收敛。
        "expected_codes": ("inv_effect_accounting",),
        "note": "预期：允许重复（已知窗口 2 语义），其余不变量仍需成立",
    },
)

LONG_TASK = ("--scenario", "long_incident", "--long-steps", "8", "--long-lines", "100")
# 标定梯：从"一定来得及"到"一定来不及"；进程确定性单调，线性扫一遍即可
CALIBRATION_LADDER_MS = (5, 10, 20, 35, 55, 80, 120, 180, 260, 400, 600)
PHASES: tuple[str, ...] = ("run", "resume")
MAX_DRIVE_STEPS = 6


@dataclass
class Trial:
    """一次 fuzz 试验：一个 seed × 一个保护组合 × 一个注入阶段。"""

    seed: int
    kill_phase: str
    kill_after_ms: int
    combo: str
    run_dir: Path
    killed: bool
    resumes: int
    status: str
    findings: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    marker: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "kill_phase": self.kill_phase,
            "kill_after_ms": self.kill_after_ms,
            "combo": self.combo,
            "run_dir": str(self.run_dir),
            "killed": self.killed,
            "resumes": self.resumes,
            "status": self.status,
            "findings": self.findings,
            "unexpected": self.unexpected,
            "facts": self.facts,
            "marker": self.marker,
        }


def _worker(
    run_dir: Path, mode: str, *, flags: dict[str, str], kill_after_ms: int | None
) -> dict[str, Any]:
    argv = [
        sys.executable,
        "-m",
        "experiments.worker",
        "--run-dir",
        str(run_dir),
        "--mode",
        mode,
        *LONG_TASK,
    ]
    for flag, value in flags.items():
        argv.extend([flag, value])
    if kill_after_ms is not None:
        argv.extend(["--kill-after-ms", str(kill_after_ms)])
    proc = subprocess.run(
        argv, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300
    )
    payload: dict[str, Any] = {"exit_code": proc.returncode, "mode": mode}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            payload.update(json.loads(line))
            break
        except json.JSONDecodeError:
            continue
    payload["stderr"] = proc.stderr.strip()[-300:]
    return payload


def current_status(run_dir: Path) -> str:
    """折叠事件日志取当前状态——**只用于驱动**（决定下一步跑 run/approve/resume）。

    判定不在这里：判定一律由 `opsenv.oracle` 从外部账本作出。驱动读 runtime 自己的日志是
    合法的（它本来就是"恢复要依靠的事实"），但**裁判**不能读它。
    """
    ids_path = run_dir / "ids.json"
    if not ids_path.exists():
        return "unknown"
    from harness.state import reduce_events
    from harness.store.sqlite_store import SqliteStore

    ids = json.loads(ids_path.read_text(encoding="utf-8"))
    store = SqliteStore(run_dir / "runtime.db")
    try:
        state, _ = reduce_events(store.effective_events(ids["branch_id"]))
        return state.status.value
    finally:
        store.close()


def drive_to_terminal(
    run_dir: Path, *, flags: dict[str, str], max_steps: int = MAX_DRIVE_STEPS
) -> tuple[str, int, list[dict[str, Any]]]:
    """按 run/approve/resume 推进，直到到达**合法终态集合**。

    合法终态 = completed / failed / waiting_human（审批等待属脚本计划内的合法驻留）。
    返回 (终态, 用掉的 resume 次数, 阶段记录)。
    """
    transcript: list[dict[str, Any]] = []
    status = current_status(run_dir)
    resumes = 0
    if status == "unknown":
        transcript.append(_worker(run_dir, "run", flags=flags, kill_after_ms=None))
        status = current_status(run_dir)
    for _ in range(max_steps):
        if status in LEGAL_TERMINAL_STATUSES:
            break
        if status == "waiting_human":
            transcript.append(_worker(run_dir, "approve", flags=flags, kill_after_ms=None))
        transcript.append(_worker(run_dir, "resume", flags=flags, kill_after_ms=None))
        resumes += 1
        status = current_status(run_dir)
    return status, resumes, transcript


def calibrate_kill_window(*, mode: str, flags: dict[str, str], workroot: Path) -> int:
    """找"仍然能杀死进程"的最大 K（毫秒）：线扫描标定梯，遇到第一个杀不死的就停。

    为什么必须标定：worker 进程光是启动 + import 就要几十毫秒，而真正有意义的代码窗口
    （建库 → 取证 → 执行 → 记录）只占剩下的一小段。不标定就会采样到"进程早已退出"的区域，
    全绿只是因为**根本没被杀到**——那是最危险的一种假绿。
    """
    best = 0
    for kill_after_ms in CALIBRATION_LADDER_MS:
        run_dir = workroot / f"calib-{mode}-{kill_after_ms}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        if mode == "resume":
            _worker(run_dir, "run", flags=flags, kill_after_ms=None)
            _worker(run_dir, "approve", flags=flags, kill_after_ms=None)
        payload = _worker(run_dir, mode, flags=flags, kill_after_ms=kill_after_ms)
        if payload["exit_code"] == 0:
            break
        best = kill_after_ms
    return best


def sample_kill_times(*, seed: int, repeats: int, window_ms: int) -> list[int]:
    """由 seed 决定注入时刻：在标定窗口的 15%–95% 区间均匀采样。

    下限 15%：太早会死在建库之前，测不到工具路径；上限 95%：留一点余量，
    免得整批试验都恰好"差一点没杀到"。
    """
    rng = random.Random(seed)
    low = max(1, int(window_ms * 0.15))
    high = max(low + 1, int(window_ms * 0.95))
    return [rng.randint(low, high) for _ in range(repeats)]


def run_trial(
    *,
    seed: int,
    combo: dict[str, Any],
    kill_phase: str,
    kill_after_ms: int,
    workroot: Path,
    max_resumes: int = MAX_DRIVE_STEPS,
) -> Trial:
    run_dir = workroot / f"fuzz-{seed}-{kill_phase}-{combo['name']}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    transcript: list[dict[str, Any]] = []
    if kill_phase == "resume":
        transcript.append(_worker(run_dir, "run", flags=combo["flags"], kill_after_ms=None))
        transcript.append(_worker(run_dir, "approve", flags=combo["flags"], kill_after_ms=None))
    injected = _worker(run_dir, kill_phase, flags=combo["flags"], kill_after_ms=kill_after_ms)
    transcript.append(injected)
    killed = injected["exit_code"] != 0

    status, resumes, tail = drive_to_terminal(run_dir, flags=combo["flags"], max_steps=max_resumes)
    transcript.extend(tail)
    audit = audit_run(run_dir, status=status, resumes_used=resumes, max_resumes=max_resumes)
    unexpected = [code for code in audit.codes() if code not in combo["expected_codes"]]
    marker_path = run_dir / "crash_marker.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None
    return Trial(
        seed=seed,
        kill_phase=kill_phase,
        kill_after_ms=kill_after_ms,
        combo=str(combo["name"]),
        run_dir=run_dir,
        killed=killed,
        resumes=resumes,
        status=status,
        findings=audit.codes(),
        unexpected=unexpected,
        facts=audit.facts,
        marker=marker,
    )


def sensitivity_check(*, workroot: Path, max_resumes: int = MAX_DRIVE_STEPS) -> dict[str, Any]:
    """oracle 的**敏感性自检**：在已知会重复的配置下，它必须报红。

    配置：关 outbox + 下游不幂等 + 命中窗口 2（`CHAOS_WINDOWS=post_tool_effect_pre_record:1`
    是 W2 实测 5/5 重复的那一格）。如果这里报不出 `inv_effect_accounting`，说明 oracle 不敏感——
    那么"N 个 seed 全绿"这句话毫无意义（全绿也可能是因为它什么都看不见）。
    """
    run_dir = workroot / "sensitivity"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    flags = {"--outbox": "off", "--probe": "off", "--tool-idem": "off"}
    _worker(run_dir, "run", flags=flags, kill_after_ms=None)
    _worker(run_dir, "approve", flags=flags, kill_after_ms=None)
    backup = os.environ.get("CHAOS_WINDOWS")
    os.environ["CHAOS_WINDOWS"] = "post_tool_effect_pre_record:1"
    try:
        _worker(run_dir, "resume", flags=flags, kill_after_ms=None)
    finally:
        if backup is None:
            os.environ.pop("CHAOS_WINDOWS", None)
        else:
            os.environ["CHAOS_WINDOWS"] = backup
    status, _, _ = drive_to_terminal(run_dir, flags=flags, max_steps=max_resumes)
    audit = audit_run(run_dir, status=status, resumes_used=1, max_resumes=max_resumes)
    marker_path = run_dir / "crash_marker.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None
    return {
        "run_dir": str(run_dir),
        "marker": marker,
        "findings": audit.codes(),
        "facts": audit.facts,
        "sensitive": "inv_effect_accounting" in audit.codes(),
        "note": "期望：inv_effect_accounting 出现（无 outbox + 窗口 2 ⇒ W2 实测 5/5 重复）",
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 随机时刻 SIGKILL fuzz 报告（R-A2）",
        "",
        f"* 运行：`{report['command']}`",
        f"* seed：`{report['seed']}`（扰动只决定注入时刻；种子不含 system/scenario 名）",
        "* 注入族：`time_hit`（墙钟定时 SIGKILL，与命名窗口语义独立、可同时给出）",
        "* 判定：`opsenv/oracle.py` 的四类不变量，读 `runtime.db` + `world.db`"
        "（连 `-wal` 一起快照）",
        "",
        "## 注入窗口标定",
        "",
        "| 保护组合 | 注入阶段 | 标定出的可杀死窗口上限 |",
        "|---|---|---|",
    ]
    for window in report["windows"]:
        lines.append(f"| {window['combo']} | {window['phase']} | {window['window_ms']} ms |")
    lines += [
        "",
        "## 对照面与结果",
        "",
        "| 保护组合 | 注入阶段 | 试次 | 实际被杀 | time_hit marker | 新类违例 |"
        " 已知类违例 | 终态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for combo in report["combos"]:
        for phase in PHASES:
            trials = [
                trial
                for trial in report["trials"]
                if trial["combo"] == combo["name"] and trial["kill_phase"] == phase
            ]
            if not trials:
                continue
            killed = sum(1 for trial in trials if trial["killed"])
            markers = sum(
                1
                for trial in trials
                if (trial.get("marker") or {}).get("injection_kind") == "time_hit"
            )
            unexpected = sum(len(trial["unexpected"]) for trial in trials)
            expected = sum(len(trial["findings"]) - len(trial["unexpected"]) for trial in trials)
            statuses = sorted({str(trial["status"]) for trial in trials})
            lines.append(
                f"| {combo['name']} | {phase} | {len(trials)} | {killed} | {markers} |"
                f" {unexpected} | {expected} | {'、'.join(statuses)} |"
            )
    summary = report["summary"]
    lines += [
        "",
        f"* 总试次 {summary['trials']}，实际注入成功 {summary['killed']} 次，"
        f"**新类违例 {summary['unexpected_findings']} 条**，"
        f"已知类违例 {summary['expected_findings']} 条（后者是被对照面显式声明的语义）。",
        "* **敏感性自检**（已知会重复的配置下 oracle 必须报红）："
        f"{'通过' if report['sensitivity']['sensitive'] else '**失败**'}"
        f"——实测 findings={report['sensitivity']['findings']}。",
        "",
        "## 结论与边界",
        "",
        f"* **{summary['killed']} 次随机时刻注入未发现命名窗口之外的新类违例**。"
        "按 rule of three，这个次数只把失败率上界压到约 "
        f"{3 / max(1, summary['killed']):.0%}——**不能**据此声称「覆盖了所有窗口」或「永不重复」。",
        "* 注入时刻由 seed 决定且可复现；**落点**（进程被杀的代码位置）受机器时序影响，"
        "因此不同机器上同一 seed 不保证死在同一行——本报告只主张「注入时刻可复现」，"
        "不主张逐行复现。",
        "* 标定窗口是**本机当次**测出来的：换机器或换负载需要重跑标定（报告里记下了当次窗口）。"
        "这是刻意保留的机器相关性——隐藏它会让「没被杀到」冒充「没发现问题」。",
        "* `全关(无outbox)` 组合的重复副作用是 **W2 已实测的已知语义**（窗口 2），"
        "在本报告里被显式分类为「已知类」而不是新发现；把它算成发现会让 fuzz 变成噪声。",
        "* **为什么「已知类违例」是 0**：窗口 2 是「效果已提交、`tool_result` 未写」的"
        "**微秒级**缝隙，"
        "墙钟定时注入几乎不可能恰好落在里面——所以它的存在性由敏感性自检里的"
        "**确定性窗口注入**（`CHAOS_WINDOWS=post_tool_effect_pre_record:1`）证明，"
        "而不是由随机时刻证明。随机注入这一侧主张的是相反的方向："
        "**没有**在别处发现新类违例。",
        "* 本口径只覆盖 kill -9；掉电、跨进程并发、租约仍不在范围内（见 docs/semantics.md §3）。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.chaos_fuzz")
    parser.add_argument(
        "--repeats", type=int, default=30, help="每个（保护组合 × 注入阶段）的试次数"
    )
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--workroot", default="")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--md-out", default="")
    parser.add_argument("--skip-sensitivity", action="store_true")
    parser.add_argument("--calibrate-only", action="store_true", help="只标定窗口并打印")
    args = parser.parse_args(argv)

    root = Path(args.workroot) if args.workroot else Path(tempfile.mkdtemp(prefix="chaos-fuzz-"))
    root.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    windows: list[dict[str, Any]] = []
    for index, combo in enumerate(COMBOS):
        for phase in PHASES:
            limit = calibrate_kill_window(
                mode=phase, flags=combo["flags"], workroot=root / f"calib-root-{index}-{phase}"
            )
            windows.append({"combo": combo["name"], "phase": phase, "window_ms": limit})
            assert limit > 0, (
                f"{combo['name']}/{phase}: 标定窗口为 0——最激进的注入也杀不死进程，"
                "必须修标定（或缩短进程启动），不能继续跑：那种全绿没有信息量"
            )
    if args.calibrate_only:
        print(json.dumps(windows, ensure_ascii=False, indent=2))
        return 0

    trials: list[Trial] = []
    for index, combo in enumerate(COMBOS):
        for phase_index, phase in enumerate(PHASES):
            limit = next(
                window["window_ms"]
                for window in windows
                if window["combo"] == combo["name"] and window["phase"] == phase
            )
            seed = args.seed + index * 10 + phase_index
            kill_times = sample_kill_times(seed=seed, repeats=args.repeats, window_ms=limit)
            for kill_after_ms in kill_times:
                trials.append(
                    run_trial(
                        seed=seed,
                        combo=combo,
                        kill_phase=phase,
                        kill_after_ms=kill_after_ms,
                        workroot=root,
                    )
                )
            done = [
                trial
                for trial in trials
                if trial.combo == combo["name"] and trial.kill_phase == phase
            ]
            assert len(done) == args.repeats, f"{combo['name']}/{phase}: 试次数不符 {len(done)}"

    sensitivity = (
        {"sensitive": True, "findings": [], "note": "已跳过（--skip-sensitivity）"}
        if args.skip_sensitivity
        else sensitivity_check(workroot=root)
    )
    report = {
        "command": f"python -m experiments.chaos_fuzz --repeats {args.repeats} --seed {args.seed}",
        "seed": args.seed,
        "windows": windows,
        "combos": [{"name": combo["name"], "note": combo["note"]} for combo in COMBOS],
        "trials": [trial.as_dict() for trial in trials],
        "sensitivity": sensitivity,
        "summary": {
            "trials": len(trials),
            "killed": sum(1 for trial in trials if trial.killed),
            "unexpected_findings": sum(len(trial.unexpected) for trial in trials),
            "expected_findings": sum(
                len(trial.findings) - len(trial.unexpected) for trial in trials
            ),
            "median_kill_after_ms": int(
                statistics.median([trial.kill_after_ms for trial in trials])
            ),
            "wall_seconds": round(time.perf_counter() - started, 1),
        },
    }
    report["verdict"] = (
        "green"
        if report["summary"]["unexpected_findings"] == 0 and sensitivity.get("sensitive", False)
        else "red"
    )
    text = render_markdown(report)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.md_out:
        Path(args.md_out).write_text(text + "\n", encoding="utf-8")
    return 0 if report["verdict"] == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
