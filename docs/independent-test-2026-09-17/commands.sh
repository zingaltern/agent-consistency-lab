#!/usr/bin/env bash
# 独立功能验证（W9 交付 A+B）——可执行的复现脚本
#
# 纪律：
#   * 被审计的仓库**只读**：本脚本把仓库克隆到 $CHECKOUT 再跑，全部产物写 $EVID；
#   * 只在 /tmp 下做"故意改坏"的实验，每一步都在副本里做并立即还原；
#   * 慢步骤（mutmut 全量 ≈ 6 分钟）用 RUN_SLOW=0 可跳过。
#
# 用法：
#   bash docs/independent-test-2026-09-17/commands.sh                  # 全量
#   RUN_SLOW=0 bash docs/independent-test-2026-09-17/commands.sh       # 跳过 mutmut
#   COMMIT=49f9f51 bash docs/independent-test-2026-09-17/commands.sh   # 指定被测 commit
set -u

SRC_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMMIT="${COMMIT:-49f9f51}"
CHECKOUT="${CHECKOUT:-/tmp/verify-w9}"
EVID="${EVID:-/tmp/w9-evidence}"
PROBES="$EVID/probes"
RUN_SLOW="${RUN_SLOW:-1}"

PASS=0; FAIL=0
ok()   { echo -e "  \033[32mPASS\033[0m $*"; PASS=$((PASS+1)); }
bad()  { echo -e "  \033[31mFAIL\033[0m $*"; FAIL=$((FAIL+1)); }
head1(){ echo; echo "=== $* ==="; }
expect_exit() { # expect_exit <期望码> <实际码> <描述>
  if [ "$1" = "$2" ]; then ok "$3（exit=$2）"; else bad "$3：期望 exit=$1，实际 exit=$2"; fi
}

head1 "0. 隔离环境"
if [ ! -d "$CHECKOUT/.venv" ]; then
  rm -rf "$CHECKOUT"
  git clone -q "$SRC_REPO" "$CHECKOUT"
  ( cd "$CHECKOUT" && git checkout -q "$COMMIT" )
  python3 -m venv "$CHECKOUT/.venv"
  "$CHECKOUT/.venv/bin/pip" install -q -e "$CHECKOUT[dev,eval]"
fi
mkdir -p "$EVID" "$PROBES"
cd "$CHECKOUT"
echo "被测 commit: $(git rev-parse HEAD)  $(git rev-parse --abbrev-ref HEAD)"
export PYTHONDONTWRITEBYTECODE=1
PY="$CHECKOUT/.venv/bin/python"

head1 "1. A-G1 claim 门禁：先跑通过与三类退化注入"
$PY scripts/check_facts.py --run verify > "$EVID/commands_facts_verify.log" 2>&1
expect_exit 0 $? "verify 轻集（27 条）全过"
$PY scripts/check_facts.py --run nightly > "$EVID/commands_facts_nightly.log" 2>&1
expect_exit 0 $? "nightly 整量集（45 条）全过"

# 注入①：改 README 里被引用的数字（预期：**不红**——门禁不读正文，见 P1-2）
cp README.md "$EVID/README.bak"
$PY - <<'PY'
import pathlib
p = pathlib.Path("README.md"); s = p.read_text(encoding="utf-8")
p.write_text(s.replace("命中率 57.4% → 0%", "命中率 87.4% → 0%", 1), encoding="utf-8")
PY
$PY scripts/check_facts.py --run verify > "$EVID/commands_inject_readme.log" 2>&1
expect_exit 0 $? "注入① 改 README 数字后仍全过（= P1-2 假阴）"
cp "$EVID/README.bak" README.md

# 注入②：改 reports/*.json 里被引用的字段（预期：红）
cp reports/mutation_baseline.json "$EVID/mutation_baseline.bak"
$PY - <<'PY'
import json, pathlib
p = pathlib.Path("reports/mutation_baseline.json"); d = json.loads(p.read_text())
d["survivors"] = d["survivors"][:-1]
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
$PY scripts/check_facts.py --run verify --id mutation-survivors > "$EVID/commands_inject_reports.log" 2>&1
expect_exit 1 $? "注入② 改 reports/*.json 字段 → 红"
cp "$EVID/mutation_baseline.bak" reports/mutation_baseline.json

# 注入③：容差即灵敏度（收紧→红；放宽到形同虚设→漏掉明显偏离）
cp reports/documented-facts.json "$EVID/facts.bak"
$PY - <<'PY'
import json, pathlib
p = pathlib.Path("reports/documented-facts.json"); d = json.loads(p.read_text())
for c in d["claims"]:
    if c["id"] == "w4-e1-hit-ratio-tail":
        c["value"] = 0.673812           # 真值 0.573812
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
$PY scripts/check_facts.py --run verify --id w4-e1-hit-ratio-tail > "$EVID/commands_inject_tol_tight.log" 2>&1
expect_exit 1 $? "注入③a 真漂移 + 紧容差（1e-06）→ 红"
$PY - <<'PY'
import json, pathlib
p = pathlib.Path("reports/documented-facts.json"); d = json.loads(p.read_text())
for c in d["claims"]:
    if c["id"] == "w4-e1-hit-ratio-tail":
        c["tolerance"] = 1e9
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
$PY scripts/check_facts.py --run verify --id w4-e1-hit-ratio-tail > "$EVID/commands_inject_tol_vacuous.log" 2>&1
expect_exit 0 $? "注入③b 同一漂移 + 形同虚设的容差 → 漏掉（表格同时打印 0.673812/0.573812）"

