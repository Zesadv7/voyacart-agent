# -*- coding: utf-8 -*-
"""M5：真实 Redis 的身份保留、服务端排队期限及有界恢复合同。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from contextvars import Context

import pytest
import redis

from app.domain.queue.ports.task_queue import QueueCapacityExceeded, TaskStatus
from app.infrastructure.queue.archive import QueueArchive
from app.infrastructure.queue.maintenance import maintain_queue
import app.infrastructure.queue.redis_stream_queue as module
from app.infrastructure.queue.redis_stream_queue import (
    RedisStreamTaskQueue, _STREAM, _LARGE_STREAM, _GROUP, _STATUS_PREFIX,
    _DEADLINE_INDEX, _EXPIRED_INDEX, _QUEUE_DEADLINE_ERROR, _lease_key,
)
from tests.test_queue_reliability import (
    isolated_redis_url, real_redis, task, consume, eventually, stop_consumer, has_state,
)


async def _make_due(client, task_id):
    """用 Redis TIME 确定过期屏障，不伪造本机时间。"""
    seconds, micros = await client.time()
    key = _STATUS_PREFIX + task_id
    raw = json.loads(await client.get(key))
    raw['queue_deadline_at'] = seconds + micros / 1_000_000 - .001
    await client.set(key, json.dumps(raw))
    await client.zadd(_DEADLINE_INDEX, {task_id: raw['queue_deadline_at']})


def agui_task(task_id='agui-1', **kwargs):
    return replace(task(task_id, **kwargs), ag_ui_input={'threadId': 's', 'runId': task_id, 'fixture': True})


async def test_nonterminal_identity_survives_old_shortened_ttl(real_redis, monkeypatch):
    monkeypatch.setattr(module, '_STATUS_TTL', 1)
    queue = RedisStreamTaskQueue(real_redis)
    originals = []
    for state in ('queued', 'running', 'retrying'):
        item = agui_task(state, session=state)
        originals.append(item)
        await queue.enqueue(item)
        if state != 'queued':
            await queue.set_status(TaskStatus(state, state))
    await asyncio.sleep(1.1)
    for item in originals:
        assert (await queue.get_status(item.task_id)).state == item.task_id
        assert await queue.get_task(item.task_id) == item
        assert await real_redis.ttl(_STATUS_PREFIX + item.task_id) == -1
        await queue.enqueue(item)
        with pytest.raises(ValueError, match='request_id'):
            await queue.enqueue(replace(item, raw_query='different fixture'))
    assert await real_redis.xlen(_STREAM) == 3


async def test_constructor_cap_covers_default_and_higher_override_both_streams(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.ensure_group()
    for index in range(200):
        await queue.enqueue(task(str(index), session=str(index), priority=index % 2),
                            **({'max_pending': 1000} if index % 2 else {}))
    # pending 与 lag 同时存在仍合计，重放优先于背压。
    await real_redis.xreadgroup(_GROUP, 'holding-worker', {_STREAM: '>'}, count=3)
    assert await queue.depth() == 200
    await queue.enqueue(task('0', session='0'))
    with pytest.raises(QueueCapacityExceeded):
        await queue.enqueue(task('overflow'), max_pending=1000)
    assert await real_redis.zcard(_DEADLINE_INDEX) == 200


async def test_explicit_limit_only_tightens_constructor(real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_pending=3)
    await queue.enqueue(task('one'), max_pending=1)
    with pytest.raises(QueueCapacityExceeded):
        await queue.enqueue(task('two'), max_pending=1)
    await queue.enqueue(task('two'))
    await queue.enqueue(task('three'), max_pending=999)
    with pytest.raises(QueueCapacityExceeded):
        await queue.enqueue(task('four'))
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            await queue.enqueue(task('invalid'), max_pending=invalid)


@pytest.mark.parametrize('options', [{'max_pending': 0}, {'max_pending': True}, {'max_age_seconds': 0},
                                    {'max_age_seconds': float('inf')}, {'max_age_seconds': float('nan')}])
def test_queue_bounds_require_positive_finite_values(options):
    with pytest.raises(ValueError):
        RedisStreamTaskQueue(None, **options)


async def test_deadline_uses_redis_time_not_task_client_timestamp(real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_age_seconds=19)
    before = await real_redis.time()
    for index, timestamp in enumerate(('1900-01-01T00:00:00Z', '9999-12-31T23:59:59Z', 'invalid')):
        item = replace(task(str(index)), enqueued_at=timestamp)
        await queue.enqueue(item)
        raw = json.loads(await real_redis.get(_STATUS_PREFIX + item.task_id))
        after = await real_redis.time()
        assert before[0] + before[1] / 1_000_000 <= raw['accepted_at'] <= after[0] + after[1] / 1_000_000
        assert raw['queue_deadline_at'] - raw['accepted_at'] == pytest.approx(19, abs=.0001)
        assert raw['task']['enqueued_at'] == timestamp


async def test_expiry_batches_bound_legacy_work_and_keep_full_identity(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    originals = [task(str(i)) for i in range(5)]
    for item in originals:
        await queue.enqueue(item)
        await _make_due(real_redis, item.task_id)
    for expected in (2, 4, 5):
        assert await queue.expire_due(limit=2) == []
        states = [await queue.get_status(item.task_id) for item in originals]
        assert sum(item.state == 'failed' for item in states) == expected
    assert await real_redis.zcard(_DEADLINE_INDEX) == await real_redis.zcard(_EXPIRED_INDEX) == 0
    for item in originals:
        assert await queue.get_task(item.task_id) == item
        status = json.loads(await real_redis.get(_STATUS_PREFIX + item.task_id))
        assert status['payload_fingerprint'] and status['terminal_at']
        assert status['error'] == _QUEUE_DEADLINE_ERROR
        await queue.enqueue(item)
    assert await real_redis.xlen(_STREAM) == 5


async def test_expired_agui_replays_until_materialization_ack_and_rotates(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    for index in range(5):
        await queue.enqueue(agui_task(str(index)))
        await _make_due(real_redis, str(index))
    observed = set()
    for _ in range(8):
        batch = await queue.expire_due(limit=2)
        assert len(batch) <= 2
        observed.update(item.task_id for item in batch)
    assert observed == {str(i) for i in range(5)}
    assert await real_redis.zcard(_EXPIRED_INDEX) == 5
    for task_id in observed:
        await queue.acknowledge_expired(task_id)
        await queue.acknowledge_expired(task_id)
        assert (await queue.get_status(task_id)).state == 'failed'
    assert await queue.expire_due() == []


async def test_worker_does_not_execute_when_expiry_wins_running_race(real_redis):
    reached, release = asyncio.Event(), asyncio.Event()
    class PausedQueue(RedisStreamTaskQueue):
        async def set_status(self, status):
            if status.state == 'running':
                reached.set()
                await release.wait()
            await super().set_status(status)
    queue = PausedQueue(real_redis)
    await queue.enqueue(agui_task())
    calls = []
    async def handler(item):
        calls.append(item.task_id)
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping)
    try:
        await asyncio.wait_for(reached.wait(), 2)
        await _make_due(real_redis, 'agui-1')
        assert [item.task_id for item in await queue.expire_due()] == ['agui-1']
        release.set()
        await eventually(lambda: _no_pending(queue))
    finally:
        release.set()
        await stop_consumer(runner, stopping)
    assert calls == []
    assert (await queue.get_status('agui-1')).error == _QUEUE_DEADLINE_ERROR


async def test_atomic_running_transition_expires_due_without_maintenance(real_redis):
    class DueAtStartQueue(RedisStreamTaskQueue):
        async def set_status(self, status):
            if status.state == 'running':
                await _make_due(real_redis, status.task_id)
            await super().set_status(status)
    queue = DueAtStartQueue(real_redis)
    await queue.enqueue(agui_task())
    calls = []
    async def handler(item):
        calls.append(item.task_id)
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping)
    try:
        await eventually(lambda: has_state(queue, 'agui-1', 'failed'))
        await eventually(lambda: _no_pending(queue))
    finally:
        await stop_consumer(runner, stopping)
    assert calls == []
    assert [item.task_id for item in await queue.expire_due()] == ['agui-1']


async def test_running_wins_deadline_race_and_is_not_queue_expired(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(agui_task())
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(_):
        entered.set()
        await release.wait()
        return 'completed fixture'
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await _make_due(real_redis, 'agui-1')
        assert await queue.expire_due() == []
        assert (await queue.get_status('agui-1')).state == 'running'
        assert await real_redis.zcard(_DEADLINE_INDEX) == 0
        release.set()
        await eventually(lambda: has_state(queue, 'agui-1', 'done'))
    finally:
        release.set()
        await stop_consumer(runner, stopping)


async def test_session_waiter_expires_without_stealing_active_session(real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_age_seconds=.15)
    stopping = asyncio.Event()
    calls = []
    async def handler(item):
        calls.append(item.task_id)
    async with queue.session_lease('session-1', lease_ms=600) as owner:
        await queue.enqueue(agui_task())
        runner = consume(queue, handler, stopping)
        try:
            await eventually(lambda: has_state(queue, 'agui-1', 'failed'))
            await eventually(lambda: _no_pending(queue))
        finally:
            await stop_consumer(runner, stopping)
        assert await real_redis.get(_lease_key('session', 'session-1')) == owner.owner
        assert owner.is_valid()
        assert not await real_redis.keys('voyacart:lease:message:*')
        assert not await real_redis.keys('voyacart:lease:task:*')
    assert calls == []


async def test_running_redelivery_does_not_hold_session_wait_slot_forever(real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_age_seconds=.05)
    await queue.ensure_group()
    await queue.enqueue(task())
    await queue.set_status(TaskStatus('task-1', 'running'))
    message_id, fields = (await real_redis.xreadgroup(_GROUP, 'worker', {_STREAM: '>'}))[0][1][0]
    key = _lease_key('session', 'session-1')
    await real_redis.set(key, 'existing-session-owner', px=2000)
    async def forbidden(_):
        raise AssertionError('不能抢占当前会话')
    await asyncio.wait_for(queue._handle_one(_STREAM, message_id, fields, forbidden, 3,
                                            lease_ms=600, heartbeat_interval_ms=30), 1)
    assert (await real_redis.xpending(_STREAM, _GROUP))['pending'] == 1
    assert (await queue.get_status('task-1')).state == 'running'
    assert await real_redis.get(key) == 'existing-session-owner'
    assert not await real_redis.keys('voyacart:lease:message:*')


async def test_bad_deadline_index_rejects_before_xadd_or_status(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await real_redis.set(_DEADLINE_INDEX, 'wrong type')
    with pytest.raises(redis.ResponseError):
        await queue.enqueue(task())
    assert await real_redis.xlen(_STREAM) == 0
    assert await queue.get_task('task-1') is None


async def test_depth_propagates_outage_or_wrongtype_and_counts_ungrouped(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(task())
    assert await queue.depth() == 1
    await real_redis.set(_LARGE_STREAM, 'wrong type')
    with pytest.raises(redis.ResponseError):
        await queue.depth()


async def test_expired_payload_not_pruned_before_journal_ack(real_redis, tmp_path):
    archive = QueueArchive(tmp_path / 'archive.db')
    queue = RedisStreamTaskQueue(real_redis, archive)
    await queue.ensure_group()
    original = agui_task()
    await queue.enqueue(original)
    await _make_due(real_redis, original.task_id)
    await queue.expire_due()
    message_id, fields = (await real_redis.xreadgroup(_GROUP, 'worker', {_STREAM: '>'}))[0][1][0]
    async def forbidden(_):
        raise AssertionError('过期不能执行')
    await queue._handle_one(_STREAM, message_id, fields, forbidden, 3)
    before = await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True)
    assert before['deleted'] == 0
    assert await queue.get_task(original.task_id) == original
    await queue.acknowledge_expired(original.task_id)
    after = await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True)
    assert after['deleted'] == 1
    assert await queue.get_task(original.task_id) == original
    assert (await queue.get_status(original.task_id)).error == _QUEUE_DEADLINE_ERROR


async def test_terminal_expiry_is_not_overwritten_by_late_dead_letter(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.ensure_group()
    await queue.enqueue(agui_task())
    message_id, fields = (await real_redis.xreadgroup(_GROUP, 'worker', {_STREAM: '>'}))[0][1][0]
    delivery = await queue._delivery(_STREAM, message_id, fields)
    await _make_due(real_redis, 'agui-1')
    await queue.expire_due()
    await queue._dead_letter(delivery, 'late failure', 'agui-1')
    assert (await queue.get_status('agui-1')).error == _QUEUE_DEADLINE_ERROR
    assert await real_redis.xlen(module._DEAD_STREAM) == 0


async def _no_pending(queue):
    return await queue.depth() == 0


@pytest.mark.parametrize('remove_before', [False, True])
async def test_identity_removed_at_running_write_never_starts_handler(real_redis, remove_before):
    class LostIdentityQueue(RedisStreamTaskQueue):
        async def set_status(self, status):
            if status.state == 'running' and remove_before:
                await real_redis.delete(_STATUS_PREFIX + status.task_id)
            await super().set_status(status)
            if status.state == 'running' and not remove_before:
                await real_redis.delete(_STATUS_PREFIX + status.task_id)
    queue = LostIdentityQueue(real_redis)
    await queue.ensure_group()
    await queue.enqueue(task())
    message_id, fields = (await real_redis.xreadgroup(_GROUP, 'worker', {_STREAM: '>'}))[0][1][0]
    calls = []
    async def handler(item):
        calls.append(item.task_id)
    await queue._handle_one(_STREAM, message_id, fields, handler, 3)
    assert calls == []
    assert (await real_redis.xpending(_STREAM, _GROUP))['pending'] == 1
    assert await queue.get_status('task-1') is None


async def test_legacy_deadline_uses_stream_server_time_and_keeps_payload(real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_age_seconds=1)
    await queue.ensure_group()
    original = replace(agui_task(), enqueued_at='9999-12-31T23:59:59Z')
    await queue.enqueue(original)
    # 旧状态没有服务器期限；Stream ID 是原始接收时间，不能使用用户时间戳。
    raw = json.loads(await real_redis.get(_STATUS_PREFIX + original.task_id))
    raw.pop('queue_deadline_at')
    raw.pop('accepted_at')
    await real_redis.set(_STATUS_PREFIX + original.task_id, json.dumps(raw), ex=1)
    await real_redis.delete(_STREAM)
    await real_redis.xgroup_create(_STREAM, _GROUP, id='0', mkstream=True)
    seconds, _ = await real_redis.time()
    await real_redis.xadd(_STREAM, {'payload': json.dumps(original.to_dict())}, id=f'{(seconds - 10) * 1000}-0')
    message_id, fields = (await real_redis.xreadgroup(_GROUP, 'worker', {_STREAM: '>'}))[0][1][0]
    async def forbidden(_):
        raise AssertionError('旧消息也必须校验服务端期限')
    await queue._handle_one(_STREAM, message_id, fields, forbidden, 3)
    assert (await queue.get_status(original.task_id)).error == _QUEUE_DEADLINE_ERROR
    assert await queue.get_task(original.task_id) == original
    assert await real_redis.ttl(_STATUS_PREFIX + original.task_id) == -1
    assert [item.task_id for item in await queue.expire_due()] == [original.task_id]


async def test_wrongtype_recovery_index_cannot_half_commit_expiry(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(agui_task())
    await _make_due(real_redis, 'agui-1')
    await real_redis.set(_EXPIRED_INDEX, 'wrong type')
    with pytest.raises(redis.ResponseError):
        await queue.expire_due()
    assert (await queue.get_status('agui-1')).state == 'queued'
    assert await real_redis.zcard(_DEADLINE_INDEX) == 1
    await real_redis.delete(_EXPIRED_INDEX)
    assert [item.task_id for item in await queue.expire_due()] == ['agui-1']


async def test_atomic_cap_under_parallel_mixed_priority_submissions(real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_pending=2)
    await queue.ensure_group()
    outcomes = await asyncio.gather(*(queue.enqueue(task(str(index), priority=index % 2))
                                      for index in range(32)), return_exceptions=True)
    assert sum(value is None for value in outcomes) == 2
    assert sum(isinstance(value, QueueCapacityExceeded) for value in outcomes) == 30
    assert await queue.depth() == 2
    assert await real_redis.zcard(_DEADLINE_INDEX) == 2


async def test_expiry_metrics_count_only_first_transition(real_redis, monkeypatch):
    recorded = []
    monkeypatch.setattr(module, 'observe_queue', lambda event, **kwargs: recorded.append((event, kwargs.get('count', 1))))
    queue = RedisStreamTaskQueue(real_redis)
    for index in range(3):
        await queue.enqueue(agui_task(str(index)))
        await _make_due(real_redis, str(index))
    await queue.expire_due(limit=2)
    await queue.expire_due(limit=2)
    await queue.expire_due(limit=2)
    assert sum(count for event, count in recorded if event == 'expired') == 3
    assert sum(count for event, count in recorded if event == 'failed') == 3


async def test_delayed_final_eligibility_response_cannot_start_after_lease_expiry(real_redis):
    class DelayedClient:
        def __init__(self):
            self.checks = 0
        def __getattr__(self, name):
            return getattr(real_redis, name)
        async def eval(self, script, *args):
            result = await real_redis.eval(script, *args)
            if script == module._CHECK_EXECUTION_SCRIPT:
                self.checks += 1
                if self.checks == 2:
                    await asyncio.sleep(.15)
            return result
    queue = RedisStreamTaskQueue(DelayedClient())
    await queue.ensure_group()
    await queue.enqueue(task())
    message_id, fields = (await real_redis.xreadgroup(_GROUP, 'worker', {_STREAM: '>'}))[0][1][0]
    calls = []
    async def handler(item):
        calls.append(item.task_id)
    await queue._handle_one(_STREAM, message_id, fields, handler, 3,
                            lease_ms=100, heartbeat_interval_ms=10000)
    assert calls == []
    assert (await real_redis.xpending(_STREAM, _GROUP))['pending'] == 1
