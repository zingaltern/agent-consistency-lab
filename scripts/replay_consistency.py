"""scripted 与 replay 的双路一致性对账（B-M1 验收）。

流程（全部在 /tmp 的工作副本上做，仓库只读）：

1. ``record`` 模式（``--transport scripted``：用脚本模型当供应商）在同一条轨迹上录一份 cassette；
2. ``scripted`` 模式跑同一条轨迹（不录制）；
3. ``replay`` 模式在同一份 cassette 上跑同一条轨迹；
4. 按**白名单**比对两次运行：token 段 / cost / 事件序列 / tool_calls 结构。
   时间戳类字段（``created_at``、``at``、``recorded_at``）**不比较**——它们天然不同。

为什么白名单而不是全量比对：``run_id`` / 事件 id / 时间戳在三次运行里必然不同，
全量比对只会产出永远红的测试；而"白名单 + 明确列出不比较什么"是可以被审阅的口径。

输出 JSON（供 claim 门禁与测试共用）::

    {"identical": true, "whitelist": [...], "excluded": [...], "diffs": [], ...}
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

WHITELIST = (
    "outcome.status",
    "outcome.step",
    "outcome.executed",
    "outcome.replayed",
    "outcome.reconciled",
    "outcome.unknown",
    "outcome.rejected",
    "outcome.probes",
    "outcome.checkpoints",
    "outcome.compactions",
    "outcome.overflows",
    "outcome.cost_usd",
    "outcome.view_violations",
    "events.sequence",
    "events.context_tokens",
    "events.cache_read_tokens",
    "events.cache_write_tokens",
    "events.cost_usd",
    "tool_calls.structure",
)
EXCLUDED = (
    "created_at / recorded_at / at（时间戳类字段）",
    "run_id / thread_id / branch_id / event_id / tool_call_id 之外的 id 生成",
    "avg_wall_ms 等计时噪声",
    "replay 模式下 view_fingerprint（恢复路径会合法改变视图，见开放问题 §五）",
)


def _worker(run_dir: Path, mode: str, *extra: str) -> dict[str, Any]:
    argv = [
        sys.executable,
        "-m",
        "experiments.worker",
        "--run-dir",
        str(run_dir),
        "--mode",
        mode,
        *extra,
    ]
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


def _fingerprint(run_dir: Path) -> dict[str, Any]:
    """白名单指纹：outcome + 事件序列 + token/cost 段（都不含时间戳）。"""
    from harness.store.sqlite_store import SqliteStore

    ids = json.loads((run_dir / "ids.json").read_text(encoding="utf-8"))
    store = SqliteStore(run_dir / "runtime.db")
    try:
        events = store.effective_events(ids["branch_id"])
    finally:
        store.close()
    sequence: list[str] = []
    tool_calls: list[tuple[str, str, str]] = []
    tokens: list[tuple[int, int, int, float]] = []
    for event in events:
        sequence.append(f"{event.kind.value}:{event.type}")
        payload = event.payload
        if event.type == "tool_call":
            tool_calls.append(
                (
                    str(payload.get("tool_call_id")),
                    str(payload.get("tool")),
                    json.dumps(payload.get("args"), sort_keys=True, ensure_ascii=False),
                )
            )
        if event.type == "agent_message":
            tokens.append(
                (
                    int(payload.get("context_tokens", 0) or 0),
                    int(payload.get("cache_read_tokens", 0) or 0),
                    int(payload.get("cache_write_tokens", 0) or 0),
                    round(float(payload.get("cost_usd", 0.0) or 0.0), 12),
                )
            )
    outcome: dict[str, Any] = {}
    final = run_dir / "outcome_resume.json"
    if final.exists():
        outcome = json.loads(final.read_text(encoding="utf-8"))
    return {
        "outcome": outcome,
        "events": sequence,
        "tool_calls": tool_calls,
        "tokens": tokens,
        "branch_id": ids["branch_id"],
    }


def _compare(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for key in ("status", "step", "executed", "replayed", "reconciled", "unknown", "rejected",
                "probes", "checkpoints", "compactions", "overflows", "cost_usd",
                "view_violations", "input_tokens", "cache_read_tokens", "cache_write_tokens"):
        a, b = left["outcome"].get(key), right["outcome"].get(key)
        if a != b:
            diffs.append({"field": f"outcome.{key}", "left": a, "right": b})
    if left["events"] != right["events"]:
        diffs.append(
            {
                "field": "events.sequence",
                "left": left["events"][:20],
                "right": right["events"][:20],
            }
        )
    if left["tool_calls"] != right["tool_calls"]:
        diffs.append(
            {
                "field": "tool_calls.structure",
                "left": left["tool_calls"],
                "right": right["tool_calls"],
            }
        )
    if left["tokens"] != right["tokens"]:
        diffs.append(
            {"field": "events.tokens_cost", "left": left["tokens"], "right": right["tokens"]}
        )
    return diffs


def run_consistency(*, workroot: Path, quiet: bool = False) -> dict[str, Any]:
    if workroot.exists():
        shutil.rmtree(workroot)
    workroot.mkdir(parents=True)
    cassette_dir = workroot / "cassette"
    record_dir = workroot / "record"
    scripted_dir = workroot / "scripted"
    replay_dir = workroot / "replay"

    phases = ("run", "approve", "resume")
    for phase in phases:
        payload = _worker(
            record_dir,
            phase,
            "--model",
            "record",
            "--transport",
            "scripted",
            "--record-dir",
            str(cassette_dir),
        )
        if payload["exit_code"] != 0:
            return {
                "identical": False,
                "error": f"record/{phase} 失败：{payload}",
                "whitelist": list(WHITELIST),
                "excluded": list(EXCLUDED),
            }
    for phase in phases:
        payload = _worker(scripted_dir, phase)
        if payload["exit_code"] != 0:
            return {
                "identical": False,
                "error": f"scripted/{phase} 失败：{payload}",
                "whitelist": list(WHITELIST),
                "excluded": list(EXCLUDED),
            }
    for phase in phases:
        payload = _worker(replay_dir, phase, "--model", "replay", "--record-dir", str(cassette_dir))
        if payload["exit_code"] != 0:
            return {
                "identical": False,
                "error": f"replay/{phase} 失败：{payload}",
                "whitelist": list(WHITELIST),
                "excluded": list(EXCLUDED),
            }

    cassette = json.loads((cassette_dir / "cassette.json").read_text(encoding="utf-8"))
    diffs = _compare(_fingerprint(scripted_dir), _fingerprint(replay_dir))
    result = {
        "identical": not diffs,
        "diffs": diffs,
        "whitelist": list(WHITELIST),
        "excluded": list(EXCLUDED),
        "cassette": {
            "path": str(cassette_dir / "cassette.json"),
            "entries": len(cassette["entries"]),
            "model": cassette["meta"]["model"],
            "provider": cassette["meta"]["provider"],
            "schema_version": cassette["meta"]["schema_version"],
            "usage_segments": (
                sorted(cassette["entries"][0]["usage"]) if cassette["entries"] else []
            ),
            "has_tool_calls": bool(
                cassette["entries"] and cassette["entries"][0]["tool_calls"]
            ),
        },
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/replay_consistency.py")
    parser.add_argument("--workroot", default="/tmp/replay-consistency")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)
    result = run_consistency(workroot=Path(args.workroot))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if result["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
