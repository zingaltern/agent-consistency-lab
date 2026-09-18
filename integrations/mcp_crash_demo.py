"""M2 演示：MCP 服务被**真 SIGKILL** → 重启 → 同一 run 续跑 → 外部账本判分。

对照是两格、**单变量只差 outbox**（都 `--tool-idem off` 让下游非幂等、`--probe on`）：

| 格 | 期望（外部账本 `world.db`） |
|---|---|
| `with_outbox`（`--outbox on`） | 效果**恰好 1 次**：重启后靠意图行 + 探针对账，不重跑 |
| `without_outbox`（`--outbox off`） | **重复 2 次**：没有意图行可对账，恢复只能重跑 |

只报"1 次"不算演示——没有对照就无法区分"机制在起作用"与"什么都没发生"
（`docs/HANDOFF.md` §六 第 7 条）。因此两格都必须跑，且都留崩溃证据。

纪律（与 `experiments/crash_matrix.py` 同规格）：

* **真 SIGKILL**：由 `harness/chaos.py` 的命名窗口 `post_tool_effect_pre_record` 在
  **服务进程内**触发（命中即 `os.kill(os.getpid(), SIGKILL)`），不 mock、不模拟抛异常。
* **崩溃位置证据**：服务进程自己写下的 `crash_marker.json`（window / occurrence / counts / pid）
  ＋ 退出码文件（137 = 128+9，即被 SIGKILL 杀死）。
* **判分只认外部账本**：`opsenv.oracle.read_effects`（读 `world.db` 时连 `-wal`/`-shm`
  一起快照）。runtime 自己的日志不作为"发生了几次"的证据。

运行环境：POSIX（要用 SIGKILL 与 `/bin/sh` 记录退出码）。不联网、不需要 Docker。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from opsenv.oracle import read_effects

from .mcp_server import APPROVE_TOOL, build_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 崩溃窗口：效果已发生、任何记录未落盘——W2 认定的唯一一致性窗口。
CRASH_WINDOW = "post_tool_effect_pre_record"

WRITE_TOOL = "scale_pool"
WRITE_ARGS = {"service": "payment", "size": 64}

CELLS: tuple[tuple[str, bool], ...] = (("with_outbox", True), ("without_outbox", False))

CLIENT_TIMEOUT_SECONDS = 20.0


def _server_command(run_dir: Path, *, outbox: bool, exit_code_file: Path) -> list[str]:
    """把服务包一层 ``/bin/sh``：服务被 SIGKILL 后由 sh 把退出码写进文件。

    为什么要这一层：SDK 的 stdio 客户端自己 spawn 子进程，拿不到它的退出码；
    而"真 SIGKILL"最硬的证据就是退出码。137 = 128 + 9（被 SIGKILL 杀死）。
    """
    inner = (
        f"{shlex.quote(sys.executable)} -m integrations.mcp_server"
        f" --run-dir {shlex.quote(str(run_dir))}"
        f" --outbox {'on' if outbox else 'off'}"
        " --tool-idem off --probe on"
    )
    return ["/bin/sh", "-c", f"{inner}; echo $? > {shlex.quote(str(exit_code_file))}"]


def _chaos_env(spec: str) -> dict[str, str]:
    return {"CHAOS_WINDOWS": spec, "PYTHONHASHSEED": "0"}


async def _phase_crash(
    run_dir: Path, *, outbox: bool, exit_code_file: Path
) -> dict[str, Any]:
    """起服务 → 写调用停在审批门 → approve（执行时被 SIGKILL）。"""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command="/bin/sh",
        args=_server_command(run_dir, outbox=outbox, exit_code_file=exit_code_file)[1:],
        cwd=str(PROJECT_ROOT),
        env=_chaos_env(f"{CRASH_WINDOW}:1"),
    )
    evidence: dict[str, Any] = {"client_saw": "", "gated": None}
    async with stdio_client(params) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream
    ) as session:
        await session.initialize()
        gated = await session.call_tool(WRITE_TOOL, dict(WRITE_ARGS))
        payload = gated.structured_content or {}
        evidence["gated"] = payload.get("status")
        approve_args = {"tool_call_id": payload.get("tool_call_id"), "decision": "approve"}
        try:
            await session.call_tool(APPROVE_TOOL, approve_args)
            evidence["client_saw"] = "approve 正常返回（说明服务没死——异常路径）"
        except Exception as exc:  # 服务在请求中途被杀，客户端必然看到连接层错误
            evidence["client_saw"] = f"{type(exc).__name__}"
    return evidence


async def _phase_restart(run_dir: Path, *, outbox: bool) -> dict[str, Any]:
    """重启服务（**不带崩溃窗口**）：启动即恢复，把未闭合的调用续跑完。"""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "integrations.mcp_server",
            "--run-dir",
            str(run_dir),
            "--outbox",
            "on" if outbox else "off",
            "--tool-idem",
            "off",
            "--probe",
            "on",
        ],
        cwd=str(PROJECT_ROOT),
        env=_chaos_env(""),  # 重启不许再杀：否则对照格会陷入"恢复→再被杀"的循环
    )
    async with stdio_client(params) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream, read_timeout_seconds=30.0
    ) as session:
        await session.initialize()  # 启动时的恢复已经跑完
        listed = await session.call_tool("list_pending_approvals", {})
        return {"pending_after_restart": (listed.structured_content or {}).get("pending", [])}


def _crash_evidence(run_dir: Path, exit_code_file: Path) -> dict[str, Any]:
    marker_path = run_dir / "crash_marker.json"
    evidence: dict[str, Any] = {
        "marker_path": str(marker_path),
        "marker_present": marker_path.exists(),
        "exit_code": None,
    }
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        evidence.update(
            {
                "window": marker.get("window"),
                "occurrence": marker.get("occurrence"),
                "injection_kind": marker.get("injection_kind"),
                "pid": marker.get("pid"),
                "counts": marker.get("counts"),
            }
        )
    if exit_code_file.exists():
        text = exit_code_file.read_text(encoding="utf-8").strip()
        evidence["exit_code"] = int(text) if text.isdigit() else None
    evidence["killed_by_sigkill"] = evidence["exit_code"] == 137
    return evidence


def _write_call_outcome(run_dir: Path) -> dict[str, Any]:
    """从事件日志读那次写调用的最终结论（含是否**靠探针对账**而不是重跑）。"""
    env = build_environment(run_dir, tool_idem=False, probe=True, outbox=True)
    try:
        events = env.store.effective_events(env.ctx.branch_id)
    finally:
        env.close()
    for event in reversed(events):
        if event.type == "tool_result" and event.payload.get("status") == "executed":
            result = event.payload.get("result") or {}
            return {
                "status": event.payload["status"],
                "reconstructed_from_probe": bool(result.get("reconstructed_from_probe")),
            }
    return {"status": "missing", "reconstructed_from_probe": False}


def _event_type_counts(run_dir: Path) -> dict[str, int]:
    env = build_environment(run_dir, tool_idem=False, probe=True, outbox=True)
    try:
        events = env.store.effective_events(env.ctx.branch_id)
    finally:
        env.close()
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    return counts


def run_cell(workroot: Path, name: str, *, outbox: bool) -> dict[str, Any]:
    """跑一格：崩溃 → 重启 → 判分。"""
    run_dir = workroot / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    exit_code_file = run_dir / "server_exit_code.txt"

    crash_client = asyncio.run(_phase_crash(run_dir, outbox=outbox, exit_code_file=exit_code_file))
    crash = _crash_evidence(run_dir, exit_code_file)
    ledger_after_crash = read_effects(run_dir)

    restart = asyncio.run(_phase_restart(run_dir, outbox=outbox))
    ledger = read_effects(run_dir)
    outcome = _write_call_outcome(run_dir)

    expected = 1 if outbox else 2
    return {
        "outbox": "on" if outbox else "off",
        "tool_idem": "off",
        "crash": crash,
        "client_saw": crash_client["client_saw"],
        "gated_status": crash_client["gated"],
        "after_crash_max_per_key": ledger_after_crash["max_per_key"],
        "effects": ledger["ledger_rows"],
        "max_per_key": ledger["max_per_key"],
        "expected_effects": expected,
        "reconstructed_from_probe": outcome["reconstructed_from_probe"],
        "write_call_status": outcome["status"],
        "pending_after_restart": restart["pending_after_restart"],
        "event_type_counts": _event_type_counts(run_dir),
        "verdict": "as-predicted" if ledger["max_per_key"] == expected else "prediction-violated",
        "run_dir": str(run_dir),
    }


def run_demo(workroot: Path) -> dict[str, Any]:
    cells = {name: run_cell(workroot, name, outbox=outbox) for name, outbox in CELLS}
    kills = sum(1 for cell in cells.values() if cell["crash"]["marker_present"])
    sigkills = sum(1 for cell in cells.values() if cell["crash"]["killed_by_sigkill"])
    with_outbox = cells["with_outbox"]
    without_outbox = cells["without_outbox"]
    return {
        "cells": cells,
        "summary": {
            "cells": len(cells),
            "real_kills": kills,
            "sigkill_exit_codes": sigkills,
            "crash_window": CRASH_WINDOW,
            "exactly_once_cell_effects": with_outbox["max_per_key"],
            "control_cell_effects": without_outbox["max_per_key"],
            "control_shows_duplication": without_outbox["max_per_key"] > 1,
            "reconciled_via_probe": with_outbox["reconstructed_from_probe"],
            "all_as_predicted": all(cell["verdict"] == "as-predicted" for cell in cells.values()),
        },
        "note": (
            "对照只有一处不同（outbox on/off，下游都是非幂等写 + 探针可用）。"
            "判分只认外部账本 world.db（连 -wal 一起快照）；runtime 日志只用来读"
            "'这次是不是靠探针对账收敛的'。本演示只对 kill -9 语义成立，不是掉电语义。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="integrations.mcp_crash_demo",
        description="MCP 服务真 SIGKILL → 重启续跑 → 外部账本判分（含对照组）",
    )
    parser.add_argument("--work-root", required=True, help="跑两格的工作目录（会被清空重建）")
    parser.add_argument("--json-out", default="", help="把结果 JSON 写到文件")
    args = parser.parse_args(argv)

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("需要 [mcp] extra：pip install -e '.[mcp]'", file=sys.stderr)
        return 2

    workroot = Path(args.work_root)
    workroot.mkdir(parents=True, exist_ok=True)
    result = run_demo(workroot)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    summary = result["summary"]
    ok = (
        summary["all_as_predicted"]
        and summary["control_shows_duplication"]
        and summary["sigkill_exit_codes"] == summary["cells"]
    )
    print(
        f"[mcp_crash_demo] 恰好一次={summary['exactly_once_cell_effects']} "
        f"对照组={summary['control_cell_effects']} 真 SIGKILL={summary['sigkill_exit_codes']}"
        f"/{summary['cells']} ⇒ {'as-predicted' if ok else 'PREDICTION-VIOLATED'}",
        file=sys.stderr,
    )
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
