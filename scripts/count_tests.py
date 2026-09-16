"""把「测试数量」变成可再生的 claim 来源：跑 ``pytest --collect-only -q`` 并把计数写成 JSON。

为什么单独一个脚本：测试数会随代码走（演进类 claim），文档里**永远不手写绝对数**
（设计文档 A §R-A1、B §5）。claim 门禁只认 JSON + 路径，所以这里把采集口径固定下来：
命令与口径同时写进输出，任何人在任何机器上跑同一条命令都得到同一个可核对的数字。

用法::

    .venv/bin/python scripts/count_tests.py --json-out /tmp/facts/tests.json
    # → {"collected": 236, "source": "pytest -o addopts= -p no:cacheprovider --collect-only -q"}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTEST_ARGS = ("-o", "addopts=", "-p", "no:cacheprovider", "--collect-only", "-q")


def collect_count(timeout: float = 300.0) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *PYTEST_ARGS],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    collected = None
    for line in reversed(lines):
        head = line.split()
        if len(head) >= 2 and head[1] == "tests" and head[0].isdigit():
            collected = int(head[0])
            break
    if collected is None:
        raise SystemExit(
            "pytest --collect-only 没有给出计数；stdout 尾部:\n"
            + "\n".join(lines[-5:])
            + f"\nstderr 尾部:\n{proc.stderr[-400:]}"
        )
    return {
        "collected": collected,
        "exit_code": proc.returncode,
        "source": " ".join(["pytest", *PYTEST_ARGS]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/count_tests.py")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)
    payload = collect_count()
    print(json.dumps(payload, ensure_ascii=False))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
