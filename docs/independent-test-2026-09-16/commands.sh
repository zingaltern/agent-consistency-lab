#!/usr/bin/env bash
# 复现 agent-consistency-lab 外部测试的全部结论（干净环境）。
# 用法：  REPO=/path/to/agent-consistency-lab bash commands.sh
# 约定：仓库只读，所有产物写 /tmp/acl-audit/；PYTHONDONTWRITEBYTECODE=1 保持仓库无字节码写入。
set -uo pipefail
REPO="${REPO:-$PWD}"   # 默认取当前目录；也可显式 REPO=/path/to/agent-consistency-lab
PY="$REPO/.venv/bin/python"
AUD=/tmp/acl-audit
mkdir -p "$AUD"/{logs,raw,scripts,manual,run}
export PYTHONDONTWRITEBYTECODE=1
rlog() { python3 "$AUD/scripts/runlog.py" "$@"; }   # 记录退出码/耗时/输出到 logs/

# ------------------------------------------------------------------ 0 只读证明
git -C "$REPO" status --porcelain > "$AUD/logs/git_status_before.txt"
git -C "$REPO" rev-parse HEAD          > "$AUD/logs/git_head.txt"

# ------------------------------------------------------------------ A 可复现性
cd "$REPO"
rlog "$AUD/logs/A1_pytest.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/pytest -q -o addopts= -p no:cacheprovider
rlog "$AUD/logs/A2_ruff.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/ruff check . --no-cache
rlog "$AUD/logs/A3_suite.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m opsenv.suite \
    --per-fault 8 --repeats 3 --gate \
    --md-out "$AUD/raw/A3_eval.md" --json-out "$AUD/raw/A3_eval.json" --runs-out "$AUD/raw/A3_runs.json"
# 确定性：换 hash seed 重跑一次，比较 statistics/cells 应逐字相同
rlog "$AUD/logs/A3b_suite_hashseed0.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 -- \
    .venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate --json-out "$AUD/raw/A3b_eval_hashseed0.json"
rlog "$AUD/logs/A4_crash_matrix.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.crash_matrix \
    --repeats 5 --workroot "$AUD/run/crash_matrix" \
    --json-out "$AUD/raw/A4_crash_matrix.json" --md-out "$AUD/raw/A4_crash_matrix.md" \
    --runs-out "$AUD/raw/A4_crash_matrix_runs.json"
rlog "$AUD/logs/A5_context_cost.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.context_cost \
    --workroot "$AUD/run/context_cost" --md-out "$AUD/raw/A5_context_cost.md" --json-out "$AUD/raw/A5_context_cost.json"
rlog "$AUD/logs/A6_context_sweep.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.context_sweep \
    --repeats 2 --workroot "$AUD/run/context_sweep" \
    --md-out "$AUD/raw/A6_sweep.md" --json-out "$AUD/raw/A6_sweep.json" --svg-out "$AUD/raw/A6_sweep.svg"
# README 的手工单次崩溃（4 步；第 3 步退出码应为 -9/137）
D="$AUD/manual/demo"; rm -rf "$D"
rlog "$AUD/logs/A7_1_run.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.worker --run-dir "$D" --mode run
rlog "$AUD/logs/A7_2_approve.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.worker --run-dir "$D" --mode approve
rlog "$AUD/logs/A7_3_resume_crash.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 CHAOS_WINDOWS=post_tool_effect_pre_record:1 -- \
    .venv/bin/python -m experiments.worker --run-dir "$D" --mode resume --tool-idem off
rlog "$AUD/logs/A7_4_resume_after.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.worker --run-dir "$D" --mode resume --tool-idem off
rlog "$AUD/logs/A9_ops_demo.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m examples.ops_demo
rlog "$AUD/logs/A10_trace.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m harness.trace --run-dir "$D"

# ------------------------------------------------------------------ B 承诺验证
rlog "$AUD/logs/B1_append_only.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/b1_append_only.py" "$D/runtime.db"
rlog "$AUD/logs/B5_invariants.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/b5_invariants.py"
# B2 需要一个"崩在 post_record_pre_commit"的 run 目录
B2="$AUD/manual/b2"; rm -rf "$B2"
rlog "$AUD/logs/B2_0_run.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.worker --run-dir "$B2" --mode run
rlog "$AUD/logs/B2_0b_approve.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.worker --run-dir "$B2" --mode approve
rlog "$AUD/logs/B2_0c_crash.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 CHAOS_WINDOWS=post_record_pre_commit:1 -- .venv/bin/python -m experiments.worker --run-dir "$B2" --mode resume
rlog "$AUD/logs/B2_checkpoint.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/b2_checkpoint_not_authoritative.py" "$REPO"
rlog "$AUD/logs/B4_B7_purity_compaction.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/b4_purity_b7_compaction.py" \
    "$D" "$AUD/run/context_sweep/variant-1-0.30" "$AUD/run/crash_matrix/(long-baseline)-outbox1-idem0-probe1-tamper0-xd59ed6bc-0"
rlog "$AUD/logs/B3_log_audit.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/b3_log_audit.py" \
    "$AUD/run/crash_matrix" "$AUD/run/context_sweep" "$AUD/manual"
B6="$AUD/manual/b6_budget"; rm -rf "$B6"
rlog "$AUD/logs/B6_budget_tiny.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m experiments.worker --run-dir "$B6" --mode run --budget-usd 0.0005

# ------------------------------------------------------------------ C 崩溃语义（扩展矩阵，外部账本裁决）
rlog "$AUD/logs/C1_extended_matrix.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/c1_extended_matrix.py" "$REPO"

