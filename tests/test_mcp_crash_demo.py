"""M2 验收：MCP 服务被真 SIGKILL → 重启续跑 → 外部账本 1 次；对照组显示重复。

这一组**跑的是同一份演示代码**（`integrations/mcp_crash_demo.py`），不另写一套：
`python -m integrations.mcp_crash_demo` 与这里的断言共用 `run_demo`。

判定全部由外部账本（`world.db`，读时连 `-wal`/`-shm` 一起快照）作出；
runtime 自己的日志只用来回答"这次是不是靠探针对账收敛的"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - pytest 通常已经加了仓库根
    sys.path.insert(0, str(PROJECT_ROOT))


def _require_sdk() -> None:
    pytest.importorskip("mcp", reason="需要 [mcp] extra：pip install -e '.[mcp]'")
    pytest.importorskip("mcp.client.stdio")


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """整个模块跑一次演示（两格 × 崩溃 + 重启），避免重复 4 次进程开销。"""
    _require_sdk()
    from integrations.mcp_crash_demo import run_demo

    workroot = tmp_path_factory.mktemp("mcp-crash")
    return run_demo(workroot)


def test_both_cells_were_really_sigkilled(demo: dict) -> None:
    """崩溃必须是**真 SIGKILL**：退出码 137（128+9）＋ 服务自己写下的崩溃位置证据。"""
    from integrations.mcp_crash_demo import CRASH_WINDOW

    assert demo["summary"]["cells"] == 2
    assert demo["summary"]["real_kills"] == 2, "两格都要留下 crash_marker.json"
    assert demo["summary"]["sigkill_exit_codes"] == 2, "两格都要是 137（被 SIGKILL 杀死）"
    for name, cell in demo["cells"].items():
        crash = cell["crash"]
        assert crash["marker_present"] is True, name
        assert crash["killed_by_sigkill"] is True, name
        assert crash["exit_code"] == 137, name
        assert crash["window"] == CRASH_WINDOW, name
        assert crash["occurrence"] == 1, name
        assert crash["injection_kind"] == "window", name
        assert isinstance(crash["pid"], int) and crash["pid"] > 0, name
        # 崩溃点就在"效果已发生、任何记录未落盘"那个窗口上
        assert crash["counts"][CRASH_WINDOW] == 1, name
        assert crash["counts"]["post_approval_pre_exec"] == 1, name


def test_exactly_once_cell_keeps_the_ledger_at_one(demo: dict) -> None:
    """格 A（outbox on）：崩溃时效果已发生，重启后靠意图行 + 探针对账收敛，**不重跑**。"""
    cell = demo["cells"]["with_outbox"]
    assert cell["outbox"] == "on"
    assert cell["gated_status"] == "pending_approval", "写调用必须先停在审批门"
    assert cell["after_crash_max_per_key"] == 1, "崩溃前效果已经发生（W2 窗口）"
    assert cell["max_per_key"] == 1, "重启续跑后仍然恰好一次"
    assert cell["effects"] == 1
    assert cell["reconstructed_from_probe"] is True, "结论必须来自探针对账，而不是重跑"
    assert cell["pending_after_restart"] == []
    assert cell["verdict"] == "as-predicted"


def test_control_cell_shows_the_duplicate(demo: dict) -> None:
    """格 B（outbox off）：没有意图行可对账 ⇒ 恢复只能重跑 ⇒ 外部账本 2 行。

    没有这一格，"1 次"就区分不了"机制在起作用"与"什么都没发生"
    （`docs/HANDOFF.md` §六 第 7 条）。
    """
    cell = demo["cells"]["without_outbox"]
    assert cell["outbox"] == "off"
    assert cell["after_crash_max_per_key"] == 1, "两格的崩溃前状态相同"
    assert cell["max_per_key"] == 2, "对照组必须看到重复"
    assert cell["effects"] == 2
    assert cell["reconstructed_from_probe"] is False
    assert cell["verdict"] == "as-predicted"


def test_the_two_cells_differ_only_in_outbox(demo: dict) -> None:
    """对照必须是单变量：只有 outbox 不同，其余（下游非幂等、探针）逐字相同。"""
    first, second = demo["cells"]["with_outbox"], demo["cells"]["without_outbox"]
    assert first["outbox"] != second["outbox"]
    assert first["tool_idem"] == second["tool_idem"] == "off"
    assert first["crash"]["window"] == second["crash"]["window"]
    assert first["gated_status"] == second["gated_status"] == "pending_approval"
    assert first["write_call_status"] == second["write_call_status"] == "executed"
    assert demo["summary"]["control_shows_duplication"] is True
    assert demo["summary"]["reconciled_via_probe"] is True
    assert demo["summary"]["all_as_predicted"] is True


def test_ledger_is_read_through_a_wal_aware_snapshot(demo: dict) -> None:
    """判分读的是**外部账本**而不是 runtime 日志：两份证据都在，且互相独立。

    这一条守的是"禁止用 runtime 自己的日志当裁判"——演示必须能从 run 目录里
    拿出 `world.db` 的计数，而不是只复述事件日志里的说法。
    """
    from integrations.mcp_crash_demo import _event_type_counts
    from opsenv.oracle import read_effects

    for name, cell in demo["cells"].items():
        run_dir = Path(cell["run_dir"])
        assert (run_dir / "world.db").exists(), name
        ledger = read_effects(run_dir)
        assert ledger["max_per_key"] == cell["max_per_key"], f"{name}: 账本与报告不一致"
        # 事件日志里的 tool_result 各自只有一条（runtime 侧看起来都"执行了一次"）——
        # 重复只体现在外部账本上，这正是"日志不是裁判"的原因。
        counts = _event_type_counts(run_dir)
        assert counts["tool_result"] == 1, name
