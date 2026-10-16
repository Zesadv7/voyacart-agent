# -*- coding: utf-8 -*-
"""完整公开文本的确定性审核；不是任意秘密识别器或逐 token DLP。"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, unquote, urlsplit

REDACTED = "[已脱敏]"
_INTERNAL_TOOLS = (
    "product_search_tool", "category_insight_tool", "web_search_tool",
    "create_order_tool", "query_order_tool", "cancel_order_tool",
    "remember_preference_tool", "forget_preference_tool", "update_preference_tool",
    "load_agent_skill_tool", "lookup_strategy_memory_tool", "task_dispatch",
)
_SECRET_KEY = re.compile(
    r"^(?:authorization|proxyauthorization|password|passwd|pwd|secret|clientsecret|"
    r"apikey|accesskey|secretkey|accesstoken|refreshtoken|idtoken|token|credential|"
    r"credentials|privatekey|cookie|setcookie|signature|xapikey|xamzcredential|xamzsignature)$", re.I,
)


def is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return bool(_SECRET_KEY.fullmatch(normalized) or normalized.endswith(("apikey", "accesstoken", "refreshtoken", "clientsecret", "secretaccesskey", "password")))


SENSITIVE_PATTERNS = [
    r"shopping_session_id\s*[:=]\s*[\w-]+",
    r"\bsk-[a-zA-Z0-9_-]{12,}",
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    r"(?i)\b(?:bearer|basic)\s+[a-z0-9_+./=-]+",
]
_compiled = [re.compile(pattern) for pattern in SENSITIVE_PATTERNS]
_tools = re.compile(r"\b(?:" + "|".join(_INTERNAL_TOOLS) + r")\b")
_urls = re.compile(r"https?://[^\s<>\"']+", re.I)


# 正文标签复用结构化键判据；不假定秘密必须带 sk- 等供应商前缀。
_assignment_label = re.compile(r"[\"']?(?P<key>[A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*")


def _redact_tagged_credentials(text: str) -> str:
    spans = []
    for match in _assignment_label.finditer(text):
        if not is_secret_key(match.group("key")):
            continue
        start = match.end()
        line_end = text.find("\n", start)
        end = len(text) if line_end < 0 else line_end
        if start < end and text[start] in {"\"", "'"}:
            # 引号值仅移除该字段；无引号凭据保守移除余下整行，覆盖 Cookie 多值。
            quote, cursor = text[start], start + 1
            while cursor < end:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == quote:
                    end = cursor + 1
                    break
                cursor += 1
        spans.append((match.start(), end))
    result, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            continue
        result.extend((text[cursor:start], REDACTED))
        cursor = end
    return "".join(result) + text[cursor:]


def safe_url(value: str) -> str | None:
    """公开 URL 不得含 userinfo、凭据 query、内网/本机地址或混淆控制符。"""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if any(ord(c) < 32 or c.isspace() or 0xD800 <= ord(c) <= 0xDFFF for c in decoded) or "\\" in decoded:
        return None
    if any(pattern.search(decoded) for pattern in _compiled[1:]) or _redact_tagged_credentials(decoded) != decoded:
        return None
    try:
        parsed = urlsplit(decoded)
        if (not parsed.scheme and not parsed.netloc and decoded.startswith("/products/")
                and ".." not in parsed.path.split("/") and not parsed.query and not parsed.fragment):
            return value
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.port == 0 or "." not in host
                or host.endswith((".localhost", ".local", ".internal", ".localdomain"))):
            return None
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return None
        except ValueError:
            # 浏览器会将十六/八进制、缩写、混合数字主机解释成 IPv4。
            # 只允许 ipaddress 已识别的标准地址；拒绝这些歧义别名。
            if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", host.rsplit(".", 1)[-1]):
                return None
        if any(is_secret_key(key) or key.lower().startswith("x-amz-") for key, _ in parse_qsl(parsed.query)):
            return None
        return value
    except ValueError:
        return None


def audit_output(text: str, *, redact_tools: bool = True) -> tuple[bool, str]:
    """只在完整文本上调用；协议名称不使用正文工具名规则。"""
    if not text:
        return True, text
    cleaned = _urls.sub(lambda match: safe_url(match.group()) or REDACTED, text)
    cleaned = _redact_tagged_credentials(cleaned)
    for pattern in _compiled:
        cleaned = pattern.sub(REDACTED, cleaned)
    if redact_tools:
        cleaned = _tools.sub(REDACTED, cleaned)
    return cleaned == text, cleaned
