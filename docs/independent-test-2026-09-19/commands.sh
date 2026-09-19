#!/bin/sh
# 独立验证 2026-09-19 跑过的完整命令清单（按执行顺序）。
# 逐条可执行；耗时标注是本机（Python 3.14.6 / macOS 15）实测，仅作参考。
#
# 纪律：每一步都要单独看**退出码**——管道/重定向会吞掉它（历史事故）。
set -u
cd "$(dirname "$0")/../.."

echo "== 0. 基线四件套（先确认仓库是健康的，否则后面的发现会被误读） =="
.venv/bin/pytest -o addopts= -p no:cacheprovider -q            # 429 passed，约 15s
.venv/bin/ruff check .
.venv/bin/python scripts/check_facts.py --run verify            # 44/44
.venv/bin/python -m experiments.crash_matrix --repeats 5        # 16 格 as-predicted，70 次真实 SIGKILL

echo "== 1. P0-1：append-only 能不能被 INSERT OR REPLACE 绕过 =="
.venv/bin/python docs/independent-test-2026-09-19/repro/p0_1_append_only_bypass.py

echo "== 2. P0-2：落盘顺序（真 SIGKILL + 项目自己的 oracle 判） =="
.venv/bin/pytest -o addopts= -p no:cacheprovider -q \
    tests/test_audit_regressions.py -k event_lands_before_the_dedup_row_is_closed
# 退化注入：把 _resolve_pending 的 complete() 挪到 _append_tool_result() 之前 ⇒ 该用例必须变红

echo "== 3. P0-3 / P1-1：四条关系门禁的单元用例 =="
.venv/bin/pytest -o addopts= -p no:cacheprovider -q tests/test_stats_and_gates.py

echo "== 4. 门禁本身（18 条，基线全绿、退出码 0） =="
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate \
    --json-out /tmp/w11_eval.json --md-out /tmp/w11_eval.md

echo "== 5. P1-2：W7 产物 vs 当前代码 =="
sh docs/independent-test-2026-09-19/repro/p1_3_w7_artifact_drift.sh

echo "== 5b. P2-5：DDL 防线三层（防 / 写前核查 / 离线核查） =="
.venv/bin/python docs/independent-test-2026-09-19/repro/p2_5_ddl_bypass_guarded.py

echo "== 6. 文档数字对账（W7 的 9 条 claim 已升到 verify，这一步会多跑约 38s） =="
.venv/bin/python scripts/check_facts.py --run verify

echo "== 7. 变异门禁：刷新基线（脚本自己会先把陈旧的 mutants/ 移开） =="
# ⚠️ 关键：mutants/ 缓存还在时 `mutmut run` 只重跑"函数哈希变了"的变异体。
# 本轮踩过这个坑：改过 harness/execution.py 后直接 --update-baseline，
# 14.7 秒"跑完"、no_tests 从 18 虚增到 241 —— 那是混合了旧判决的增量结果。
# 现在这条纪律是**代码**：--update-baseline 会先整体移开缓存（mutants.stale-<UTC>/）
# 再跑全量；要沿用旧缓存必须显式 --allow-incremental-refresh（会大声警告）。
.venv/bin/python scripts/mutation_check.py --update-baseline --timeout 2400 \
    --json-out /tmp/mutation-w11-run1.json

echo "== 7b. 判决稳定性实测：同代码再全量跑一遍（应零翻转、退出 0） =="
mv mutants /tmp/mutants-w11-run1
.venv/bin/python scripts/mutation_check.py --timeout 2400 \
    --json-out /tmp/mutation-w11-run2.json

echo "== 8. 收尾：再跑一遍完整档，确认没有回退 =="
.venv/bin/python -m experiments.crash_matrix --repeats 5
.venv/bin/python -m experiments.context_cost
.venv/bin/python -m experiments.context_sweep --repeats 2
