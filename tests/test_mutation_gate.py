"""变异门禁的"门禁自检"：基线与判定逻辑本身也要被钉住。

变异测试跑一次要几分钟（2000+ 个变异体 × 一遍测试集），不可能进默认测试集。所以这里
只测**判定逻辑与入库基线**这两件便宜但关键的事：

1. `mutation_summary` 的**全状态记账**（三类划分、幸存率的分母只含"被判定的变异体"、
   不可见空间的规模）；
2. `gate_verdict` 的六条红灯条件——每条都要有一个"改坏必须变红"的用例
   （AGENTS.md 红线 5；独立验证报告 P0-1 / P0-2 就是这条没做到）；
3. 入库基线 `reports/mutation_baseline.json` 的形状与自洽性。

真实的变异运行由 nightly job `mutation` 执行。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "reports" / "mutation_baseline.json"
SCRIPT = PROJECT_ROOT / "scripts" / "mutation_check.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("mutation_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline_v2(status_by_mutant: dict[str, str]) -> dict[str, Any]:
    """按"每个变异体的状态"造一份 v2 基线（gate 只依赖这一个字段）。"""
    survivors = [name for name, status in status_by_mutant.items() if status == "survived"]
    no_tests = [name for name, status in status_by_mutant.items() if status == "no tests"]
    return {
        "schema": "mutation-baseline/v2",
        "status_by_mutant": status_by_mutant,
        "survivors": survivors,
        "no_tests": no_tests,
        "counts": {"survived": len(survivors), "no_tests": len(no_tests)},
        "survivor_rate": 0.0,
    }


def _buckets(status_to_names: dict[str, list[str]]) -> dict[str, list[str]]:
    return {status: list(names) for status, names in status_to_names.items()}


def _run_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    current: dict[str, list[str]],
    baseline: dict[str, str] | None,
    extra_args: tuple[str, ...] = (),
    baseline_payload: dict[str, Any] | None = None,
    fingerprints: dict[str, str | None] | None = None,
) -> int:
    """跑一次门禁判定（不真的跑 mutmut）：返回退出码。

    ``current`` 是 `{状态: [变异体名]}`（= `collect_status()` 的形状），
    ``baseline`` 是 `{变异体名: 状态}`，``fingerprints`` 是 `{变异体名: 指纹}`。
    """
    module = _load_script()
    calls: dict[str, Any] = {}
    monkeypatch.setattr(module, "run_mutmut", lambda **kwargs: calls.update(kwargs))
    monkeypatch.setattr(module, "collect_status", lambda **_kwargs: _buckets(current))
    monkeypatch.setattr(module, "show_mutant", lambda name: f"# {name}")

    def _fake_fingerprints(names: Any, **_kwargs: Any) -> dict[str, str | None]:
        if fingerprints is None:
            # 默认：每条名字给一个稳定的假指纹（等价于「内容没变」）。不给默认值会让
            # 「一条指纹都取不到」的 fail-closed 路径把每个用例都挡在写基线之前。
            return {str(name): f"sha256:{name}" for name in names}
        return {str(name): fingerprints.get(str(name)) for name in names}

    monkeypatch.setattr(module, "compute_mutant_fingerprints", _fake_fingerprints)
    # **MUTANTS_DIR 一律指到 tmp**：`--update-baseline` 会真的把缓存整体移开
    # （`baseline_refresh_plan` 的 move-aside），不隔离就会动仓库里那份。
    # 本用例集在同一轮里真的踩过一次：三个守卫用例没隔离，把正在跑的**全量刷新**
    # 的 `mutants/` 搬走了，mutmut 父进程写 `mutants/harness/loop.py.meta` 时
    # FileNotFoundError 退出 1（好在那条路径是"跑不起来 ⇒ 判失败"，不是静默绿）。
    monkeypatch.setattr(module, "MUTANTS_DIR", tmp_path / "mutants")
    baseline_path = tmp_path / "baseline.json"
    payload = baseline_payload if baseline_payload is not None else _baseline_v2(baseline or {})
    baseline_path.write_text(json.dumps(payload), encoding="utf-8")
    code = module.main(["--baseline", str(baseline_path), "--timeout", "1", *extra_args])
    _run_gate.calls = calls  # type: ignore[attr-defined]
    return code


# ------------------------------------------------------------------ 全状态记账


def test_mutation_summary_counts_and_rate() -> None:
    module = _load_script()
    summary = module.mutation_summary(
        {
            "killed": ["a", "b", "c"],
            "survived": ["d"],
            "no tests": ["e", "f"],
        }
    )
    assert summary["killed"] == 3
    assert summary["survived"] == ["d"]
    assert summary["no_tests"] == ["e", "f"]
    assert summary["total"] == 6
    # 分母只含 killed + survived（no tests 不混进去）
    assert summary["survivor_rate"] == pytest.approx(1 / 4)
    # 未覆盖类（no tests）算进不可见空间：它既不进 survivor_rate，也不等于"测试没问题"。
    assert summary["invisible_count"] == 2
    assert summary["invisible_share"] == pytest.approx(2 / 6)


def test_every_mutmut_status_lands_in_exactly_one_class() -> None:
    """全状态记账：**每一个** mutmut 状态都必须被归类（不得静默丢弃）。

    修复前会怎样：`segfault` / `timeout` / `not checked` / `skipped` / `suspicious` /
    `caught by type check` / `check was interrupted by user` 只出现在 `total` 的算式之外，
    完全不可见——独立验证报告 P0-1/P1-3 就是这个洞。
    """
    module = _load_script()
    statuses = [
        "killed",
        "survived",
        "no tests",
        "segfault",
        "timeout",
        "suspicious",
        "skipped",
        "not checked",
        "caught by type check",
        "check was interrupted by user",
    ]
    buckets = {status: [f"m__{status.replace(' ', '_')}"] for status in statuses}
    summary = module.mutation_summary(buckets)
    assert set(summary["status_counts"]) == set(statuses)
    assert all(count == 1 for count in summary["status_counts"].values())
    assert summary["total"] == len(statuses)
    assert summary["decided"] == 2
    assert summary["invisible_count"] == len(statuses) - 2
    # 无结论类与未覆盖类都必须出现在"不可见空间"的名单里（有名字，不只是一个数）
    invisible = set(summary["inconclusive"]) | set(summary["no_tests"])
    assert len(invisible) == len(statuses) - 2


def test_unknown_status_is_not_silently_accepted() -> None:
    """mutmut 升版带来新状态名时**必须先归类**：不认识的状态算进不可见空间并判失败。"""
    module = _load_script()
    summary = module.mutation_summary({"killed": ["a"], "brand new status": ["b"]})
    assert summary["unknown_statuses"] == ["brand new status"]
    assert "b" in summary["inconclusive"]


def test_mutation_summary_handles_empty_buckets() -> None:
    module = _load_script()
    summary = module.mutation_summary({})
    assert summary["survivor_rate"] == 0.0
    assert summary["total"] == 0
    assert summary["invisible_share"] == 0.0


# ------------------------------------------------------------------ 六条红灯条件


def test_new_survivor_turns_the_gate_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """条件 1：新增幸存变异 ⇒ 退出 1（保留首版行为）。"""
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b", "c"]},
        baseline={"a": "killed", "b": "survived"},
    )
    assert code == 1


def test_baseline_killed_becoming_inconclusive_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条件 2：基线里**有判定**的变异体本轮变成无结论 ⇒ 退出 1（P0-1 的活体场景）。

    修复前会怎样：`survived` 里没有它 ⇒ 判据看不见 ⇒ 打印"已被杀死（好事）"⇒ **退出 0**。
    活体实例：`harness/store/checkpoints.py` 逐字未改，76 条基线幸存变异被判成 `segfault`。
    """
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "segfault": ["b"]},
        baseline={"a": "killed", "b": "killed"},
    )
    assert code == 1


