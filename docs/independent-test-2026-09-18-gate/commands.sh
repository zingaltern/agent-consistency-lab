#!/usr/bin/env bash
# 独立验证（第二轮）的可执行复现命令 —— 被测 commit ed8b3662fd5b0d1d96257c517b3d82480c3613fc
#
# 纪律：仓库只读。下面所有命令都在 /tmp 的克隆/副本里跑，产物全部写 /tmp。
# 我自写的脚本在 /tmp/vg-repro/ 与 /tmp/vg-shim/（不在仓库内，故此处内联生成）。
#
# 用法: bash commands.sh            # 全部跑一遍（含 3 次全量变异 run，约 30~40 分钟）
#       bash commands.sh a|b|c|d|e  # 只跑某一组
set -uo pipefail

SRC="/Users/zingaltern/Documents/Default Project/agent-consistency-lab"
COMMIT="ed8b3662fd5b0d1d96257c517b3d82480c3613fc"
GROUP="${1:-all}"

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 0. 环境准备
setup() {
  log "0. 隔离环境（只读克隆 + 两个解释器）"
  rm -rf /tmp/verify-gate
  git clone -q "$SRC" /tmp/verify-gate
  cd /tmp/verify-gate && git checkout -q "$COMMIT"
  python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev,eval,mcp,otel]"
  rm -rf /tmp/venv311
  python3.11 -m venv /tmp/venv311 && /tmp/venv311/bin/pip install -q -e ".[dev,eval,mcp]"
  export PYTHONDONTWRITEBYTECODE=1
  echo "CLONE=$(git rev-parse HEAD)"
}

# ---------------------------------------------------------------- A. 两条 P0
group_a() {
  log "A1 真 mutmut：冷缓存 + 只跑 1 条变异体 ⇒ 其余 2048 条从判决里消失"
  cd /tmp/verify-gate && rm -rf mutants .mutmut-cache
  .venv/bin/python scripts/mutation_check.py \
      --module 'harness.approval.xǁApprovalBindingǁis_expired__mutmut_1' \
      --timeout 900 --json-out /tmp/a1-vanished.json > /tmp/a1-vanished.log 2>&1
  echo "A1 退出码 = $?（期望 1）"
  grep -a "全状态记账\|^--- \[" /tmp/a1-vanished.log | head -6
  grep -a "not checked" /tmp/a1-vanished.log | head -1

  log "A1b/A1c 判据矩阵（我自写的 mutmut 替身 + 14 格场景）"
  mkdir -p /tmp/vg-shim
  cat > /tmp/vg-shim/mutmut.py <<'PY'
import json, os, sys
argv = sys.argv[1:]
cmd = argv[0] if argv else ""
if cmd == "run":
    sys.exit(int(os.environ.get("VG_RUN_EXIT", "0")))
if cmd == "show":
    print("(shim)"); sys.exit(0)
if cmd == "results":
    with open(os.environ["VG_TABLE"], encoding="utf-8") as fh:
        table = json.load(fh)
    if os.environ.get("VG_RESULTS_EXIT", "0") != "0":
        print("shim: 故意失败", file=sys.stderr); sys.exit(int(os.environ["VG_RESULTS_EXIT"]))
    for name, status in table.items():
        print(f"    {name}: {status}")
    sys.exit(0)
sys.exit(0)
PY
  .venv/bin/python /tmp/vg-repro/gate_matrix.py /tmp/verify-gate \
      "$SRC/reports/mutation_baseline.json" /tmp/vg-matrix 2>&1 | tail -20
}

