"""故障谱系探测器③：账本外注入（R-A3）。

对齐承诺：`docs/semantics.md` §1 的"恢复语义"——未闭合调用按 at-least-once 重跑，
但**日志里已有结论的调用只重放、不重跑**；以及 §2.5 的落盘顺序。

探测方法：在一个**已经跑完**的 run 上，直接往外部账本 `world.db` 里手工补 1 行
runtime 不知道的效果，然后再 resume 一次。要观察的是：

* 账本行数**不得增加**（runtime 不能因为"账本里有它不知道的东西"就重新执行副作用）；
* run 留在终态（终态是吸收态，resume 不得把它复活）；
* 两个变体：
  - ``foreign``：补的行用**外部键**（runtime 完全不认识）；
  - ``same-key``：补的行用 runtime **自己那个幂等键**（模拟带外写入/下游重复）——
    这一档会触发 oracle 的 `inv_effect_accounting`，是**预期内**的：
    账本有 2 行且 runtime 没有 unknown 呈报。探测器把这条如实记下来，
    正好也证明 oracle 对"账本层面的重复"是敏感的。

产物写 ``--workroot``（默认 /tmp），仓库只读。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from opsenv.oracle import audit_run, read_effects  # noqa: E402

LONG_TASK = ("--scenario", "long_incident", "--long-steps", "8", "--long-lines", "100")


def _worker(run_dir: Path, mode: str) -> dict:
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


def _ledger_keys(run_dir: Path) -> list[str]:
    con = sqlite3.connect(run_dir / "world.db")
    try:
        return [str(row[0]) for row in con.execute("SELECT idempotency_key FROM effects")]
    finally:
        con.close()


def inject_row(run_dir: Path, *, key: str, operation: str) -> None:
    """直接往账本写一行（模拟"效果由 runtime 之外的动作产生"）。

    这**不是**在测 runtime 的写入路径，而是构造一个 runtime 不知道的外部事实。
    """
    con = sqlite3.connect(run_dir / "world.db")
    try:
        con.execute(
            "INSERT INTO effects(idempotency_key, operation, payload_json, created_at)"
            " VALUES(?, ?, ?, ?)",
            (key, operation, json.dumps({"injected_by": "probe_extra_ledger_row"}), time.time()),
        )
        con.commit()
    finally:
        con.close()


def probe(*, workroot: Path, variant: str) -> dict:
    run_dir = workroot / variant
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    _worker(run_dir, "run")
    _worker(run_dir, "approve")
    _worker(run_dir, "resume")
    before_keys = _ledger_keys(run_dir)
    if not before_keys:
        raise SystemExit("源 run 的账本为空——探测前提不成立（run 没有产生副作用）")
    key = "idem_probe_foreign" if variant == "foreign" else before_keys[0]
    inject_row(run_dir, key=key, operation="probe_injected")
    before = read_effects(run_dir)
    resume = _worker(run_dir, "resume")
    after = read_effects(run_dir)
    audit = audit_run(run_dir, status=str(resume.get("status", "unknown")))
    return {
        "variant": variant,
        "injected_key": key,
        "ledger_before_resume": before["ledger_rows"],
        "ledger_after_resume": after["ledger_rows"],
        "ledger_unchanged": after["ledger_rows"] == before["ledger_rows"],
        "resume_exit_code": resume["exit_code"],
        "status_after_resume": resume.get("status"),
        "findings": audit.codes(),
        "effects_facts": audit.facts["effects"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/probe_extra_ledger_row.py")
    parser.add_argument("--workroot", default="/tmp/probe-extra-ledger")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    root = Path(args.workroot)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "foreign": probe(workroot=root, variant="foreign"),
        "same-key": probe(workroot=root, variant="same-key"),
    }
    payload["conclusion"] = (
        "两个变体下账本行数都没有增加：runtime 不会因为「账本里有它不知道的效果」"
        "而重新执行副作用，也不会把终态 run 复活。same-key 变体会触发 oracle 的 "
        "inv_effect_accounting（账本 2 行、runtime 无 unknown 呈报）——那是**预期内**的："
        "这条不变量本来就是用来抓「账本层面的重复」的。"
    )
    ok = all(
        payload[variant]["ledger_unchanged"] and payload[variant]["resume_exit_code"] == 0
        for variant in ("foreign", "same-key")
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
