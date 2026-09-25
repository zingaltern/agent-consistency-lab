"""结构断言：模块顶层不得出现同名重复定义（`opsenv/` 与 `harness/` 的非测试文件）。

**修复前会怎样**（拆包前的 `opsenv/systems.py`，W11 之后实测）：`_agent_output_tokens` 与
`_run_state` 各被定义了**两次**，两份函数体逐字相同，第二份静默遮蔽第一份。Python 不报错、
既有用例全绿、门禁全绿——于是「改第一份」不生效（调用拿到的是第二份），而没有任何机制会发现
这件事。这类缺陷的危害不是崩溃，而是**看起来改了、实际没改**：审计里最难发现的一种
（与 `docs/HANDOFF.md` §六 第 7 条「没有坏事发生 ≠ 机制在工作」同源）。

**扫描面**：`git ls-files` 登记的 `opsenv/` 与 `harness/` 下的 `.py` 文件，并上目录里
实际存在的 `.py`（只扫存在的文件：工作区已删、索引还没跟上的路径属于 `git status`
的事，不是这里要判的重复定义——文件搬移的中途态不该让本用例报一个看不懂的错）。
为什么要并集：变异测试的沙箱（`mutants/`，被 `.gitignore` 排除）不是工作树的一部分，
在该沙箱里 `git ls-files opsenv harness` 返回**空表**——只信 git，这个用例在沙箱里就会
空真通过（门禁恰恰在最需要它的那一半时间里是瞎的）。并集在两种环境下都覆盖真实文件；
`checked` 下限断言挡住"扫错目录 ⇒ 空真通过"。
"""

from __future__ import annotations

import ast
import subprocess
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("opsenv", "harness")
# 实际扫描面约 50 个文件（拆包后）；下限取一半，专门挡"路径写错 ⇒ 一个文件都没扫到"
MIN_SCANNED = 25


def _tracked_python_files(directory: str) -> list[Path]:
    """`git ls-files` 登记的该目录下 Python 文件；git 不可用或不在仓库里 ⇒ 空表。"""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", directory],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [PROJECT_ROOT / line for line in proc.stdout.splitlines() if line.endswith(".py")]


def _on_disk_python_files(directory: str) -> list[Path]:
    return sorted(
        path
        for path in (PROJECT_ROOT / directory).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _scanned_files() -> list[Path]:
    seen: dict[Path, None] = {}
    for directory in SCAN_DIRS:
        candidates = _tracked_python_files(directory) + _on_disk_python_files(directory)
        for path in candidates:
            if path.name.startswith("test_"):
                continue  # 只扫非测试文件
            if not path.exists():
                continue  # 索引里还在、工作区已删（搬移中途态）
            seen[path] = None
    return sorted(seen, key=str)


def _top_level_names(tree: ast.Module) -> list[str]:
    """模块顶层的定义名（函数 / 类 / 赋值目标），按出现顺序——重名即重复定义。"""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def test_no_module_defines_the_same_top_level_name_twice() -> None:
    """任一模块的顶层都不许同名重复定义（后一份会静默遮蔽前一份，且无人报警）。"""
    duplicates: list[str] = []
    checked = 0
    for path in _scanned_files():
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        repeated = sorted(
            name for name, count in Counter(_top_level_names(tree)).items() if count > 1
        )
        if repeated:
            duplicates.append(f"{path.relative_to(PROJECT_ROOT)}: {repeated}")
    assert checked >= MIN_SCANNED, f"扫描面太小（{checked} 个文件），可能扫错了目录"
    assert not duplicates, (
        "模块顶层出现同名重复定义——后一份会静默遮蔽前一份，改前一份不会生效：\n"
        + "\n".join(duplicates)
    )
