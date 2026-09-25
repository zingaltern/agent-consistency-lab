"""变异测试门禁（R-A5）：把"测试盲区"变成一条会红的门禁。

语义：**幸存的变异 = 测试盲区**。变异测试与 `tests/test_audit_regressions.py` 互补——
审计回归防已知的 P0 复活，变异测试探没人写过的分支。

为什么是"防倒退基线"而不是"追求某个绝对值"：
* 这几个模块里相当一部分路径由**子进程驱动**的崩溃矩阵与四系统评测覆盖（`experiments/`、
  `opsenv/`），它们不在 `pytest` 进程内，mutmut 看不见——因此幸存清单里必然包含
  "其实被更外层验证保护着"的变异。把幸存率当绝对质量分会得出错误结论。
* 真正要防的是：**新增代码把盲区扩大**。所以首版把当前幸存清单冻结成基线
  （`reports/mutation_baseline.json`），之后只判"有没有**新增**幸存变异"。

## 全状态记账（2026-09-18 修，来源：独立验证报告 P0-1 / P0-2）

首版判据只看本轮 `survived` 里有没有新名字。这有两个后果，都被独立验证的**反例**证伪过：

* **P0-1｜"缺项"被当成好消息。** 任何**从本轮结果里消失**的基线幸存变异
  （未评估 / `segfault` / `timeout` / `no tests`）都会落进 `fixed`，
  被打印成"已被杀死（好事）"并**退出 0**。判据结构上缺项**不可能**导致红灯。
  活体场景：`harness/store/checkpoints.py` 逐字未改，76 条基线幸存变异在某轮里
  被 mutmut 改判成 `segfault`——`segfault` 原先是"不计入任何计数"的垃圾桶。
* **P0-2｜抓不到"新增的、完全没被测的代码"。** 新增的变异体被判为 `no tests`，
  而判据只看 `survived` ⇒ 覆盖率为零的新代码**不会变红**，与 AGENTS.md 红线 5 冲突。

因此本脚本现在把 mutmut 的**全部状态**分三类记账，**不再静默丢弃任何一个**：

| 类 | 状态 | 含义 |
|---|---|---|
| 判定类 | `killed` / `survived` | 真的得到了判决 |
| 未覆盖类 | `no tests` | 连覆盖都没有（另一类盲区） |
| 无结论类 | 见下 | 跑了但**判决不可信**，或根本没判决 |

无结论类 = `segfault` / `timeout` / `suspicious` / `skipped` / `not checked` /
`caught by type check` / `check was interrupted by user`
（名单在代码里是 `INCONCLUSIVE_STATUSES`，来自 `mutmut/stats.py::status_by_exit_code`）。

后两类合起来是**不可见空间**：它们不参与 `survivor_rate`，每次运行都打印其规模——
`survivor_rate` **不是覆盖率**，别读成覆盖率。

### 六条红灯条件（2026-09-18 起；第七条见下）

1. 新增幸存变异（保留首版行为；基线里 `killed` 的变异体变成 `survived` 也算新增）；
2. 基线里是**判定类**的变异体，本轮变成**非判定类**（P0-1 的活体场景）；
3. 基线里的变异体本轮**完全缺失**（未评估 / 改名 / 缓存缺项）；
4. 新增 `no tests`（P0-2：对"新增的、完全没被测的代码"不敏感）；
5. 本轮**没有任何变异体**、或**没有任何一条被判定的变异体**（"跑不起来"≠"没有盲区"）；
6. **无结论集合较基线增长**（有新名字进入不可见空间）。

外加一条 fail-closed：出现**本脚本不认识的状态名** ⇒ 判失败（mutmut 升版带来的新分类
必须先被归类，否则就是新一轮"静默丢数据"）。

### 第七条：同名不同指纹（2026-09-26 加）

判据只按"名字 + 状态"对账时，有一格是空的（独立验证 2026-09-18 · 报告 §5-3）：
同一函数体内的**等量改写**（常数改值、语句换序等既不增删条数、也不移位编号的改动）会让
同一批名字指向**不同**的变异，而门禁看不出差别——可能**静默绿**。
现在基线为每个变异体存一条**内容指纹**（`sha256` of 规范化后的变异 diff），
对账时"同名不同指纹"报 `[mutant-content-changed]` 并退出 1。

指纹的口径（两句话）：

* 内容 = mutmut 自己渲染的变异函数 diff（`get_diff_for_mutant`，与 `mutmut show` 的
  diff 部分**逐字相同**，本机实测 40 条样本零差异）；取它在**进程内**算，
  2050 条约 17 秒——用 `mutmut show` 起子进程要 219 ms/条（2050 条约 448 秒），
  且逐条重扫 `find_mutant`；
* 规范化 = 逐行去尾空格 + 丢弃首尾空行（行尾空格与文件末尾换行不构成"内容变了"）。
  指纹只回答"这条变异还是不是原来那条"，**不是**质量分。

指纹拿不到时（`mutants/` 缺文件、`.meta` 里没有这个名字）如实记 `None` 并打印条数：
**不比对、不假装比对过**。刷新基线时若一条都取不到，直接判失败（不许写一份没有指纹的基线，
那等于把这条判据静默关掉）。


用法::

    .venv/bin/python scripts/mutation_check.py                     # 判定（CI nightly 用）
    .venv/bin/python scripts/mutation_check.py --update-baseline   # 显式刷新基线（PR 里说明）
    .venv/bin/python scripts/mutation_check.py --module harness/approval.py --timeout 300
    .venv/bin/python scripts/mutation_check.py --module "harness.approval.*" --timeout 300

纪律：

* **超时即失败**：跑不完不许当成功（"没跑完"和"没发现回归"是两件事）；
* 新增幸存变异 → 退出 1，并逐条打印 `mutmut show` 的 diff（失败必须可读）；
* 基线只能由 `--update-baseline` 改写，且必须在 PR 描述里写明为什么放宽；
* **`--update-baseline` 必须从干净缓存开始**：`mutants/` 还在就整体移开
  （见 `baseline_refresh_plan`）。要沿用旧缓存得显式写 `--allow-incremental-refresh`，
  并且它会大声警告——2026-09-19 实测过一次"混合了旧判决的假基线"：
  14.7 秒"跑完"、`no tests` 从 18 虚增到 241。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = PROJECT_ROOT / "reports" / "mutation_baseline.json"
# mutmut 的**增量缓存**：`mutmut run` 只在函数哈希变化时重跑变异体，其余沿用旧判决。
# 刷新基线时它必须整体移开（`baseline_refresh_plan`），否则写出的基线是混合物。
MUTANTS_DIR = PROJECT_ROOT / "mutants"
# 顺序与 pyproject.toml [tool.mutmut].only_mutate 一致（harness/execution.py 是七步管线的
# 实现所在：设计文档 C 抽取后必须一并纳入，否则管线失去变异覆盖而门禁不会变红）。
DEFAULT_MODULES = (
    "harness/execution.py",
    "harness/loop.py",
    "harness/store/checkpoints.py",
    "harness/approval.py",
)

# 三类划分。mutmut 的编号在 mutmut/stats.py::status_by_exit_code；这里按**状态名**归类，
# 不按退出码——名称是 mutmut 的公开面，退出码是实现细节。
DECIDED_STATUSES = ("killed", "survived")
UNCOVERED_STATUSES = ("no tests",)
INCONCLUSIVE_STATUSES = (
    "segfault",
    "timeout",
    "suspicious",
    "skipped",
    "not checked",
    "caught by type check",
    "check was interrupted by user",
)
KNOWN_STATUSES = DECIDED_STATUSES + UNCOVERED_STATUSES + INCONCLUSIVE_STATUSES

# 内容指纹的算法标识：写进基线，**指纹换了口径必须换这个名字**——
# 否则新旧基线会被当成"内容变了"而集体误报（判据要说得出自己是怎么算的）。
FINGERPRINT_ALGORITHM = "sha256(normalized-mutmut-diff)/v1"


@contextmanager
def _chdir(path: Path) -> Iterator[None]:
    """临时切到 ``path``：mutmut 的读函数按**当前工作目录**找 ``mutants/``。

    `get_diff_for_mutant` / `MutantLineSpans.load` 内部写死了 ``Path("mutants") / path``，
    给不了目录参数。本脚本其余部分（`mutmut run` / `mutmut results`）本来就以
    `cwd=PROJECT_ROOT` 起子进程，所以在同一目录里读是一致的；离开时必还原。
    """
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def mutant_paths_in_cache(mutants_dir: Path) -> dict[str, Path]:
    """变异体名 → 变异文件路径（相对 ``mutants/``）。

    映射取自 `mutants/**/*.meta` 里的 `exit_code_by_key`：那是 mutmut 自己写下的账本
    （`find_mutant` 也按它走）。**不用名字反推路径**（`harness.approval.xǁ…` →
    `harness/approval.py`）——那是把 mutmut 的命名约定再抄一遍，抄错就是静默错位。
    """
    mapping: dict[str, Path] = {}
    if not mutants_dir.exists():
        return mapping
    for meta_path in sorted(mutants_dir.glob("**/*.meta")):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        relative = meta_path.relative_to(mutants_dir).with_suffix("")
        for name in payload.get("exit_code_by_key", {}):
            mapping[str(name)] = relative
    return mapping


def normalize_fingerprint_source(diff: str) -> str:
    """规范化：逐行去尾空格 + 丢弃首尾空行。

    行尾空格与文件末尾换行不构成"变异内容变了"；反过来，任何真实的代码改写
    （常数改值、条件取反、语句换序）都会改变规范化文本 ⇒ 指纹改变。
    """
    return "\n".join(line.rstrip() for line in diff.splitlines()).strip()


def compute_mutant_fingerprints(
    names: list[str] | set[str], *, mutants_dir: Path | None = None
) -> dict[str, str | None]:
    """给每条变异体算内容指纹；取不到就记 ``None``（**绝不编一个**）。

    内容取自 mutmut 自己的渲染（`get_diff_for_mutant`），因此与 `mutmut show` 的 diff 部分
    逐字相同——本机实测 40 条样本零差异，且同一条重复调用输出逐字相同。
    取不到的三类原因都落到 ``None``：`mutants/` 不存在、名字不在任何 `.meta` 里、
    渲染时读文件失败；调用方负责把"有几条取不到"打印出来。
    """
    from mutmut.mutation.diff_apply import get_diff_for_mutant  # 懒 import：--summary-only 不需要

    mutants_dir = Path(mutants_dir or MUTANTS_DIR)
    if not mutants_dir.exists():
        return {name: None for name in names}
    if mutants_dir.name != "mutants":
        raise ValueError(
            f"{mutants_dir} 不叫 `mutants`：mutmut 的读函数按当前目录找 `mutants/`，"
            "换个名字会让指纹静默全空"
        )
    paths = mutant_paths_in_cache(mutants_dir)
    fingerprints: dict[str, str | None] = {}
    with _chdir(mutants_dir.parent):
        for name in sorted(names):
            relative = paths.get(name)
            if relative is None:
                fingerprints[name] = None
                continue
            try:
                diff = get_diff_for_mutant(name, path=str(relative))
            except Exception:  # 渲染失败 ⇒ 如实记 None（下面会打印条数）
                fingerprints[name] = None
                continue
            digest = hashlib.sha256(
                normalize_fingerprint_source(diff).encode("utf-8")
            ).hexdigest()
            fingerprints[name] = f"sha256:{digest}"
    return fingerprints


def fingerprint_report(fingerprints: dict[str, str | None]) -> list[str]:
    """要打印的指纹口径说明（每次运行都打，免得被读成"内容肯定没变"）。"""
    total = len(fingerprints)
    readable = sum(1 for value in fingerprints.values() if value)
    lines = [
        f"[mutation] 内容指纹（{FINGERPRINT_ALGORITHM}）：{readable}/{total} 条可读"
    ]
    if readable < total:
        lines.append(
            f"  ⚠️ {total - readable} 条取不到内容（mutants/ 缺文件或 .meta 里没有这个名字）："
            "这些条目**不参与**指纹比对，不假装比对过。"
        )
    return lines


def fingerprint_comparison_lines(
    fingerprints: dict[str, str | None], baseline_fingerprints: dict[str, str]
) -> list[str]:
    """"这一轮到底比了几条"——**必须分两种说法**：基线没有 vs 本轮取不到。

    两种形态的处置不同：前者跑一次 `--update-baseline` 就有得比了；后者说明这一轮
    `mutants/` 侧取不到内容，得先修缓存。把两者混成一句"没有可比对的指纹"会指向错的处置。
    """
    compared = sum(
        1 for name, value in fingerprints.items() if value and baseline_fingerprints.get(name)
    )
    if compared:
        return [f"  与基线逐条比对：{compared} 条同名可比对。"]
    if baseline_fingerprints:
        return [
            "  ⚠️ 本轮没有任何条目能与基线比对（当前侧取不到内容）："
            "**只记账、不判内容**——这不是「内容没变」，而是「这一格没在看」。"
        ]
    return [
        "  ⚠️ 基线里没有可比对的指纹（旧基线或刚刷新）：本轮**只记账、不判内容**；"
        "跑一次 `--update-baseline` 即可让这条判据生效。"
    ]



def normalize_module_filter(value: str) -> str:
    """把 ``--module`` 的参数翻译成 `mutmut run` 的位置参数。

    `mutmut run` 的位置参数是**变异体名**的 fnmatch 模式（`harness.approval.xǁ…__mutmut_1`），
    不是文件路径。首版把 `DEFAULT_MODULES` 里的**路径**直接传进去，于是
    `--module harness/approval.py` 恒失败（`AssertionError: Filtered for specific mutants,
    but nothing matches`）——独立验证报告 P1-1。

    这里接受两种写法：文件路径（`harness/approval.py` → `harness.approval.*`）与
    变异体名 glob（原样传下去，例如 `harness.approval.*` 或某一条完整的变异体名）。
    """
    value = value.strip()
    if not value:
        return value
    if value.endswith(".py") or "/" in value:
        # 只按"以 .py 结尾"或"含路径分隔符"判定为路径；其余（含 `.` 的 glob）原样透传。
        parts = list(Path(value).with_suffix("").parts)
        return ".".join(parts) + ".*"
    return value


def _mutmut_argv(module: str | None) -> list[str]:
    argv = [sys.executable, "-m", "mutmut", "run"]
    if module:
        argv.append(module)
    return argv


def baseline_refresh_plan(
    *, update_baseline: bool, allow_incremental: bool, mutants_dir: Path, stamp: str
) -> dict[str, Any]:
    """``--update-baseline`` 之前该做什么。**纯函数**：只看入参，不碰文件系统。

    为什么需要它（2026-09-19 实测）：``mutmut run`` 是**增量**的——``mutants/`` 缓存还在时
    它只重跑"函数哈希变了"的变异体，其余直接沿用旧判决。于是改过代码之后直接
    ``--update-baseline``，写出的是一份**混合了旧判决**的基线：本轮实测过一次，
    14.7 秒"跑完"、``no tests`` 从 18 虚增到 241——那不是基线，是混合结果。

    "模块 mtime 变了"之类的信号不可靠：同一个模块里**没改**的函数，它的变异体本来
    就应当保留旧判决。唯一可靠的判据是**缓存干不干净**，所以刷新基线时把缓存整体移开，
    而不是去猜哪些条目还有效。
    """
    target = f"{mutants_dir}.stale-{stamp}"
    if not update_baseline:
        return {
            "action": "none",
            "mutants_dir": str(mutants_dir),
            "moved_to": None,
            "reason": "不是刷新基线的一轮：增量是 mutmut 的正常工作方式（判定读的是全量结果表）",
        }
    if not mutants_dir.exists():
        return {
            "action": "full-run",
            "mutants_dir": str(mutants_dir),
            "moved_to": None,
            "reason": "没有 mutants/ 缓存：本轮本来就是全量",
        }
    if allow_incremental:
        return {
            "action": "incremental",
            "mutants_dir": str(mutants_dir),
            "moved_to": None,
            "reason": "显式给了 --allow-incremental-refresh",
            "warning": (
                f"{mutants_dir} 还在，本轮沿用其中的旧判决 ⇒ 写出的基线可能混合"
                "（旧判决 + 这次重跑的判决）。只在确认缓存与当前代码一致时这么做，"
                "并在 PR 里写明；否则去掉 --allow-incremental-refresh 重跑。"
            ),
        }
    return {
        "action": "move-aside",
        "mutants_dir": str(mutants_dir),
        "moved_to": target,
        "reason": (
            f"刷新基线要先有干净缓存：把 {mutants_dir} 整体移开，本轮因而是全量重跑"
        ),
    }


def apply_baseline_refresh_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """执行 :func:`baseline_refresh_plan` 的决定（唯一的副作用点），返回更新后的 plan。"""
    if plan["action"] == "move-aside":
        source = Path(plan["mutants_dir"])
        target = Path(str(plan["moved_to"]))
        suffix = 2
        while target.exists():  # 同一秒里刷新两次也要有个去处，不许覆盖
            target = Path(f"{plan['moved_to']}-{suffix}")
            suffix += 1
        source.rename(target)
        plan["moved_to"] = str(target)
        print(f"[mutation] {plan['reason']}\n  移到了 {target}（可删；已 gitignore）")
    elif plan["action"] == "incremental":
        print(f"[mutation] **警告** {plan['warning']}")
    return plan


def run_mutmut(
    *, module: str | None, timeout: float, max_children: int, json_out: str = ""
) -> float:
    """跑 mutmut，返回这一轮的**墙钟秒数**（写进 `--json-out` 的 `elapsed_s`）。

    为什么要返回值：预算够不够是唯一一个**只有 CI 能回答**的定量问题（独立验证
    2026-09-18 · 报告 §5-1），而先前只有 print 一行用时、不落盘 ⇒ 夜里跑完也留不下数。
    返回的区间是"这一条 mutmut 子进程的墙钟"，与 nightly 的 job 墙钟
    （`gh run view <id> --json jobs`，含 checkout/install）对照即可判断余量。
    """
    argv = _mutmut_argv(module)
    print(f"[mutation] 运行: {' '.join(argv)} --max-children {max_children}（上限 {timeout:.0f}s）")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [*argv, "--max-children", str(max_children)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _abort(
            "timeout",
            f"[mutation] 超时失败：{timeout:.0f} 秒内没跑完（module={module or '全部'}）。\n"
            "  这是**失败**而不是跳过：跑不完就无法判定「有没有新增盲区」。\n"
            "  处置：缩小 --module 范围、提高 runner 并行度，或在 PR 里记录本次跳过及原因。\n"
            "  已产生的输出尾部："
            f"{(exc.stdout or '')[-200:] if isinstance(exc.stdout, str) else ''}",
            json_out=json_out,
            extra={"elapsed_s": round(time.perf_counter() - started, 1)},
        )
    elapsed = time.perf_counter() - started
    print(f"[mutation] 用时 {elapsed:.1f}s")
    if proc.returncode != 0:
        # `mutmut run` 非 0 = 本轮没跑成功（配置没生效、collect error、被 OOM 杀…）。
        # 绝不能继续判定：mutmut 的结果读的是 `mutants/` 缓存，失败时残留的旧结果
        # 会被当成"这一轮的结果"（评审 P0-1）。
        _abort(
            "mutmut_run_failed",
            f"[mutation] `mutmut run` 退出码 {proc.returncode}：本轮没有跑成功，判定作废。\n"
            f"  stdout 尾部：{proc.stdout.strip()[-300:]}\n"
            f"  stderr 尾部：{proc.stderr.strip()[-300:]}",
            json_out=json_out,
            extra={"elapsed_s": round(elapsed, 1)},
        )
    return elapsed


def collect_status(*, json_out: str = "") -> dict[str, list[str]]:
    """读 mutmut 的结果表：``{status: [mutant 名]}``。**全状态**，不做任何过滤。"""
    proc = subprocess.run(
        # mutmut 3.x 的 --all 是**带值**选项（default=False 且没有 is_flag），
        # 必须写 --all=true，否则报 "Option '--all' requires an argument"。
        # 要全量是因为 survivor_rate 需要 killed 数作分母（只看非 killed 会把比率算成 1.0），
        # 也因为"无结论类"必须被看见（P0-1 的教训）。
        [sys.executable, "-m", "mutmut", "results", "--all=true"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        _abort(
            "results_unreadable",
            f"[mutation] 读结果失败：{proc.stderr[-400:]}",
            json_out=json_out,
        )
    buckets: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, status = line.rpartition(":")
        buckets.setdefault(status.strip(), []).append(name.strip())
    return buckets


def all_mutant_names(buckets: dict[str, list[str]]) -> set[str]:
    """结果表里的**全部**变异体名（所有状态，不做过滤）。

    指纹要对"结果表里出现过的每一条"取——包括幸存、no tests 与无结论类：
    无结论类变成有结论时，内容也该是同一份。
    """
    return {name for names in buckets.values() for name in names}


def mutation_summary(
    buckets: dict[str, list[str]], *, fingerprints: dict[str, str | None] | None = None
) -> dict[str, Any]:
    """把 mutmut 的状态表折算成"全状态记账"的摘要。**不丢弃任何状态**。

    ``fingerprints`` 是可选的内容指纹（`compute_mutant_fingerprints` 的产物）：
    默认空 dict 时，摘要与加指纹之前**逐字相同**（旧调用点不用改）。
    """
    by_status = {status: sorted(names) for status, names in buckets.items()}
    status_by_mutant = {name: status for status, names in buckets.items() for name in names}
    counts = {status: len(buckets.get(status, [])) for status in KNOWN_STATUSES}
    unknown_statuses = sorted(status for status in buckets if status not in KNOWN_STATUSES)
    decided = counts["killed"] + counts["survived"]
    total = len(status_by_mutant)
    # 不认识的状态名一律计入"不可见空间"（fail-closed）：宁可让它显得更差，
    # 也不能让它悄悄消失。
    inconclusive = sorted(
        name
        for name, status in status_by_mutant.items()
        if status in INCONCLUSIVE_STATUSES or status in unknown_statuses
    )
    return {
        "killed": counts["killed"],
        "survived": by_status.get("survived", []),
        "no_tests": by_status.get("no tests", []),
        "inconclusive": inconclusive,
        "status_counts": counts,
        "unknown_statuses": unknown_statuses,
        "by_status": by_status,
        "status_by_mutant": status_by_mutant,
        "total": total,
        "decided": decided,
        "uncovered_count": counts["no tests"],
        "invisible_count": total - decided,
        "invisible_share": (total - decided) / total if total else 0.0,
        # 幸存率只在"被判定的变异体"上算：no tests 与无结论类是另一类盲区，
        # 混进分母会让这个数看起来更好看。**它不是覆盖率**。
        "survivor_rate": (counts["survived"] / decided) if decided else 0.0,
        "fingerprints": dict(fingerprints or {}),
    }


def show_mutant(name: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mutmut", "show", name],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout.strip()[-600:]


def load_baseline(path: Path) -> dict[str, Any]:
    """读基线。**兼容 v1 / v2**：v1 只有 `survivors`/`no_tests` 两个列表；
    v2 有逐条 `status_by_mutant` 但没有内容指纹（v3 起才有）。

    v1 的 `killed` 只有计数没有名字，所以条件 2/3 在 v1 上只能覆盖
    "基线幸存者" 与 "基线 no tests"——这一点会被打印出来，免得被读成全覆盖。
    v2 缺指纹 ⇒ 条件 7 不生效（打印出来，不假装比对过）。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    full = isinstance(payload.get("status_by_mutant"), dict) and bool(payload["status_by_mutant"])
    if full:
        status_by_mutant = {str(k): str(v) for k, v in payload["status_by_mutant"].items()}
    else:
        status_by_mutant = {}
        for name in payload.get("survivors", []):
            status_by_mutant[str(name)] = "survived"
        for name in payload.get("no_tests", []):
            status_by_mutant[str(name)] = "no tests"
    raw_fingerprints = payload.get("fingerprints")
    fingerprints = (
        {str(k): str(v) for k, v in raw_fingerprints.items()}
        if isinstance(raw_fingerprints, dict)
        else {}
    )
    return {
        "schema": str(payload.get("schema", "mutation-baseline/v1")),
        "status_by_mutant": status_by_mutant,
        "full_status_coverage": full,
        "fingerprints": fingerprints,
        "fingerprint_algorithm": str(payload.get("fingerprint_algorithm", "")),
        "inconclusive": set(
            name for name, status in status_by_mutant.items() if status in INCONCLUSIVE_STATUSES
        ),
    }


