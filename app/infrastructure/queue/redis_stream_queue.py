# -*- coding: utf-8 -*-
"""Redis Stream 可靠消费与跨进程事件背板。

处理任务前同时持有消息、task 与会话租约；只有租约有效且结果成功才确认。
重投语义为 at-least-once，业务写入仍须使用独立的确认及幂等约束。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
from app.infrastructure.operational_metrics import observe_queue
import os
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from app.domain.queue.ports.task_queue import IntentTask, QueueCapacityExceeded, QueueDelivery, TaskQueue, TaskStatus
from app.infrastructure.eventbus import TradeEvent

logger = logging.getLogger(__name__)
_STREAM = "voyacart:intents"
_LARGE_STREAM = "voyacart:intents:large"
_DEAD_STREAM = "voyacart:intents:dead"
_GROUP = "voyacart-workers"
_STATUS_PREFIX = "voyacart:task:"
# 仅保留旧测试/升级诊断的符号；非终态身份不再设置 TTL。
_STATUS_TTL = 3600
_DEADLINE_INDEX = "voyacart:queue:deadlines"
_EXPIRED_INDEX = "voyacart:queue:expired"
_QUEUE_DEADLINE_ERROR = "QUEUE_DEADLINE_EXCEEDED"
_EVENT_CHANNEL_PREFIX = "voyacart:events:"
_LEASE_PREFIX = "voyacart:lease:"

# 两类索引只用于有界查找；任务状态与完整 payload 仍是读取依据。
_DEADLINE_HELPERS = """
local function now_seconds()
  local now = redis.call('TIME')
  return tonumber(now[1]) + tonumber(now[2]) / 1000000
end
local function check_indexes(waiting, expired)
  for _, key in ipairs({waiting, expired}) do
    local kind = redis.call('TYPE', key).ok
    if kind ~= 'none' and kind ~= 'zset' then error('WRONGTYPE queue expiry index') end
  end
end
local function expire_task(key, status, waiting, expired, now)
  check_indexes(waiting, expired)
  status.state = 'failed'
  status.error = 'QUEUE_DEADLINE_EXCEEDED'
  status.final_text = ''
  status.terminal_at = now
  redis.call('SET', key, cjson.encode(status))
  redis.call('ZREM', waiting, status.task_id)
  if status.task and type(status.task.ag_ui_input) == 'table' then
    redis.call('ZADD', expired, now, status.task_id)
  end
end
local function due(status, now)
  return status.state ~= 'running' and status.state ~= 'done' and status.state ~= 'failed'
    and status.queue_deadline_at and tonumber(status.queue_deadline_at) <= now
end
"""
_ENQUEUE_SCRIPT = _DEADLINE_HELPERS + """
local old = redis.call('GET', KEYS[1])
if old then
  if cjson.decode(old).payload_fingerprint ~= ARGV[4] then return -1 end
  return 0
end
local depth = 0
for i = 3, 4 do
  if redis.call('EXISTS', KEYS[i]) == 1 then
    local count = redis.call('XLEN', KEYS[i])
    for _, group in ipairs(redis.call('XINFO', 'GROUPS', KEYS[i])) do
      local info = {}
      for j = 1, #group, 2 do info[group[j]] = group[j + 1] end
      if info.name == ARGV[6] then
        -- Redis 在手工更改 group 游标后可能无法计算 lag；保守使用 XLEN。
        count = tonumber(info.pending) + (tonumber(info.lag) or count)
      end
    end
    depth = depth + count
  end
end
if depth >= tonumber(ARGV[5]) then return -2 end
-- Lua 运行时异常不会回滚，先校验辅助索引类型，避免 XADD 后才报错。
local index_type = redis.call('TYPE', KEYS[5]).ok
if index_type ~= 'none' and index_type ~= 'zset' then
  return redis.error_reply('WRONGTYPE queue deadline index')