# ---------------------------------------------------------------- B. 根因与修法
group_b() {
  log "B4 最小复现：父进程预热 getproxies() + fork ⇒ 子进程 SIGSEGV"
  /tmp/verify-gate/.venv/bin/python /tmp/vg-repro/segv_repro.py;        echo "冷（对照）退出=$?（期望 0）"
  /tmp/verify-gate/.venv/bin/python /tmp/vg-repro/segv_repro.py --hot;  echo "热（实验）退出=$?（期望 11=SIGSEGV）"
  /tmp/venv311/bin/python /tmp/vg-repro/segv_repro.py --hot;            echo "3.11 热退出=$?"
  echo "崩溃栈（本机崩溃报告）："
  /usr/bin/python3 - <<'PY'
import json, pathlib
files = sorted(pathlib.Path.home().joinpath('Library/Logs/DiagnosticReports').glob('Python*.ips'),
               key=lambda p: p.stat().st_mtime, reverse=True)
if files:
    raw = files[0].read_text(encoding='utf-8', errors='replace').split('\n', 1)
    body = json.loads(raw[1])
    frames = body['threads'][body.get('faultingThread', 0)]['frames']
    print(' ', files[0].name, [f.get('symbol') for f in frames[:6] if f.get('symbol')])
PY

  log "B5 修法有效性：修复版 -11=0；把 conftest 两行改回去 ⇒ -11 回来"
  /usr/bin/env python3 - <<'PY'
import json, pathlib, collections
for root in ('/tmp/verify-gate', '/tmp/vg-revert-conftest'):
    p = pathlib.Path(root, 'mutants')
    if not p.exists():
        print(f"  {root}: 还没有 mutants/（先跑一次全量 run）"); continue
    tot = collections.Counter()
    for f in p.rglob('*.meta'):
        tot.update(json.loads(f.read_text(encoding='utf-8')).get('exit_code_by_key', {}).values())
    print(f"  {root}: -11={tot.get(-11,0)} -24={tot.get(-24,0)} None={tot.get(None,0)} "
          f"killed={tot.get(1,0)} survived={tot.get(0,0)} no_tests={tot.get(33,0)}")
PY
  echo "（'/tmp/vg-revert-conftest' 见 setup_revert；期望：verify-gate 全 0，revert 版 -11=16 且含 3 条点名）"

  log "B6 影响面：回环请求走代理还是直连（四格）"
  /tmp/vg-repro/../verify-gate/.venv/bin/python /tmp/vg-repro/proxy_impact.py 2>/dev/null || \
    /tmp/verify-gate/.venv/bin/python /tmp/vg-repro/proxy_impact.py

  log "B7 产品路径 fork 审计"
  cd /tmp/verify-gate
  grep -rn "multiprocessing\|os\.fork" --include="*.py" harness/ opsenv/ experiments/ integrations/ scripts/ \
    || echo "  0 命中（只有 subprocess：$(grep -rn 'subprocess\.\(run\|Popen\)' --include='*.py' harness/ opsenv/ experiments/ integrations/ scripts/ | wc -l) 处）"
}

# ---------------------------------------------------------------- D. 语义与数字
group_d() {
  log "D12 approve 顶层 isError（含正对照 + 外部账本）"
  rm -rf /tmp/vg-approve
  cd /tmp/verify-gate && .venv/bin/python /tmp/vg-repro/approve_iserror.py /tmp/verify-gate /tmp/vg-approve \
    > /tmp/vg-approve-result.txt 2>&1
  echo "退出码=$?（期望 0）"; grep -a "case=\|note:" /tmp/vg-approve-result.txt | head -4

  log "D13/D14 基线自洽复算"
  .venv/bin/python - <<'PY'
import json, collections
p = json.load(open('reports/mutation_baseline.json'))
c, sbm = p['counts'], p['status_by_mutant']
cnt = collections.Counter(sbm.values())
print("  counts:", c)
print("  实际:", dict(cnt))
print("  self-consistent:", c['killed']==cnt['killed'] and c['survived']==cnt['survived']
      and c['no_tests']==cnt['no tests'] and c['total']==len(sbm) and c['decided']==c['killed']+c['survived'])
print(f"  survivor_rate 复算 = {c['survived']}/{c['decided']} = {round(c['survived']/c['decided'],4)}（基线 {p['survivor_rate']}）")
print(f"  invisible_share 复算 = {c['invisible']}/{c['total']} = {round(c['invisible']/c['total'],4)}（基线 {p['invisible_share']}）")
print(f"  18/2049 = {18/2049*100:.2f}%")
PY
  echo "  print-time-estimates 行数 = $(.venv/bin/python -m mutmut print-time-estimates 2>/dev/null | grep -c '^<')（期望 2049 = 结果表条数）"
  echo "  --summary-only 字段类型："
  .venv/bin/python scripts/mutation_check.py --summary-only --json-out /tmp/summary-only.json >/dev/null
  .venv/bin/python -c "
import json; d=json.load(open('/tmp/summary-only.json'))
print('   survivors:', type(d['survivors']).__name__, len(d['survivors']), '| survivor_count:', type(d['survivor_count']).__name__, d['survivor_count'])
"
}