def test_baseline_survivor_becoming_segfault_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条件 2（幸存变异那一侧）：基线幸存者本轮变成 `segfault` ⇒ 退出 1。

    `is_expired__mutmut_9` 那条真幸存变异被 mutmut 误判成 `segfault` 时，就是这一格在报警。
    """
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "segfault": ["s1"]},
        baseline={"a": "killed", "s1": "survived"},
    )
    assert code == 1


def test_vanished_baseline_mutant_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条件 3：基线里的变异体本轮完全缺失 ⇒ 退出 1（独立验证报告 F24 的原始场景）。

    修复前会怎样：180 条基线幸存变异全部缺失 → 输出"有 180 条…已被杀死（好事，不判失败）"
    → **退出 0**。
    """
    baseline = {f"harness.approval.x__mutmut_{i}": "survived" for i in range(180)}
    baseline["killed"] = "killed"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["harness.approval.x__mutmut_1"]},
        baseline=baseline,
    )
    assert code == 1


def test_new_no_tests_turns_the_gate_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """条件 4：新增 `no tests` ⇒ 退出 1（P0-2：新增的、完全没被测的代码）。

    修复前会怎样：新增 12 条 `no tests`（killed 26→27）→ **退出 0**。
    """
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "no tests": ["fresh1", "fresh2"]},
        baseline={"a": "killed"},
    )
    assert code == 1


