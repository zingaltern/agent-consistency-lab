"""`python -m scripts.mutation_check` 的入口（拆包后这条命令必须继续可用）。"""

from __future__ import annotations

import sys

from .runtime import main

if __name__ == "__main__":
    sys.exit(main())
