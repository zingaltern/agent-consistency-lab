"""崩溃注入器：窗口规格解析与进程内计数（真实 SIGKILL 由矩阵测试覆盖）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.chaos import WINDOWS, Chaos, parse_spec


def test_parse_spec_single_window() -> None:
    assert parse_spec("pre_tool_exec") == {"pre_tool_exec": 1}


def test_parse_spec_with_occurrence_and_spaces() -> None:
    assert parse_spec(" pre_tool_exec:2 , during_compaction ") == {
        "pre_tool_exec": 2,
        "during_compaction": 1,
    }


def test_parse_spec_empty_is_disabled() -> None:
    assert parse_spec("") == {}


def test_parse_spec_rejects_unknown_window() -> None:
    with pytest.raises(ValueError, match="unknown chaos window"):
        parse_spec("not_a_window")


def test_all_documented_windows_exist() -> None:
    assert len(WINDOWS) == 6
    assert "post_tool_effect_pre_record" in WINDOWS


def test_hit_counts_without_arming(tmp_path: Path) -> None:
    marker = tmp_path / "crash_marker.json"
    chaos = Chaos({"pre_tool_exec": 5}, marker)
    for _ in range(4):
        chaos.hit("pre_tool_exec")  # 未到第 5 次，不会触发 SIGKILL
    assert not marker.exists()


def test_disabled_chaos_never_writes_marker(tmp_path: Path) -> None:
    chaos = Chaos.disabled()
    chaos.hit("pre_tool_exec")
    assert not (tmp_path / "crash_marker.json").exists()
    assert chaos.windows == {}


def test_marker_payload_shape(tmp_path: Path) -> None:
    """计数一次后直接调用私有写入方法验证标记内容（真实触发会 SIGKILL 当前进程）。"""
    marker = tmp_path / "crash_marker.json"
    chaos = Chaos({"post_record_pre_commit": 2}, marker)
    chaos.hit("post_record_pre_commit")  # 第 1 次只计数，不触发
    chaos._write_marker("post_record_pre_commit")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["window"] == "post_record_pre_commit"
    assert payload["occurrence"] == 1
    assert isinstance(payload["pid"], int)