def test_empty_result_set_is_a_failure_not_a_pass(tmp_path: Path, monkeypatch) -> None:
    """条件 5：**P0-1 回归**：`mutmut results` 在没有结果时退出 0 且无输出。

    修复前会怎样：`survived=[]` ⇒ `new_survivors=[]` ⇒ 打印"没有新增幸存变异" ⇒ **退出 0**。
    于是"配置没生效 / 缓存被清 / collect error"都会让 nightly 静静地变绿，
    而基线里的幸存变异还会被报告成"已被杀死"。
    """
    module = _load_script()
    monkeypatch.setattr(module, "run_mutmut", lambda **_: None)
    monkeypatch.setattr(module, "collect_status", lambda **_kwargs: {})
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"survivors": ["harness.loop.x__mutmut_1"], "counts": {"survived": 1}}),
        encoding="utf-8",
    )
    assert module.main(["--baseline", str(baseline), "--timeout", "1"]) == 1


def test_nothing_decided_is_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """条件 5 的另一半：跑出变异体但**一条都没判定** ⇒ 退出 1（survivor_rate=0 毫无意义）。"""
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"no tests": ["a"], "segfault": ["b"]},
        baseline={"a": "no tests", "b": "segfault"},
    )
    assert code == 1


def test_inconclusive_set_growth_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条件 6：无结论集合较基线**增长** ⇒ 退出 1（有新名字进入不可见空间）。"""
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "timeout": ["t1", "t2"]},
        baseline={"a": "killed", "t1": "timeout"},
    )
    assert code == 1
    # 反向对照：无结论集合**缩小**（有名字离开不可见空间）不算红灯——那是好转。
    ok = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a", "t2"], "timeout": ["t1"]},
        baseline={"a": "killed", "t1": "timeout", "t2": "timeout"},
    )
    assert ok == 0


def test_unknown_status_turns_the_gate_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """兜底条件：出现不认识的状态名 ⇒ 退出 1（先归类，再判定）。"""
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "new thing": ["b"]},
        baseline={"a": "killed"},
    )
    assert code == 1


def test_gate_is_green_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正对照：状态与基线逐条一致 ⇒ 退出 0。没有这一格，上面七个"变红"证明不了什么。"""
    same = {"killed": ["a", "b"], "survived": ["c"], "no tests": ["d"], "segfault": ["e"]}
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current=same,
        baseline={
            "a": "killed", "b": "killed", "c": "survived", "d": "no tests", "e": "segfault",
        },
    )
    assert code == 0


def test_mutant_content_change_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条件 7：**同名但内容指纹变了** ⇒ 退出 1（独立验证 2026-09-18 · 报告 §5-3）。

    修复前会怎样：判据只按"名字 + 状态"对账 ⇒ 在同一函数体内做**等量改写**
    （常数改值、语句换序——既不增删变异体条数、也不移位编号）时，同一批名字指向
    完全不同的变异，而门禁**静默绿**。指纹把"这条还是不是原来那条"变成可判定的。
    """
    base_fp = {"m1": "sha256:aaaa", "m2": "sha256:bbbb"}
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["m1", "m2"]},
        baseline={"m1": "killed", "m2": "killed"},
        fingerprints={"m1": "sha256:aaaa", "m2": "sha256:CHANGED"},
        baseline_payload={
            **_baseline_v2({"m1": "killed", "m2": "killed"}),
            "schema": "mutation-baseline/v3",
            "fingerprint_algorithm": "sha256(normalized-mutmut-diff)/v1",
            "fingerprints": base_fp,
        },
    )
    assert code == 1
    # 反向对照：指纹逐条相同 ⇒ 不退化为"有指纹就红"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["m1", "m2"]},
        baseline={"m1": "killed", "m2": "killed"},
        fingerprints=dict(base_fp),
        baseline_payload={
            **_baseline_v2({"m1": "killed", "m2": "killed"}),
            "fingerprints": base_fp,
        },
    )
    assert code == 0


def test_missing_fingerprints_do_not_fake_a_comparison(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """取不到指纹的条目**不参与**比对，而且要把两种"没法比"说清楚（不假装比对过）。

    这是条件 7 的边界用例：门禁不能因为"没得比"而变红，也不能因为它而变绿——
    只能如实报告这一格没在看。两种形态的文案不同，因为处置不同：
    ① 当前侧取不到内容（本轮先把 mutants/ 修好）；② 基线还是旧版（先跑 --update-baseline）。
    """
    # ① 当前侧有名字取不到内容（基线有指纹）
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["m1"]},
        baseline={"m1": "killed"},
        fingerprints={"m1": None},
        baseline_payload={
            **_baseline_v2({"m1": "killed"}),
            "fingerprints": {"m1": "sha256:aaaa"},
        },
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "内容指纹" in out
    assert "这些条目**不参与**指纹比对" in out
    assert "这一格没在看" in out

    # ② 基线是旧版（没有 fingerprints 字段）
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["m1"]},
        baseline={"m1": "killed"},
        fingerprints={"m1": "sha256:aaaa"},
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "基线里没有可比对的指纹" in out
    assert "只记账、不判内容" in out


def test_all_fingerprints_unreadable_refuses_to_write_a_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fail-closed：一条指纹都取不到时**拒绝写基线**（否则等于静默关掉条件 7）。

    修复前会怎样：写出一份没有 `fingerprints` 的基线，之后每轮都"没有可比对的指纹"，
    条件 7 再不生效，而所有输出都是绿的。
    """
    module = _load_script()
    monkeypatch.setattr(module, "run_mutmut", lambda **_kwargs: None)
    monkeypatch.setattr(module, "collect_status", lambda **_kwargs: _buckets({"killed": ["m1"]}))
    monkeypatch.setattr(
        module, "compute_mutant_fingerprints", lambda names, **_kwargs: {n: None for n in names}
    )
    # 必须把 MUTANTS_DIR 指到 tmp：刷新路径会真的把缓存**整体移开**，
    # 不设替身就会动仓库里那份（本用例第一版就是这么把 mutants/ 搬走的）。
    monkeypatch.setattr(module, "MUTANTS_DIR", tmp_path / "mutants")
    baseline_path = tmp_path / "baseline.json"
    code = module.main(
        ["--baseline", str(baseline_path), "--timeout", "1", "--update-baseline"]
    )
    assert code == 1
    assert not baseline_path.exists(), "拒绝写入就一条都不该留下"


