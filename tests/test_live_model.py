"""live 路径的**反例测试**（B-M1 / B §6.5）。

这个文件里的用例都带 ``-m live`` 标记，因为它们的主题是"真实模型接入这条路径"。
CI 会在**没有 key** 的环境下跑 ``pytest -m live``，此时它们断言的是：

* 没有 key 时，live 模式给出**可读失败**并以非零码退出（不是裸 ``KeyError``，也不是
  静默退回脚本模型）；
* 预算约束在 live 录制上生效。

真正的在线调用需要一个 key，本仓库的 CI **永远不提供**（设计文档 B §8 的边界纪律）。
因此这里没有"成功调用"的用例——那需要真实凭据，属于人工验证，结论必须注明数据生成于 live。

为什么不注册 marker 就会出事（对抗审查第 15 条）：``pytest -m live`` 在空选集下以
**exit 5（no tests ran）** 退出，CI 会把"没有测试可跑"误读成"反例测试通过"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.live


def _run_worker(run_dir: Path, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.worker",
            "--run-dir",
            str(run_dir),
            "--mode",
            "run",
            *extra,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )


def test_missing_key_is_a_readable_failure(tmp_path: Path) -> None:
    """无 key ⇒ 可读失败：说清设哪个环境变量，而不是抛裸异常。"""
    env = {**os.environ}
    env.pop("OPENAI_API_KEY", None)  # 无论运行环境有没有 key，这里都必须"没有"
    proc = _run_worker(
        tmp_path / "run",
        "--model",
        "record",
        "--transport",
        "live",
        "--record-dir",
        str(tmp_path / "cassette"),
        "--budget-usd",
        "0.05",
        "--model-key-env",
        "OPENAI_API_KEY",
        env=env,
    )
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "OPENAI_API_KEY" in output
    assert "live 模式需要真实 API key" in output
    # 绝不能"悄悄退回脚本模型"：那就等于没测过 live 路径
    assert "scripted" not in output.split("\n")[0]


def test_api_key_helper_rejects_blank_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.live_transport import MissingAPIKeyError, api_key_from_env

    monkeypatch.setenv("PROBE_KEY", "   ")
    with pytest.raises(MissingAPIKeyError) as excinfo:
        api_key_from_env("PROBE_KEY")
    assert "PROBE_KEY" in str(excinfo.value)


def test_transport_rejects_unknown_usage_shape_before_calling() -> None:
    """usage 归一失败必须是可读错误——供应商换了字段口径时不该静默记 0。"""
    from harness.cassette import CassetteError, normalize_usage

    with pytest.raises(CassetteError):
        normalize_usage({"whatever": 1})
