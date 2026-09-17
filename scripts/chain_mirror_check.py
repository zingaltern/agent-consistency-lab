"""哈希链的 mirror check（R-B3）：干净库必须通过，被篡改的副本必须被指出第一个断点。

这是 `tests/test_hash_chain.py::test_mirror_test_detects_a_single_tampered_row` 的
**可再生命令版**：测试证明"代码里有这条性质"，本脚本产出 JSON 让 claim 门禁能对账它。

流程（全部在 /tmp，仓库只读）：

1. 真跑一遍 run → approve → resume（不是构造假库：链必须由**真实写入路径**产生）；
2. 审计原库 → 期望 ``ok=true``；
3. 复制一份，绕过触发器改掉一行历史（模拟"库被换过/被改过"），审计副本
   → 期望 ``ok=false`` 且 ``first_break.code == INV-008``，并报出断点位置。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.audit_chain import audit  # noqa: E402


def _worker(run_dir: Path, mode: str) -> int:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.worker",
            "--run-dir",
            str(run_dir),
            "--mode",
            mode,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode


def _tamper(db_path: Path, *, seq: int) -> None:
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER IF EXISTS events_no_update")
    con.execute("DROP TRIGGER IF EXISTS events_no_delete")
    con.execute(
        "UPDATE events SET payload_json=? WHERE seq=?",
        (json.dumps({"text": "被人改过的历史"}, ensure_ascii=False), seq),
    )
    con.commit()
    con.close()


def run_check(*, workroot: Path, tamper_seq: int = 4) -> dict:
    if workroot.exists():
        shutil.rmtree(workroot)
    run_dir = workroot / "run"
    run_dir.mkdir(parents=True)
    for mode in ("run", "approve", "resume"):
        code = _worker(run_dir, mode)
        if code != 0:
            raise SystemExit(
                f"{mode} 阶段失败（退出码 {code}）——镜像检查的前提是能跑完一次真实 run"
            )

    clean = audit(run_dir / "runtime.db")

    mirror_dir = workroot / "mirror"
    shutil.copytree(run_dir, mirror_dir)
    _tamper(mirror_dir / "runtime.db", seq=tamper_seq)
    tampered = audit(mirror_dir / "runtime.db")

    return {
        "clean_ok": clean["ok"],
        "clean_events": clean["events_checked"],
        "tampered_ok": tampered["ok"],
        "detection_code": (tampered["first_break"] or {}).get("code"),
        "first_break_seq": (tampered["first_break"] or {}).get("seq"),
        "first_break_detail": (tampered["first_break"] or {}).get("detail"),
        "note": (
            "干净库通过、被篡改的副本必须被指出第一个断点——链不阻止篡改，只让篡改无法静默"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/chain_mirror_check.py")
    parser.add_argument("--workroot", default="/tmp/chain-mirror")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)
    result = run_check(workroot=Path(args.workroot))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    ok = result["clean_ok"] and not result["tampered_ok"] and result["detection_code"] == "INV-008"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