end
local initial = cjson.decode(ARGV[1])
initial.payload_fingerprint = ARGV[4]
initial.accepted_at = now_seconds()
initial.queue_deadline_at = initial.accepted_at + tonumber(ARGV[3])
redis.call('XADD', KEYS[2], '*', 'payload', ARGV[2])
redis.call('SET', KEYS[1], cjson.encode(initial))
redis.call('ZADD', KEYS[5], initial.queue_deadline_at, initial.task_id)
return 1
"""
_STATUS_SCRIPT = _DEADLINE_HELPERS + """
for i = 4, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[3] then return -1 end
end
local old = redis.call('GET', KEYS[1])
-- 执行期间身份丢失不能靠状态更新重建，否则会丢掉指纹并再次执行。
if not old and #KEYS > 3 then return -2 end
local updated = cjson.decode(ARGV[1])
local now = now_seconds()
if old then
  old = cjson.decode(old)
  updated.payload_fingerprint = old.payload_fingerprint
  updated.task = old.task
  updated.accepted_at = old.accepted_at
  updated.queue_deadline_at = old.queue_deadline_at
  updated.started_at = old.started_at
  if old.state == 'done' or old.state == 'failed' then return 0 end
  if ARGV[2] == 'queued' and old.state ~= 'queued' then return 0 end
  if ARGV[2] == 'running' and due(old, now) then
    expire_task(KEYS[1], old, KEYS[2], KEYS[3], now)
    return 2
  end
end
if ARGV[2] == 'done' or ARGV[2] == 'failed' then
  updated.terminal_at = now
  redis.call('ZREM', KEYS[2], updated.task_id)
elseif ARGV[2] == 'running' then
  updated.started_at = updated.started_at or now
  redis.call('ZREM', KEYS[2], updated.task_id)
elseif updated.queue_deadline_at then
  redis.call('ZADD', KEYS[2], updated.queue_deadline_at, updated.task_id)
end
redis.call('SET', KEYS[1], cjson.encode(updated))
return 1
"""
_CHECK_EXECUTION_SCRIPT = _DEADLINE_HELPERS + """
for i = 4, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[1] then return -1 end
end
local raw = redis.call('GET', KEYS[1])
if not raw then return -2 end
local status = cjson.decode(raw)
if status.state == 'done' or status.state == 'failed' then return 0 end
-- 兼容升级前无服务端期限的消息，以 Redis Stream ID 的服务端时间界定等待。
-- 不信任任务中客户端提供的 enqueued_at，也不扫描/修复已丢失的旧身份。
if not status.queue_deadline_at then
  local accepted = tonumber(string.match(ARGV[2], '^(%d+)%-'))
  status.queue_deadline_at = (accepted and accepted / 1000 or now_seconds()) + tonumber(ARGV[3])
  check_indexes(KEYS[2], KEYS[3])
  redis.call('SET', KEYS[1], cjson.encode(status))
  if status.state ~= 'running' then
    redis.call('ZADD', KEYS[2], status.queue_deadline_at, status.task_id)
  end
end
local now = now_seconds()
if due(status, now) then
  expire_task(KEYS[1], status, KEYS[2], KEYS[3], now)
  return 2
end
return 1
"""
_EXPIRE_DUE_SCRIPT = _DEADLINE_HELPERS + """
check_indexes(KEYS[1], KEYS[2])
local now = now_seconds()
local expired_count = 0
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, ARGV[2])
for _, id in ipairs(ids) do
  local key = ARGV[1] .. id
  local raw = redis.call('GET', key)
  if raw then
    local status = cjson.decode(raw)
    if due(status, now) then
      expire_task(key, status, KEYS[1], KEYS[2], now)
      expired_count = expired_count + 1
    elseif status.state == 'running' or status.state == 'done' or status.state == 'failed' then
      redis.call('ZREM', KEYS[1], id)
    end
  else
    -- 丢失的身份不能编造成功或以当前请求重建，留下 Stream 审计来源。
    redis.call('ZREM', KEYS[1], id)
  end
end
local result = {}
for _, id in ipairs(redis.call('ZRANGE', KEYS[2], 0, tonumber(ARGV[2]) - 1)) do
  local raw = redis.call('GET', ARGV[1] .. id)
  if raw then
    local status = cjson.decode(raw)
    if status.state == 'failed' and status.error == 'QUEUE_DEADLINE_EXCEEDED'
      and status.task and type(status.task.ag_ui_input) == 'table' then
      table.insert(result, cjson.encode(status.task))
      -- 尚未 ACK 的恢复项轮转，避免一项失败永久饿死其后的有界批次。
      redis.call('ZADD', KEYS[2], now, id)
    else
      redis.call('ZREM', KEYS[2], id)
    end
  else
    redis.call('ZREM', KEYS[2], id)
  end
