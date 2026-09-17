"""OTLP 导出桥（R-B4）：把事件日志投影出的 span 树送进真实后端。

三条纪律：

1. **默认行为零改变**：不装 `[otel]` extra、不传 `--otlp-endpoint` 时，
   `import harness.trace` 与全部既有测试的行为一模一样——OTel 的 import 全部在函数体内，
   模块顶层不引入任何新依赖。
2. **映射复用 `Trace.to_otel()`**：那份 JSON 是"投影形状"的唯一定义，
   本模块只负责把它**翻译成 SDK 的 span**（同一批字段：trace/span/parent id、
   名称、kind、起止时间纳秒、属性键值）。两份定义并存迟早漂移。
3. **不启动守护、不做重试队列**：endpoint 每次显式传入，导出失败就是可读错误。

用法::

    python -m harness.trace --run-dir /tmp/demo --otlp-endpoint http://localhost:4318
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .trace import Trace

OTEL_EXTRA_HINT = (
    "需要 OTLP 导出能力：pip install -e '.[otel]'（opentelemetry-sdk + exporter）。"
    "本仓库不把 OTel 放进内核依赖：不加这个 extra 时 import harness.trace 与全部既有行为不变。"
)


class OtelUnavailableError(RuntimeError):
    pass


class OtelExportError(RuntimeError):
    pass


def _load_sdk() -> tuple[Any, Any]:
    """延迟 import：顶层不引入 otel，缺 extra 时给出可读失败。"""
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:  # pragma: no cover - 取决于环境是否装了 extra
        raise OtelUnavailableError(f"{OTEL_EXTRA_HINT}（原始错误：{exc}）") from exc
    return Resource, TracerProvider


def build_sdk_spans(trace: Trace) -> tuple[Any, list[Any]]:
    """把 ``Trace`` 翻成 SDK 的 span 列表（与 ``to_otel()`` 同一批字段）。

    返回 ``(provider, spans)``：调用方需要 provider 才能把 span 交给 exporter。
    """
    Resource, TracerProvider = _load_sdk()
    payload = trace.to_otel()["resourceSpans"][0]
    resource_attributes = {
        item["key"]: item["value"].get("stringValue") for item in payload["resource"]["attributes"]
    }
    provider = TracerProvider(resource=Resource.create(resource_attributes))
    tracer = provider.get_tracer(payload["scopeSpans"][0]["scope"]["name"])

    spans = []
    for item in payload["scopeSpans"][0]["spans"]:
        span = tracer.start_span(
            name=item["name"],
            # SDK 的 start_time/end_time 就是**纳秒**（不是微秒）：直接给投影值
            start_time=item["startTimeUnixNano"],
            attributes={
                attribute["key"]: attribute["value"].get("stringValue")
                for attribute in item["attributes"]
            },
        )
        span.set_attribute("harness.span_id", item["spanId"])
        span.set_attribute("harness.parent_span_id", item["parentSpanId"])
        span.end(end_time=item["endTimeUnixNano"])
        spans.append((span, item))
    return provider, spans


def export_otlp(
    trace: Trace,
    *,
    endpoint: str,
    exporter: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """把 span 树导出到 OTLP/HTTP endpoint。

    ``exporter`` 可注入（内存内实现，供单测用）——注入时**不接触网络**，
    这也是"CI 不引入外部 collector 进程"的落地方式。
    """
    from opentelemetry.sdk.trace.export import SpanExportResult

    provider, spans = build_sdk_spans(trace)
    if exporter is None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as exc:  # pragma: no cover
            raise OtelUnavailableError(f"{OTEL_EXTRA_HINT}（原始错误：{exc}）") from exc
        # timeout 必须真的传下去：原先它是个死参数（评审 P2-15），
        # 于是"超时"这件事在真实导出里不可控。
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers=headers, timeout=timeout
        )

    exported = []
    for span, item in spans:
        result = exporter.export([span])
        if result is not SpanExportResult.SUCCESS:
            raise OtelExportError(
                f"导出失败：endpoint={endpoint} span={item['spanId']} result={result}"
            )
        exported.append(
            {
                "spanId": item["spanId"],
                "parentSpanId": item["parentSpanId"],
                "name": item["name"],
                "startTimeUnixNano": item["startTimeUnixNano"],
                "endTimeUnixNano": item["endTimeUnixNano"],
                "attribute_count": len(item["attributes"]),
            }
        )
    provider.shutdown()
    return {
        "endpoint": endpoint,
        # SDK 的 to_json() 返回 dict（不是 list）；这里只留 service.name 级别的摘要
        "resource": json.loads(provider.resource.to_json()).get("attributes", {}),
        "spans": exported,
        "span_count": len(exported),
    }


def post_otlp_json(trace: Trace, *, endpoint: str, timeout: float = 10.0) -> dict[str, Any]:
    """不装 extra 时的**降级路径**：OTLP/JSON over HTTP（协议允许 JSON 编码）。

    这条路径刻意保留：它让"没有 SDK"的环境也能把 trace 发到后端，代价是少了
    SDK 的批处理与重试（本仓库不做重试——一次失败即可读错误，见模块纪律 3）。
    """
    body = json.dumps(trace.to_otel(), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/v1/traces",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"endpoint": endpoint, "status": response.status, "spans": len(trace.flatten())}
    except urllib.error.URLError as exc:
        raise OtelExportError(f"OTLP/JSON 导出失败（{endpoint}）：{exc}") from exc
