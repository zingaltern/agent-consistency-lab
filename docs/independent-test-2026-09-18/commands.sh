#!/usr/bin/env bash
# 独立验证的可再生命令集（2026-09-18，被测 commit 1d4b3f3710e15c629d34e7ab9fc65209a299ffbb）
#
# 纪律：本脚本**不改被测仓库**。它把仓库克隆到 /tmp，在副本里跑；
# 变异门禁的退化注入会在副本里临时改代码，脚本末尾自动还原。
#
# 用法：
#   bash commands.sh setup     # 建隔离环境（约 1 分钟）
#   bash commands.sh a         # A 功能面（工具清单 / 只读对拍 / 大结果 / 失败语义）
#   bash commands.sh b         # B 治理语义（审批三条路径 / 错 id / 重启续跑 / TOCTOU）
#   bash commands.sh c         # C 崩溃对照（作者脚本 ×2 + 我的自写驱动）
#   bash commands.sh d         # D 可观测
#   bash commands.sh e         # E 边界（AST / 反向 import / 时长 / 措辞 / semantics）
#   bash commands.sh f         # F 高风险项（变异门禁三项 + 对拍 + 回归可证伪）
#   bash commands.sh g         # G 非回归门禁
#   bash commands.sh all       # 全部
#
# 依赖：git / python3（>=3.11）。全部产物写 /tmp/ev/ 与 /tmp/drv/。

set -uo pipefail

SRC="/Users/zingaltern/Documents/Default Project/agent-consistency-lab"
TIP="1d4b3f3710e15c629d34e7ab9fc65209a299ffbb"
WORK=/tmp/verify-mcp
MAIN=/tmp/verify-main
EV=/tmp/ev
DRV=/tmp/drv
PY="$WORK/.venv/bin/python"

export PYTHONDONTWRITEBYTECODE=1

setup() {
  mkdir -p "$EV" "$DRV" "$EV/mut"
  rm -rf "$WORK"
  git clone "$SRC" "$WORK"
  ( cd "$WORK" && git checkout "$TIP" )
  python3 -m venv "$WORK/.venv"
  "$WORK/.venv/bin/pip" install -q -e "$WORK[dev,eval,mcp]"
  echo "[setup] 完成。mcp 版本："
  "$WORK/.venv/bin/pip" list | grep -E "^mcp "
  # main 对照副本（复用同一个 venv，用 PYTHONPATH 抢占 import 顺序）
  rm -rf "$MAIN"
  git clone -q "$SRC" "$MAIN"
  ( cd "$MAIN" && git checkout -q main )
}

# ---------------------------------------------------------------- A
a() {
  # A1：工具清单与 schema（不需要 [mcp] extra 的纯映射路径）
  "$PY" -m integrations.mcp_server --run-dir /tmp/ev/a_list --list-tools --json-out "$EV/tools.json" >/dev/null
  "$PY" - <<'PY'
import json
d = json.load(open("/tmp/ev/tools.json"))
print("tool_count =", d["tool_count"], " governance =", d["governance_tool_count"])
for t in d["tools"]:
    s = t["inputSchema"]
    print(f'  {t["name"]:24s} props={list(s.get("properties", {}))} required={s.get("required")}')
print("read_artifact 下发了吗：", any(t["name"] == "read_artifact" for t in d["tools"]))
PY
  # A2/A3/A4：真 stdio 会话（自写驱动）
  "$PY" "$DRV/a_functional.py" > "$EV/a_functional.json" 2>"$EV/a_functional.err"
  "$PY" "$DRV/a4_exc.py"      > "$EV/a4_exc.json"      2>/dev/null
  echo "[a] 见 $EV/a_functional.json 与 $EV/a4_exc.json"
  "$PY" -c "
import json; d=json.load(open('$EV/a_functional.json'))
print('只读对拍：MCP', d['query_metrics']['structured']['result'])
print('          进程内', d['inproc_query_metrics']['result'])
print('大结果：', {k:v for k,v in d['big_result'].items() if k!='result_keys'})
"
}

# ---------------------------------------------------------------- B
b() {
  "$PY" "$DRV/b_governance.py" > "$EV/b_governance.json" 2>/dev/null
  "$PY" "$DRV/b_extra.py"      > "$EV/b_extra.json"      2>/dev/null
  "$PY" "$DRV/b_restart.py"    > "$EV/b_restart.json"    2>/dev/null
  "$PY" "$DRV/b_toctou.py"     > "$EV/b_toctou.json"     2>/dev/null
  "$PY" -c "
import json
for n in ('b_governance','b_extra','b_restart','b_toctou'):
    d=json.load(open(f'$EV/{n}.json')); print('===', n, '===')
    print(json.dumps(d, ensure_ascii=False)[:1200]); print()
"
}