def test_baseline_v1_is_read_with_a_visible_limitation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """兼容读取 v1：只有 `survivors`/`no_tests` 两个列表，且把局限**打印出来**。

    v1 里 `killed` 没有名字，所以条件 2/3 只能覆盖基线幸存者与 no tests——
    这一点不能被读成"全覆盖"。
    """
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["s1"]},
        baseline=None,
        baseline_payload={"schema": "mutation-baseline/v1", "survivors": ["s1"], "no_tests": []},
    )
    assert code == 0
    assert "v1" in capsys.readouterr().out


def test_baseline_v1_cannot_hide_a_vanished_survivor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v1 兼容下 P0-1 仍然会红：基线幸存者消失 ⇒ 条件 3。"""
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"]},
        baseline=None,
        baseline_payload={
            "schema": "mutation-baseline/v1",
            "survivors": ["s1", "s2"],
            "no_tests": [],
        },
    )
    assert code == 1


# ------------------------------------------------------------------ 不可见空间 / 选项语义


def test_every_run_prints_the_invisible_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """每次运行都打印"不可见空间"的规模：防 survivor_rate 被读成覆盖率。"""
    _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b"], "segfault": ["c"], "no tests": ["d"]},
        baseline={"a": "killed", "b": "survived", "c": "segfault", "d": "no tests"},
    )
    out = capsys.readouterr().out
    assert "不可见空间" in out
    assert "2/4" in out
    assert "survivor_rate 不是覆盖率" in out


def test_module_path_is_translated_to_a_mutant_name_glob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--module` 传**文件路径**时必须翻译成变异体名 glob（独立验证报告 P1-1）。

    修复前会怎样：路径直接传给 `mutmut run` 的位置参数，而它按**变异体名** fnmatch 过滤
    ⇒ `AssertionError: Filtered for specific mutants, but nothing matches` ⇒ 恒失败。
    """
    module = _load_script()
    assert module.normalize_module_filter("harness/approval.py") == "harness.approval.*"
    assert (
        module.normalize_module_filter("harness/store/checkpoints.py")
        == "harness.store.checkpoints.*"
    )
    # 已经是 glob / 完整变异体名的原样透传
    assert module.normalize_module_filter("harness.approval.*") == "harness.approval.*"
    assert (
        module.normalize_module_filter("harness.approval.xǁApprovalBindingǁis_expired__mutmut_9")
        == "harness.approval.xǁApprovalBindingǁis_expired__mutmut_9"
    )
    _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"]},
        baseline={"a": "killed"},
        extra_args=("--module", "harness/approval.py"),
    )
    assert _run_gate.calls["module"] == "harness.approval.*"  # type: ignore[attr-defined]