# 注入④：置空 source_path（设计文档 A §R-A1 的第三类）
$PY - <<'PY'
import json, pathlib
p = pathlib.Path("reports/documented-facts.json"); d = json.loads(p.read_text())
for c in d["claims"]:
    if c["id"] == "w4-e3-compactions":
        c["source_path"] = []
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
$PY scripts/check_facts.py --run verify > "$EVID/commands_inject_emptypath.log" 2>&1
expect_exit 1 $? "注入④ 置空 source_path → 红"
cp "$EVID/facts.bak" reports/documented-facts.json
git status --short   # 必须为空

head1 "2. A-G1 claim 成色：抽查并与仓库产物二次互证"
$PY - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, "/tmp/verify-w9"); sys.path.insert(0, "/tmp/verify-w9/scripts")
from check_facts import command_key
claims = {c["id"]: c for c in json.loads(Path("reports/documented-facts.json").read_text())["claims"]}
SAMPLE = [("w4-e1-hit-ratio-tail", "w4_context_cost.json"), ("w4-e3-compactions", "w4_context_cost.json"),
          ("w3-real-kills", "w4_crash_matrix.json"), ("w3-window2-no-outbox-duplicates", "w4_crash_matrix.json"),
          ("w5-catalog-total", "w6_eval.json"), ("w5-harness-correct-competent", "w6_eval.json"),
          ("w6-paired-red-line-point", "w6_eval.json"), ("w6-paired-pairs", "w6_eval.json"),
          ("w7-selected-threshold", "w7_sweep.json"), ("w7-net-cost-delta-ratio", "w7_sweep.json"),
          ("fuzz-killed-injections", "chaos_fuzz_report.json"), ("replay-consistency-identical", "replay_consistency.json")]
bad = 0
for cid, report in SAMPLE:
    c = claims[cid]
    out = Path(f"/tmp/facts/cmd-{command_key(c['source_cmd'])}.json")
    regen = json.loads(out.read_text()) if out.exists() else None
    repo = json.loads(Path("reports", report).read_text())
    def walk(p, path):
        for s in path: p = p[s] if not isinstance(p, list) else p[int(s)]
        return p
    got = walk(regen, c["source_path"]) if regen else None
    other = walk(repo, c["source_path"]) if report != "mutation_baseline.json" else None
    def close(x):                       # 数值按 claim 的容差判，布尔/字符串按相等判
        if isinstance(c["value"], bool): return x is c["value"]
        if x is None: return False
        return abs(float(x) - float(c["value"])) <= max(float(c["tolerance"]), 1e-12)
    same = close(got) and (other is None or close(other))
    print(f"  {'OK ' if same else 'BAD'} {cid}: claim={c['value']}±{c['tolerance']} 再生={got} 仓库={other}")
    bad += not same
print(f"抽查 {len(SAMPLE)} 条：{'全部一致' if not bad else f'{bad} 条不一致'}")
sys.exit(1 if bad else 0)
PY
expect_exit 0 $? "12 条 claim 双路径互证"

head1 "3. A-G2 fuzz：标定复现 + seed 可复现 + 标定改坏（P1-1）"
$PY -m experiments.chaos_fuzz --calibrate-only > "$EVID/commands_fuzz_calib.json" 2>&1
expect_exit 0 $? "本机标定（应得 run=180ms / resume=35ms）"
grep -o '"window_ms": [0-9]*' "$EVID/commands_fuzz_calib.json" | sort -u | tr '\n' ' '; echo
$PY - <<'PY'
import json
from experiments.chaos_fuzz import COMBOS, PHASES, sample_kill_times
delivered = json.load(open("reports/chaos_fuzz_report.json"))
windows = {(w["combo"], w["phase"]): w["window_ms"] for w in delivered["windows"]}
same = 0
for i, combo in enumerate(COMBOS):
    for pi, phase in enumerate(PHASES):
        seed = 20260917 + i * 10 + pi
        mine = sample_kill_times(seed=seed, repeats=30, window_ms=windows[(combo["name"], phase)])
        theirs = [t["kill_after_ms"] for t in delivered["trials"]
                  if t["combo"] == combo["name"] and t["kill_phase"] == phase]
        same += (mine == theirs)
print(f"与交付 JSON 逐格一致: {same}/6")
raise SystemExit(0 if same == 6 else 1)
PY
expect_exit 0 $? "注入时刻由 seed 逐位可复现（6/6 格）"