# ---------------------------------------------------------------- C
c() {
  "$PY" -m integrations.mcp_crash_demo --work-root /tmp/mcp-crash-1 --json-out "$EV/crash1.json" >/dev/null 2>"$EV/crash1.err"
  "$PY" -m integrations.mcp_crash_demo --work-root /tmp/mcp-crash-2 --json-out "$EV/crash2.json" >/dev/null 2>"$EV/crash2.err"
  "$PY" "$DRV/c_crash.py" > "$EV/c_crash.json" 2>"$EV/c_crash.err"
  grep mcp_crash_demo "$EV/crash1.err" "$EV/crash2.err"
  "$PY" -c "
import json
for n in ('crash1','crash2','c_crash'):
    d=json.load(open(f'$EV/{n}.json')); print('===', n, '===')
    print(json.dumps(d.get('summary', {k:{kk:vv for kk,vv in v.items() if kk in ('flags','exit_137','final_rows','max_per_key','tool_results')} for k,v in d.items() if isinstance(v,dict) and 'flags' in v or k in ('with_outbox','without_outbox')}), ensure_ascii=False))
    if 'single_variable_diff' in d: print('单变量差异项 =', d['single_variable_diff'])
"
}

# ---------------------------------------------------------------- D
d() {
  rm -rf /tmp/otel-demo
  "$PY" -m integrations.otlp_local_sink --selftest --work-root /tmp/otel-demo > "$EV/d_sink.txt" 2>&1
  "$PY" -c "
import json,re
t=open('$EV/d_sink.txt').read()
j=json.loads(t[t.index('{'):t.rindex('}')+1])
print({k:j[k] for k in ('span_count','tool_span_count','model_span_count','derived_duration_span_count','derived_flag_matches_model_spans','has_ledger_attributes','request_paths')})
keys=sorted({k for s in j['spans'] for k in s['attribute_keys']})
sus=[k for k in keys if any(w in k.lower() for w in ('ledger','effect','outbox','count','rows','times'))]
print('可疑的账本类属性键：', sus or '（无）')
"
}

# ---------------------------------------------------------------- E
e() {
  "$PY" "$DRV/e_ast.py"                                   # 依赖方向（自写 AST）
  "$PY" "$DRV/e_reverse.py"                               # 反向 import 泄漏
  "$PY" -c "
import tomllib
d=tomllib.load(open('$WORK/pyproject.toml','rb'))
print('内核依赖:', d['project']['dependencies'])
print('dev 含 mcp:', any('mcp' in x.lower() for x in d['project']['optional-dependencies']['dev']))
"
  # verify 时长与用例数（同口径：默认 addopts）
  for rev in "$MAIN" "$WORK"; do
    ( cd "$rev" && PYTHONPATH="$rev" "$PY" -m pytest -o addopts= -p no:cacheprovider -q -m "not live and not mcp" 2>&1 | tail -1 | sed "s|^|$rev: |" )
  done
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest -o addopts= -p no:cacheprovider --collect-only -q 2>/dev/null | tail -1 )
  grep -rn "平台\|服务化\|生产级\|高可用" "$WORK/integrations/" "$WORK/docs/integrations.md" || echo "措辞自检：0 命中"
  ( cd "$SRC" && git diff main.."$TIP" -- docs/semantics.md | wc -l | sed 's/^/semantics.md diff 行数: /' )
}

