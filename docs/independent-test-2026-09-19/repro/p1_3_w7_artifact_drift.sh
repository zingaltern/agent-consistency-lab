#!/bin/sh
# P1-3 最小复现：W7 的成本数字还再生得出来吗？
#
#   sh docs/independent-test-2026-09-19/repro/p1_3_w7_artifact_drift.sh
#
# 做法：把入库产物 `reports/w7_sweep.json` 与"按 claim 里的再生命令重跑一次"的结果
# 逐位比较。确定性实验的 `findings` 段应当完全一致。
#
# **修复前会怎样**：两者不同——入库产物是旧代码跑出来的，文档引用的数字
# （$0.11834 / 选点 0.70 / +40%）在**当前代码**上再生不出来。
# 根因不是"忘了重跑"，而是这 9 条 claim 当时只在 CI 的 nightly 作业里对账，
# push 上没有人看得见（升到 verify 后，同样的漂移会在 PR 阶段变红）。
#
# 修复后：exit 0。
set -eu

cd "$(dirname "$0")/../../.."
OUT="$(mktemp -d)/w7_sweep.json"

.venv/bin/python -m experiments.context_sweep --repeats 2 --json-out "$OUT" >/dev/null

.venv/bin/python - "$OUT" <<'PY'
import json, sys, pathlib

fresh = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
stored = json.loads(pathlib.Path("reports/w7_sweep.json").read_text(encoding="utf-8"))

def flat(prefix, node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            flat(f"{prefix}.{key}" if prefix else key, value, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            flat(f"{prefix}[{index}]", value, out)
    else:
        out[prefix] = node
    return out

fresh = {k: v for k, v in flat("", fresh, {}).items() if not k.endswith("wall_ms")}
stored = {k: v for k, v in flat("", stored, {}).items() if not k.endswith("wall_ms")}

drift = {k: (stored.get(k), fresh.get(k)) for k in sorted(set(fresh) | set(stored))
         if stored.get(k) != fresh.get(k)}
if drift:
    print(f"漂移 {len(drift)} 处（入库值 vs 重跑值）：")
    for key, (old, new) in list(drift.items())[:20]:
        print(f"  {key}: {old!r} -> {new!r}")
    print("\n失败：入库产物相对当前代码是陈旧的")
    raise SystemExit(1)
print(f"通过：{len(fresh)} 个字段（除计时）逐位一致，产物可再生")
PY