cat > "$PROBES/fuzz_sabotage.py" <<'PY'
import io, sys
from contextlib import redirect_stdout
sys.path.insert(0, "/tmp/verify-w9")
import experiments.chaos_fuzz as cf
for name, window, workroot in (("A 放大到 2000ms", 2000, "/tmp/w9-evidence/commands-sabotage-a"),
                               ("B 归零", 0, "/tmp/w9-evidence/commands-sabotage-b")):
    cf.calibrate_kill_window = lambda **kw: window
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = cf.main(["--repeats", "3", "--seed", "7", "--workroot", workroot])
    except AssertionError as exc:
        print(f"  [B {name}] assert 拦下（期望行为）: {str(exc)[:70]}")
        continue
    line = [l for l in buf.getvalue().splitlines() if "总试次" in l]
    print(f"  [{name}] exit={code}  {line[0] if line else ''}")
PY
$PY "$PROBES/fuzz_sabotage.py" | tee "$EVID/commands_fuzz_sabotage.log"

head1 "4. A-G2 oracle：合成库自造反例 + 假阳性对照"
cat > "$PROBES/oracle_synthetic.py" <<'PY'
"""只用 opsenv.oracle.audit_run + 自造的 sqlite 库（不复用作者 fixture）。"""
import json, shutil, sqlite3, sys
from pathlib import Path
sys.path.insert(0, "/tmp/verify-w9")
from opsenv.oracle import audit_run
ROOT = Path("/tmp/w9-evidence/commands-oracle")
DDL = """CREATE TABLE events (branch_id TEXT, seq INTEGER, event_id TEXT, kind TEXT, type TEXT,
        payload_json TEXT, PRIMARY KEY (branch_id, seq));
        CREATE TABLE tool_calls (tool_call_id TEXT PRIMARY KEY, run_id TEXT, branch_id TEXT, tool TEXT,
        args_json TEXT, args_sha256 TEXT, idempotency_key TEXT, status TEXT);"""
WDDL = """CREATE TABLE effects (idempotency_key TEXT, effect_id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT);"""
def build(case, events, rows, effects):
    d = ROOT / case
    if d.exists(): shutil.rmtree(d)
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "runtime.db"); con.executescript(DDL)
    for seq, eid, etype, ref in events:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                    ("br", seq, eid, "tree", etype, json.dumps({"tool_call_id": ref})))
    for cid, status, key in rows:
        con.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?)",
                    (cid, "run", "br", "restart_service", "{}", "sha", key, status))
    con.commit(); con.close()
    con = sqlite3.connect(d / "world.db"); con.executescript(WDDL)
    for k in effects: con.execute("INSERT INTO effects (idempotency_key, payload) VALUES (?,'x')", (k,))
    con.commit(); con.close(); return d
def audit(d, status="completed"):
    return set(audit_run(d, status=status, resumes_used=1, max_resumes=6).codes())
CASES = [
 ("01-compliant", dict(events=[(0,"e0","agent_message",None),(1,"e1","tool_call","c1"),(2,"e2","tool_result","c1")],
                       rows=[("c1","executed","k1")], effects=["k1"]), set()),
 ("02-sideeffect-without-tool-result", dict(events=[(0,"e0","agent_message",None),(1,"e1","tool_call","c1")],
                       rows=[("c1","executed","k1")], effects=["k1"]), {"inv_closed_calls"}),
 ("03-silent-duplicate", dict(events=[(0,"e0","agent_message",None),(1,"e1","tool_call","c1"),(2,"e2","tool_result","c1")],
                       rows=[("c1","executed","k1")], effects=["k1","k1"]), {"inv_effect_accounting"}),
 ("04-dup-with-unknown-reported", dict(events=[(0,"e0","agent_message",None)],
                       rows=[("c1","unknown","k1")], effects=["k1","k1"]), set()),
 ("05-pending-row-no-result", dict(events=[(0,"e0","agent_message",None)],
                       rows=[("c1","pending","k1")], effects=[]), set()),
 ("06-seq-gap", dict(events=[(0,"e0","agent_message",None),(2,"e2","agent_message",None)], rows=[], effects=[]),
                       {"inv_no_tamper"}),
 ("07-duplicate-event-id", dict(events=[(0,"same","agent_message",None),(1,"same","agent_message",None)], rows=[], effects=[]),
                       {"inv_no_tamper"}),
]
bad = 0
for case, kw, expected in CASES:
    got = audit(build(case, **kw))
    print(f"  {'OK ' if got == expected else 'BAD'} {case}: got={sorted(got)} expected={sorted(expected)}")
    bad += got != expected
