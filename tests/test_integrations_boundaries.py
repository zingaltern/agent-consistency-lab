"""目录与依赖边界：内核**不得**依赖集成层，集成层是可选装、可选跑的。

设计文档 C §3 把这条列为"最容易被做坏的地方"：集成代码一旦渗进内核目录或默认依赖，
"零依赖内核"这条就没了。因此这里用**结构断言**守住，而不是靠自觉：

1. `harness/` `opsenv/` `fakeworld/` `experiments/`（以及 `scripts/` `examples/`）
   的任何 import 都不得指向 `integrations`；
2. `import harness.*` 不得把 MCP SDK 带进 `sys.modules`（延迟 import 纪律）；
3. 内核依赖清单仍然只有 `pydantic`，`[mcp]` 不进 `dev` extra；
4. 打包与变异配置里该有的目录不能漏（漏了会让门禁**静默**变弱）。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 依赖方向是单向的：这些目录是被测内核与测量仪器，不得反向依赖集成层。
KERNEL_DIRS = ("harness", "opsenv", "fakeworld", "experiments", "scripts", "examples")
INTEGRATION_DIRS = ("integrations",)
FORBIDDEN_TOP_LEVEL = "integrations"


def _py_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def _all_import_roots(path: Path) -> set[str]:
    """文件里出现的所有 import 根模块名（**含函数体内的延迟 import**）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _module_level_import_roots(path: Path) -> set[str]:
    """只取**顶层** import（函数体内的不算）——用于验证延迟 import 纪律。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# ------------------------------------------------------------ 依赖方向


def test_kernel_directories_never_import_the_integration_layer() -> None:
    """内核目录的任何 import（含延迟 import）都不得指向 integrations/。"""
    offenders: list[str] = []
    checked = 0
    for directory in KERNEL_DIRS:
        for path in _py_files(PROJECT_ROOT / directory):
            checked += 1
            if FORBIDDEN_TOP_LEVEL in _all_import_roots(path):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert checked > 50, f"扫描面太小，可能扫错了目录：{checked} 个文件"
    assert not offenders, f"内核目录反向依赖了集成层：{offenders}"


def test_importing_the_kernel_does_not_pull_in_the_mcp_sdk() -> None:
    """`import harness.*` 不得触发 MCP 的任何 import（延迟 import，照 harness/otel.py）。

    跑在子进程里：同一个解释器里别的用例可能已经 import 过 mcp，sys.modules 不干净。
    """
    code = (
        "import sys\n"
        "import harness, harness.loop, harness.execution, harness.tools, harness.approval\n"
        "import harness.trace, harness.otel, harness.live_transport, harness.store\n"
        "leaked = sorted(m for m in sys.modules if m == 'mcp' or m.startswith('mcp.'))\n"
        "assert not leaked, leaked\n"
        "assert 'integrations' not in sys.modules, '内核不得 import 集成层'\n"
        "print('clean')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "clean" in proc.stdout


def test_integration_modules_do_not_import_the_sdk_at_module_level() -> None:
    """集成层自己也要延迟 import：缺 extra 时 import 集成模块不能炸。"""
    for path in _py_files(PROJECT_ROOT / INTEGRATION_DIRS[0]):
        roots = _module_level_import_roots(path)
        assert "mcp" not in roots, f"{path.name} 在顶层 import 了 MCP SDK"


def test_integration_layer_may_import_the_kernel() -> None:
    """正向是允许的（集成层复用内核件），否则这个包就没有存在意义。"""
    roots: set[str] = set()
    for path in _py_files(PROJECT_ROOT / INTEGRATION_DIRS[0]):
        roots |= _all_import_roots(path)
    assert {"harness", "fakeworld"} <= roots, roots


# ------------------------------------------------------------ 依赖与打包


def test_kernel_dependencies_are_still_only_pydantic() -> None:
    """内核依赖清单一个字都不许变——集成能力必须走 extra。"""
    data = _pyproject()
    assert data["project"]["dependencies"] == ["pydantic>=2.7"]


def test_mcp_extra_exists_and_is_not_part_of_dev() -> None:
    """`[mcp]` 必须在，且不得混进 `dev`（装 dev 不该把 MCP 全家桶拉进来）。"""
    extras = _pyproject()["project"]["optional-dependencies"]
    assert "mcp" in extras
    assert any(spec.startswith("mcp") for spec in extras["mcp"])
    assert not any(spec.startswith("mcp") for spec in extras["dev"]), extras["dev"]
    assert not any(spec.startswith("mcp") for spec in extras.get("eval", []))


def test_wheel_and_mutation_configs_cover_the_new_package() -> None:
    """打包与变异配置漏了目录，会让集成代码"装了却没有"或门禁静默失去覆盖。"""
    data = _pyproject()
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(packages) == {"harness", "integrations"}

    mutmut = data["tool"]["mutmut"]
    assert "harness/execution.py" in mutmut["only_mutate"], (
        "管线实现搬到了 execution.py，不列进来等于管线失去变异覆盖而门禁不会变红"
    )
    assert "integrations" in mutmut["also_copy"], (
        "边界用例会读 integrations/ 源码，变异沙箱里缺它会让用例失败"
    )


def test_pytest_marker_is_registered_and_filtered_by_default() -> None:
    """marker 必须注册（否则空选集 exit 5 会被误读成"反例通过"）且默认过滤。"""
    ini = _pyproject()["tool"]["pytest"]["ini_options"]
    markers = ini["markers"]
    assert any(marker.startswith("mcp:") for marker in markers), markers
    addopts = ini["addopts"]
    assert "not mcp" in addopts, addopts
    assert "not live" in addopts, "live 的过滤不能被覆盖掉"


def test_ci_nightly_installs_the_extra_and_runs_the_mcp_tests() -> None:
    """nightly 显式跑 `-m mcp`：否则这一层在 CI 里完全没人看。"""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert ".[dev,eval,mcp]" in workflow, "nightly 需要装 [mcp] extra"
    assert "-m mcp" in workflow, "nightly 需要显式跑 MCP 用例"
    verify = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "mcp" not in verify, (
        "verify 作业不得引入 MCP：默认用例必须在不装 extra 的环境里全绿，"
        "且它的时长预算只有几秒（设计文档 C §3.4）"
    )


def test_documented_facts_entries_are_still_wellformed() -> None:
    """新增 claim 后，claim 文件仍然可被门禁加载（结构性自检）。"""
    payload = json.loads(
        (PROJECT_ROOT / "reports" / "documented-facts.json").read_text(encoding="utf-8")
    )
    ids = [claim["id"] for claim in payload["claims"]]
    assert len(ids) == len(set(ids)), "claim id 重复"


