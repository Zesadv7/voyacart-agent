# -*- coding: utf-8 -*-
"""共享熔断原子性：仅启动隔离真实 Redis，不以 Python 替身证明 Lua 竞争。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.infrastructure.resilience import ToolResilienceMiddleware
from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
from tests.test_queue_reliability import isolated_redis_url, real_redis  # noqa: F401


def registry(client, **kwargs):
    return SharedCircuitBreakerRegistry(SimpleNamespace(enabled=True, client=client), **kwargs)


async def state(client, name="tool"):
    return json.loads(await client.get(f"voyacart:breaker:{name}"))


async def test_concurrent_failures_are_atomic(real_redis):
    regs = [registry(real_redis, failure_threshold=100) for _ in range(64)]
    permits = await asyncio.gather(*(reg.acquire_async("tool") for reg in regs))
    await asyncio.gather(*(reg.record_failure_async("tool", permit=p) for reg, p in zip(regs, permits)))
    assert (await state(real_redis))["failures"] == 64


async def test_concurrent_threshold_opens_once_and_fences_other_calls(real_redis):
    reg = registry(real_redis, failure_threshold=8)
    permits = await asyncio.gather(*(reg.acquire_async("tool") for _ in range(32)))
    await asyncio.gather(*(reg.record_failure_async("tool", permit=p) for p in permits))
    result = await state(real_redis)
    assert result["failures"] == 8
    assert await reg.status_async("tool") == "open"
    await reg.record_success_async("tool", permit=permits[-1])
    assert await state(real_redis) == result


async def test_single_half_open_probe_across_instances(real_redis):
    regs = [registry(real_redis, failure_threshold=1, reset_seconds=10) for _ in range(64)]
    await regs[0].record_failure_async("tool", now=1000)
    assert not (await regs[0].acquire_async("tool", now=1005)).allowed
    permits = await asyncio.gather(*(reg.acquire_async("tool", now=1011) for reg in regs))
    assert sum(p.allowed for p in permits) == 1
    winner = next(i for i, p in enumerate(permits) if p.allowed)
    assert permits[winner].owner
    await regs[winner].record_success_async("tool", permit=permits[winner], now=1012)
    assert await regs[0].status_async("tool", now=1012) == "closed"


@pytest.mark.parametrize("old_result", ["success", "failure"])
@pytest.mark.parametrize("new_result", [None, "success", "failure"])
async def test_expired_owner_cannot_change_new_epoch(real_redis, old_result, new_result):
    a, b = [registry(real_redis, failure_threshold=1, reset_seconds=1, probe_lease_seconds=2) for _ in range(2)]
    await a.record_failure_async("tool", now=100)
    old = await a.acquire_async("tool", now=102)
    fresh = await b.acquire_async("tool", now=105)
    assert old.allowed and fresh.allowed and old.epoch != fresh.epoch
    if new_result:
        await getattr(b, f"record_{new_result}_async")("tool", permit=fresh, now=105.5)
    before = await state(real_redis)
    await getattr(a, f"record_{old_result}_async")("tool", permit=old, now=106)
    assert await state(real_redis) == before


@pytest.mark.parametrize("result", ["success", "failure"])
async def test_expired_probe_result_is_ignored_before_replacement(real_redis, result):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0, probe_lease_seconds=1)
    await reg.record_failure_async("tool", now=100)
    old = await reg.acquire_async("tool", now=101)
    before = await state(real_redis)
    await getattr(reg, f"record_{result}_async")("tool", permit=old, now=102)
    assert await state(real_redis) == before
    assert await reg.status_async("tool", now=102) == "open"
    assert (await reg.acquire_async("tool", now=102)).allowed


async def test_key_expiry_cannot_reuse_an_old_epoch(real_redis):
    reg = registry(real_redis, failure_threshold=1)
    old = await reg.acquire_async("tool")
    # 模拟状态 TTL 到期，只删除该隔离测试进程的合成工具键。
    await real_redis.delete(reg._key("tool"))
    await reg.record_failure_async("tool", permit=old)
    assert await real_redis.get(reg._key("tool")) is None
    fresh = await reg.acquire_async("tool")
    assert fresh.epoch != old.epoch
    await reg.record_failure_async("tool", permit=old)
    assert (await state(real_redis))["failures"] == 0


async def test_cancelled_probe_releases_only_after_bounded_lease(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0, probe_lease_seconds=.08)
    await reg.record_failure_async("tool")
    started = asyncio.Event()
    async def probe():
        assert await reg.allow_async("tool")
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(probe())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (await reg.acquire_async("tool")).allowed
    await asyncio.sleep(.1)
    permits = await asyncio.gather(*(reg.acquire_async("tool") for _ in range(16)))
    assert sum(p.allowed for p in permits) == 1


async def test_process_exit_probe_recovers_after_lease(real_redis, isolated_redis_url):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0, probe_lease_seconds=.12)
    await reg.record_failure_async("tool")
    code = '''
import asyncio, os, sys
from app.infrastructure.cache.redis_cache import RedisCache
from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
async def main():
    cache = RedisCache(sys.argv[1])
    reg = SharedCircuitBreakerRegistry(cache, failure_threshold=1, reset_seconds=0, probe_lease_seconds=.12)
    permit = await reg.acquire_async("tool")
    print(permit.allowed, flush=True)
    os._exit(0)
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, isolated_redis_url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=os.environ.copy())
    output, error = await asyncio.wait_for(child.communicate(), 10)
    assert child.returncode == 0, error.decode()
    assert output.strip() == b"True"
    await asyncio.sleep(.15)
    permits = await asyncio.gather(*(reg.acquire_async("tool") for _ in range(16)))
    assert sum(p.allowed for p in permits) == 1


async def test_legacy_json_state_migrates_without_reopening_cooldown(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=10)
    await real_redis.set(reg._key("tool"), json.dumps({"failures": 3, "opened_at": 1000, "half_open": 1}))
    assert not (await reg.acquire_async("tool", now=1005)).allowed
    permits = await asyncio.gather(*(reg.acquire_async("tool", now=1011) for _ in range(8)))
    assert sum(p.allowed for p in permits) == 1
    assert (await state(real_redis))["version"] == 2


async def test_unowned_result_cannot_close_or_reopen_probe(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0)
    await reg.record_failure_async("tool")
    assert (await reg.acquire_async("tool")).allowed
    before = await state(real_redis)
    await reg.record_success_async("tool")
    await reg.record_failure_async("tool")
    assert await state(real_redis) == before


async def test_permit_cannot_be_used_for_another_tool_or_registry(real_redis):
    a, b = registry(real_redis), registry(real_redis)
    p = await a.acquire_async("tool")
    await b.record_failure_async("tool", permit=p)
    await a.record_failure_async("another", permit=p)
    assert (await state(real_redis))["failures"] == 0
    assert await real_redis.get(a._key("another")) is None


async def test_fail_open_call_does_not_write_when_redis_recovers(real_redis):
    class FailingClient:
        async def eval(self, *args):
            raise ConnectionError("synthetic Redis outage")
    cache = SimpleNamespace(enabled=True, client=FailingClient())
    reg = SharedCircuitBreakerRegistry(cache, failure_threshold=1)
    permit = await reg.acquire_async("tool")
    assert permit.allowed and permit.mode == "bypass"
    await reg.record_failure_async("tool", permit=permit)
    assert await reg.status_async("tool") == "closed"
    cache.client = real_redis
    healthy = await reg.acquire_async("tool")
    before = await state(real_redis)
    await reg.record_failure_async("tool", permit=permit)
    assert await state(real_redis) == before
    await reg.record_failure_async("tool", permit=healthy)
    assert await reg.status_async("tool") == "open"


@pytest.mark.parametrize("bad_lease", [0, -1, float("inf"), float("nan")])
def test_invalid_probe_lease_rejected(real_redis, bad_lease):
    with pytest.raises(ValueError, match="probe_lease_seconds"):
        registry(real_redis, probe_lease_seconds=bad_lease)


async def collect(middleware, name, handler):
    return [chunk async for chunk in middleware.on_tool_call(SimpleNamespace(name=name), {}, handler)]


def chunk(state=ToolResultState.SUCCESS, text="ok"):
    return ToolChunk(content=[TextBlock(type="text", text=text)], state=state)


@pytest.mark.parametrize("outcome", ["success", "exception", "timeout", "transient", "business"])
async def test_middleware_records_the_same_probe_permit(real_redis, outcome):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0)
    await reg.record_failure_async("tool")
    middleware = ToolResilienceMiddleware(reg, timeouts={"tool": .03})
    async def handler():
        if outcome == "exception": raise RuntimeError("synthetic failure")
        if outcome == "timeout": await asyncio.sleep(10)
        if outcome == "transient": yield chunk(ToolResultState.ERROR, "503 Service Unavailable")
        elif outcome == "business": yield chunk(ToolResultState.ERROR, "商品不存在")
        else: yield chunk()
    result = await collect(middleware, "tool", handler)
    assert result
    assert await reg.status_async("tool") == ("closed" if outcome in {"success", "business"} else "open")
    assert (await state(real_redis))["probe_owner"] == ""


async def test_nested_same_tool_preserves_outer_probe_permit(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0)
    await reg.record_failure_async("tool")
    middleware = ToolResilienceMiddleware(reg)
    async def inner():
        pytest.fail("同名嵌套调用应被正在运行的探针短路")
        yield chunk()
    async def outer():
        result = await collect(middleware, "tool", inner)
        assert result[0].state == ToolResultState.ERROR
        yield chunk()
    result = await collect(middleware, "tool", outer)
    assert result[0].state == ToolResultState.SUCCESS
    assert await reg.status_async("tool") == "closed"


async def test_middleware_cancel_does_not_close_probe(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0)
    await reg.record_failure_async("tool")
    middleware = ToolResilienceMiddleware(reg, timeouts={"tool": .1})
    started = asyncio.Event()
    async def handler():
        started.set()
        await asyncio.Event().wait()
        yield chunk()
    task = asyncio.create_task(collect(middleware, "tool", handler))
    await started.wait()
    before = await state(real_redis)
    assert before["probe_deadline"] > before["opened_at"] + 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await reg.status_async("tool") == "half_open"
    assert not (await reg.acquire_async("tool")).allowed
    assert (await reg.acquire_async("tool", now=before["probe_deadline"] + 1)).allowed


async def test_bool_compatibility_is_isolated_between_async_tasks(real_redis):
    reg = registry(real_redis, failure_threshold=100)
    async def invoke():
        assert await reg.allow_async("tool")
        await asyncio.sleep(0)
        await reg.record_failure_async("tool")
    await asyncio.gather(*(invoke() for _ in range(32)))
    assert (await state(real_redis))["failures"] == 32
    assert not reg._pending


async def test_closed_success_resets_consecutive_failures(real_redis):
    reg = registry(real_redis, failure_threshold=2)
    for result in ("failure", "success", "failure"):
        permit = await reg.acquire_async("tool")
        await getattr(reg, f"record_{result}_async")("tool", permit=permit)
    assert (await state(real_redis))["failures"] == 1
    assert await reg.status_async("tool") == "closed"


async def test_denied_permit_cannot_change_open_state(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=60)
    await reg.record_failure_async("tool")
    denied = await reg.acquire_async("tool")
    assert not denied.allowed
    before = await state(real_redis)
    await reg.record_success_async("tool", permit=denied)
    await reg.record_failure_async("tool", permit=denied)
    assert await state(real_redis) == before


async def test_zero_timestamp_and_lease_longer_than_cooldown(real_redis):
    reg = registry(real_redis, failure_threshold=1, reset_seconds=0, probe_lease_seconds=180)
    await reg.record_failure_async("tool", now=0)
    assert await reg.status_async("tool", now=0) == "open"
    permit = await reg.acquire_async("tool", now=0)
    assert permit.allowed and permit.owner
    assert await real_redis.ttl(reg._key("tool")) >= 359
    assert not (await reg.acquire_async("tool", now=179)).allowed
    assert (await reg.acquire_async("tool", now=180)).allowed
