"""三种模型模式（scripted / record / replay）的接口回归（R-B1）。

这个文件回答四个问题，每个都对应一条会被误实现的地方：

1. **录制的 schema 够不够驱动工具循环**：条目必须含 ``tool_calls``（含 id/tool/args）
   与 canonical usage 四段 —— 缺了 tool_calls，loop 根本走不动；
2. **usage 归一**：OpenAI 与 Anthropic 两种口径的字段映射（含"prompt_tokens 是否含缓存"
   这条会差一个量级的口径差异）；
3. **replay 的失败可读**：未命中要说清缺哪份录制与期望的提示词；
   提示词不一致是**警告**（崩溃恢复会合法改变视图），不是失败；
4. **录制/回放不改变崩溃语义**：``CHAOS_WINDOWS`` 注入在两模式下与既有语义一致。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.cassette import (
    CassetteError,
    CassetteMeta,
    CassetteStore,
    RecordedTurn,
    normalize_usage,
    prompt_includes_cache,
    request_fingerprint,
)
from harness.llm import (
    RawCompletion,
    RecordingLLMClient,
    ReplayLLMClient,
    ScriptedTransport,
    total_input_tokens,
)
from harness.model import ModelTurn
from harness.tools import ToolCallRequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSISTENCY = PROJECT_ROOT / "scripts" / "replay_consistency.py"


class _FakeView:
    """最小视图替身：只需要 block_texts() / sections / fingerprint()。"""

    def __init__(self, text: str) -> None:
        self.blocks = (text,)
        self.sections = {"history": len(text)}

    def block_texts(self) -> tuple[str, ...]:
        return self.blocks

    def fingerprint(self) -> str:
        return request_fingerprint(self.blocks)


class _StaticTransport:
    """固定返回一条带工具调用的响应（记录 transport 的输入输出关系）。"""

    def __init__(self, *, text: str, usage: dict, tool: str = "") -> None:
        self.text = text
        self.usage = usage
        self.tool = tool
        self.calls: list[int] = []

    def complete(self, *, blocks: tuple[str, ...], step: int) -> RawCompletion:
        self.calls.append(step)
        return RawCompletion(
            text=self.text,
            tool_calls=(
                [ToolCallRequest(tool_call_id=f"tc_{step}", tool=self.tool, args={"size": 64})]
                if self.tool
                else []
            ),
            usage_raw=dict(self.usage),
        )


class _ScriptedModel:
    def __init__(self) -> None:
        self.turns = 0

    def next_turn(self, *, step: int, view: object = None) -> ModelTurn:
        self.turns += 1
        return ModelTurn(
            text=f"turn {step}",
            tool_calls=[ToolCallRequest(tool_call_id=f"tc_{step}", tool="scale_pool", args={})],
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )


# ------------------------------------------------------------------ usage 归一


def test_normalize_usage_openai_shape() -> None:
    raw = {
        "prompt_tokens": 1200,
        "completion_tokens": 80,
        "prompt_tokens_details": {"cached_tokens": 900},
    }
    segments = normalize_usage(raw)
    assert segments == {
        "prompt_tokens": 1200,
        "completion_tokens": 80,
        "cache_read_tokens": 900,
        "cache_write_tokens": 0,
    }
    assert prompt_includes_cache(raw) is True
    # OpenAI 口径下 prompt_tokens 已含缓存 ⇒ 总输入就是它本身
    assert total_input_tokens(segments, raw) == 1200


def test_normalize_usage_anthropic_shape() -> None:
    raw = {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 100,
    }
    segments = normalize_usage(raw)
    assert segments == {
        "prompt_tokens": 200,
        "completion_tokens": 50,
        "cache_read_tokens": 900,
        "cache_write_tokens": 100,
    }
    assert prompt_includes_cache(raw) is False
    # 不含缓存 ⇒ 总输入 = input + read + write
    assert total_input_tokens(segments, raw) == 1200


def test_normalize_usage_unknown_shape_is_readable_failure() -> None:
    """认不出的 usage 形态必须报错，而不是按 0 计（那会变成"这次调用不要钱"）。"""
    with pytest.raises(CassetteError) as excinfo:
        normalize_usage({"tokens_used": 10})
    assert "缺少输入侧字段" in str(excinfo.value)
    assert "tokens_used" in str(excinfo.value)


# ------------------------------------------------------------------ 录制物


def test_cassette_roundtrip_keeps_tool_calls_and_usage(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    store.append(
        RecordedTurn(
            step=0,
            attempt=0,
            text="先取指标",
            tool_calls=[
                ToolCallRequest(tool_call_id="tc1", tool="query_metrics", args={"svc": "x"})
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 2, "cache_read_tokens": 0,
                   "cache_write_tokens": 0},
            usage_raw={"input_tokens": 10, "output_tokens": 2},
            request_fingerprint="abc",
        ),
        meta=CassetteMeta(model="m", provider="p"),
    )
    loaded = store.load()
    assert loaded.meta.model == "m"
    entry = loaded.find(step=0, attempt=0)
    assert entry is not None
    assert entry.tool_calls[0].tool == "query_metrics"
    assert entry.tool_calls[0].args == {"svc": "x"}
    assert set(entry.usage) == {
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    }


def test_cassette_append_replaces_same_key(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    for text in ("first", "second"):
        store.append(RecordedTurn(step=1, attempt=0, text=text))
    loaded = store.load()
    assert len(loaded.entries) == 1
    assert loaded.entries[0].text == "second"


def test_cassette_load_missing_is_readable(tmp_path: Path) -> None:
    with pytest.raises(CassetteError) as excinfo:
        CassetteStore(tmp_path / "nope").load()
    assert "录制物不存在" in str(excinfo.value)


# ------------------------------------------------------------------ 客户端行为


def test_record_client_writes_canonical_usage_and_returns_response(tmp_path: Path) -> None:
    transport = _StaticTransport(
        text="hello", usage={"input_tokens": 30, "output_tokens": 7}, tool="scale_pool"
    )
    client = RecordingLLMClient(transport, store=CassetteStore(tmp_path), meta=CassetteMeta())
    response = client.complete(step=0, view=_FakeView("prompt-a"))
    assert response.turn.tool_calls[0].tool == "scale_pool"
    assert response.usage.input_tokens == 30
    entry = CassetteStore(tmp_path).load().find(step=0, attempt=0)
    assert entry is not None and entry.usage["completion_tokens"] == 7


def test_attempt_increments_within_the_same_step(tmp_path: Path) -> None:
    """同一步的第二次调用（上下文溢出重试）必须另起 attempt，否则 replay 会串味。"""
    transport = _StaticTransport(text="t", usage={"input_tokens": 1, "output_tokens": 1})
    store = CassetteStore(tmp_path)
    client = RecordingLLMClient(transport, store=store)
    client.complete(step=3, view=_FakeView("a"))
    client.complete(step=3, view=_FakeView("a"))
    entries = store.load().entries
    assert [(entry.step, entry.attempt) for entry in entries] == [(3, 0), (3, 1)]


def test_replay_hit_returns_recorded_values_without_recompute(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    store.append(
        RecordedTurn(
            step=0,
            attempt=0,
            text="recorded",
            usage={"prompt_tokens": 5, "completion_tokens": 1, "cache_read_tokens": 0,
                   "cache_write_tokens": 0},
            cost_usd=0.000123,
        )
    )
    client = ReplayLLMClient(store=store)
    response = client.complete(step=0, view=_FakeView("whatever"))
    assert response.turn.text == "recorded"
    assert response.cost_usd == pytest.approx(0.000123)  # 直接回放，不重算
    assert client.hits == 1


def test_replay_miss_is_readable(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    store.append(
        RecordedTurn(
            step=0,
            attempt=0,
            text="only step 0",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "cache_read_tokens": 0,
                   "cache_write_tokens": 0},
        )
    )
    client = ReplayLLMClient(store=store)
    client.complete(step=0, view=_FakeView("same"))
    with pytest.raises(CassetteError) as excinfo:
        client.complete(step=1, view=_FakeView("next"))
    message = str(excinfo.value)
    assert "未命中" in message and "step=1" in message and "期望的请求指纹" in message


def test_replay_prompt_mismatch_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """崩溃恢复会**合法地**改变视图（探针重建），因此指纹不一致只能是警告。"""
    store = CassetteStore(tmp_path)
    store.append(
        RecordedTurn(
            step=0,
            attempt=0,
            text="recorded",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "cache_read_tokens": 0,
                   "cache_write_tokens": 0},
            request_fingerprint="different",
        )
    )
    client = ReplayLLMClient(store=store)
    response = client.complete(step=0, view=_FakeView("changed prompt"))
    assert response.turn.text == "recorded"
    assert len(client.replay_warnings) == 1
    assert client.replay_warnings[0]["step"] == 0


def test_scripted_transport_reports_cache_segments(tmp_path: Path) -> None:
    """脚本 transport 按 Anthropic 口径报缓存段（否则与脚本模式的成本口径不可比）。"""
    transport = ScriptedTransport(_ScriptedModel())
    first = transport.complete(blocks=("很长的前缀块" * 200,), step=0)
    second = transport.complete(blocks=("很长的前缀块" * 200,), step=0)
    assert first.usage_raw["cache_creation_input_tokens"] > 0
    assert second.usage_raw["cache_read_input_tokens"] > 0
    assert first.usage_raw["input_tokens"] + first.usage_raw["cache_creation_input_tokens"] > 0


# ------------------------------------------------------------------ 端到端


def test_scripted_and_replay_are_whitelist_identical(tmp_path: Path) -> None:
    """B-M1 的核心验收：同一份录制上，scripted 与 replay 的白名单字段一致。"""
    from scripts.replay_consistency import run_consistency

    result = run_consistency(workroot=tmp_path / "consistency", quiet=True)
    assert result["identical"], result.get("diffs") or result.get("error")
    assert result["cassette"]["entries"] >= 3
    assert result["cassette"]["has_tool_calls"] is True
    assert result["cassette"]["usage_segments"] == [
        "cache_read_tokens",
        "cache_write_tokens",
        "completion_tokens",
        "prompt_tokens",
    ]


def test_replay_mode_accepts_chaos_injection(tmp_path: Path) -> None:
    """replay 模式必须接受既有 CHAOS_WINDOWS 注入（崩溃语义一个字不改）。"""
    workroot = tmp_path / "chaos"
    cassette = workroot / "cassette"
    run_dir = workroot / "run"
    for phase in ("run", "approve", "resume"):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "experiments.worker",
                "--run-dir",
                str(run_dir),
                "--mode",
                phase,
                "--model",
                "record",
                "--transport",
                "scripted",
                "--record-dir",
                str(cassette),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    # 用录制重跑一遍，并在 resume 阶段注入窗口 2
    second = workroot / "replay-crashed"
    for phase in ("run", "approve"):
        subprocess.run(
            [
                sys.executable, "-m", "experiments.worker", "--run-dir", str(second),
                "--mode", phase, "--model", "replay", "--record-dir", str(cassette),
            ],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300, check=True,
        )
    env = {**os.environ, "CHAOS_WINDOWS": "post_tool_effect_pre_record:1"}
    crashed = subprocess.run(
        [
            sys.executable, "-m", "experiments.worker", "--run-dir", str(second),
            "--mode", "resume", "--model", "replay", "--record-dir", str(cassette),
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300, env=env,
    )
    assert crashed.returncode != 0, "窗口注入必须真的杀死进程"
    marker = json.loads((second / "crash_marker.json").read_text(encoding="utf-8"))
    assert marker["window"] == "post_tool_effect_pre_record"
    assert marker["injection_kind"] == "window"
    resumed = subprocess.run(
        [
            sys.executable, "-m", "experiments.worker", "--run-dir", str(second),
            "--mode", "resume", "--model", "replay", "--record-dir", str(cassette),
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert resumed.returncode == 0, resumed.stderr[-400:]
    assert '"status": "completed"' in resumed.stdout


def test_worker_requires_record_dir_for_record_mode(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable, "-m", "experiments.worker", "--run-dir", str(tmp_path / "r"),
            "--mode", "run", "--model", "record", "--transport", "scripted",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode != 0
    assert "--record-dir" in (proc.stderr + proc.stdout)


def test_live_record_requires_budget(tmp_path: Path) -> None:
    """live 录制必须显式给预算：真实调用会花钱，无预算拒绝启动。"""
    proc = subprocess.run(
        [
            sys.executable, "-m", "experiments.worker", "--run-dir", str(tmp_path / "r"),
            "--mode", "run", "--model", "record", "--transport", "live",
            "--record-dir", str(tmp_path / "cassette"), "--budget-usd", "0",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode != 0
    assert "预算" in (proc.stderr + proc.stdout)


def test_record_mode_triggers_an_actual_budget_hard_stop(tmp_path: Path) -> None:
    """record 模式必须真的会被预算硬停拦住（B-M1 验收：--budget-usd 级别的小额 run）。

    用脚本 transport 是为了在 CI 里复现：硬停发生在 ``BudgetLedger`` 上，
    与"钱花给谁"无关——真实录制时同一段代码拦住同一件事。
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.worker",
            "--run-dir",
            str(tmp_path / "run"),
            "--mode",
            "run",
            "--model",
            "record",
            "--transport",
            "scripted",
            "--record-dir",
            str(tmp_path / "cassette"),
            "--budget-usd",
            "0.0005",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["status"] == "failed", payload
    # 预算硬停记在事件日志里（fatal=True 的错误事件），不是"悄悄停下"
    from harness.store.sqlite_store import SqliteStore

    ids = json.loads((tmp_path / "run" / "ids.json").read_text(encoding="utf-8"))
    store = SqliteStore(tmp_path / "run" / "runtime.db")
    try:
        events = store.effective_events(ids["branch_id"])
    finally:
        store.close()
    budget_errors = [
        event
        for event in events
        if event.type == "error" and event.payload.get("error_class") == "budget_exceeded"
    ]
    assert budget_errors, [event.type for event in events]
    assert budget_errors[0].payload.get("fatal") is True


def test_consistency_compare_catches_model_text_changes() -> None:
    """**P2-7 回归**：等 token 的文本篡改必须被抓到。

    修复前会怎样：白名单只覆盖 token/cost/事件序列/tool_calls 结构，模型文本
    不在任何校验面内（`request_fingerprint` 只覆盖请求）——把录制里的文本换成
    等 token 数的另一句话，replay 全绿、对账 `identical=true`，而篡改后的文本
    已经落进事件日志并进入下一次模型调用的上下文。
    """
    from scripts.replay_consistency import _compare

    base = {
        "outcome": {"status": "completed"},
        "events": ["tree_node:user_message"],
        "tool_calls": [],
        "tokens": [(100, 0, 0, 0.001)],
        "texts": [("agent_message", "先取指标。")],
    }
    tampered = {**base, "texts": [("agent_message", "先删指标。")]}
    assert _compare(base, base) == []
    diffs = _compare(base, tampered)
    assert [diff["field"] for diff in diffs] == ["events.texts"]
