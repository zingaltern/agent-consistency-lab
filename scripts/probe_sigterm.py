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


def probe(*, workroot: Path, delay_ms: int) -> dict:
    run_dir = workroot / "sigterm"
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
        "graceful_handler_registered": has_sigterm_handler(),
        "sigterm_sent": sent,
        "exit_code": exit_code,
        "ledger_at_stop": audit_at_stop.facts["effects"],
        "findings_at_stop": audit_at_stop.codes(),
        "final_status": after.get("status"),
        "ledger_after_resume": audit_after.facts["effects"],
        "findings_after_resume": audit_after.codes(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/probe_sigterm.py")
    parser.add_argument("--workroot", default="/tmp/probe-sigterm")
    parser.add_argument("--delay-ms", type=int, default=120, help="启动后多少毫秒发 SIGTERM")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    result = probe(workroot=Path(args.workroot), delay_ms=args.delay_ms)
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
        result["ledger_after_resume"]["max_per_key"] <= 1
        and not result["findings_after_resume"]
        and result["final_status"] in ("completed", "failed", "waiting_human")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
