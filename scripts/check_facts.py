"""claim 对账：把文档引用的结论数字与它们的**可再生命令**绑定，逐条重算并比对。

为什么需要它：历史 8 个 P2 里有 7 个是"改了代码没回灌文档"——数字以叙述文本的形式
存在于 README / HANDOFF / docs 里，没有任何机制能发现它已经过期。本脚本把每个被引用的
数字变成一条 claim（值 + 容差 + 再生命令 + JSON 路径 + 引用位置），由 CI 逐条对账。

用法::

    .venv/bin/python scripts/check_facts.py                 # 全量（含夜间重 claim）
    .venv/bin/python scripts/check_facts.py --run verify    # 只跑轻 claim（CI verify job）
    .venv/bin/python scripts/check_facts.py --id crash-matrix-real-kills
    .venv/bin/python scripts/check_facts.py --list          # 只列 claim，不执行

纪律：

* claim 的 ``source_cmd`` 必须携带 ``{out}`` 写盘占位符；实际输出路径由本脚本决定
  （默认 ``/tmp/facts/cmd-<hash>.json``），**绝不写进仓库**；
  同一条命令只执行一次，多条 claim 共享同一次执行结果。
* ``source_path`` 逐层取 JSON：字符串是 dict 键、整数是 list 下标。
* 数值按 ``|actual - value| <= tolerance`` 判过；字符串/布尔必须完全相等。
* 缺字段、``source_path`` 为空、``run`` 取值非法 ⇒ 直接判失败（退出 1），
  不给"没绑定也算过"留后门。
* ``docs`` 里的引用位置（``路径#锚点``）必须**指向真实存在的文件与逐字出现的锚点**——
  评审 P1-4：这条挡的是"数字改对了、引用位置没跟着改"，也顺手把
  P0-2（README 的数字与入库基线矛盾而门禁全绿）那类缺陷变成可判定的。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FACTS = PROJECT_ROOT / "reports" / "documented-facts.json"
DEFAULT_OUTDIR = Path("/tmp/facts")
REQUIRED_KEYS = ("id", "value", "tolerance", "run", "source_cmd", "source_path", "what", "docs")
VALID_RUNS = ("verify", "nightly")


class ClaimError(RuntimeError):
    """claim 文件本身不合法（不是"数字漂移"，而是"绑定没写好"）。"""


def load_claims(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise ClaimError(f"{path}: 顶层缺少 claims 数组")
    problems: list[str] = []
    seen: set[str] = set()
    for index, claim in enumerate(claims):
        label = str(claim.get("id") or f"<第 {index} 条无 id>")
        for key in REQUIRED_KEYS:
            if key not in claim:
                problems.append(f"{label}: 缺字段 {key!r}")
        if label in seen:
            problems.append(f"{label}: claim id 重复")
        seen.add(label)
        if not claim.get("source_path"):
            problems.append(f"{label}: source_path 为空——空路径等于没有绑定，无法对账")
        if not claim.get("source_cmd"):
            problems.append(f"{label}: source_cmd 为空")
        elif "{out}" not in claim["source_cmd"]:
            problems.append(f"{label}: source_cmd 未携带 {{out}} 写盘占位符")
        elif claim["source_cmd"][0] != "{python}":
            problems.append(
                f"{label}: source_cmd 必须以 {{python}} 开头（本地 .venv 与 CI 解释器不同）"
            )
        if claim.get("run") not in VALID_RUNS:
            problems.append(f"{label}: run 必须是 {VALID_RUNS} 之一")
        tolerance = claim.get("tolerance")
        if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool):
            problems.append(f"{label}: tolerance 必须是数字")
        elif tolerance < 0:
            problems.append(f"{label}: tolerance 不能为负")
        if not claim.get("docs"):
            problems.append(f"{label}: docs 为空——没有引用位置的 claim 无法回灌")
        else:
            for entry in claim["docs"]:
                problems.extend(_check_doc_reference(label, entry))
    if problems:
        raise ClaimError("\n".join(problems))
    return claims


def _check_doc_reference(label: str, entry: str) -> list[str]:
    """``路径#锚点`` 必须指到真文件与真锚点（评审 P1-4）。

    只校验"文件存在 + 锚点逐字出现"：不做语义比对（那要靠人），但"引用位置写错"
    这一类（改了数字没改引用、引用了一个被删掉的段落）从此会红。
    """
    problems: list[str] = []
    path_part, _, anchor = str(entry).partition("#")
    if not path_part:
        return [f"{label}: docs 引用缺少路径：{entry!r}"]
    target = PROJECT_ROOT / path_part
    if not target.exists():
        return [f"{label}: docs 引用的文件不存在：{path_part}（写全相对仓库根的路径）"]
    if anchor and anchor not in target.read_text(encoding="utf-8"):
        problems.append(
            f"{label}: docs 锚点在 {path_part} 里找不到：{anchor!r}"
            "（锚点必须是文件里逐字出现的片段）"
        )
    return problems


def command_key(source_cmd: list[str]) -> str:
    """同一条命令（未替换 {out} 前）共享一次执行与一个输出文件。"""
    blob = "\x1f".join(source_cmd)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def resolve_cmd(source_cmd: list[str], out_path: Path) -> list[str]:
    """替换占位符：``{out}`` = 输出路径，``{python}`` = 当前解释器。

    ``{python}`` 的存在是为了本地与 CI 用同一条 claim：本地是 ``.venv/bin/python``，
    CI 是系统解释器（``pip install -e .[dev,eval]`` 装进当前环境）。
    启动命令必须显式写 ``{python}``，否则 claim 被 load_claims 判为不合法。
    """
    return [
        part.replace("{out}", str(out_path)).replace("{python}", sys.executable)
        for part in source_cmd
    ]


def walk(payload: Any, path: list[Any], *, label: str) -> Any:
    current = payload
    walked: list[str] = []
    for step in path:
        if isinstance(current, list):
            try:
                index = int(step)
            except (TypeError, ValueError) as exc:
                raise ClaimError(
                    f"{label}: 路径 {walked} 处遇到 list，但下一层 {step!r} 不是整数下标"
                ) from exc
            if not -len(current) <= index < len(current):
                raise ClaimError(f"{label}: 路径 {walked} 下标 {index} 越界（长度 {len(current)}）")
            current = current[index]
        elif isinstance(current, dict):
            if step not in current:
                raise ClaimError(
                    f"{label}: 路径 {walked} 处找不到键 {step!r}；"
                    f"可用键: {sorted(current)[:12]}"
                )
            current = current[step]
        else:
            raise ClaimError(f"{label}: 路径 {walked} 已到标量，无法继续取 {step!r}")
        walked.append(str(step))
    return current


def compare(expected: Any, actual: Any, tolerance: float) -> tuple[bool, str]:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual, f"期望 {expected!r}，实测 {actual!r}"
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        deviation = float(actual) - float(expected)
        ok = abs(deviation) <= float(tolerance)
        return (
            ok,
            f"期望 {expected}±{tolerance}，实测 {actual}（偏离 {deviation:+.6g}）",
        )
    ok = expected == actual
    return ok, (f"期望 {expected!r}，实测 {actual!r}" if not ok else "相等")


def run_claim(
    claim: dict[str, Any],
    *,
    outdir: Path,
    cache: dict[str, tuple[int, str, str]],
    timeout: float,
) -> dict[str, Any]:
    label = claim["id"]
    out_path = outdir / f"cmd-{command_key(claim['source_cmd'])}.json"
    cmd = resolve_cmd(claim["source_cmd"], out_path)
    if " ".join(cmd) not in cache:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)  # 绝不读到上一轮的残留
        try:
            proc = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # 超时按"这一条失败"处理：整轮 traceback 虽然退出码非零（不会假绿），
            # 但拿不到逐条报表，排查时不知道是"数字漂了"还是"命令挂了"（评审 P2-10）
            cache[" ".join(cmd)] = (
                124,
                "",
                f"再生命令超过 {timeout:.0f}s 未完成（timeout）",
            )
        else:
            cache[" ".join(cmd)] = (proc.returncode, proc.stdout, proc.stderr)
    code, _stdout, stderr = cache[" ".join(cmd)]

    record: dict[str, Any] = {
        "id": label,
        "run": claim["run"],
        "what": claim["what"],
        "docs": claim["docs"],
        "command": cmd,
        "source_path": claim["source_path"],
        "expected": claim["value"],
        "tolerance": claim["tolerance"],
    }
    if code != 0:
        record.update(
            {
                "ok": False,
                "actual": None,
                "detail": f"再生命令退出码 {code}；stderr 尾部: {stderr.strip()[-300:]}",
            }
        )
        return record
    if not out_path.exists():
        record.update(
            {
                "ok": False,
                "actual": None,
                "detail": f"命令未写出 {out_path}（source_cmd 缺写盘参数？）",
            }
        )
        return record
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        record.update({"ok": False, "actual": None, "detail": f"{out_path} 不是合法 JSON: {exc}"})
        return record
    try:
        actual = walk(payload, claim["source_path"], label=label)
    except ClaimError as exc:
        record.update({"ok": False, "actual": None, "detail": str(exc)})
        return record
    ok, detail = compare(claim["value"], actual, float(claim["tolerance"]))
    record.update({"ok": ok, "actual": actual, "detail": detail})
    return record


def check(
    claims: list[dict[str, Any]],
    *,
    outdir: Path = DEFAULT_OUTDIR,
    timeout: float = 900.0,
) -> list[dict[str, Any]]:
    cache: dict[str, tuple[int, str, str]] = {}
    return [run_claim(claim, outdir=outdir, cache=cache, timeout=timeout) for claim in claims]


def render_markdown(records: list[dict[str, Any]]) -> str:
    lines = [
        "| claim | job | 期望 | 实测 | 结果 | 引用位置 |",
        "|---|---|---|---|---|---|",
    ]
    for record in records:
        mark = "通过" if record["ok"] else "**失败**"
        docs = "、".join(record["docs"])
        lines.append(
            f"| `{record['id']}` | {record['run']} | {record['expected']} |"
            f" {record['actual']} | {mark} | {docs} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/check_facts.py")
    parser.add_argument("--facts", default=str(DEFAULT_FACTS))
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    parser.add_argument("--run", choices=("verify", "nightly", "all"), default="all")
    parser.add_argument("--id", action="append", default=[], help="只对账指定 claim（可重复）")
    parser.add_argument("--list", action="store_true", help="只列出 claim，不执行命令")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    try:
        claims = load_claims(Path(args.facts))
    except ClaimError as exc:
        print("claim 文件不合法（未执行任何命令）：")
        print(str(exc))
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取 claim 文件 {args.facts}: {exc}")
        return 1

    if args.run != "all":
        claims = [claim for claim in claims if claim["run"] == args.run]
    if args.id:
        wanted = set(args.id)
        unknown = wanted - {claim["id"] for claim in claims}
        if unknown:
            print(f"未知 claim id: {sorted(unknown)}")
            return 1
        claims = [claim for claim in claims if claim["id"] in wanted]

    if args.list:
        for claim in claims:
            print(f"{claim['run']:8s} {claim['id']:38s} {claim['what']}")
        print(f"共 {len(claims)} 条")
        return 0

    if not claims:
        print("没有要跑的 claim（--run/--id 过滤后为空）")
        return 0

    records = check(claims, outdir=Path(args.outdir), timeout=args.timeout)
    print(render_markdown(records))
    failed = [record for record in records if not record["ok"]]
    for record in failed:
        print(f"\n[失败] {record['id']}：{record['what']}")
        print(f"  命令: {' '.join(record['command'])}")
        print(f"  路径: {record['source_path']}")
        print(f"  {record['detail']}")
        print(f"  引用: {'、'.join(record['docs'])}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "total": len(records),
                    "passed": len(records) - len(failed),
                    "failed": len(failed),
                    "claims": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(f"\nclaim 对账：{len(records) - len(failed)}/{len(records)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
