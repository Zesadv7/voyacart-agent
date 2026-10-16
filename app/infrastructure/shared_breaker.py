# -*- coding: utf-8 -*-
"""Redis 原子共享熔断：单探测租约、调用代次和旧回调隔离。

正常共享路径只通过一段 Lua 读取/修改状态，时钟以 Redis 为准。中间件为每次
调用持有显式 permit，避免不同协程或嵌套工具的结果串用；探针取消/进程退出后
靠有限租期恢复。租约并不能强制停止已经发给远端的工作。

边界：未配置 Redis 时仍使用原本的进程内实现；已配置但不可用时保留 fail-open。
这种故障窗口不保证跨实例单探针，且旁路调用结果不会在恢复后修改共享状态。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
from typing import Optional
from uuid import uuid4
from weakref import WeakKeyDictionary

from app.infrastructure.cache.redis_cache import RedisCache
from app.infrastructure.resilience import CircuitBreakerRegistry

logger = logging.getLogger(__name__)
_KEY_PREFIX = "voyacart:breaker:"
_TTL_MULTIPLIER = 10

# 沿用 JSON 键，可原子升级旧状态。epoch 用随机代次，防键 TTL 到期后出现 ABA；
# 每次打开、认领探针、关闭都换代。owner + epoch + deadline 三者共同约束探针结果。
_STATE_SCRIPT = """
local action = ARGV[1]
local now = tonumber(ARGV[2])
if not now then
    local clock = redis.call('TIME')
    now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
end
local threshold = tonumber(ARGV[3])
local cooldown = tonumber(ARGV[4])
local lease = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])
local nonce = ARGV[7]
local expected_epoch = ARGV[8]
local expected_owner = ARGV[9]
local raw = redis.call('GET', KEYS[1])
local state
if raw then
    state = cjson.decode(raw)
    if not state.epoch then
        state.epoch = nonce
        state.probe_owner = ''
        state.probe_deadline = 0
        state.half_open = 0
        if not state.opened_at or state.opened_at == 0 then state.opened_at = false end
    end
else
    if expected_epoch ~= '' then return {0, '', ''} end
    state = {failures=0, opened_at=false, half_open=0, epoch=nonce,
             probe_owner='', probe_deadline=0}
end
local function save()
    state.version = 2
    redis.call('SET', KEYS[1], cjson.encode(state), 'EX', ttl)
end
local probing = state.probe_owner ~= '' and state.probe_deadline > now
if action == 'status' then
    if not state.opened_at then return {'closed'} end
    if probing then return {'half_open'} end
    return {'open'}
end
if action == 'acquire' then
    if not state.opened_at then
        save()
        return {1, state.epoch, ''}
    end
    if now - state.opened_at < cooldown or probing then return {0, '', ''} end
    state.epoch = nonce
    state.probe_owner = nonce
    state.probe_deadline = now + lease
    state.half_open = 1
    save()
    return {1, state.epoch, state.probe_owner}
end
-- 无 permit 的兼容记账只作用于 closed，绝不能越过打开/探针代次。
if expected_epoch ~= '' and expected_epoch ~= state.epoch then return {0, '', ''} end
if state.opened_at then
    if expected_epoch == '' or expected_owner == '' or
       expected_owner ~= state.probe_owner or not probing then return {0, '', ''} end
elseif expected_owner ~= '' then
    return {0, '', ''}
end
if action == 'success' then
    state.failures = 0
    if state.opened_at then state.epoch = nonce end
    state.opened_at = false
elseif action == 'failure' then
    state.failures = state.failures + 1
    if state.opened_at or state.failures >= threshold then
        state.opened_at = now
        state.epoch = nonce
    end