def test_json_out_is_written_on_the_verdict_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--json-out` 在主判定路径也要落盘（nightly 上传的就是它）。

    修复前会怎样：只有 `--summary-only` 写这个文件，夜里跑的那条路根本不生成
    `/tmp/mutation.json`，上传步骤拿到的是空产物。
    """
    out = tmp_path / "mutation.json"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b"]},
        baseline={"a": "killed", "b": "survived"},
        extra_args=("--json-out", str(out)),
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status_by_mutant"] == {"a": "killed", "b": "survived"}
    assert payload["counts"]["invisible"] == 0
    # 判失败也必须留下产物（nightly 的 upload 步骤是 if: always()）
    failing = tmp_path / "failing.json"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b", "fresh"]},
        baseline={"a": "killed", "b": "survived"},
        extra_args=("--json-out", str(failing)),
    )
    assert code == 1
    assert json.loads(failing.read_text(encoding="utf-8"))["survivors"] == ["b", "fresh"]


def test_summary_only_reports_survivors_as_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--summary-only` 的 `survivors` 统一为**列表** + `survivor_count`。

    修复前会怎样：`survivors` 在这里是**个数**，在主判定路径是**列表**——同一字段名两种类型，
    claim 的 `source_path` 对账会踩空。
    """
    module = _load_script()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            _baseline_v2({"a": "killed", "b": "survived", "c": "survived", "d": "segfault"})
        ),
        encoding="utf-8",
    )
    out = tmp_path / "summary.json"
    assert module.main(["--summary-only", "--baseline", str(baseline), "--json-out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["survivors"] == ["b", "c"]
    assert payload["survivor_count"] == 2
    assert payload["inconclusive"] == ["d"]
    # 不变量：`--summary-only` 只是**读回基线**，不得凭空造一个墙钟（基线里没有这个字段）
    assert "elapsed_s" not in payload
    assert json.loads(capsys.readouterr().out.splitlines()[0])["survivor_count"] == 2


# ------------------------------------------------- 墙钟落盘（CI 预算的唯一量化来源）


def test_run_mutmut_returns_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_mutmut` 返回这一轮的墙钟秒数。

    修复前会怎样：用时只在 print 里出现、不落盘 ⇒ 夜里上传的 artifact 回答不了
    "CI 上 mutmut 本身跑了多久、离预算还有多少"——独立验证 2026-09-18 的报告 §5-1
    把这条列为"预算够不够"的唯一定量缺口。
    """
    import types

    module = _load_script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ticks = iter([100.0, 112.5])
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(ticks))
    assert module.run_mutmut(module=None, timeout=60, max_children=1) == pytest.approx(12.5)