end
return {expired_count, result}
"""
_ACK_EXPIRED_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local status = cjson.decode(raw)
if status.state ~= 'failed' or status.error ~= 'QUEUE_DEADLINE_EXCEEDED' then return 0 end
return redis.call('ZREM', KEYS[2], ARGV[1])
"""
_RENEW_SCRIPT = """
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[1] then return 0 end
end
if ARGV[3] ~= '' then
  local ids = redis.call('XCLAIM', ARGV[3], ARGV[4], ARGV[5], 0, ARGV[6], 'JUSTID')
  if #ids == 0 then return 0 end
end
for i = 1, #KEYS do redis.call('PEXPIRE', KEYS[i], ARGV[2]) end
return 1
"""
_RELEASE_SCRIPT = """
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then redis.call('DEL', KEYS[i]) end
end
return 1
"""
_ACK_SCRIPT = """
for i = 2, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[3] then return -1 end
end
return redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
"""
_DEAD_SCRIPT = """
local added = 0
for i = 5, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[8] then return -1 end
end
if redis.call('EXISTS', KEYS[3]) == 0 then
  local old = redis.call('GET', KEYS[2])
  if not old or (cjson.decode(old).state ~= 'done' and cjson.decode(old).state ~= 'failed') then
    redis.call('XADD', ARGV[1], '*', 'payload', ARGV[2], 'reason', ARGV[3],
      'stream', KEYS[1], 'message_id', ARGV[4], 'deliveries', ARGV[5], 'task_id', ARGV[6])
    added = 1
    if ARGV[6] ~= '' then
      local failed = cjson.decode(ARGV[7])
      if old then
        failed.payload_fingerprint = cjson.decode(old).payload_fingerprint
        failed.task = cjson.decode(old).task
        failed.accepted_at = cjson.decode(old).accepted_at
        failed.queue_deadline_at = cjson.decode(old).queue_deadline_at
        failed.started_at = cjson.decode(old).started_at
      end
      local now = redis.call('TIME')
      failed.terminal_at = tonumber(now[1]) + tonumber(now[2]) / 1000000
      redis.call('SET', KEYS[2], cjson.encode(failed))
      redis.call('ZREM', KEYS[4], ARGV[6])
    end
  end
  redis.call('SET', KEYS[3], '1')
end
redis.call('XACK', KEYS[1], ARGV[9], ARGV[4])
return added
"""


class QueueLeaseLost(RuntimeError):
    """执行权已过期，后续任务状态与会话快照不得提交。"""


class SessionLeaseTimeout(TimeoutError):
    """同一会话的前一个执行仍未结束。"""


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _lease_key(kind: str, identifier: str) -> str:
    digest = hashlib.sha256(identifier.encode()).hexdigest()
    return f"{_LEASE_PREFIX}{kind}:{digest}"


@dataclass
class ExecutionLease:
    session_id: str
    owner: str
    lease_ms: int
    consumer_name: str = ""
    delivery: QueueDelivery | None = None
    keys: list[str] = field(default_factory=list)
    expires_at: float = 0.0
    valid: bool = True
    ready: bool = False

    def is_valid(self) -> bool:
        """同步持久化护栏；deadline 从请求发出时计算，避免网络延迟虚增租期。"""
        return self.valid and self.ready and time.monotonic() < self.expires_at

    def invalidate(self) -> None:
        self.valid = False


_execution_lease: ContextVar[ExecutionLease | None] = ContextVar("voyacart_execution_lease", default=None)


def current_execution_lease() -> ExecutionLease | None:
    return _execution_lease.get()