def gate_verdict(summary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """七条红灯条件 + 未知状态 fail-closed。返回 ``{"failures": [...], ...}``。

    纯函数：不吃 mutmut、不读文件——判定逻辑本身要能被单元测试与退化注入直接钉住。
    """
    base = baseline["status_by_mutant"]
    base_decided = {name for name, status in base.items() if status in DECIDED_STATUSES}
    current = summary["status_by_mutant"]

    # 1. 新增幸存变异（基线里 killed 的变成 survived 也算：那是新增的盲区）。
    new_survivors = [name for name in summary["survived"] if base.get(name) != "survived"]
    # 2. 基线里是判定类的变异体，本轮变成非判定类（含 no tests / segfault / timeout…）。
    regressed_from_decided = sorted(
        name for name in base_decided if name in current and current[name] not in DECIDED_STATUSES
    )
    # 3. 基线里存在的变异体本轮完全缺失（未评估 / 改名 / 缓存缺项）。
    vanished = sorted(name for name in base if name not in current)
    # 4. 新增 no tests（P0-2）。
    new_no_tests = [name for name in summary["no_tests"] if base.get(name) != "no tests"]
    # 6. 无结论集合较基线增长。
    new_inconclusive = sorted(set(summary["inconclusive"]) - baseline["inconclusive"])
    # 7. 同名不同指纹：判据只按"名字 + 状态"对账时，等量改写会让同一批名字指向不同的变异
    #    （独立验证 2026-09-18 · 报告 §5-3）。只比"两边都取到指纹"的名字——
    #    取不到的那一侧由 fingerprint_report 打印条数，绝不假装比对过。
    base_fingerprints = baseline.get("fingerprints") or {}
    content_changed = sorted(
        name
        for name, value in (summary.get("fingerprints") or {}).items()
        if value and base_fingerprints.get(name) and value != base_fingerprints[name]
    )

    failures: list[dict[str, Any]] = []

    def fail(key: str, label: str, names: list[str], hint: str = "") -> None:
        failures.append(
            {"key": key, "label": label, "count": len(names), "names": names, "hint": hint}
        )

    if summary["total"] == 0:
        fail(
            "empty-result-set",
            "本轮**没有产出任何变异体**（total=0）",
            [],
            "这不是「没有盲区」，而是「没跑起来」。常见原因：mutmut 配置未生效、"
            "`mutants/` 被清空、collect error、runner 崩溃。",
        )
    elif summary["decided"] == 0:
        fail(
            "no-decided-mutants",
            "本轮**没有任何一条变异体得到判定**（killed=survived=0）",
            summary["inconclusive"],
            "全是无结论类 / 未覆盖类：这种情况下的 survivor_rate=0 毫无意义。",
        )
    if new_survivors:
        fail("new-survivors", "新增幸存变异（= 新增的测试盲区）", new_survivors)
    if regressed_from_decided:
        fail(
            "decided-to-inconclusive",
            "基线里**有判定**的变异体，本轮变成无结论 / 未覆盖",
            regressed_from_decided,
            "它们从门禁视野里消失了——只有「确实被 killed」才算好消息。",
        )
    if vanished:
        fail(
            "vanished-from-results",
            "基线里的变异体本轮**完全缺失**（未评估 / 改名 / 缓存缺项）",
            vanished,
        )
    if new_no_tests:
        fail(
            "new-no-tests",
            "新增 `no tests` 变异体（= 新增的、完全没有用例覆盖的代码）",
            new_no_tests,
        )
    if new_inconclusive:
        fail(
            "inconclusive-grew",
            "无结论集合较基线**增长**（有新名字进入不可见空间）",
            new_inconclusive,
        )
    if content_changed:
        fail(
            "mutant-content-changed",
            "同名变异体的**内容指纹变了**（名字与状态都对得上，但已经不是同一条变异）",
            content_changed,
            "这是「等量改写」的窗口：条数与编号都没变，只有变异内容变了——"
            "只按名字 + 状态对账会让它静默绿。要么把改动还原，要么确认新内容后"
            "跑 --update-baseline（并在 PR 里说明）。",
        )
    if summary["unknown_statuses"]:
        fail(
            "unknown-status",
            "出现本脚本不知道的 mutmut 状态名（全状态记账的兜底）",
            summary["unknown_statuses"],
            "先把它归类进判定类 / 未覆盖类 / 无结论类，再判定；不要让它漏过去。",
        )

    return {
        "failures": failures,
        "new_survivors": new_survivors,
        "regressed_from_decided": regressed_from_decided,
        "vanished": vanished,
        "new_no_tests": new_no_tests,
        "new_inconclusive": new_inconclusive,
        "content_changed": content_changed,
        "baseline_full_status_coverage": baseline["full_status_coverage"],
        "ok": not failures,
    }


def _report_line(summary: dict[str, Any]) -> str:
    sc = summary["status_counts"]
    parts = [f"{status}={count}" for status, count in sc.items() if count]
    unknown = summary["unknown_statuses"]
    if unknown:
        parts.append("未知状态=" + ",".join(unknown))
    return "[mutation] 全状态记账：" + " ".join(parts) if parts else "[mutation] 全状态记账：（空）"


def _invisible_space_lines(summary: dict[str, Any]) -> list[str]:
    total = summary["total"]
    decided = summary["decided"]
    invisible = summary["invisible_count"]
    share = summary["invisible_share"]
    rate = (
        f"{summary['survivor_rate']:.3f}"
        if decided
        else "n/a（没有任何一条变异体得到判定）"
    )
    return [
        _report_line(summary),
        f"[mutation] 判定类={decided} survivor_rate={rate}（分母只含 killed+survived）",
        f"[mutation] 不可见空间（无结论类 + 未覆盖类）= {invisible}/{total} "
        f"= {share:.1%}：这一部分既不进 survivor_rate，也不等于「测试没问题」；"
        "**survivor_rate 不是覆盖率**。",
    ]


def _write_json_out(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _abort(
    reason: str, message: str, *, json_out: str, extra: dict[str, Any] | None = None
) -> NoReturn:
    """判定**作废**时的退出路径：先落产物，再退出。

    为什么要有这个 helper：nightly 上传的就是 `--json-out` 指向的文件，而"跑不起来"
    （超时 / `mutmut run` 非零退出 / 结果读不出）恰恰是最需要产物的那条路径——
    先前这三条路都是裸 `SystemExit`，夜里出问题只留一行 stderr，artifact 是空的
    （独立验证报告 P1-1）。

    ``reason`` 是给机器读的稳定标识（timeout / mutmut_run_failed / results_unreadable），
    ``message`` 是给人读的原文——两者都不做截断，产物里要能直接定位原因。
    ``extra`` 是调用方额外知道的量化信息（目前只有 `elapsed_s`：**超时那一刻已经跑了多久**
    是处置超时的第一手证据，能区分"差一点就跑完"和"配置根本没生效"）。
    """
    payload: dict[str, Any] = {
        "schema": "mutation-run/v2",
        "verdict": "aborted",
        "reason": reason,
        "message": message,
    }
    if extra:
        payload.update(extra)
    _write_json_out(json_out, payload)
    raise SystemExit(message)


def _summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """`--summary-only` / `--json-out` 的公开字段。

    `survivors` 一律是**列表**（v1 曾在这里写个数，而主判定路径写列表——
    同一个字段名两种类型，claim 对账会踩空）；个数是 `survivor_count`。
    """
    counts = payload.get("counts", {})
    status_by_mutant = payload.get("status_by_mutant") or {}
    # 老基线可能只写了计数没写名单：能从 `status_by_mutant` 推的就推出来，
    # 推不出来才回落到 0——但绝不假装"没有不可见空间"。
    if "inconclusive" in payload:
        inconclusive = list(payload["inconclusive"])
    else:
        inconclusive = sorted(
            name for name, status in status_by_mutant.items() if status in INCONCLUSIVE_STATUSES
        )
    invisible = counts.get("invisible")
    if invisible is None:
        decided = counts.get("decided")
        if decided is None:
            decided = counts.get("killed", 0) + counts.get("survived", 0)
        total = counts.get("total", len(status_by_mutant))
        invisible = total - decided
    return {
        "schema": payload.get("schema", "mutation-baseline/v1"),
        "counts": counts,
        "status_counts": payload.get("status_counts", counts),
        "survivor_rate": payload["survivor_rate"],
        "survivors": list(payload.get("survivors", [])),
        "survivor_count": len(payload.get("survivors", [])),
        "no_tests": list(payload.get("no_tests", [])),
        "no_tests_count": len(payload.get("no_tests", [])),
        "inconclusive": inconclusive,
        "inconclusive_count": len(inconclusive),
        "invisible_count": invisible,
        "invisible_share": payload.get("invisible_share", 0.0),
        "modules": payload.get("modules", []),
        # 只给**条数**与算法名：指纹本身 2000+ 条，逐条打在这一行 stdout 上没人读得动。
        # （它是给门禁逐条比对用的，完整名单在基线文件与 `--json-out` 的产物里。）
        "fingerprint_algorithm": payload.get("fingerprint_algorithm", ""),
        "fingerprint_count": len(payload.get("fingerprints") or {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/mutation_check.py")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument(
        "--json-out",
        default="",
        help="把本轮摘要写成 JSON 到这里（判定失败时也会写：nightly 要能上传它）",
    )
    parser.add_argument(
        "--module",
        default="",
        help="只跑一部分变异体：接受文件路径（`harness/approval.py`，会翻译成 "
        "`harness.approval.*`）或变异体名 glob（`harness.approval.*`、某条完整变异体名）。"
        "默认跑 pyproject.toml [tool.mutmut].only_mutate 里的全部四个模块。"
        "**注意**：它只影响「哪些变异体被重新评估」，判定读的是 `mutmut results` 的**全量**结果表，"
        "所以 `--baseline` 必须仍然是那份全量基线（传切片基线会把所有没被切中的变异体"
        "误报成新增）。",
    )
    parser.add_argument("--timeout", type=float, default=780.0, help="整体墙钟上限（秒）")
    parser.add_argument("--max-children", type=int, default=4)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--allow-incremental-refresh",
        action="store_true",
        help="与 --update-baseline 连用：明知 mutants/ 缓存不干净仍然沿用旧判决"
        "（会打出警告，且写出的基线可能是新旧混合）。默认不允许——刷新基线必须全量重跑。",
    )
    parser.add_argument("--max-report", type=int, default=10, help="最多展示几条新增幸存变异")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="只读基线并输出 JSON 摘要（不跑 mutmut）：给 claim 门禁做对账用",
    )
    args = parser.parse_args(argv)

    if args.summary_only:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"没有基线文件 {baseline_path}")
            return 1
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        summary_payload = _summary_payload(payload)
        print(json.dumps(summary_payload, ensure_ascii=False))
        _write_json_out(args.json_out, summary_payload)
        return 0

    module_filter = normalize_module_filter(args.module)
    if args.module and module_filter != args.module:
        print(f"[mutation] `--module {args.module}` → 变异体名 glob `{module_filter}`")
    # 刷新基线之前先处理增量缓存：这一步**必须**在 run_mutmut 之前（缓存干不干净
    # 决定了这一轮是全量还是增量）。计划是纯函数算的，副作用只在这里发生。
    refresh = apply_baseline_refresh_plan(
        baseline_refresh_plan(
            update_baseline=args.update_baseline,
            allow_incremental=args.allow_incremental_refresh,
            mutants_dir=MUTANTS_DIR,
            stamp=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        )
    )
    # 墙钟：优先用 `run_mutmut` 自己的计时（它量的正是那条子进程的生命周期）；
    # 若返回 None（测试替身 / 未来的其它 runner），退回在这里量这一段——
    # 拿不到就写这一段实测，不写一个"看起来像实测"的常数。
    run_started = time.perf_counter()
    elapsed = run_mutmut(
        module=module_filter or None,
        timeout=args.timeout,
        max_children=args.max_children,
        json_out=args.json_out,
    )
    elapsed_s = round(
        float(elapsed if elapsed is not None else time.perf_counter() - run_started), 1
    )
    buckets = collect_status(json_out=args.json_out)
    # 内容指纹（条件 7）：从这一轮刚生成的 `mutants/` 读，逐条算；
    # 取不到的条目如实记 None，条数与"是否真的比对了"由 fingerprint_report 打印。
    fingerprints = compute_mutant_fingerprints(
        all_mutant_names(buckets), mutants_dir=MUTANTS_DIR
    )
    summary = mutation_summary(buckets, fingerprints=fingerprints)
    print(
        f"[mutation] killed={summary['killed']} survived={len(summary['survived'])} "
        f"no_tests={len(summary['no_tests'])} survivor_rate={summary['survivor_rate']:.3f}"
    )
    for line in _invisible_space_lines(summary):
        print(line)
    for line in fingerprint_report(fingerprints):
        print(line)

    run_payload = {
        "schema": "mutation-run/v2",
        "modules": list(DEFAULT_MODULES) if not args.module else [args.module],
        "command": (
            f"mutmut run {module_filter or '（全部模块）'}"
            f" --max-children {args.max_children}"
        ),
        "counts": {
            "killed": summary["killed"],
            "survived": len(summary["survived"]),
            "no_tests": len(summary["no_tests"]),
            "inconclusive": len(summary["inconclusive"]),
            "invisible": summary["invisible_count"],
            "total": summary["total"],
            "decided": summary["decided"],
        },
        "status_counts": summary["status_counts"],
        "status_by_mutant": summary["status_by_mutant"],
        "survivor_rate": round(summary["survivor_rate"], 4),
        "survivors": summary["survived"],
        "no_tests": summary["no_tests"],
        "inconclusive": summary["inconclusive"],
        "unknown_statuses": summary["unknown_statuses"],
        # 这一轮 `mutmut run` 的**墙钟秒数**（2026-09-26 新加）：nightly 上传的就是这个文件，
        # 而"预算够不够"只有 CI 能回答（独立验证 2026-09-18 · 报告 §5-1）——先前只有 print，
        # 夜里跑完留不下数。对照 nightly job 的墙钟（`gh run view <id> --json jobs`，含
        # checkout/install）即可算余量。不按模块拆分：本脚本一次 `mutmut run` 跑完
        # `only_mutate` 的全部模块，模块级细分要改成串行多次运行（那是另一种预算形态）。
        "elapsed_s": elapsed_s,
        # 内容指纹（条件 7 的原料）：逐条写进产物，两次运行的产物可以直接 diff 出
        # "哪些同名变异换了内容"；基线里也存同一份（算法标识一并写，换口径必须改名）。
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "fingerprints": fingerprints,
        # 这一轮是"全量重跑"还是"沿用了旧缓存"：基线混合与否只能从这里读出来
        "refresh": refresh,
    }
    # **主判定路径也要写 --json-out**：nightly 上传的就是这个文件，先前只有
    # `--summary-only` 写，于是夜里跑的那条路根本不会生成 `/tmp/mutation.json`
    # （独立验证报告 P1-1 的同族问题）。放在判定之前写：判失败也有产物可看。
    run_payload["invisible_share"] = round(summary["invisible_share"], 4)
    _write_json_out(args.json_out, run_payload)

    baseline_path = Path(args.baseline)
    if summary["total"] == 0:
        # 评审 P0-1：`mutmut results` 在没有结果时**退出 0 且无输出**。
        print(
            "[mutation] 本轮**没有产出任何变异体**（total=0）：这不是「没有盲区」，"
            "而是「没跑起来」，判定失败。\n"
            "  常见原因：mutmut 配置未生效、`mutants/` 被清空、collect error、runner 崩溃。"
        )
        return 1

    if args.update_baseline:
        unreadable = sorted(name for name, value in fingerprints.items() if not value)
        if fingerprints and len(unreadable) == len(fingerprints):
            # fail-closed：写一份**一条指纹都没有**的基线，等于把条件 7 静默关掉
            # （门禁之后会一直"没有可比对的指纹"而全绿）。宁可判失败。
            print(
                "[mutation] 一条内容指纹都取不到（mutants/ 缺文件 / .meta 里没有这些名字）："
                "拒绝写基线——那会把「同名不同指纹」这条判据静默关掉。\n"
                f"  mutants_dir={MUTANTS_DIR}，本轮变异体 {len(fingerprints)} 条"
            )
            return 1
        payload_to_write = {
            "schema": "mutation-baseline/v3",
            "tool": "mutmut 3.x（见 pyproject.toml [tool.mutmut]）",
            "modules": list(DEFAULT_MODULES),
            "command": "mutmut run --max-children 4（nightly job `mutation`）",
            # 这份基线是什么条件下冻的：全量重跑（`action=full-run`/`move-aside`）
            # 还是"沿用了旧缓存的增量结果"（`action=incremental`）。后者是
            # 混合基线，写在这里而不是靠人记。
            "refresh": refresh,
            "counts": {
                "killed": summary["killed"],
                "survived": len(summary["survived"]),
                "no_tests": len(summary["no_tests"]),
                "inconclusive": len(summary["inconclusive"]),
                "invisible": summary["invisible_count"],
                "total": summary["total"],
                "decided": summary["decided"],
            },
            "status_counts": summary["status_counts"],
            # 每个变异体的状态：条件 2/3 靠它才能看见"killed 变成 segfault"
            # 与"整条重命名"（v1 只有 survivors/no_tests 两个列表，看不见这些）。
            "status_by_mutant": summary["status_by_mutant"],
            # 每条变异体的**内容指纹**（v3 起）：条件 7 靠它才能看见"同名但已不是同一条"
            # ——等量改写（常数改值、语句换序）既不增删条数也不移位编号，只看名字会静默绿。
            "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
            "fingerprints": {name: value for name, value in fingerprints.items() if value},
            "survivor_rate": round(summary["survivor_rate"], 4),
            "invisible_share": round(summary["invisible_share"], 4),
            "survivors": summary["survived"],
            "no_tests": summary["no_tests"],
            # 已知无结论集合：门禁对它的**增长**敏感，对它本身不做断言。
            # 它不得被读成"不是盲区"——2026-09-18 之前这个集合里装着 906 条
            # `segfault`（macOS fork 子进程碰系统代理解析导致的误判），其中
            # `harness.approval.xǁApprovalBindingǁis_expired__mutmut_9` 经手工应用
            # 整套用例全绿 ⇒ 是一条**真幸存变异**。根因修掉后该集合为空。
            "inconclusive": summary["inconclusive"],
            "note": (
                "幸存变异 = 测试盲区。基线只用来防「新增盲区」，不是质量分："
                "部分路径由子进程驱动的崩溃矩阵/四系统评测覆盖，mutmut 看不见，"
                "因此清单里包含「其实被外层验证保护着」的变异。"
                "无结论类（segfault/timeout/not checked…）与未覆盖类（no tests）"
                "合起来是**不可见空间**，它们不进 survivor_rate——"
                "survivor_rate 不是覆盖率。"
                "基线的 `status_by_mutant` 记了每一条的状态：判定类变成非判定类、"
                "整条缺失、新增 no tests、无结论集合增长，都会让门禁变红。"
                "`fingerprints` 记了每一条的内容指纹：同名**不同指纹**同样变红"
                f"（算法 {FINGERPRINT_ALGORITHM}；换口径必须换标识名，否则会集体误报）。"
                "`inconclusive` 是「已知无结论集合」，门禁只判它的增长、不判它本身——"
                "**它不得被读成「不是盲区」**：2026-09-18 之前这个集合里装着 906 条 "
                "`segfault`（macOS 上 fork 出的子进程碰系统代理解析导致的误判），"
                "其中 is_expired__mutmut_9 经手工应用后整套用例全绿，是一条真幸存变异；"
                "根因修掉后该集合为空，调查见 "
                "docs/design/2026-09-18-mutation-segfault-investigation.md。"
            ),
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(payload_to_write, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[mutation] 基线已更新：{baseline_path}"
            f"（schema v3，含状态与内容指纹 {len(payload_to_write['fingerprints'])} 条）"
        )
        return 0

    if not baseline_path.exists():
        print(f"[mutation] 没有基线文件 {baseline_path}：请先跑 --update-baseline 并入库")
        return 1
    baseline = load_baseline(baseline_path)
    if not baseline["status_by_mutant"]:
        print(f"[mutation] 基线文件 {baseline_path} 里没有任何变异体条目：判定作废")
        return 1
    if not baseline["full_status_coverage"]:
        print(
            "[mutation] 注意：基线是 v1（只有 survivors/no_tests 两个列表，没有 killed 的名字）。"
            "条件 2/3 因此只能覆盖基线幸存者与 no tests；跑一次 --update-baseline 可升到 v3。"
        )
    for line in fingerprint_comparison_lines(fingerprints, baseline["fingerprints"]):
        print(line)

    verdict = gate_verdict(summary, baseline)
    if verdict["ok"]:
        print("[mutation] 没有新增幸存变异、没有判定类退化、没有新盲区：盲区没有扩大")
        return 0

    print(f"\n[mutation] 判定失败：{len(verdict['failures'])} 条红灯条件成立")
    for failure in verdict["failures"]:
        print(f"\n--- [{failure['key']}] {failure['label']}：{failure['count']} 条")
        if failure["hint"]:
            print(f"    {failure['hint']}")
        for name in failure["names"][: args.max_report]:
            print(f"      {name}")
        if len(failure["names"]) > args.max_report:
            print(f"      （其余 {len(failure['names']) - args.max_report} 条省略）")

    for name in verdict["new_survivors"][: args.max_report]:
        print(f"\n--- {name}")
        print(show_mutant(name))
    print(
        "\n处置：给这些分支补用例（docs/testing.md §3：新增机制三件套），"
        "或者——如果放宽是有意的——在 PR 里说明并跑 --update-baseline。\n"
        "「本轮没看到」不等于「修好了」：判定类变成无结论、整条缺失、新增 no tests "
        "都算盲区扩大，不许当成好消息。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
