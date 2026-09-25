"""路径与词表：**唯一一份**可变开关（`MUTANTS_DIR`）与状态分类。

为什么要单独一个模块：`MUTANTS_DIR` 会被测试替换（把缓存指到 tmp），而它有**两个**消费者
（刷新基线的 move-aside、内容指纹的读取）。两个消费者各自 `from .config import MUTANTS_DIR`
拿到的是各自的绑定；测试改一处就会只生效一半——这是"改开关却静默无效"的老坑
（`opsenv/suite` 拆包时 `PROFILES` 的间接层就是为同一件事留的）。这里的约定是：
**消费者一律在调用点读 `config.MUTANTS_DIR`**。
"""

from pathlib import Path

# 比单文件版多一层：本文件在 scripts/mutation_check/ 里，仓库根是 parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
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