def test_json_out_records_elapsed_s(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判定路径的产物里必须有 `elapsed_s`，且**既有键一个不动**（逐键断言）。

    修复前会怎样：`/tmp/mutation.json` 里全是计数与名单，没有任何时间字段——
    "CI 比本机慢多少倍"只能从 job 级别反推（含 checkout/install），答不出变异那一步的耗时。
    """
    out = tmp_path / "run.json"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b"]},
        baseline={"a": "killed", "b": "survived"},
        extra_args=("--json-out", str(out)),
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(payload["elapsed_s"], float)
    assert payload["elapsed_s"] >= 0.0
    # 既有键逐键仍在（新增字段不许顶掉任何一个）
    for key in (
        "schema",
        "modules",
        "command",
        "counts",
        "status_counts",
        "status_by_mutant",
        "survivor_rate",
        "survivors",
        "no_tests",
        "inconclusive",
        "unknown_statuses",
        "refresh",
        "invisible_share",
    ):
        assert key in payload, f"既有键 {key} 不见了"


def test_timeout_fails_and_still_writes_the_artifact(monkeypatch, tmp_path: Path) -> None:
    """超时必须判失败，**而且**留下产物：跑不完 ≠ 没有回归。

    为什么"产物"这一半同等重要：nightly 上传的就是 `--json-out` 指向的文件，
    而"跑不起来"恰恰是最需要产物的那条路径——修复前它走裸 `SystemExit`，
    退出码虽然是对的，但文件根本不存在，夜里出问题只留一行 stderr、artifact 是空的
    （独立验证报告 P1-1）。

    本用例是**行为断言**，替代先前那条只读源码字符串的版本：那种断言在重构后会假绿
    （把实现搬走、字符串还在，它就继续通过）。
    """
    module = _load_script()
    json_out = tmp_path / "mutation.json"

    def _timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise module.subprocess.TimeoutExpired(cmd="mutmut run", timeout=1.0, output="partial")

    monkeypatch.setattr(module.subprocess, "run", _timeout)
    with pytest.raises(SystemExit) as excinfo:
        module.run_mutmut(module=None, timeout=1.0, max_children=1, json_out=str(json_out))

    assert "超时失败" in str(excinfo.value), "超时必须判失败，不能静默跳过"
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["verdict"] == "aborted"
    assert payload["reason"] == "timeout"
    assert "超时失败" in payload["message"]
    # 超时那一刻已经跑了多久是第一手处置证据（"差一点跑完" vs "配置没生效"）
    assert isinstance(payload["elapsed_s"], float)


def test_unreadable_results_fail_and_still_write_the_artifact(
    monkeypatch, tmp_path: Path
) -> None:
    """读结果失败同样：判失败 + 留产物（`reason` 给机器读，`message` 给人读）。"""
    import types

    module = _load_script()
    json_out = tmp_path / "mutation.json"
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout="", stderr="cannot read"),
    )
    with pytest.raises(SystemExit):
        module.collect_status(json_out=str(json_out))

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["verdict"] == "aborted"
    assert payload["reason"] == "results_unreadable"


def test_mutmut_non_zero_exit_voids_the_verdict(monkeypatch) -> None:
    """`mutmut run` 非 0（配置错 / collect error / OOM）⇒ 判定作废，不许读缓存继续比。"""
    import subprocess as real_subprocess
    import types

    module = _load_script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=3, stdout="boom", stderr="boom"),
    )
    try:
        module.run_mutmut(module=None, timeout=5, max_children=1)
    except SystemExit as exc:
        assert "没有跑成功" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非 0 退出码必须终止判定")
    assert real_subprocess is not None


# ------------------------------------------- 刷新基线不许顺手把盲区冻进去（更宽即拒绝）


def test_baseline_widening_names_the_four_categories() -> None:
    """「更宽」按今天已有的红灯条件定义，四条：

    新增幸存 / 新增 no tests / 无结论增长 / 旧条目整条不见。
    纯函数用例：判据本身可被单元测试钉住，不依赖 mutmut。
    """
    module = _load_script()
    old = {
        "status_by_mutant": {
            "a": "killed",
            "b": "survived",
            "c": "no tests",
            "d": "timeout",
        },
        "inconclusive": ["d"],
    }
    candidate = {
        "status_by_mutant": {
            "a": "survived",  # 新增幸存
            "b": "survived",
            "c2": "no tests",  # 新增 no tests
            "e": "timeout",  # 无结论集合增长
            "f": "killed",
        },
        "inconclusive": ["e"],
        "survivors": ["a", "b"],
        "no_tests": ["c2"],
    }
    keys = {item["key"] for item in module.baseline_widening(old, candidate)}
    assert keys == {
        "new-survivors",
        "new-no-tests",
        "inconclusive-grew",
        "baseline-entries-missing",
    }
    # 正对照 1：与旧基线**逐条相同** ⇒ 不比旧的宽
    assert module.baseline_widening(old, old) == []
    # 正对照 2：**缩窄**（幸存变 killed、无结论变判定）⇒ 也不是"更宽"
    narrower = {
        "status_by_mutant": {"a": "killed", "b": "killed", "c": "killed", "d": "killed"},
        "inconclusive": [],
    }
    assert module.baseline_widening(old, narrower) == []


def test_wider_baseline_is_refused_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """刷新基线时**新增幸存变异 ⇒ 拒绝写入并退出 1**（独立验证 2026-09-18 · 报告 §5-2）。

    修复前会怎样：`--update-baseline` 是文档化的逃生门，但没有守卫——
    "顺手把这一轮的盲区冻进基线"在机制上没有任何阻力（第一版就出过一份混合基线：
    14.7 秒"跑完"、`no tests` 从 18 虚增到 241）。现在默认拒绝，且旧基线**原样不动**。
    """
    baseline_path = tmp_path / "baseline.json"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b", "fresh"]},
        baseline={"a": "killed", "b": "survived"},
        extra_args=("--update-baseline",),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "拒绝写基线" in out
    assert "new-survivors" in out
    assert "fresh" in out
    written = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert written["schema"] == "mutation-baseline/v2", "拒绝写入时旧基线必须原样不动"


def test_wider_baseline_can_be_written_with_the_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """显式 `--allow-wider-baseline` 才放行：大声警告 + 把放行条件写进基线。

    为什么"写进基线"这一半同等重要：下一轮评审看基线文件就能看出"这份基线是放宽后冻的"，
    不用去翻 PR 描述——条件不能只靠人记。
    """
    baseline_path = tmp_path / "baseline.json"
    out_path = tmp_path / "run.json"
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b", "fresh"]},
        baseline={"a": "killed", "b": "survived"},
        extra_args=(
            "--update-baseline",
            "--allow-wider-baseline",
            "--json-out",
            str(out_path),
        ),
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "警告" in printed and "更宽" in printed
    written = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert written["schema"] == "mutation-baseline/v3"
    override = written["refresh"]["widening_override"]
    assert override["allowed"] is True
    assert {"key": "new-survivors", "count": 1} in override["categories"]
    # `--json-out` 里的 refresh 也要带上它（夜里上传的就是那个文件）
    assert (
        json.loads(out_path.read_text(encoding="utf-8"))["refresh"]["widening_override"]["allowed"]
        is True
    )


def test_equal_baseline_is_written_without_an_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正对照：候选与旧基线逐条相同 ⇒ 正常写入，且**不该**留下放行标记。

    没有这一格，"更宽必被拒"可能只是守卫恒真（把正常刷新也一起挡住）。
    """
    baseline_path = tmp_path / "baseline.json"
    same = {"a": "killed", "b": "survived", "c": "no tests"}
    code = _run_gate(
        monkeypatch,
        tmp_path,
        current={"killed": ["a"], "survived": ["b"], "no tests": ["c"]},
        baseline=same,
        extra_args=("--update-baseline",),
    )
    assert code == 0
    written = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert written["status_by_mutant"] == same
    assert "widening_override" not in written["refresh"]


# ------------------------------------------------------------------ 入库基线


@pytest.mark.skipif(not BASELINE.exists(), reason="基线尚未生成（跑 --update-baseline）")
def test_baseline_is_internally_consistent() -> None:
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    counts = payload["counts"]
    assert payload["schema"] == "mutation-baseline/v3"
    assert counts["survived"] == len(payload["survivors"])
    assert counts["no_tests"] == len(payload["no_tests"])
    assert counts["inconclusive"] == len(payload["inconclusive"])
    assert counts["total"] == counts["decided"] + counts["invisible"]
    assert counts["invisible"] == counts["no_tests"] + counts["inconclusive"]
    decided = counts["decided"]
    assert payload["survivor_rate"] == pytest.approx(
        round(counts["survived"] / decided, 4), abs=1e-4
    )
    assert payload["modules"] == [
        "harness/execution.py",
        "harness/loop.py",
        "harness/store/checkpoints.py",
        "harness/approval.py",
    ]
    assert len(set(payload["survivors"])) == len(payload["survivors"]), "幸存清单里有重复项"


@pytest.mark.skipif(not BASELINE.exists(), reason="基线尚未生成（跑 --update-baseline）")
def test_baseline_records_every_mutant_status() -> None:
    """v2 必须**逐条**记状态：条件 2/3 靠它才能看见"killed 变 segfault"与"整条改名"。"""
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    status_by_mutant = payload["status_by_mutant"]
    assert len(status_by_mutant) == payload["counts"]["total"]
    module = _load_script()
    known = set(module.KNOWN_STATUSES)
    assert set(status_by_mutant.values()) <= known, "基线里出现了未归类的状态"
    # `status_counts` 是"状态 → 条数"，必须与 `status_by_mutant` 逐条对得上
    for status, count in payload["status_counts"].items():
        assert count == sum(1 for s in status_by_mutant.values() if s == status), status


@pytest.mark.skipif(not BASELINE.exists(), reason="基线尚未生成（跑 --update-baseline）")
def test_baseline_records_the_boundary_of_what_it_can_see() -> None:
    """基线必须写明它看不见什么——否则会有人把幸存率当绝对质量分。"""
    note = json.loads(BASELINE.read_text(encoding="utf-8"))["note"]
    assert "子进程" in note
    assert "测试盲区" in note
    assert "不可见空间" in note
    assert "不是覆盖率" in note
    # 已知无结论集合里混着真盲区，这条必须写在基线里（不得读成"不是盲区"）
    assert "is_expired__mutmut_9" in note


# ------------------------------------------- 刷新基线必须先有干净缓存（增量陷阱）


def test_refresh_plan_moves_a_stale_cache_aside(tmp_path: Path) -> None:
    """**修复前会怎样**：`--update-baseline` 直接在旧缓存上跑，`mutmut run` 走增量路径，
    写出的"基线"是旧判决与新判决的混合物（2026-09-19 实测：14.7 秒"跑完"、
    `no tests` 从 18 虚增到 241）。现在默认把 `mutants/` 整体移开再跑全量。"""
    module = _load_script()
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / "state").write_text("旧判决", encoding="utf-8")

    plan = module.baseline_refresh_plan(
        update_baseline=True,
        allow_incremental=False,
        mutants_dir=mutants,
        stamp="20260919T101010Z",
    )
    assert plan["action"] == "move-aside"
    assert plan["moved_to"].endswith("mutants.stale-20260919T101010Z")

    module.apply_baseline_refresh_plan(plan)
    assert not mutants.exists(), "旧缓存必须整体让开，否则本轮仍是增量"
    moved = Path(plan["moved_to"])
    assert (moved / "state").read_text(encoding="utf-8") == "旧判决", "移开而不是删掉"


