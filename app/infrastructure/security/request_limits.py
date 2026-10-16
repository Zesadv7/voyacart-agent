"""直连 ASGI 的有限请求边界；速率是进程总额，不能代替集群/模型准入。"""
from __future__ import annotations

import asyncio
import math
import ipaddress
import re
import time
from collections import deque
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def validate_public_settings(settings) -> None:
    """公网配置显式失败关闭；不产生、读取或补写任何服务端密钥。"""
    if settings.identity_mode != "hmac" or not settings.session_owner_binding or not settings.output_guard_enabled:
        raise ValueError("PUBLIC_DEPLOYMENT 必须启用 hmac 身份、SESSION_OWNER_BINDING 和 OUTPUT_GUARD_ENABLED")
    secret = settings.identity_hmac_secret
    # 长度/重复字符检查仅排除明显弱值，不能证明熵；运维仍须使用密码学随机密钥。
    if len(secret.encode("utf-8")) < 32 or len(set(secret)) < 16:
        raise ValueError("PUBLIC_DEPLOYMENT 需要至少 32 字节的独立随机 IDENTITY_HMAC_SECRET，禁止重复弱值")
    origins = settings.cors_origins
    if not origins:
        raise ValueError("PUBLIC_DEPLOYMENT 需要明确的 HTTPS CORS_ORIGINS")
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            host = parsed.hostname or ""
            try:
                ipaddress.ip_address(host)
                valid_host = True
            except ValueError:
                valid_host = bool(host) and len(host) <= 253 and all(
                    re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                    for label in host.split("."))
            valid = (parsed.scheme == "https" and valid_host and parsed.port != 0
                     and parsed.username is None and parsed.password is None and not parsed.path
                     and not parsed.query and not parsed.fragment and "*" not in origin
                     and not any(character.isspace() for character in origin))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("PUBLIC_DEPLOYMENT 的 CORS_ORIGINS 必须是明确 HTTPS Origin，不含路径、凭据或通配符")
    if type(settings.token_budget_total) is not int or settings.token_budget_total <= 0:
        raise ValueError("PUBLIC_DEPLOYMENT 必须设置有限正值 TOKEN_BUDGET_TOTAL")
    if type(settings.http_requests_per_minute) is not int or settings.http_requests_per_minute <= 0:
        raise ValueError("PUBLIC_DEPLOYMENT 必须设置有限正值 HTTP_REQUESTS_PER_MINUTE（每进程）")
    request_limit_options(settings)


def request_limit_options(settings) -> dict:
    """供 server 注册使用；兼容现有只包含少数字段的测试容器。"""
    maximum = getattr(settings, "http_max_request_bytes", 1048576)
    timeout = getattr(settings, "http_body_timeout_seconds", 30.0)
    rate = getattr(settings, "http_requests_per_minute", 0)
    if type(maximum) is not int or not 1 <= maximum <= 16 * 1048576:
        raise ValueError("HTTP_MAX_REQUEST_BYTES 必须为 1 至 16777216 字节")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("HTTP_BODY_TIMEOUT_SECONDS 必须为 0 至 300 秒之间的有限正值")
    if type(rate) is not int or not 0 <= rate <= 100000:
        raise ValueError("HTTP_REQUESTS_PER_MINUTE 必须为 0 至 100000（0 仅限私有环境）")
    return {"max_request_bytes": maximum, "body_timeout_seconds": timeout, "requests_per_minute": rate}


class RequestLimitsMiddleware:
    """先读取有限 body，再交给路由，避免超限前半段触发业务副作用。

    不信任 Content-Length 或 X-Forwarded-For；实际分片也累计计数。
    仅 /commerce 和 /internal 参与全进程滚动速率限额；健康检查仍可访问。
    不是 DDoS 防护，不管理后台运行容量，也不在 worker 间共享计数。
    """

    def __init__(self, app, *, max_request_bytes=1048576, body_timeout_seconds=30.0,
                 requests_per_minute=0, clock=time.monotonic):
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.body_timeout_seconds = body_timeout_seconds
        self.requests_per_minute = requests_per_minute
        self.clock = clock
        self._arrivals = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status, code, message, headers=None):
            await JSONResponse({"detail": {"code": code, "message": message}}, status_code=status,
                               headers={"Cache-Control": "no-store", **(headers or {})})(scope, receive, send)

        path = scope.get("path", "")
        limited = path in {"/commerce", "/internal"} or path.startswith(("/commerce/", "/internal/"))
        if self.requests_per_minute and limited and scope.get("method") != "OPTIONS":
            now = self.clock()
            while self._arrivals and self._arrivals[0] <= now - 60:
                self._arrivals.popleft()
            if len(self._arrivals) >= self.requests_per_minute:
                retry = max(1, math.ceil(60 - (now - self._arrivals[0])))
                await reject(429, "HTTP_RATE_LIMIT", "请求过于频繁，请稍后重试", {"Retry-After": str(retry)})
                return
            # 此段没有 await，同一事件循环的检查与写入不可交错。
            self._arrivals.append(now)

        lengths = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                await reject(400, "INVALID_CONTENT_LENGTH", "请求长度声明无效")
                return
            # 超长十进制字符串不传给 int，避免无必要的转换消耗/转换长度异常。
            if len(lengths[0]) > 10 or int(lengths[0]) > self.max_request_bytes:
                await reject(413, "REQUEST_TOO_LARGE", "请求正文超出允许大小")
                return

        body = bytearray()
        try:
            async with asyncio.timeout(self.body_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_request_bytes:
                        await reject(413, "REQUEST_TOO_LARGE", "请求正文超出允许大小")
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408, "REQUEST_BODY_TIMEOUT", "接收请求正文超时")
            return

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # StreamingResponse 仍能看到真实断连；不可伪造 disconnect 截断 SSE。
            return await receive()

        await self.app(scope, replay, send)