class RedisStreamTaskQueue(TaskQueue):
    def __init__(self, client: Any, archive: Any = None, *, max_pending: int = 200,
                 max_age_seconds: float = 900) -> None:
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending 必须是正整数")
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds 必须是有限正数")
        self._client = client
        self._archive = archive
        self._max_pending = max_pending
        self._max_age_seconds = float(max_age_seconds)
        self._claim_cursors: dict[str, str] = {_STREAM: "0-0", _LARGE_STREAM: "0-0"}

    async def ensure_group(self) -> None:
        for stream in (_STREAM, _LARGE_STREAM):
            try:
                await self._client.xgroup_create(stream, _GROUP, id="0", mkstream=True)
            except Exception as err:
                if "BUSYGROUP" not in str(err):
                    raise

    async def enqueue(self, task: IntentTask, *, max_pending: int | None = None) -> None:
        """状态与消息原子可见；同一个 task_id 不会重复入队。

        Lua 不回滚运行时错误，因此先 XADD，避免流类型异常时留下幽灵 queued。
        """
        stream = _LARGE_STREAM if task.priority > 0 else _STREAM
        initial = json.loads(self._status_payload(TaskStatus(task.task_id, "queued")))
        initial["task"] = task.to_dict()
        initial = json.dumps(initial, ensure_ascii=False)
        semantic_payload = {key: value for key, value in task.to_dict().items()
                            if key not in {"enqueued_at", "priority", "traceparent", "tracestate", "request_id"}}
        fingerprint = hashlib.sha256(json.dumps(semantic_payload, ensure_ascii=False,
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if max_pending is not None and (type(max_pending) is not int or max_pending < 1):
            raise ValueError("max_pending 必须是正整数")
        effective_limit = min(self._max_pending, max_pending) if max_pending is not None else self._max_pending
        result = await self._client.eval(_ENQUEUE_SCRIPT, 5, f"{_STATUS_PREFIX}{task.task_id}", stream, _STREAM, _LARGE_STREAM,
                                        _DEADLINE_INDEX, initial, json.dumps(task.to_dict(), ensure_ascii=False),
                                        self._max_age_seconds, fingerprint, effective_limit, _GROUP)
        if result == -2:
            observe_queue("rejected_full")
            raise QueueCapacityExceeded("运行队列已满，请稍后重试")
        if result == -1:
            raise ValueError("相同 request_id 已用于不同的选购需求")
        if result == 1:
            observe_queue("enqueued")
        elif result == 0:
            observe_queue("replayed")

    async def get_task(self, task_id: str) -> IntentTask | None:
        raw = await self._client.get(f"{_STATUS_PREFIX}{task_id}")
        if not raw:
            return None
        data = json.loads(raw)
        if data.get("archived"):
            if self._archive is None:
                raise RuntimeError("该任务已归档，但当前进程未配置归档读取")
            data = await self._archive.get_status(task_id)
            if data is None:
                raise RuntimeError("任务归档丢失，不能当作任务不存在或重新执行")
        payload = data.get("task")
        return IntentTask.from_dict(payload) if payload is not None else None

    async def expire_due(self, *, limit: int = 100) -> list[IntentTask]:
        """有界收口未启动任务，重放尚未由 journal 持久确认的 AGUI 过期项。

        两个批次各不超过 limit；消费者仍须读取终态并 ACK 原始消息。
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须是 1 到 1000 的整数")
        expired_count, payloads = await self._client.eval(_EXPIRE_DUE_SCRIPT, 2, _DEADLINE_INDEX,
                                                          _EXPIRED_INDEX, _STATUS_PREFIX, limit)
        if expired_count:
            observe_queue("expired", count=int(expired_count))
            observe_queue("failed", count=int(expired_count))
        return [IntentTask.from_dict(json.loads(raw)) for raw in payloads]

    async def acknowledge_expired(self, task_id: str) -> None:
        """调用方已持久确认 journal 终态后，才移除待恢复索引；不删除任务身份。"""
        await self._client.eval(_ACK_EXPIRED_SCRIPT, 2, f"{_STATUS_PREFIX}{task_id}",
                                _EXPIRED_INDEX, task_id)

    @staticmethod
    def _status_payload(status: TaskStatus) -> str:
        return json.dumps({"task_id": status.task_id, "state": status.state,
                           "final_text": status.final_text, "error": status.error,
                           "stream": status.stream, "message_id": status.message_id,
                           "deliveries": status.deliveries}, ensure_ascii=False)

    async def set_status(self, status: TaskStatus) -> None:
        lease = current_execution_lease()
        keys = [f"{_STATUS_PREFIX}{status.task_id}", _DEADLINE_INDEX, _EXPIRED_INDEX]
        if lease is not None:
            if not lease.is_valid():
                raise QueueLeaseLost("执行租约已失效，拒绝写入任务状态")
            keys.extend(lease.keys)
            if lease.delivery:
                status = TaskStatus(status.task_id, status.state, status.final_text, status.error,
                                    stream=lease.delivery.stream, message_id=lease.delivery.message_id,
                                    deliveries=lease.delivery.deliveries)
        result = await self._client.eval(_STATUS_SCRIPT, len(keys), *keys,
                                         self._status_payload(status), status.state, lease.owner if lease else "")
        if result in {-1, -2}:
            if lease:
                lease.invalidate()
            raise QueueLeaseLost("任务身份缺失，不能重建或执行" if result == -2 else "执行权已被其他实例接管")
        if result == 2:
            observe_queue("expired")
            observe_queue("failed")
        if result == 1 and status.state in {"running", "retrying", "done", "failed"}:
            observe_queue({"running": "started", "retrying": "retried", "done": "completed", "failed": "failed"}[status.state])

    async def get_status(self, task_id: str) -> Optional[TaskStatus]:
        raw = await self._client.get(f"{_STATUS_PREFIX}{task_id}")
        if raw is None:
            return None
        data = json.loads(raw)
        if data.get("archived"):
            if self._archive is None:
                raise RuntimeError("该任务已归档，但当前进程未配置归档读取，不能返回空的成功结果")
            restored = await self._archive.get_status(task_id)
            if restored is None:
                raise RuntimeError("任务归档丢失，请恢复归档库后读取结果；不会重新执行任务")
            data = restored
        return TaskStatus(task_id=data["task_id"], state=data["state"],
                          final_text=data.get("final_text", ""), error=data.get("error", ""),
                          queue_position=await self.depth() if data["state"] == "queued" else 0,
                          stream=data.get("stream", ""), message_id=data.get("message_id", ""),
                          deliveries=int(data.get("deliveries", 0)))

    async def depth(self) -> int:
        return sum([await self._stream_depth(stream) for stream in (_STREAM, _LARGE_STREAM)])

    async def _stream_depth(self, stream: str) -> int:
        try:
            groups = await self._client.xinfo_groups(stream)
        except Exception as err:
            # 尚未建流是健康空队列；连接/类型等故障必须传播，不能伪装为 depth=0。
            if "no such key" in str(err).lower():
                return 0
            raise
        for group in groups:
            if _text(group.get("name", group.get(b"name", ""))) == _GROUP:
                lag = group.get("lag", group.get(b"lag", 0))
                pending = group.get("pending", group.get(b"pending", 0))
                return int(lag if lag is not None else await self._client.xlen(stream)) + int(pending or 0)
        return int(await self._client.xlen(stream))

    async def _acquire(self, lease: ExecutionLease, key: str) -> bool:
        started = time.monotonic()
        acquired = await asyncio.wait_for(self._client.set(key, lease.owner, nx=True, px=lease.lease_ms),
                                          timeout=lease.lease_ms / 1000)
        if acquired:
            lease.keys.append(key)
            expires = started + lease.lease_ms / 1000
            lease.expires_at = min(lease.expires_at, expires) if lease.expires_at else expires
        return bool(acquired)

    async def _heartbeat(self, lease: ExecutionLease, owner_task: asyncio.Task, interval_ms: int) -> None:
        try:
            while lease.valid:
                await asyncio.sleep(interval_ms / 1000)
                started = time.monotonic()
                if started >= lease.expires_at:
                    raise QueueLeaseLost("续租开始前租约已过期")
                delivery = lease.delivery
                renewed = await asyncio.wait_for(self._client.eval(
                    _RENEW_SCRIPT, len(lease.keys), *lease.keys, lease.owner, lease.lease_ms,
                    delivery.stream if delivery else "", _GROUP, lease.consumer_name,
                    delivery.message_id if delivery else ""),
                    timeout=max(0.001, lease.expires_at - started))
                if not renewed:
                    raise QueueLeaseLost("Redis 租约或 pending 所有权已丢失")
                lease.expires_at = started + lease.lease_ms / 1000
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("执行租约失效，取消当前任务：%s", err)
            lease.invalidate()
            owner_task.cancel()

    async def _release(self, lease: ExecutionLease) -> None:
        lease.invalidate()
        if not lease.keys:
            return
        try:
            await asyncio.wait_for(self._client.eval(_RELEASE_SCRIPT, len(lease.keys), *lease.keys, lease.owner), 2)
        except Exception as err:
            logger.warning("释放租约失败，等待 TTL 回收：%s", err)

    @asynccontextmanager
    async def session_lease(self, session_id: str, *, wait_timeout: float = 30.0,
                            lease_ms: int = 30000) -> AsyncIterator[ExecutionLease]:
        """API 与 worker 共用会话租约；worker 已持有同会话租约时直接复用。"""
        existing = current_execution_lease()
        if existing is not None:
            if existing.session_id != session_id or not existing.is_valid():
                raise QueueLeaseLost("当前执行上下文未持有该会话的有效租约")
            yield existing
            return
        if lease_ms < 100 or wait_timeout < 0:
            raise ValueError("lease_ms 至少为 100，wait_timeout 不能为负")
        lease = ExecutionLease(session_id, uuid.uuid4().hex, lease_ms)
        heartbeat: asyncio.Task | None = None
        token = None
        deadline = time.monotonic() + wait_timeout
        try:
            while not await self._acquire(lease, _lease_key("session", session_id)):
                if time.monotonic() >= deadline:
                    raise SessionLeaseTimeout("当前会话仍在处理中，请稍后重试")
                await asyncio.sleep(min(0.1, max(0.001, deadline - time.monotonic())))
            lease.ready = True
            token = _execution_lease.set(lease)
            heartbeat = asyncio.create_task(self._heartbeat(lease, asyncio.current_task(), max(10, lease_ms // 3)))
            yield lease
            if not lease.is_valid():
                raise QueueLeaseLost("会话执行完成前租约已失效")
        finally:
            lease.invalidate()
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if token is not None:
                _execution_lease.reset(token)
            await self._release(lease)

    async def _delivery_count(self, stream: str, message_id: str) -> int:
        pending = await self._client.xpending_range(stream, _GROUP, message_id, message_id, 1)
        if not pending:
            return 1
        return int(pending[0].get("times_delivered", pending[0].get(b"times_delivered", 1)))

    async def _delivery(self, stream: str, message_id: Any, fields: dict) -> QueueDelivery:
        message_id = _text(message_id)
        payload = fields.get("payload", fields.get(b"payload"))
        return QueueDelivery(stream, message_id, _text(payload) if payload is not None else "",
                             await self._delivery_count(stream, message_id))

    async def claim_stale(self, consumer_name: str, idle_ms: int = 60000,
                          count: int = 10) -> list[QueueDelivery]:
        """双流回收实际消息信封，不丢失 ACK 与审计所需的 stream/message_id。"""
        deliveries: list[QueueDelivery] = []
        for stream in (_STREAM, _LARGE_STREAM):
            if len(deliveries) >= count:
                break
            result = await self._client.xautoclaim(stream, _GROUP, consumer_name,
                min_idle_time=idle_ms, start_id=self._claim_cursors[stream], count=count - len(deliveries))
            self._claim_cursors[stream] = _text(result[0])
            for message_id, fields in result[1]:
                deliveries.append(await self._delivery(stream, message_id, fields))
        return deliveries

    async def consume(self, consumer_name: str, handler: Callable[[IntentTask], Awaitable[Any]],
                      should_stop: Callable[[], bool], block_ms: int = 2000,
                      max_deliveries: int = 3, concurrency: int = 1,
                      reclaim_idle_ms: int = 60000, lease_ms: int = 30000,
                      heartbeat_interval_ms: int | None = None) -> None:
        if concurrency < 1 or max_deliveries < 1 or lease_ms < 100 or reclaim_idle_ms < 30:
            raise ValueError("并发度/最大投递次数须为正，租期至少 100ms，回收间隔至少 30ms")
        heartbeat_ms = heartbeat_interval_ms or max(10, min(lease_ms, reclaim_idle_ms) // 3)
        if not 0 < heartbeat_ms < min(lease_ms, reclaim_idle_ms):
            raise ValueError("心跳间隔必须小于租期与回收等待时间")
        await self.ensure_group()
        in_flight: set[asyncio.Task] = set()
        try:
            while not should_stop():
                finished = {task for task in in_flight if task.done()}
                for task in finished:
                    with suppress(asyncio.CancelledError):
                        error = task.exception()
                        if error:
                            logger.warning("消费任务异常，保留 pending：%s", error)
                in_flight.difference_update(finished)
                free_slots = concurrency - len(in_flight)
                if free_slots <= 0:
                    await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                    continue
                try:
                    ready = await self.claim_stale(consumer_name, reclaim_idle_ms, free_slots)
                    # COUNT 对多 stream 是分别生效的；逐流领取，避免实际执行翻倍。
                    for stream in (_STREAM, _LARGE_STREAM):
                        remaining = free_slots - len(ready)
                        if remaining <= 0:
                            break
                        batches = await self._client.xreadgroup(_GROUP, consumer_name, {stream: ">"}, count=remaining)
                        for _stream, entries in batches:
                            for message_id, fields in entries:
                                ready.append(await self._delivery(_text(_stream), message_id, fields))
                except Exception as err:
                    logger.warning("队列读取失败，稍后重试：%s", err)
                    await asyncio.sleep(min(max(block_ms / 1000, 0.01), 1.0))
                    continue
                for delivery in ready:
                    in_flight.add(asyncio.create_task(self._handle_one(
                        delivery.stream, delivery.message_id, {"payload": delivery.payload}, handler,
                        max_deliveries, consumer_name=consumer_name, delivery=delivery,
                        lease_ms=lease_ms, heartbeat_interval_ms=heartbeat_ms)))
                if not ready:
                    await asyncio.sleep(min(max(block_ms / 1000, 0.01), 1.0))
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        except asyncio.CancelledError:
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    async def _ack(self, delivery: QueueDelivery, lease: ExecutionLease | None = None) -> None:
        if lease is None:
            await self._client.xack(delivery.stream, _GROUP, delivery.message_id)
            return
        if not lease.is_valid():
            raise QueueLeaseLost("租约失效，不能确认消息")
        result = await self._client.eval(_ACK_SCRIPT, 1 + len(lease.keys), delivery.stream, *lease.keys,
                                         _GROUP, delivery.message_id, lease.owner)
        if result == -1:
            lease.invalidate()
            raise QueueLeaseLost("ACK 前租约已丢失")

    async def _dead_letter(self, delivery: QueueDelivery, reason: str, task_id: str = "",
                           lease: ExecutionLease | None = None) -> None:
        if lease is not None and not lease.is_valid():
            raise QueueLeaseLost("租约失效，不能提交死信")
        status = TaskStatus(task_id, "failed", error=reason, stream=delivery.stream,
                            message_id=delivery.message_id, deliveries=delivery.deliveries)
        marker = _lease_key("dead", f"{delivery.stream}/{delivery.message_id}")
        keys = [delivery.stream, f"{_STATUS_PREFIX}{task_id}", marker, _DEADLINE_INDEX, *(lease.keys if lease else [])]
        result = await self._client.eval(_DEAD_SCRIPT, len(keys), *keys, _DEAD_STREAM,
            delivery.payload, reason, delivery.message_id, delivery.deliveries, task_id,
            self._status_payload(status), lease.owner if lease else "", _GROUP)
        if result == -1:
            raise QueueLeaseLost("提交死信时执行权已丢失")
        if result == 1:
            observe_queue("dead_lettered")
            if task_id:
                observe_queue("failed")

    async def _may_execute(self, task: IntentTask, delivery: QueueDelivery,
                           lease: ExecutionLease) -> bool:
        result = await self._client.eval(_CHECK_EXECUTION_SCRIPT, 3 + len(lease.keys),
            f"{_STATUS_PREFIX}{task.task_id}", _DEADLINE_INDEX, _EXPIRED_INDEX, *lease.keys,
            lease.owner, delivery.message_id, self._max_age_seconds)
        if not lease.valid or time.monotonic() >= lease.expires_at:
            lease.invalidate()
            raise QueueLeaseLost("检查执行资格返回时本地租约已失效")
        if result == -2:
            lease.invalidate()
            raise QueueLeaseLost("任务身份缺失，保留消息等待恢复，不能重新执行")
        if result == -1:
            lease.invalidate()
            raise QueueLeaseLost("检查执行资格时租约已失效")
        if result == 2:
            observe_queue("expired")
            observe_queue("failed")
        return result == 1

    async def _handle_one(self, stream: str, message_id: str, fields: dict,
                          handler: Callable[[IntentTask], Awaitable[Any]], max_deliveries: int,
                          *, consumer_name: str = "manual", delivery: QueueDelivery | None = None,
                          lease_ms: int = 30000, heartbeat_interval_ms: int = 10000) -> None:
        delivery = delivery or await self._delivery(stream, message_id, fields)
        try:
            task = IntentTask.from_dict(json.loads(delivery.payload))
            if not task.task_id or not task.shopping_session_id:
                raise ValueError("task_id 与 shopping_session_id 不能为空")
        except Exception as err:
            await self._dead_letter(delivery, f"任务解析失败：{err}")
            return
        existing = await self.get_status(task.task_id)
        if existing and existing.state in {"done", "failed"}:
            await self._ack(delivery)
            return
        lease = ExecutionLease(task.shopping_session_id, uuid.uuid4().hex, lease_ms,
                               consumer_name=consumer_name, delivery=delivery)
        heartbeat: asyncio.Task | None = None
        token = None
        try:
            for key in (_lease_key("message", f"{stream}/{message_id}"), _lease_key("task", task.task_id)):
                if not await self._acquire(lease, key):
                    return
            heartbeat = asyncio.create_task(self._heartbeat(lease, asyncio.current_task(), heartbeat_interval_ms))
            session_wait_deadline = time.monotonic() + self._max_age_seconds
            while True:
                if not lease.valid or time.monotonic() >= lease.expires_at:
                    raise QueueLeaseLost("等待会话执行权时租约已失效")
                if not await self._may_execute(task, delivery, lease):
                    # 等待者只持 message/task 租约也能确认已终结消息，不夺取会话租约。
                    lease.ready = True
                    await self._ack(delivery, lease)
                    return
                if await self._acquire(lease, _lease_key("session", task.shopping_session_id)):
                    break
                if time.monotonic() >= session_wait_deadline:
                    # 已 running 的重投由运行 journal 收口，不能以排队错误覆盖它。
                    # 释放本 owner 的租约并留 pending，避免一个 consumer 槽无限等待。
                    raise SessionLeaseTimeout("等待会话租约超过本次领取期限")
                await asyncio.sleep(min(0.1, heartbeat_interval_ms / 1000))
            lease.ready = True
            token = _execution_lease.set(lease)
            existing = await self.get_status(task.task_id)
            if existing and existing.state in {"done", "failed"}:
                await self._ack(delivery, lease)
                return
            if delivery.deliveries > max_deliveries:
                await self._dead_letter(delivery, "消息超过最大投递次数", task.task_id, lease)
                return
            await self.set_status(TaskStatus(task.task_id, "running"))
            # 过期维护可先于 running Lua 胜出；不能只依靠先前 Python 的状态读取。
            if not await self._may_execute(task, delivery, lease):
                await self._ack(delivery, lease)
                return
            final_text = await handler(task)
            await self.set_status(TaskStatus(task.task_id, "done", final_text=final_text if isinstance(final_text, str) else ""))
            await self._ack(delivery, lease)
        except asyncio.CancelledError:
            lease.invalidate()
            raise
        except QueueLeaseLost as err:
            lease.invalidate()
            logger.warning("失去执行权，消息留在 pending：%s（%s）", task.task_id, err)
        except Exception as err:
            if not lease.is_valid():
                logger.warning("租约不可用，保留 pending：%s（%s）", task.task_id, err)
                return
            if delivery.deliveries >= max_deliveries:
                await self._dead_letter(delivery, str(err), task.task_id, lease)
            else:
                await self.set_status(TaskStatus(task.task_id, "retrying", error=str(err)))
                logger.warning("任务第 %d 次失败，等待重投：%s（%s）", delivery.deliveries, task.task_id, err)
        finally:
            lease.invalidate()
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if token is not None:
                _execution_lease.reset(token)
            await self._release(lease)


class RedisEventBackplane:
    """跨进程事件广播：worker 发布 → API 进程订阅 → 转发给本地 WS。

    必须带发送方标识并跳过自己发的消息：Pub/Sub 不会排除发布者，
    API 进程既发布又订阅同一频道，不过滤就会把自己的事件再投递一次
    （实测现象：前端收到两条 task.queued）。
    """

    def __init__(self, client: Any, origin: Optional[str] = None) -> None:
        self._client = client
        self._origin = origin or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

    @property
    def origin(self) -> str:
        return self._origin

    async def publish(self, event: TradeEvent) -> None:
        channel = f"{_EVENT_CHANNEL_PREFIX}{event.shopping_session_id}"
        envelope = {"origin": self._origin, "event": event.to_dict()}
        try:
            await self._client.publish(channel, json.dumps(envelope, ensure_ascii=False))
        except Exception as err:  # noqa: BLE001 —— 广播失败不影响本进程投递
            logger.warning("事件广播失败：%s（%s）", channel, err)

    async def listen(self) -> AsyncIterator[TradeEvent]:
        """订阅所有会话频道，逐条产出**其他进程**的事件。"""
        pubsub = self._client.pubsub()
        await pubsub.psubscribe(f"{_EVENT_CHANNEL_PREFIX}*")
        try:
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                try:
                    envelope = json.loads(message["data"])
                    if envelope.get("origin") == self._origin:
                        continue  # 自己发的，本地已投递过
                    yield TradeEvent.from_dict(envelope["event"])
                except Exception as err:  # noqa: BLE001
                    logger.warning("远端事件解析失败，跳过：%s", err)
        finally:
            await pubsub.punsubscribe()
            await pubsub.aclose()