# ------------------------------------------------------------------ D 审批语义
rlog "$AUD/logs/D_approval.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/d1_d6_approval.py" "$REPO"
rlog "$AUD/logs/D6b_scope_session.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/d6_scope_session_fixed.py" "$REPO"

# ------------------------------------------------------------------ E 评测口径
rlog "$AUD/logs/E1a_bypass_perfault1.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m opsenv.suite --per-fault 1 --repeats 1 --gate
rlog "$AUD/logs/E1b_bypass_onesystem.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m opsenv.suite --systems harness --gate
rlog "$AUD/logs/E1d_empty_data.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m opsenv.suite --per-fault 0 --gate   # 期望 exit=2（防空真通过）
rlog "$AUD/logs/E4_coverage.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/e4_interval_coverage.py" "$REPO"
# 退化注入（只在 /tmp 副本里改代码；两处补丁都断言恰好命中 1 处）
for d in copy-base copy-degraded-silentdrop copy-degraded-nogate; do
  rm -rf "$AUD/$d"; rsync -a --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.zcode' --exclude '.DS_Store' "$REPO/" "$AUD/$d/"
done
python3 - "$AUD/copy-degraded-silentdrop/opsenv/systems.py" <<'EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); src = p.read_text()
anchor = '        if result.action == "none":\n            return self._turn(f"结论：{result.root_cause}；证据不足，转人工。")\n'
insert = ('        _forbidden = set(self._env.scenario.forbidden_actions)\n'
          '        if self._env.scenario.novel_forbidden:\n'
          '            _forbidden.add(self._env.scenario.novel_forbidden)\n'
          '        if result.action in _forbidden:\n'
          '            # DEGRADATION(by external tester): silently drop the destructive action\n'
          '            return self._turn(f"结论：{result.root_cause}；处置动作不可用，未采取行动。")\n')
assert src.count(anchor) == 1
p.write_text(src.replace(anchor, anchor + insert)); assert "DEGRADATION" in p.read_text()
print("silent-drop patch applied")
EOF
python3 - "$AUD/copy-degraded-nogate/opsenv/systems.py" <<'EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); src = p.read_text()
assert src.count("requires_approval=True,") == 1
p.write_text(src.replace("requires_approval=True,", "requires_approval=False,  # DEGRADATION"))
print("no-gate patch applied")
EOF
rlog "$AUD/logs/E2a_control_copy.log" "$AUD/copy-base" PYTHONDONTWRITEBYTECODE=1 -- "$PY" -m opsenv.suite --per-fault 8 --repeats 3 --gate            # 期望 exit=0
rlog "$AUD/logs/E2b_silentdrop.log"   "$AUD/copy-degraded-silentdrop" PYTHONDONTWRITEBYTECODE=1 -- "$PY" -m opsenv.suite --per-fault 8 --repeats 3 --gate  # 期望 exit=1，gated/blocked 变红
rlog "$AUD/logs/E3_nogate.log"        "$AUD/copy-degraded-nogate" PYTHONDONTWRITEBYTECODE=1 -- "$PY" -m opsenv.suite --per-fault 8 --repeats 3 --gate       # 期望 exit=1，红线/拦下/CI 变红

# ------------------------------------------------------------------ F 边界与诚实性
grep -rn "ArtifactEventType.LEASE\|ArtifactEventType.STATE_UPDATE" "$REPO/harness" "$REPO/opsenv" "$REPO/fakeworld" "$REPO/experiments" "$REPO/examples"   # 应无输出（无生产者）
grep -rn "exactly.once\|恰好一次" "$REPO/harness" "$REPO/fakeworld" "$REPO/opsenv" "$REPO/experiments" "$REPO/examples"
find "$REPO/harness" "$REPO/fakeworld" "$REPO/opsenv" "$REPO/experiments" "$REPO/tests" "$REPO/examples" -name '*.py' -exec cat {} + | wc -l   # 12351
"$PY" -c "import sys;sys.path.insert(0,'$REPO');from harness.chaos import WINDOWS;print(len(WINDOWS), sorted(WINDOWS))"   # 6 个窗口
# W5 §八 的 rule_full 反事实独立复算（期望：100% / 0% / $0.00394）
rlog "$AUD/logs/G_rule_full.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python "$AUD/scripts/w5_rule_full_check.py" "$REPO"
# 橡皮图章消融（期望 harness/langgraph red_line 4.17%/20.83%）——必须传全部四个系统，子集会在 findings() 崩溃（缺陷 D-1）
rlog "$AUD/logs/G_lazy_operator.log" "$REPO" PYTHONDONTWRITEBYTECODE=1 -- .venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --operator lazy --json-out "$AUD/raw/G_lazy.json"
# 崩溃计数（D-7：README 宣称 80 次 SIGKILL，实测 crash_marker=70）
find "$AUD/run/crash_matrix" -name crash_marker.json | wc -l
# 已入库报告 vs 重跑（期望：w7/w4_context_cost 字节一致、w6 statistics 一致、w4 矩阵 md 一致）
python3 - <<'EOF'
import json
print(json.load(open("reports/w6_eval.json"))["statistics"]["paired_harness_vs_single_shot"])
print(json.load(open("/tmp/acl-audit/raw/A3_eval.json"))["statistics"]["paired_harness_vs_single_shot"])
EOF

# ------------------------------------------------------------------ 收尾：只读证明（应与 before 相同）
git -C "$REPO" status --porcelain > "$AUD/logs/git_status_after.txt"
diff "$AUD/logs/git_status_before.txt" "$AUD/logs/git_status_after.txt" && echo "REPO UNCHANGED"
