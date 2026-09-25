"""产物落盘与"判定作废"出口：所有失败路径都要留下 `--json-out`。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn


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


