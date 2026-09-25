#!/usr/bin/env bash
# 独立黑盒测试的一键入口（四套基线命令）。
#
# 纪律：
#   * **仓库只读**——本脚本不写仓库里的任何文件；产物全部落 $OUT（默认 /tmp 下）。
#   * 退出码单独打印**并**汇总：管道/重定向会吞退出码（docs/testing.md §2 末段的历史事故）。
#   * 末尾自检 `git status --porcelain` 是否干净——不是"我们相信没写进仓库"，而是"核对过"。
#   * 本脚本**只是起点**：必测项 A–F 见 docs/tester-prompt.md §3，基线全绿不等于测试完成。
#
# 用法：
#   bash docs/independent-test-2026-09-26/commands.sh [输出目录]
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-/tmp/independent-test-2026-09-26}"
PY="$REPO/.venv/bin/python"

mkdir -p "$OUT"
cd "$REPO" || exit 2
echo "仓库: $REPO"
echo "产物: $OUT"
echo

declare -a NAMES=() CODES=()

run() {
  local name="$1"; shift
  echo "=== $name ==="
  echo "命令: $*"
  "$@" >"$OUT/$name.log" 2>&1
  local code=$?
  NAMES+=("$name"); CODES+=("$code")
  echo "退出码: $code（输出尾部）"
  tail -n 5 "$OUT/$name.log" | sed 's/^/    /'
  echo
}

run pytest   "$REPO/.venv/bin/pytest" -o addopts= -p no:cacheprovider -q
run ruff     "$REPO/.venv/bin/ruff" check .
run facts    "$PY" scripts/check_facts.py --run verify --json-out "$OUT/facts-verify.json"
run crash-matrix "$PY" -m experiments.crash_matrix --repeats 5 \
    --json-out "$OUT/matrix.json" --md-out "$OUT/matrix.md"

echo "=== 退出码汇总 ==="
FAILED=0
for i in "${!NAMES[@]}"; do
  printf '  %-14s %s\n' "${NAMES[$i]}" "${CODES[$i]}"
  [ "${CODES[$i]}" -ne 0 ] && FAILED=$((FAILED + 1))
done
echo
echo "用例数（口径：claims 的再生命令，正文不手写）：$PY scripts/count_tests.py --json-out $OUT/tests-count.json"
"$PY" scripts/count_tests.py --json-out "$OUT/tests-count.json" >/dev/null 2>&1 || true

echo
echo "=== 仓库只读自检（git status --porcelain 应为空） ==="
DIRTY="$(git status --porcelain)"
if [ -n "$DIRTY" ]; then
  echo "**警告**：工作区有改动，说明有东西写进了仓库："
  echo "$DIRTY"
elif [ "$FAILED" -gt 0 ]; then
  echo "工作区干净，但有 $FAILED 条基线命令非零退出——**逐条查清，不要跳过**。"
  exit 1
else
  echo "工作区干净，四套基线全绿。"
fi
echo
echo "下一步：按 docs/tester-prompt.md §3 的 A–F 自己设计测试；报告写进"
echo "       docs/independent-test-<你的执行日期>/report.md（骨架见本目录 report.md）。"