# ---------------------------------------------------------------- F
f() {
  echo "### F23① 旧基线是否会让新代码变红（按名字集合算）"
  "$PY" -c "
import json
m=json.load(open('$MAIN/reports/mutation_baseline.json')); t=json.load(open('$WORK/reports/mutation_baseline.json'))
new=sorted(set(t['survivors'])-set(m['survivors']))
print('tip 幸存变异不在 main 基线里的条数 =', len(new), '→ 旧基线会退出 1：', bool(new))
print('main counts', m['counts'], 'rate', m['survivor_rate'])
print('tip  counts', t['counts'], 'rate', t['survivor_rate'])
"
  echo
  echo "### F24 缺项 → 忽略（P0-1）"
  ( cd "$WORK" && rm -rf mutants && PYTHONPATH="$WORK" "$PY" scripts/mutation_check.py --module "harness.approval.*" --timeout 600 --max-children 4 > "$EV/mut/gate_control2.txt" 2>&1; echo "退出码=$?" )
  tail -4 "$EV/mut/gate_control2.txt"
  echo
  echo "### P1-1 --module 传文件路径恒失败"
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" scripts/mutation_check.py --module harness/approval.py --timeout 120 > "$EV/mut/module_gate.txt" 2>&1; echo "退出码=$?" )
  grep -E "本轮没有跑成功|Filter" "$EV/mut/module_gate.txt" | head -3
  echo
  echo "### F23② 退化注入：新增未覆盖代码 → 门禁应变红（实测不变）"
  cp "$WORK/harness/approval.py" "$EV/mut/approval.py.orig"
  "$PY" - <<'PY'
from pathlib import Path
p = Path("/tmp/verify-mcp/harness/approval.py"); s = p.read_text(encoding="utf-8")
anchor = "class ApprovalError(RuntimeError):\n    pass\n"
s = s.replace(anchor, anchor + '''

def _gate_probe_uncovered(values: list[int]) -> int:
    """门禁退化注入（验证用）：没有任何用例调用本函数。"""
    total = 0
    for value in values:
        total += value * 3
    if total > 100:
        return total - 1
    return total + 1
''', 1)
old = '''        if self.scope == SCOPE_SESSION and tool == self.tool:
            return True, "session_scoped_reuse"
        return False, "approval_not_for_this_call"'''
new = '''        if self.scope == SCOPE_SESSION and tool == self.tool:
            return True, "session_scoped_reuse"
        if tool_call_id.startswith("gate-probe-never-matches-"):
            return True, "gate_probe_branch"
        return False, "approval_not_for_this_call"'''
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8")
print("已注入 1 个未被调用的函数 + 1 条永不执行的分支")
PY
  ( cd "$WORK" && rm -rf mutants && PYTHONPATH="$WORK" "$PY" scripts/mutation_check.py --module "harness.approval.*" --timeout 600 --max-children 4 > "$EV/mut/gate_injected.txt" 2>&1; echo "退出码=$?（红线 5 要求非 0）" )
  tail -4 "$EV/mut/gate_injected.txt"
  cp "$EV/mut/approval.py.orig" "$WORK/harness/approval.py"    # 还原
  echo "已还原 harness/approval.py"
  echo
  echo "### P1-3 mutmut 把真幸存变异误判成 segfault（反例）"
  cp "$WORK/harness/approval.py" "$EV/mut/approval.py.keep"
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m mutmut apply "harness.approval.xǁApprovalBindingǁis_expired__mutmut_9" >/dev/null 2>&1 )
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest -o addopts= -p no:cacheprovider -q -m "not live and not mcp" 2>&1 | tail -1 | sed 's/^/手工应用该变异体后的全套结果: /' )
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m mutmut results --all=true 2>/dev/null | grep is_expired__mutmut_9 | sed 's/^/mutmut 的判决: /' )
  cp "$EV/mut/approval.py.keep" "$WORK/harness/approval.py"
  echo "已还原 harness/approval.py"
  echo
  echo "### F26 一致性对拍（进程内 Loop vs 真 MCP 服务进程）"
  "$PY" "$DRV/f_parity.py" > "$EV/f_parity.json" 2>"$EV/f_parity.err"
  "$PY" -c "
import json; d=json.load(open('$EV/f_parity.json'))
print('管线事件逐位相同:', d['events_identical'], ' 外部账本相同:', d['ledger_identical'])
print('事件条数:', len(d['loop_pipeline_projection']))
for e in d['loop_pipeline_projection']: print('  ', e)
"
  echo
  echo "### F27 回归用例可证伪性"
  cp "$WORK/harness/execution.py" "$EV/mut/execution.py.orig"
  "$PY" - <<'PY'
from pathlib import Path
p = Path("/tmp/verify-mcp/harness/execution.py"); s = p.read_text(encoding="utf-8")
old = """            try:
                outcomes.append(self.execute(ctx, request, counters))
            except InterruptSignal as signal:
                return ResumeReport(tuple(outcomes), signal.request)
        return ResumeReport(tuple(outcomes))"""
new = """            outcomes.append(self.execute(ctx, request, counters))
        return ResumeReport(tuple(outcomes))"""
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8"); print("已把 resume_open_calls 改回异常穿透")
PY
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest -o addopts= -p no:cacheprovider -q -m "not live and not mcp" 2>&1 | tail -3 )
  cp "$EV/mut/execution.py.orig" "$WORK/harness/execution.py"
  echo "已还原 harness/execution.py"
}

# ---------------------------------------------------------------- G
g() {
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest -o addopts= -p no:cacheprovider -q 2>&1 | tail -2 )
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest -o addopts= -p no:cacheprovider -q -m mcp 2>&1 | tail -2 )
  ( cd "$WORK" && PYTHONPATH="$WORK" .venv/bin/ruff check . )
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m opsenv.suite --per-fault 8 --repeats 3 --gate 2>&1 | tail -2 )
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m experiments.crash_matrix --repeats 5 2>&1 | tail -2 )
  ( cd "$WORK" && PYTHONPATH="$WORK" "$PY" scripts/check_facts.py --run verify 2>&1 | tail -2 )
}

case "${1:-all}" in
  setup) setup ;;
  a) a ;;
  b) b ;;
  c) c ;;
  d) d ;;
  e) e ;;
  f) f ;;
  g) g ;;
  all) setup && a && b && c && d && e && f && g ;;
  *) echo "未知子命令：$1"; exit 2 ;;
esac