def test_refresh_plan_is_a_noop_when_not_refreshing(tmp_path: Path) -> None:
    """判定轮**不该**动缓存：增量是 mutmut 的正常工作方式（判定读的是全量结果表）。"""
    module = _load_script()
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    plan = module.baseline_refresh_plan(
        update_baseline=False,
        allow_incremental=False,
        mutants_dir=mutants,
        stamp="20260919T101010Z",
    )
    assert plan["action"] == "none"
    module.apply_baseline_refresh_plan(plan)
    assert mutants.exists()


def test_refresh_plan_runs_full_when_there_is_no_cache(tmp_path: Path) -> None:
    """没有缓存 ⇒ 本来就是全量，什么都不用搬。"""
    module = _load_script()
    plan = module.baseline_refresh_plan(
        update_baseline=True,
        allow_incremental=False,
        mutants_dir=tmp_path / "mutants",
        stamp="20260919T101010Z",
    )
    assert plan["action"] == "full-run"
    assert plan["moved_to"] is None


def test_incremental_refresh_needs_the_explicit_escape_hatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """沿用旧缓存必须**显式**要求，而且要有警告——默认行为不允许写出混合基线。"""
    module = _load_script()
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    plan = module.baseline_refresh_plan(
        update_baseline=True,
        allow_incremental=True,
        mutants_dir=mutants,
        stamp="20260919T101010Z",
    )
    assert plan["action"] == "incremental"
    module.apply_baseline_refresh_plan(plan)
    assert mutants.exists(), "显式要求沿用 ⇒ 不该动缓存"
    out = capsys.readouterr().out
    assert "警告" in out
    assert "混合" in out