end
state.half_open = 0
state.probe_owner = ''
state.probe_deadline = 0
save()
return {1, state.epoch, ''}
"""


@dataclass(frozen=True)
class SharedBreakerPermit:
    """单次调用的凭据；只能提交给创建它的工具/注册表。"""

    allowed: bool
    tool_name: str
    registry_id: str
    mode: str  # shared / local / bypass
    epoch: str = ""
    owner: str = ""


class SharedCircuitBreakerRegistry(CircuitBreakerRegistry):
    """原子共享注册表；异步布尔入口仍兼容同任务中顺序 allow/record 调用。"""

    def __init__(
        self,
        cache: RedisCache,
        failure_threshold: int = 3,
        reset_seconds: float = 60.0,
        probe_lease_seconds: float = 60.0,
    ) -> None:
        if not math.isfinite(probe_lease_seconds) or probe_lease_seconds <= 0:
            raise ValueError("probe_lease_seconds 必须为有限正数")
        super().__init__(failure_threshold=failure_threshold, reset_seconds=reset_seconds)
        self._cache = cache
        self.probe_lease_seconds = probe_lease_seconds
        self._registry_id = uuid4().hex
        # 不在实例上按工具名保存唯一 owner：那会让并发调用互相覆盖。
        # 弱引用也不会因取消而保留已结束的 Task；中间件使用显式 permit，不走此兼容表。
        self._pending: WeakKeyDictionary = WeakKeyDictionary()

    @property
    def shared(self) -> bool:
        return self._cache.enabled

    def _key(self, tool_name: str) -> str:
        return f"{_KEY_PREFIX}{tool_name}"

    async def _transition(
        self, action: str, tool_name: str, now: Optional[float] = None,
        *, permit: Optional[SharedBreakerPermit] = None, lease: Optional[float] = None,
    ):
        duration = self.probe_lease_seconds if lease is None else lease
        ttl = max(60, math.ceil(self.reset_seconds * _TTL_MULTIPLIER), math.ceil(duration * 2))
        try:
            return await self._cache.client.eval(
                _STATE_SCRIPT, 1, self._key(tool_name), action,
                "" if now is None else now, self.failure_threshold, self.reset_seconds,
                duration, ttl, uuid4().hex,
                permit.epoch if permit else "", permit.owner if permit else "",
            )
        except Exception:  # noqa: BLE001 —— 明确保留 Redis 故障时的 fail-open 边界
            logger.warning("共享熔断状态不可用，按旁路处理（不保证跨实例单探针）")
            return None

    async def acquire_async(
        self, tool_name: str, now: Optional[float] = None,
        *, probe_lease_seconds: Optional[float] = None,
    ) -> SharedBreakerPermit:
        """获取绑定本次调用的 permit；生产时不传 now，使用 Redis 服务器时钟。"""
        if not self.shared:
            return SharedBreakerPermit(self.allow(tool_name, now), tool_name, self._registry_id, "local")
        if probe_lease_seconds is not None and (
            not math.isfinite(probe_lease_seconds) or probe_lease_seconds <= 0
        ):
            raise ValueError("probe_lease_seconds 必须为有限正数")
        result = await self._transition("acquire", tool_name, now, lease=probe_lease_seconds)
        if result is None:
            return SharedBreakerPermit(True, tool_name, self._registry_id, "bypass")
        return SharedBreakerPermit(bool(result[0]), tool_name, self._registry_id, "shared",
                                   _text(result[1]), _text(result[2]))

    async def allow_async(self, tool_name: str, now: Optional[float] = None) -> bool:
        permit = await self.acquire_async(tool_name, now)
        task = asyncio.current_task()
        self._pending.setdefault(task, {})[tool_name] = permit
        return permit.allowed

    def _take_permit(self, tool_name: str, permit: Optional[SharedBreakerPermit]):
        if permit is not None:
            return permit
        task = asyncio.current_task()
        pending = self._pending.get(task, {})
        result = pending.pop(tool_name, None)
        if not pending:
            self._pending.pop(task, None)
        return result

    async def _record(
        self, action: str, tool_name: str, now: Optional[float], permit: Optional[SharedBreakerPermit],
    ) -> None:
        permit = self._take_permit(tool_name, permit)
        if permit is not None:
            if (not permit.allowed or permit.tool_name != tool_name or
                    permit.registry_id != self._registry_id or permit.mode == "bypass"):
                return
            if permit.mode == "local":
                if action == "success":
                    self.record_success(tool_name)
                else:
                    self.record_failure(tool_name, now)
                return
        elif not self.shared:
            if action == "success":
                self.record_success(tool_name)
            else:
                self.record_failure(tool_name, now)
            return
        await self._transition(action, tool_name, now, permit=permit)

    async def record_success_async(
        self, tool_name: str, *, permit: Optional[SharedBreakerPermit] = None, now: Optional[float] = None,
    ) -> None:
        await self._record("success", tool_name, now, permit)

    async def record_failure_async(
        self, tool_name: str, now: Optional[float] = None, *, permit: Optional[SharedBreakerPermit] = None,
    ) -> None:
        await self._record("failure", tool_name, now, permit)

    async def status_async(self, tool_name: str, now: Optional[float] = None) -> str:
        if not self.shared:
            return self.status(tool_name)
        result = await self._transition("status", tool_name, now)
        return _text(result[0]) if result else "closed"


def _text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
