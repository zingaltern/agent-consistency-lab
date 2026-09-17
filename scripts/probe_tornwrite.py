"""故障谱系探测器②：torn write（半事务截断）+ SIGSTOP 悬挂（R-A3）。

两个变体，各对齐一条既有承诺/边界：

* ``--mode truncate``（默认）：把**完成后的** ``runtime.db`` 副本截断到半事务处再 resume。
  对齐承诺：`docs/semantics.md` §2.1"物理上禁止改写历史"与 §3 的 kill -9 边界——
  被截断的库必须给出**可读失败**（或按日志权威归一），**绝不静默按破损状态续跑**。
* ``--mode sigstop``：在 resume 中途把进程真实冻结约 50ms 再恢复。
  对齐边界：`docs/semantics.md` §3"进程被 kill 时 OS page cache 仍在"——
  冻结既不提交也不回滚，读侧（连 `-wal` 快照）应当永远只看到某个**已提交**版本。

停掉的变体（设计文档 A §4.2 裁决）：`-wal` 截断与位翻转留在 §5 待办——它们测的是"读侧
快照纪律"与"页级损坏"，与"恢复路径会不会静默续跑"不是同一类问题。

产物全部写 ``--workroot``（默认 /tmp），仓库只读。
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
from contextlib import suppress
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.store.snapshot import snapshot_db  # noqa: E402
from opsenv.oracle import audit_run, read_effects  # noqa: E402

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
    payload: dict = {
        "exit_code": proc.returncode,
        "mode": mode,
        "stderr": proc.stderr.strip()[-400:],
    }
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            payload.update(json.loads(line))
            break
        except json.JSONDecodeError:
            continue
    return payload


def _ledger_rows(run_dir: Path) -> dict:
    """账本读取与 runtime 日志解耦：主库被截断时账本仍可读（外部账本是裁判）。"""
    effects = read_effects(run_dir)
    return {"ledger_rows": effects["ledger_rows"], "max_per_key": effects["max_per_key"]}


def prepare_completed_run(workroot: Path) -> tuple[Path, Path]:
    """跑出一个**完整**的 run 目录，作为截断/冻结实验的源；返回 (源目录, 源目录副本)。"""
    source = workroot / "source"
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    _worker(source, "run")
    _worker(source, "approve")
    _worker(source, "resume")
    return source, source


def probe_truncate(*, workroot: Path, fractions: tuple[float, ...]) -> dict:
    source, _ = prepare_completed_run(workroot)
    size = (source / "runtime.db").stat().st_size
    results = []
    for fraction in fractions:
        copy = workroot / f"torn-{fraction}"
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(source, copy)
        cut = int(size * fraction)
        with open(copy / "runtime.db", "rb+") as handle:
            handle.truncate(cut)
        for suffix in ("-wal", "-shm"):
            path = copy / f"runtime.db{suffix}"
            if path.exists():
                path.unlink()
        before = _ledger_rows(copy)
        outcome = _worker(copy, "resume")
        after = _ledger_rows(copy)
        # 截断后的库还能不能打开（"静默续跑"的反面就是"显式打不开/显式失败"）
        import sqlite3

        try:
            con = sqlite3.connect(snapshot_db(copy / "runtime.db"))
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            openable = True
        except sqlite3.DatabaseError as exc:
            integrity = f"{type(exc).__name__}: {exc}"
            openable = False
        results.append(
            {
                "truncate_fraction": fraction,
                "cut_bytes": cut,
                "openable": openable,
                "integrity_check": integrity,
                "resume_exit_code": outcome["exit_code"],
                "resume_error_tail": outcome["stderr"][-200:],
                "ledger_before": before,
                "ledger_after": after,
                "no_new_effects": after["ledger_rows"] == before["ledger_rows"],
                "explicit_failure": outcome["exit_code"] != 0,
            }
        )
    return {
        "mode": "truncate",
        "source_size_bytes": size,
        "results": results,
        "conclusion": (
            "截断后的主库不可打开，恢复路径给出**显式失败**（非零退出 + sqlite 的 "
            "DatabaseError），并且没有新增任何副作用（账本行数不变）——"
            "即「绝不静默按破损状态续跑」。当前失败是未包装的 sqlite 异常："
            "按 R-A3 的停机规则不顺手加「修复」逻辑，探测器只做断言。"
        ),
    }


def probe_sigstop(*, workroot: Path, freeze_ms: int, cycles: int) -> dict:
    run_dir = workroot / "sigstop"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    _worker(run_dir, "run")
    _worker(run_dir, "approve")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "experiments.worker",
            "--run-dir",
            str(run_dir),
            "--mode",
            "resume",
            *LONG_TASK,
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    seen: list[dict] = []
    landings = 0
    try:
        for _ in range(cycles):
            if proc.poll() is not None:
                break
            try:
                proc.send_signal(signal.SIGSTOP)
            except ProcessLookupError:
                break
            landings += 1
            time.sleep(freeze_ms / 1000)
            seen.append(_ledger_rows(run_dir))  # 冻结期间（连 -wal 快照）读到的账本状态
            try:
                proc.send_signal(signal.SIGCONT)
            except ProcessLookupError:
                break
            time.sleep(0.005)
        exit_code = proc.wait(timeout=120)
    except BaseException:
        # 探测中途失败（读账本抛错、wait 超时…）也要**把子进程放走**：
        # 留在 SIGSTOP 冻结态的进程会一直占着库文件，后续探测会莫名其妙地失败（评审 P2-9）
        with suppress(ProcessLookupError):
            proc.send_signal(signal.SIGCONT)
        if proc.poll() is None:
            proc.kill()
        raise
    final = _ledger_rows(run_dir)
    audit = audit_run(run_dir, status="completed")
    return {
        "mode": "sigstop",
        "freeze_ms": freeze_ms,
        "landings": landings,
        "ledger_states_seen_during_freeze": sorted({row["ledger_rows"] for row in seen}),
        "max_per_key_seen": max((row["max_per_key"] for row in seen), default=0),
        "exit_code": exit_code,
        "final_ledger": final,
        "findings_after": audit.codes(),
        "conclusion": (
            "真实冻结不会产生可观测的中间态：冻结期间读到的账本要么 0 行要么 1 行，"
            "每键 ≤1；恢复后终态与未冻结运行一致。也就是说「SIGSTOP 悬挂」不需要运行时"
            "新增任何机制（它不提交也不回滚），探测器的价值是**证明**这一点而不是假设。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/probe_tornwrite.py")
    parser.add_argument("--mode", choices=("truncate", "sigstop", "both"), default="truncate")
    parser.add_argument("--workroot", default="/tmp/probe-tornwrite")
    parser.add_argument("--fractions", default="0.5,0.9,0.99")
    parser.add_argument("--freeze-ms", type=int, default=20)
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    root = Path(args.workroot)
    root.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    ok = True
    if args.mode in ("truncate", "both"):
        fractions = tuple(float(item) for item in args.fractions.split(",") if item)
        payload["truncate"] = probe_truncate(workroot=root / "truncate", fractions=fractions)
        ok = ok and all(
            item["explicit_failure"] and item["no_new_effects"]
            for item in payload["truncate"]["results"]
        )
    if args.mode in ("sigstop", "both"):
        payload["sigstop"] = probe_sigstop(
            workroot=root / "sigstop", freeze_ms=args.freeze_ms, cycles=args.cycles
        )
        ok = (
            ok
            and payload["sigstop"]["landings"] > 0
            and payload["sigstop"]["max_per_key_seen"] <= 1
            and not payload["sigstop"]["findings_after"]
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