def test_stale_cache_target_does_not_overwrite_an_existing_one(tmp_path: Path) -> None:
    """同一秒里连刷两次也要有去处：不许覆盖前一次移开的缓存。"""
    module = _load_script()
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    first = tmp_path / "mutants.stale-20260919T101010Z"
    first.mkdir()
    (first / "state").write_text("第一次", encoding="utf-8")

    plan = module.baseline_refresh_plan(
        update_baseline=True,
        allow_incremental=False,
        mutants_dir=mutants,
        stamp="20260919T101010Z",
    )
    module.apply_baseline_refresh_plan(plan)
    assert plan["moved_to"].endswith("-2")
    assert (first / "state").read_text(encoding="utf-8") == "第一次"


def test_update_baseline_moves_the_cache_before_running_mutmut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端：`main(--update-baseline)` 在**调用 mutmut 之前**就把缓存搬走了，
    并把这次的条件写进基线与 `--json-out`（混合与否不能靠人记）。"""
    module = _load_script()
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / "state").write_text("旧判决", encoding="utf-8")
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> None:
        seen["cache_present_at_run"] = mutants.exists()

    monkeypatch.setattr(module, "MUTANTS_DIR", mutants)
    monkeypatch.setattr(module, "run_mutmut", fake_run)
    monkeypatch.setattr(module, "collect_status", lambda **_kwargs: _buckets({"killed": ["m1"]}))
    monkeypatch.setattr(module, "show_mutant", lambda name: f"# {name}")
    # 指纹也要给替身：替身造的变异体名（m1）不在真实缓存里，真函数会全部返回 None，
    # 而"一条指纹都取不到"现在会**拒绝写基线**（fail-closed，见
    # test_all_fingerprints_unreadable_refuses_to_write_a_baseline）。
    monkeypatch.setattr(
        module,
        "compute_mutant_fingerprints",
        lambda names, **_kwargs: {n: "sha256:x" for n in names},
    )

    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(_baseline_v2({"m1": "killed"})), encoding="utf-8")
    json_out = tmp_path / "run.json"
    code = module.main(
        [
            "--baseline",
            str(baseline_path),
            "--json-out",
            str(json_out),
            "--timeout",
            "1",
            "--update-baseline",
        ]
    )

    assert code == 0
    assert seen["cache_present_at_run"] is False, "缓存必须在跑之前就让开"
    assert list(tmp_path.glob("mutants.stale-*")), "旧缓存要有个去处（不是删掉）"
    written = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert written["refresh"]["action"] == "move-aside"
    assert json.loads(json_out.read_text(encoding="utf-8"))["refresh"]["action"] == "move-aside"
