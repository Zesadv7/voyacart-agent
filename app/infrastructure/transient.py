# -*- coding: utf-8 -*-
"""transient

上游瞬时故障判定。模型层与编排层共用同一份判据，避免两处标记表各自漂移。

优先按结构化 HTTP 状态码识别请求超时、限流和服务故障。
OpenAI 兼容网关在 SSE 流中返回限流时，异常可能只有消息；
缺少状态码时沿用文案判据，覆盖流内限流和连接超时。
"""
from __future__ import annotations

# 全小写匹配。"throttling" 来自实测：网关限流返回 code=Throttling.Concurrency
_TRANSIENT_ERROR_MARKERS = (
    "too many concurrent",
    "rate limit",
    "request rate",
    "too many requests",
    "throttling",
    "429",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "connection reset",
    "connection error",
)


def is_transient_error(error: BaseException) -> bool:
    """判断异常是否属于可重试的上游瞬时故障。"""
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        return status_code in (408, 429) or 500 <= status_code < 600
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_ERROR_MARKERS)
