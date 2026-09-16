"""claim 门禁的自测：**门禁自己也要被证明会红**。

为什么必须有这个文件：历史教训（`docs/HANDOFF.md` §六 第 7 条）是"没有坏事发生
≠ 机制在工作"——第一版评测门禁抓不到"静默丢弃破坏性动作"的退化，指标全绿而机制已死。
claim 对账脚本的价值全在"数字漂移时会红"，因此这里用三类**错误注入**把它逼红：

1. 改 `value`（把文档里的数字改错）；
2. 收窄 `tolerance` 至 0（数字看起来一致、实际漂移了）；
3. 置空 `source_path`（绑定失效，最危险的一类：空路径若被当成"无需对账"就会静默放过）。

另外校验真实 claim 文件的**覆盖面**（≥20 条、覆盖 W3–W7 与演进类），
以及"同一条命令只执行一次"的共享语义（否则 CI 时长按 claim 数线性膨胀）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER = PROJECT_ROOT / "scripts" / "check_facts.py"
FACTS_FILE = PROJECT_ROOT / "reports" / "documented-facts.json"

# 写一个小 JSON 的命令：值刻意取成"文档里会四舍五入的那个数"，这样 tolerance=0 必然漂移
_EMIT = (
    "import json,sys; "
    "open(sys.argv[1],'w',encoding='utf-8').write("
    "json.dumps({'statistics':{'paired':{'point':0.123456}},"
    "'gate_summary':{'total':14},'configs':[{'cost_per_call_usd':0.01431}]}))"
)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("check_facts", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _claim(**overrides: Any) -> dict[str, Any]:
    claim = {
        "id": "self-test",
        "value": 0.1235,
        "tolerance": 0.0005,
        "run": "verify",
        "source_cmd": ["{python}", "-c", _EMIT, "{out}"],
        "source_path": ["statistics", "paired", "point"],
        "what": "自测用：0.123456 四舍五入到 0.1235",
        "docs": ["tests/test_facts_gate.py"],
    }
    claim.update(overrides)
    return claim


def _write_facts(tmp_path: Path, claims: list[dict[str, Any]]) -> Path:
    path = tmp_path / "facts.json"
    path.write_text(json.dumps({"claims": claims}, ensure_ascii=False), encoding="utf-8")
    return path


def _run_checker(facts: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--facts",
            str(facts),
            "--outdir",
            str(tmp_path / "out"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_self_test_claim_passes(tmp_path: Path) -> None:
    """先证明"正确绑定会过"——否则下面三条红可能是脚本自己坏了（假红）。"""
    facts = _write_facts(tmp_path, [_claim()])
    proc = _run_checker(facts, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1/1 通过" in proc.stdout


def test_wrong_value_turns_red(tmp_path: Path) -> None:
    """错误注入 1：把 value 改成错的（0.9999）。修复前会怎样：文档数字漂移无人发现。"""
    facts = _write_facts(tmp_path, [_claim(value=0.9999)])
    proc = _run_checker(facts, tmp_path)
    assert proc.returncode == 1, proc.stdout
    assert "0/1 通过" in proc.stdout
    assert "[失败] self-test" in proc.stdout
    assert "偏离" in proc.stdout  # 报错必须可读：给出实测值与偏离量


def test_tolerance_zero_turns_red(tmp_path: Path) -> None:
    """错误注入 2：容差收窄到 0（值看起来一样，实际差了 4.4e-5）。"""
    facts = _write_facts(tmp_path, [_claim(tolerance=0)])
    proc = _run_checker(facts, tmp_path)
    assert proc.returncode == 1, proc.stdout
    assert "0/1 通过" in proc.stdout
    assert "实测 0.123456" in proc.stdout


def test_empty_source_path_turns_red(tmp_path: Path) -> None:
    """错误注入 3：清空 source_path——绑定失效必须判失败，而不是"没绑定所以放行"。"""
    facts = _write_facts(tmp_path, [_claim(source_path=[])])
    proc = _run_checker(facts, tmp_path)
    assert proc.returncode == 1, proc.stdout
    assert "source_path 为空" in proc.stdout


def test_missing_key_is_reported_with_available_keys(tmp_path: Path) -> None:
    """路径打错键名：报错要指出"在哪一层、可用的键有哪些"，否则无法修。"""
    facts = _write_facts(tmp_path, [_claim(source_path=["statistics", "nope", "point"])])
    proc = _run_checker(facts, tmp_path)
    assert proc.returncode == 1, proc.stdout
    assert "找不到键 'nope'" in proc.stdout
    assert "可用键" in proc.stdout


def test_shared_command_runs_once(tmp_path: Path) -> None:
    """两条 claim 引用同一条命令 ⇒ 只执行一次（CI 时长不按 claim 数膨胀）。"""
    marker = tmp_path / "runs.txt"
    emit = (
        "import json,sys,pathlib; "
        f"pathlib.Path({str(marker)!r}).write_text("
        f"pathlib.Path({str(marker)!r}).read_text() + 'x' if "
        f"pathlib.Path({str(marker)!r}).exists() else 'x'); "
        "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps({'v':1,'gate_summary':{'total':14}}))"
    )
    claims = [
        _claim(
            id="a",
            value=1,
            tolerance=0,
            source_path=["v"],
            source_cmd=["{python}", "-c", emit, "{out}"],
        ),
        _claim(
            id="b",
            value=14,
            tolerance=0,
            source_path=["gate_summary", "total"],
            source_cmd=["{python}", "-c", emit, "{out}"],
        ),
    ]
    facts = _write_facts(tmp_path, claims)
    proc = _run_checker(facts, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.read_text() == "x"
    assert "2/2 通过" in proc.stdout


# ------------------------------------------------------------------ 真实 claim 文件


def test_documented_facts_has_at_least_20_wellformed_claims() -> None:
    checker = _load_checker()
    claims = checker.load_claims(FACTS_FILE)  # 不合法会抛 ClaimError
    assert len(claims) >= 20, f"claim 只有 {len(claims)} 条，验收要求 ≥20"


@pytest.mark.parametrize(
    "anchor",
    ["W3", "W4", "W5", "W6", "W7", "演进"],
)
def test_documented_facts_covers_every_round(anchor: str) -> None:
    """覆盖口径：W3/W4/W5/W6/W7 的结论段数字各至少一条，以及演进类（测试数量）。"""
    payload = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    hits = [
        claim["id"]
        for claim in payload["claims"]
        if any(anchor in doc for doc in claim["docs"])
    ]
    assert hits, f"没有任何 claim 引用 {anchor}——该轮结论段数字仍未被门禁覆盖"


def test_documented_facts_run_labels_split_light_and_heavy() -> None:
    """verify 与 nightly 必须都非空：全放 verify 会拖慢主作业，全放 nightly 会失去保护。"""
    payload = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    runs = {claim["run"] for claim in payload["claims"]}
    assert runs == {"verify", "nightly"}, runs


def test_documented_facts_commands_never_write_into_repo() -> None:
    """claim 命令的产物只能写 {out}（由脚本决定落在 /tmp），不得直接写仓库路径。"""
    payload = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    for claim in payload["claims"]:
        for part in claim["source_cmd"]:
            assert not part.startswith("reports/"), f"{claim['id']} 直接写仓库: {part}"
            assert not part.startswith("docs/"), f"{claim['id']} 直接写仓库: {part}"