# ---------------------------------------------------------------- E. 非回归
group_e() {
  log "E15 非回归（3.14 + otel / 3.11 无 otel）"
  cd /tmp/verify-gate
  .venv/bin/python -m pytest -o addopts= -p no:cacheprovider -q 2>&1 | tail -1
  .venv/bin/python -m pytest -o addopts= -p no:cacheprovider -q -m "not live and not mcp" 2>&1 | tail -1
  .venv/bin/python -m pytest -o addopts= -p no:cacheprovider -m mcp -q 2>&1 | tail -1
  .venv/bin/ruff check . 2>&1 | tail -1
  /tmp/venv311/bin/python -m pytest -o addopts= -p no:cacheprovider -q 2>&1 | tail -1
  /tmp/venv311/bin/python -m pytest -o addopts= -p no:cacheprovider -q -m "not live and not mcp" 2>&1 | tail -1

  log "E15 重作业：crash_matrix --repeats 5（16 格 as-predicted）与 opsenv.suite --gate（14/14）"
  .venv/bin/python -m experiments.crash_matrix --repeats 5 2>&1 | tail -3
  .venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate 2>&1 | tail -3

  log "E18/E19 时长与 claim 对账（空闲机器上测）"
  /usr/bin/time -p .venv/bin/python scripts/check_facts.py --run verify --json-out /tmp/facts-verify.json 2>&1 | tail -4
  .venv/bin/python -c "
import json, collections
d=json.load(open('reports/documented-facts.json'))
c=collections.Counter(x.get('run','?') for x in d['claims'])
print('  claim 总数', len(d['claims']), dict(c))
"
  log "E16 旧数字残留扫描（只应出现在标为'已作废'的历史文档里）"
  grep -rn "0\.1648\|912 条\|45\.1%" --include="*.md" . | grep -v independent-test | head
  git diff --stat main...HEAD -- docs/semantics.md && echo "  semantics.md 相对 main 零改动"
}

# ------------------------------------------------- 附加：反例克隆（B5 的「改回去」那半）
setup_revert() {
  log "准备反例克隆：把 tests/conftest.py 的两行改回去"
  rm -rf /tmp/vg-revert-conftest
  git clone -q "$SRC" /tmp/vg-revert-conftest
  cd /tmp/vg-revert-conftest && git checkout -q "$COMMIT"
  /usr/bin/env python3 - <<'PY'
import pathlib
p = pathlib.Path('tests/conftest.py'); t = p.read_text(encoding='utf-8')
for line in ('os.environ["no_proxy"] = "*"\n', 'os.environ["NO_PROXY"] = "*"\n'):
    assert line in t; t = t.replace(line, '')
p.write_text(t, encoding='utf-8'); print("  已删掉那两行")
PY
  python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev,eval]"
  .venv/bin/python -m mutmut run "harness.approval.*" --max-children 3 > /tmp/revert-approval-run.log 2>&1
  echo "  mutmut run 退出=$?"
}

case "$GROUP" in
  all) setup; group_a; group_d ;;
  a)   group_a ;;
  b)   group_b ;;
  d)   group_d ;;
  e)   group_e ;;
  revert) setup_revert ;;
  *)   echo "用法: bash commands.sh [all|a|b|d|e|revert]"; exit 2 ;;
esac

log "完成。证据索引见 report.md §6；全部产物在 /tmp。"
