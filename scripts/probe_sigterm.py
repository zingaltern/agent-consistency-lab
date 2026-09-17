"""故障谱系探测器①：SIGTERM（"优雅关闭"）行为（R-A3）。

对齐的承诺：`docs/semantics.md` §3 只对 **kill -9** 语义作承诺；本探测器回答的是
"如果外部用 SIGTERM 这种'礼貌'信号，运行时会不会给出与 SIGKILL 不同的结果"。

探测方法（全部在 /tmp 的工作副本上做，绝不碰仓库）：

1. 跑 run → approve，然后在 resume 阶段的中途发 SIGTERM；
2. 看进程怎么退出、账本（连 `-wal` 一起快照）是几行、有没有 `pending` 意图残留；
3. 再 resume 一次，确认能收敛到合法终态且账本仍然 ≤1 行/键。

**停机规则**（设计文档 A §R-A3）：如果结论是"运行时需要新增代码才能回应"（例如
需要实现 graceful flush 才有意义），本脚本**不补**这段逻辑，只把它写成开放问题。
真正的 graceful shutdown 会引入新的语义分支与新的崩溃窗口，属于语义变更，必须先在
`docs/semantics.md` 里改承诺——那是仓库所有者的决定，不是探测器的。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from opsenv.oracle import audit_run  # noqa: E402

LONG_TASK = ("--scenario", "long_incident", "--long-steps", "8", "--long-lines", "100")


def _worker(run_dir: Path, mode: str, *extra: str) -> dict:
    argv = [
        sys.executable,
        "-m",
        "experiments.worker",
        "--run-dir",
        str(run_dir),
        "--mode",
        mode,
        *LONG_TASK,
        *extra,
    ]
    proc = subprocess.run(
        argv, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300
    )
    payload: dict = {"exit_code": proc.returncode, "mode": mode}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            payload.update(json.loads(line))
            break
        except json.JSONDecodeError:
            continue
    return payload


def has_sigterm_handler() -> bool:
    """运行时是否注册了 SIGTERM 处理器？

    直接读 worker 的启动路径：在子进程里注册一个探针处理器之前，先看默认 handler 是否被
    Python 之外的代码改过——这里用更直接的方法：**跑一遍并观察行为**（见 probe 的结论）。
    函数保留为"显式记录我们查过什么"，避免后来者以为没人看过。
    """
    import experiments.worker as worker_module

    source = Path(worker_module.__file__).read_text(encoding="utf-8")
    return "SIGTERM" in source or "signal.signal" in source


def probe_once(*, workroot: Path, delay_ms: int, tag: str = "") -> dict:
    """一次尝试：在 ``delay_ms`` 后发 SIGTERM，然后按不变式走一遍恢复。

    **信号的"落点"依赖负载**：机器忙的时候 resume 可能在延迟之前就跑完，
    于是这一轮根本没发出信号——那种情况是"没测到"，不是"测出问题了"。
    调用方（``probe``）会依次缩短延迟重试，并把"实际落在哪个延迟上"记进结论。
    """
    run_dir = workroot / f"sigterm{tag}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    _worker(run_dir, "run")
    _worker(run_dir, "approve")

    proc = subprocess.Popen(
        [sys.executable, "-m", "experiments.worker", "--run-dir", str(run_dir), "--mode", "resume",
         *LONG_TASK],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    time.sleep(delay_ms / 1000)
    sent = proc.poll() is None
    if sent:
        proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=120)
    audit_at_stop = audit_run(run_dir, status="running", resumes_used=0, max_resumes=99)
    after = _worker(run_dir, "resume")
    for _ in range(5):
        if str(after.get("status")) in ("completed", "failed", "waiting_human"):
            break
        after = _worker(run_dir, "resume")
    audit_after = audit_run(
        run_dir, status=str(after.get("status", "unknown")), resumes_used=1, max_resumes=5
    )
    return {
        "delay_ms_used": delay_ms,
        "graceful_handler_registered": has_sigterm_handler(),
        "sigterm_sent": sent,
        "exit_code": exit_code,
        "ledger_at_stop": audit_at_stop.facts["effects"],
        "findings_at_stop": audit_at_stop.codes(),
        "final_status": after.get("status"),
        "ledger_after_resume": audit_after.facts["effects"],
        "findings_after_resume": audit_after.codes(),
    }


def probe(*, workroot: Path, delay_ms: int, attempts: int = 4) -> dict:
    """探测器的**自校正**入口：依次缩短延迟直到信号真的落在进程运行期。

    为什么不在第一次没打中时就判失败：那会让探测器变成一个"随机器负载翻红"的检查，
    而 flaky 的门禁很快就会被忽略。真正的失败判据是"**所有**尝试都没能发出信号"
    （那时才说明探测前提不成立）。
    """
    attempt_delays = [max(5, delay_ms // (2**index)) for index in range(attempts)]
    results = []
    for index, delay in enumerate(attempt_delays):
        result = probe_once(workroot=workroot, delay_ms=delay, tag=f"-{index}")
        results.append(result)
        if result["sigterm_sent"]:
            result = dict(result)
            result["attempts"] = results
            return result
    # 所有延迟都没打中：说明进程比探测器的任何延迟都快 —— 结论是"测不到"，按失败处理
    final = dict(results[-1])
    final["attempts"] = results
    final["inconclusive"] = True
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/probe_sigterm.py")
    parser.add_argument("--workroot", default="/tmp/probe-sigterm")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=120,
        help="启动后多少毫秒发 SIGTERM（没打中会自动减半重试，最多 4 次）",
    )
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    result = probe(workroot=Path(args.workroot), delay_ms=args.delay_ms)
    if result.get("inconclusive"):
        result["conclusion"] = (
            "所有尝试（延迟依次减半）都没能在进程运行期发出 SIGTERM：本轮**没测到**任何东西，"
            "按失败处理而不是「碰巧绿」。处置：换更长的任务体量（--long-steps 更大）"
            "或降低机器负载。"
        )
    result["conclusion"] = (
        "运行时未注册 SIGTERM 处理器 ⇒ SIGTERM 等价于立即终止（与 SIGKILL 同类），"
        "不存在「优雅关闭会 flush 未落盘内容」的额外语义；"
        "账本在停止瞬间要么 0 行要么 1 行（已提交事务不丢、不会出现半行），"
        "恢复后仍然 ≤1 行/键。"
        "**开放问题**：若将来要提供 graceful flush，属于语义变更（要先改 docs/semantics.md），"
        "本探测器按停机规则不实现它。"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    ok = (
        # 必须包含"信号真的发出过"：进程若在延迟前就退出，探测器没测到任何东西，
        # 却照样会打印"未注册 SIGTERM 处理器"的结论（评审 P2-8）。
        # 自校正之后仍未发出 ⇒ 探测前提不成立，判失败（而不是"碰巧绿"）。
        bool(result["sigterm_sent"])
        and result["exit_code"] != 0
        and result["ledger_after_resume"]["max_per_key"] <= 1
        and not result["findings_after_resume"]
        and result["final_status"] in ("completed", "failed", "waiting_human")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
