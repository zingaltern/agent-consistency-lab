"""``python -m opsenv.suite`` 的入口（拆分前是模块尾部的 ``if __name__ == "__main__"``）。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