d = build("09-corrupt-runtime-db", events=[(0,"e0","agent_message",None)], rows=[("c1","executed","k1")], effects=["k1"])
raw = (d/"runtime.db").read_bytes(); (d/"runtime.db").write_bytes(raw[: len(raw)//2])
try:
    got = audit(d)
except Exception as exc:
    got = {f"RAISED:{type(exc).__name__}"}
print(f"  {'OK ' if got == {'inv_log_readable'} else 'BAD'} 09-corrupt（文档承诺 inv_log_readable）: got={sorted(got)}")
bad += got != {"inv_log_readable"}
print(f"  → {'全部符合' if not bad else f'{bad} 例不符（09 即 P2-3）'}")
sys.exit(1 if bad else 0)
PY
$PY "$PROBES/oracle_synthetic.py" | tee "$EVID/commands_oracle.log"

head1 "5. A-G3 四档谱系探测器"
$PY scripts/probe_sigterm.py --json-out "$EVID/commands_probe_sigterm.json" > "$EVID/commands_probe_sigterm.log" 2>&1
expect_exit 0 $? "SIGTERM 探测器"
$PY scripts/probe_tornwrite.py --mode both --json-out "$EVID/commands_probe_tornwrite.json" > "$EVID/commands_probe_tornwrite.log" 2>&1
expect_exit 0 $? "torn write + SIGSTOP 探测器"
$PY scripts/probe_extra_ledger_row.py --json-out "$EVID/commands_probe_ledger.json" > "$EVID/commands_probe_ledger.log" 2>&1
expect_exit 0 $? "账本外注入探测器"
$PY - <<'PY'
import json
t = json.load(open("/tmp/w9-evidence/commands_probe_tornwrite.json"))["truncate"]["results"]
print("  torn write 各档 openable:", [r["openable"] for r in t],
      "（文档称三档都不可打开；实测 0.99 档可打开——P2-4）")
print("  resume_exit_code:", [r["resume_exit_code"] for r in t], " no_new_effects:", [r["no_new_effects"] for r in t])
PY

head1 "6. A-G4 噪声人格：复跑 + 换 seed"
$PY -m opsenv.suite --per-fault 8 --repeats 3 --reasoner noisy --error-rate 0.3 \
    --json-out "$EVID/commands_noisy.json" > "$EVID/commands_noisy.log" 2>&1
expect_exit 0 $? "噪声口径整量跑"
$PY - <<'PY'
import io, json, sys
from contextlib import redirect_stdout
sys.path.insert(0, "/tmp/verify-w9")
import opsenv.suite as suite
base = suite.PROFILES
d = json.load(open("/tmp/w9-evidence/commands_noisy.json"))
gs = d["grader_sensitivity"]
print(f"  默认 seed: strict={gs['strict']['harness']:.4f} cause_only={gs['cause_only']['harness']:.4f} "
      f"strict<cause_only={gs['strict']['harness']<gs['cause_only']['harness']}")
runs = {c["runs"] for c in d["cells"]}
print(f"  runs/格={runs} gates={d['gate_summary']['passed']}/{d['gate_summary']['total']}")
for seed in (202, 303):
    suite.PROFILES = tuple(p.model_copy(update={"noise_seed": seed}) for p in base)
    out = f"/tmp/w9-evidence/commands-noisy-seed-{seed}.json"
    with redirect_stdout(io.StringIO()):
        suite.main(["--per-fault", "8", "--repeats", "3", "--reasoner", "noisy",
                    "--error-rate", "0.3", "--json-out", out])
    g = json.load(open(out))["grader_sensitivity"]
    print(f"  seed={seed}: strict={g['strict']['harness']:.4f} cause_only={g['cause_only']['harness']:.4f} "
          f"strict<cause_only={g['strict']['harness']<g['cause_only']['harness']}")
PY

head1 "7. A-G5 变异门禁：基线可复现 + 删基线必须红"
if [ "$RUN_SLOW" = "1" ]; then
  cp reports/mutation_baseline.json "$EVID/mutation_baseline.bak"
  $PY scripts/mutation_check.py --timeout 1500 --max-children 4 > "$EVID/commands_mutation_clean.log" 2>&1
  expect_exit 0 $? "干净基线下门禁通过（真实 mutmut）"
  grep "\[mutation\] killed" "$EVID/commands_mutation_clean.log" | sed 's/^/  /'
  $PY - <<'PY'
import json, pathlib
p = pathlib.Path("reports/mutation_baseline.json"); d = json.loads(p.read_text())
removed = d["survivors"].pop(); d["counts"]["survived"] = len(d["survivors"])
p.write_text(json.dumps(d, ensure_ascii=False, indent=2)); print("  删掉基线一条:", removed)
PY
  $PY scripts/mutation_check.py --timeout 1500 --max-children 4 > "$EVID/commands_mutation_tamper.log" 2>&1
  expect_exit 1 $? "删一条基线 → 红并指名具体变异（证明门禁非空转）"
  cp "$EVID/mutation_baseline.bak" reports/mutation_baseline.json
  git status --short
else
  echo "  （RUN_SLOW=0：跳过 mutmut 全量，约 6 分钟）"
fi

# P0-1：mutmut「零产出」时门禁是否假绿（不需要真跑 mutmut）
cat > "$PROBES/mutation_zero_results.py" <<'PY'
import io, sys
from contextlib import redirect_stdout
sys.path.insert(0, "/tmp/verify-w9"); sys.path.insert(0, "/tmp/verify-w9/scripts")
import scripts.mutation_check as mc
mc.run_mutmut = lambda **kw: print("[模拟] mutmut run 零产出（立即失败）")
buf = io.StringIO()
with redirect_stdout(buf):
    code = mc.main([])
print(f"  退出码 = {code}")
for line in buf.getvalue().splitlines():
    print("   ", line)
print("  →", "P0-1 复现：零产出仍判绿" if code == 0 else f"P0-1 未复现（exit={code}）")
PY
rm -rf "$CHECKOUT/mutants-zero" && mv "$CHECKOUT/mutants" "$CHECKOUT/mutants-zero" 2>/dev/null
mkdir -p "$CHECKOUT/mutants"
$PY "$PROBES/mutation_zero_results.py" | tee "$EVID/commands_mutation_zero.log"
if grep -q "P0-1 复现" "$EVID/commands_mutation_zero.log"; then
  bad "P0-1：mutmut 零产出时门禁假绿（详见报告 §3 P0-1）"
else
  ok "P0-1 未复现（门禁已能在零产出时判失败）"
fi
rm -rf "$CHECKOUT/mutants" && mv "$CHECKOUT/mutants-zero" "$CHECKOUT/mutants" 2>/dev/null

head1 "8. B-G1 三模式：一致性 + cassette 篡改"
$PY scripts/replay_consistency.py --json-out "$EVID/commands_replay.json" > "$EVID/commands_replay.log" 2>&1
expect_exit 0 $? "scripted ↔ replay 白名单一致（identical: true）"
cat > "$PROBES/cassette_tamper.py" <<'PY'
"""改 cassette 的三个部位：usage / 文本 / 删条目（序号错位）/ 请求指纹。"""
import json, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/tmp/verify-w9"); sys.path.insert(0, "/tmp/verify-w9/scripts")
from replay_consistency import _compare, _fingerprint
REPO, BASE = "/tmp/verify-w9", Path("/tmp/replay-consistency")
OUT = Path("/tmp/w9-evidence/commands-cassette")
def replay_with(case, mutate):
    d = OUT / case
    if d.exists(): shutil.rmtree(d)
    cass = d / "cassette"; shutil.copytree(BASE / "cassette", cass)
    p = cass / "cassette.json"; data = json.loads(p.read_text()); mutate(data)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    codes = []
    for phase in ("run", "approve", "resume"):
        r = subprocess.run([f"{REPO}/.venv/bin/python", "-m", "experiments.worker", "--run-dir",
                            str(d / "replay"), "--mode", phase, "--model", "replay",
                            "--record-dir", str(cass)], cwd=REPO, capture_output=True, text=True)
        codes.append(r.returncode)
        if r.returncode != 0:
            print(f"  [{case}] 拒绝（exit={codes}）: {(r.stderr or r.stdout).strip()[-120:]}")
            return
    diffs = _compare(_fingerprint(BASE / "scripted"), _fingerprint(d / "replay"))
    print(f"  [{case}] 完成 exit={codes}；对账层差异={[x['field'] for x in diffs] or '无（identical=true）'}")
replay_with("usage", lambda d: d["entries"][0]["usage"].update(prompt_tokens=d["entries"][0]["usage"]["prompt_tokens"] + 5000))
replay_with("text", lambda d: d["entries"][0].update(text="支付服务 P99 告警，先删指标。"))
replay_with("drop-entry", lambda d: d["entries"].pop(1))
replay_with("fingerprint", lambda d: d["entries"][0].update(request_fingerprint="0" * 64))
PY
$PY "$PROBES/cassette_tamper.py" | tee "$EVID/commands_cassette.log"

head1 "9. B-G1 崩溃兼容：replay 模式下的两种注入"
D="$EVID/commands-replay-chaos"; rm -rf "$D"
CHAOS_WINDOWS="pre_tool_exec:1" $PY -m experiments.worker --run-dir "$D/run" --mode run \
    --model replay --record-dir /tmp/replay-consistency/cassette > /dev/null 2>&1
expect_exit 137 $? "replay + CHAOS_WINDOWS（真 SIGKILL）"
$PY -c "import json;print('  marker:', json.load(open('$D/run/crash_marker.json')))" 2>/dev/null || true

head1 "10. B-G2 参数级审批：两条路径 + 四类绕过"
cat > "$PROBES/argpolicy_probe.py" <<'PY'
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, "/tmp/verify-w9")
from harness.context import ViewBuilder
from harness.ids import new_id
from harness.llm import ScriptedLLMClient
from harness.loop import Loop
from harness.model import ModelTurn
from harness.store.checkpoints import SqliteCheckpointSaver
from harness.store.sqlite_store import SqliteStore
from harness.tools import ArgPolicy, Effect, Tool, ToolCallRequest, ToolRegistry
WORK, EFFECTS = Path("/tmp/w9-evidence/commands-argpolicy"), []
class Stub:
    def __init__(self, args_list): self._a, self._i = args_list, 0
    def next_turn(self, *, step, view):
        if self._i >= len(self._a): return ModelTurn(text="done", usage={"completion_tokens": 1})
        a = self._a[self._i]; self._i += 1
        return ModelTurn(text="calling", usage={"completion_tokens": 2},
                         tool_calls=[ToolCallRequest(tool_call_id=f"call{self._i}", tool="scale_service", args=a)])
def scenario(name, args_list, policy, tamper=None, approve_args=None, decision="approved", scope="once"):
    EFFECTS.clear()
    d = WORK / name
    if d.exists(): shutil.rmtree(d)
    d.mkdir(parents=True)
    store = SqliteStore(d / "runtime.db"); store.setup(); saver = SqliteCheckpointSaver(store)
    ids = {"run_id": new_id("run"), "thread_id": new_id("thr"), "branch_id": new_id("br")}
    store.create_run(ids["run_id"], thread_id=ids["thread_id"]); store.create_branch(ids["branch_id"], ids["run_id"])
    def handler(args, key):
        EFFECTS.append(dict(args)); return {"ok": True}
    reg = ToolRegistry({"scale_service": Tool(name="scale_service", effect=Effect.WRITE_NONIDEMPOTENT,
          fn=handler, requires_approval=True, arg_policy=policy)})
    loop = Loop(store, saver, llm=ScriptedLLMClient(Stub(args_list)), registry=reg,
                builder=ViewBuilder(system_prompt="sys", registry=reg), dedup=True, outbox=True, tamper=tamper)
    out = loop.start(**ids, task="x")
    if out.status == "waiting_human":
        loop.approve(**ids, decision=decision, approved_args=approve_args, scope=scope)
        out = loop.resume(**ids)
    res = [e.payload for e in store.effective_events(ids["branch_id"]) if e.type == "tool_result"]
    store.close()
    return [str(r.get("error_class")) for r in res], list(EFFECTS)
P = ArgPolicy(field="replicas", min=1, max=5)
r, e = scenario("p1", [{"replicas": 3}], P, tamper=lambda q: ToolCallRequest(tool_call_id=q.tool_call_id, tool=q.tool, args={"replicas": 99}), approve_args={"replicas": 3})
print(f"  P1 审批后改参越界: handler={len(e)} results={r}")
r, e = scenario("p1b", [{"replicas": 3}], P, approve_args={"replicas": 50})
print(f"  P1b 批准了越界参数: handler={len(e)} results={r}")
r, e = scenario("p2", [{"replicas": 3}, {"replicas": 99}], P, scope="session")
print(f"  P2 session 复用: handler={e} results={r}")
b = {"min": ArgPolicy(field="n", min=1, max=5).evaluate({"n": 1})[0],
     "max": ArgPolicy(field="n", min=1, max=5).evaluate({"n": 5})[0],
     "below": not ArgPolicy(field="n", min=1, max=5).evaluate({"n": 0.999})[0],
     "above": not ArgPolicy(field="n", min=1, max=5).evaluate({"n": 5.001})[0],
     "str": not ArgPolicy(field="n", min=1, max=5).evaluate({"n": "3"})[0],
     "bool": not ArgPolicy(field="n", min=1, max=5).evaluate({"n": True})[0],
     "allowed_True": ArgPolicy(field="m", allowed=[1]).evaluate({"m": True})[0]}
print("  边界/类型:", b, "（allowed_True=True 即 P2-8：布尔等价穿透）")
r, e = scenario("reject", [{"replicas": 3}], P, decision="rejected")
print(f"  先拒绝: handler={len(e)} results={r}")
PY
$PY "$PROBES/argpolicy_probe.py" | tee "$EVID/commands_argpolicy.log"

head1 "11. B-G3 哈希链：自算公式 + 改历史 + 删段 + 分叉"
R="$EVID/commands-chain/src"; rm -rf "$EVID/commands-chain"; mkdir -p "$R"
for m in run approve resume; do $PY -m experiments.worker --run-dir "$R" --mode $m > /dev/null 2>&1; done
cat > "$PROBES/chain_verify.py" <<'PY'
import hashlib, json, shutil, sqlite3, subprocess, sys
from pathlib import Path
REPO, SRC = "/tmp/verify-w9", Path("/tmp/w9-evidence/commands-chain/src")
WORK = Path("/tmp/w9-evidence/commands-chain")
FIELDS = ("event_id","run_id","branch_id","seq","kind","type","source","parent_id","payload","created_at")
def row_hash(prev, row):
    record = {k: row[k] for k in FIELDS}
    return hashlib.sha256(prev.encode() + json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
def load(db):
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    rows = [{"event_id": r["event_id"], "run_id": r["run_id"], "branch_id": r["branch_id"], "seq": int(r["seq"]),
             "kind": r["kind"], "type": r["type"], "source": r["source"], "parent_id": r["parent_id"],
             "payload": json.loads(r["payload_json"]), "created_at": float(r["created_at"]),
             "prev_hash": str(r["prev_hash"] or ""), "event_hash": str(r["event_hash"] or "")} for r in con.execute("SELECT * FROM events ORDER BY branch_id, seq")]
    con.close(); return rows
def cli(db):
    p = subprocess.run([f"{REPO}/.venv/bin/python", "-m", "harness.audit_chain", "--db", str(db)],
                       cwd=REPO, capture_output=True, text=True)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {})
rows = load(SRC / "runtime.db")
prev, bad = "0" * 64, 0
for r in rows:
    bad += (r["prev_hash"] != prev) + (row_hash(r["prev_hash"], r) != r["event_hash"]); prev = r["event_hash"]
print(f"  独立按 semantics §2.4.1 重算 {len(rows)} 条：{'逐条一致' if not bad else f'{bad} 处不符'}")
code, out = cli(SRC / "runtime.db")
print(f"  正常链：exit={code} ok={out.get('ok')} events={out.get('events_checked')}")
for label, action in (("UPDATE seq=5 的 payload", "update"), ("DELETE seq=5", "delete")):
    dst = WORK / ("tampered" if action == "update" else "deleted")
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(SRC, dst)
    con = sqlite3.connect(dst / "runtime.db")
    con.execute("DROP TRIGGER IF EXISTS events_no_update"); con.execute("DROP TRIGGER IF EXISTS events_no_delete")
    if action == "update":
        pl = json.loads(con.execute("SELECT payload_json FROM events WHERE seq=5").fetchone()[0])
        pl["__tampered__"] = "attacker"
        con.execute("UPDATE events SET payload_json=? WHERE seq=5", (json.dumps(pl, ensure_ascii=False),))
    else:
        con.execute("DELETE FROM events WHERE seq=5")
    con.commit(); con.close()
    code, out = cli(dst / "runtime.db")
    fb = out.get("first_break") or {}
    print(f"  {label}：exit={code} first_break=seq {fb.get('seq')} code={fb.get('code')}")
PY
$PY "$PROBES/chain_verify.py" | tee "$EVID/commands_chain.log"

head1 "12. B-G4 OTLP：默认输出逐字节不变 + 无顶层 otel import"
git worktree add -q "$EVID/commands-pre-otlp" 151557a 2>/dev/null || true
$PY -c "
import sys, harness.trace
print('  import harness.trace OK；opentelemetry 模块:', [m for m in sys.modules if m.startswith('opentelemetry')] or '无')"
$PY -m harness.trace --run-dir "$R" > "$EVID/commands_trace_after.txt" 2>&1
( cd "$EVID/commands-pre-otlp" && PYTHONDONTWRITEBYTECODE=1 "$PY" -m harness.trace --run-dir "$R" ) > "$EVID/commands_trace_before.txt" 2>&1
if diff -q "$EVID/commands_trace_before.txt" "$EVID/commands_trace_after.txt" > /dev/null; then
  ok "不带 --otlp-endpoint 的输出与交付前（151557a）逐字节相同（$(wc -c < "$EVID/commands_trace_after.txt") 字节）"
else
  bad "默认输出与交付前不一致"
fi

head1 "13. B-G5 artifact GC：四类来源 + dry-run + 共享根误删"
cat > "$PROBES/gc_probe.py" <<'PY'
import json, shutil, sqlite3, sys
from pathlib import Path
sys.path.insert(0, "/tmp/verify-w9")
from harness.artifacts import ArtifactStore, referenced_digests, sweep
BASE = Path("/tmp/w9-evidence/commands-gc")
DDL = """CREATE TABLE events (event_id TEXT PRIMARY KEY, payload_json TEXT);
         CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, state_json TEXT);
         CREATE TABLE checkpoint_writes (idx INTEGER PRIMARY KEY, payload_json TEXT);"""
def run_dir(name, digests, events_only=False):
    d = BASE / name
    if d.exists(): shutil.rmtree(d)
    (d / "artifacts").mkdir(parents=True)
    con = sqlite3.connect(d / "runtime.db"); con.executescript(DDL)
    for i, (src, dig) in enumerate(digests.items()):
        if src == "events":
            con.execute("INSERT INTO events VALUES (?,?)", (f"e{i}", json.dumps({"artifact_ref": {"digest": dig}})))
        elif src == "ckpt":
            con.execute("INSERT INTO checkpoints VALUES (?,?)", (f"c{i}", json.dumps({"ref": {"digest": dig}})))
        else:
            con.execute("INSERT INTO checkpoint_writes VALUES (?,?)", (i, json.dumps({"v": {"digest": dig}})))
    con.commit(); con.close(); return d
E, K, W, O = "1" * 64, "2" * 64, "3" * 64, "4" * 64
d = run_dir("single", {"events": E, "ckpt": K, "writes": W, "orphan": O})
for dig in (E, K, W, O):
    p = d / "artifacts" / dig[:2] / dig; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(dig)
refs, counts = referenced_digests(d)
print(f"  四类来源枚举: {len(refs)} 个引用 {counts}；孤儿未被算引用={O not in refs}")
rep = sweep(ArtifactStore(d / "artifacts"), run_dir=d, dry_run=True)
print(f"  dry-run: orphans={rep.orphans_found} kept={rep.kept_referenced} 文件仍在={((d/'artifacts'/O[:2]/O).exists())}")
rep2 = sweep(ArtifactStore(d / "artifacts"), run_dir=d, dry_run=False)
left = sorted(p.name[:4] for p in (d / "artifacts").rglob("*") if p.is_file())
print(f"  apply: deleted={len(rep2.deleted)} 剩余={left}")
# 共享 artifacts 根：从 runA sweep，runB 仍引用的对象会不会被删？
shared = BASE / "shared"; 
runA = run_dir("runA", {"events": E}); runB = run_dir("runB", {"events": K})
if shared.exists(): shutil.rmtree(shared)
shared.mkdir(parents=True)
for dig in (E, K):
    (shared / dig).write_text(dig)
sweep(ArtifactStore(shared), run_dir=runA, dry_run=False)
print(f"  共享根 sweep(runA) 后：digestA 存在={(shared/E).exists()} digestB（runB 引用）存在={(shared/K).exists()}"
      f" → {'P1-3 复现' if not (shared/K).exists() else '未复现'}")
PY
$PY "$PROBES/gc_probe.py" | tee "$EVID/commands_gc.log"

head1 "14. B-G6 租约：两个真进程 + 过期"
cat > "$PROBES/lease_probe.py" <<'PY'
import json, shutil, subprocess, sys, time
from pathlib import Path
REPO, WORK = "/tmp/verify-w9", Path("/tmp/w9-evidence/commands-lease")
RUN = WORK / "run"
HELPER = r'''
import json, sys; from pathlib import Path
sys.path.insert(0, "__REPO__")
from harness.store.sqlite_store import SqliteStore
from harness.lease import SingleWriterLease, LeaseNotHeld
run_dir, owner, action, ttl = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
store = SqliteStore(Path(run_dir) / "runtime.db"); store.setup()
ids = json.loads((Path(run_dir) / "ids.json").read_text())
lease = SingleWriterLease(store, run_id=ids["run_id"], branch_id=ids["branch_id"], owner=owner)
out = {"action": action}
try:
    if action == "acquire": out.update(ok=True, expires_at=lease.acquire(ttl_seconds=ttl).expires_at)
    elif action == "require": out.update(ok=True, lease_id=lease.require().lease_id)
    elif action == "renew": out.update(ok=True, lease_id=lease.renew(ttl_seconds=ttl).lease_id)
except LeaseNotHeld as exc:
    out.update(ok=False, error=type(exc).__name__, reason=str(exc).split("：")[0])
store.close(); print(json.dumps(out, ensure_ascii=False))
'''
def call(owner, action, ttl=60.0):
    p = subprocess.run([f"{REPO}/.venv/bin/python", "-c", HELPER.replace("__REPO__", REPO),
                        str(RUN), owner, action, str(ttl)], cwd=REPO, capture_output=True, text=True)
    for line in reversed(p.stdout.strip().splitlines()):
        try: return json.loads(line)
        except json.JSONDecodeError: continue
    return {"error": "no-output"}
if WORK.exists(): shutil.rmtree(WORK)
RUN.mkdir(parents=True)
subprocess.run([f"{REPO}/.venv/bin/python", "-m", "experiments.worker", "--run-dir", str(RUN), "--mode", "run"],
               cwd=REPO, capture_output=True, text=True)
print("  未持有 require:", call("worker-a", "require"))
print("  A acquire:", call("worker-a", "acquire")["ok"])
print("  B（他人）require:", call("worker-b", "require").get("reason"))
print("  B renew（不做隐式接管）:", call("worker-b", "renew").get("error"))
call("worker-a", "acquire", ttl=0.05); time.sleep(0.4)
print("  过期后 require:", call("worker-a", "require").get("reason"))
PY
$PY "$PROBES/lease_probe.py" | tee "$EVID/commands_lease.log"

head1 "15. 非回归（合并门槛 1–5）"
$PY -m pytest -o addopts= -p no:cacheprovider -q > "$EVID/commands_pytest.log" 2>&1
expect_exit 0 $? "pytest 全绿（$(tail -1 "$EVID/commands_pytest.log")）"
$PY -m pytest -p no:cacheprovider -q -m live >> "$EVID/commands_pytest.log" 2>&1
expect_exit 0 $? "pytest -m live（无 key 下的反例测试）"
PYTHONDONTWRITEBYTECODE=1 "$CHECKOUT/.venv/bin/ruff" check . > "$EVID/commands_ruff.log" 2>&1
expect_exit 0 $? "ruff 全净"
$PY -m opsenv.suite --per-fault 8 --repeats 3 --gate --json-out "$EVID/commands_eval.json" > "$EVID/commands_eval.log" 2>&1
expect_exit 0 $? "opsenv.suite --gate（14 条）"
$PY -m experiments.crash_matrix --repeats 5 --json-out "$EVID/commands_matrix.json" --md-out /dev/null > "$EVID/commands_matrix.log" 2>&1
expect_exit 0 $? "crash_matrix --repeats 5（16 格 as-predicted）"
$PY - <<'PY'
import json
new = json.load(open("/tmp/w9-evidence/commands_matrix.json")); old = json.load(open("reports/w4_crash_matrix.json"))
same_sum = json.dumps(new["summary_kills"], sort_keys=True) == json.dumps(old["summary_kills"], sort_keys=True)
diff = [k for k in new["summary_by_cell"] if json.dumps(new["summary_by_cell"][k], sort_keys=True)
        != json.dumps(old["summary_by_cell"].get(k), sort_keys=True)]
print(f"  新跑 vs 入库：summary_kills 相同={same_sum} 逐格差异={len(diff)}")
ev, oldev = json.load(open("/tmp/w9-evidence/commands_eval.json")), json.load(open("reports/w6_eval.json"))
r1 = {(c["system"], c["profile"]): c["rates"] for c in ev["cells"]}
r2 = {(c["system"], c["profile"]): c["rates"] for c in oldev["cells"]}
print(f"  w6_eval 8 格 rates 相同={r1 == r2}")
PY

head1 "汇总"
echo "PASS=$PASS  FAIL=$FAIL"
echo "原始证据：$EVID"
echo "注意：注入类实验都在 $CHECKOUT 副本里做并已还原；被审计仓库未被修改。"
[ "$FAIL" = "0" ] || exit 1
