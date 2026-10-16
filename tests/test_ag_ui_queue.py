"""真实 Redis 与持久运行日志的队列链路验证。"""
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
import pytest
from ag_ui.core import RunAgentInput

from app.application.agent_runtime import AGUIRuntime
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalConflict, JournalForbidden, JournalNotFound
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _STREAM
from app.presentation.ag_ui import parse_intent
from app.worker import execute_ag_ui_task
from tests.test_ag_ui_journal import body, ControlledOrchestrator, application, frames, wait_status
from tests.test_queue_reliability import isolated_redis_url, real_redis, consume, stop_consumer, eventually, has_state


async def runtime_pair(tmp_path, real_redis, *, block=False, max_pending=200):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.ensure_group()
    api = AGUIRuntime(AGUIJournal(tmp_path / 'runs.db'), ControlledOrchestrator(),
                     task_queue=queue, queue_max_pending=max_pending)
    worker = AGUIRuntime(AGUIJournal(tmp_path / 'runs.db'), ControlledOrchestrator(block),
                        heartbeat_seconds=0.02)
    await api.startup()
    await worker.startup()
    return api, worker, queue


async def test_queued_sse_waits_for_worker_and_replays_once(tmp_path, real_redis):
    api, worker, queue = await runtime_pair(tmp_path, real_redis)
    stopping = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(api)), base_url='http://test') as client:
        request = asyncio.create_task(client.post('/commerce/ag-ui/run', json=body()))
        await eventually(lambda: has_state(queue, 'agui:r1', 'queued'))
        with pytest.raises(JournalNotFound):
            await api.journal.run('r1', 'b1')
        status = await client.get('/commerce/ag-ui/runs/r1', params={'buyer_id': 'b1'})
        assert status.json()['status'] == 'queued'
        runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
        result = await asyncio.wait_for(request, 5)
        events = frames(result.text)
        assert result.status_code == 200
        assert events[-1][1]['type'] == 'RUN_FINISHED'
        assert [key for key, _ in events] == [f'r1:{n}' for n in range(1,len(events)+1)]
        again = await client.post('/commerce/ag-ui/run', json=body())
        assert frames(again.text) == events
        assert api.orchestrator.calls == 0 and worker.orchestrator.calls == 1
        await stop_consumer(runner, stopping)
    await api.shutdown()
    await worker.shutdown()


async def test_queue_dedup_capacity_is_atomic_and_ownership_is_checked(tmp_path, real_redis):
    api, worker, queue = await runtime_pair(tmp_path, real_redis, max_pending=1)
    request = RunAgentInput.model_validate(body())
    await asyncio.gather(*(api.submit(request, parse_intent(request)) for _ in range(8)))
    assert await real_redis.xlen(_STREAM) == 1
    with pytest.raises(JournalForbidden):
        await api.run('r1', 'other')
    changed = RunAgentInput.model_validate(body(query='另一个需求'))
    with pytest.raises(JournalConflict):
        await api.submit(changed, parse_intent(changed))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(api)), base_url='http://test') as client:
        response = await client.post('/commerce/ag-ui/run', json=body('r2','s2'))
        assert response.status_code == 503 and response.headers['retry-after'] == '3'
    assert await real_redis.xlen(_STREAM) == 1
    await api.shutdown()
    await worker.shutdown()


async def test_enqueue_error_rejects_without_inline_fallback(tmp_path, real_redis):
    api, worker, queue = await runtime_pair(tmp_path, real_redis)
    # 真实 Redis 的流类型错误，验证入队边界异常处理。
    await real_redis.delete(_STREAM)
    await real_redis.set(_STREAM, 'invalid-stream')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(api)), base_url='http://test') as client:
        response = await client.post('/commerce/ag-ui/run', json=body())
    assert response.status_code == 503 and response.headers['retry-after'] == '3'
    assert response.json()['detail']['code'] == 'QUEUE_ACCEPTANCE_UNKNOWN'
    assert api.orchestrator.calls == 0
    with pytest.raises(JournalNotFound):
        await api.journal.run('r1', 'b1')
    await api.shutdown()
    await worker.shutdown()


async def test_cancel_from_other_runtime_and_before_worker_starts(tmp_path, real_redis):
    api, worker, queue = await runtime_pair(tmp_path, real_redis, block=True)
    request = RunAgentInput.model_validate(body())
    await api.submit(request, parse_intent(request))
    stopping = asyncio.Event()
    runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
    await asyncio.wait_for(worker.orchestrator.entered.wait(), 3)
    await api.cancel('r1','b1')
    await wait_status(api.journal, 'r1', 'stopped')
    assert worker.orchestrator.cancelled.is_set()
    await stop_consumer(runner, stopping)
    second = RunAgentInput.model_validate(body('r2','s2'))
    await api.submit(second, parse_intent(second))
    await api.cancel('r2','b1')
    stopping = asyncio.Event()
    runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
    await eventually(lambda: has_state(queue, 'agui:r2', 'done'))
    assert (await api.journal.run('r2','b1'))['status'] == 'stopped'
    assert worker.orchestrator.calls == 1
    await stop_consumer(runner, stopping)
    await api.shutdown()
    await worker.shutdown()


async def test_worker_process_cancel_then_killed_and_redelivered_without_second_execution(tmp_path, real_redis, isolated_redis_url):
    api, worker, queue = await runtime_pair(tmp_path, real_redis)
    request = RunAgentInput.model_validate(body())
    await api.submit(request, parse_intent(request))
    script = '''
import asyncio, sys
import redis.asyncio as redis
from pathlib import Path
from app.application.agent_runtime import AGUIRuntime
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue
from app.worker import execute_ag_ui_task
class Agent:
    async def handle_intent(self, *args, **kwargs):
        Path(sys.argv[3]).write_text(args[0].shopping_session_id)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            Path(sys.argv[3]+'.cancelled').touch()
            raise
async def main():
    queue=RedisStreamTaskQueue(redis.from_url(sys.argv[1],decode_responses=True))
    runtime=AGUIRuntime(AGUIJournal(Path(sys.argv[2])),Agent(),lease_seconds=.4,heartbeat_seconds=.05)
    await runtime.startup()
    await queue.consume('killed-worker',lambda t:execute_ag_ui_task(t,runtime),lambda:False,
        block_ms=10,reclaim_idle_ms=120,lease_ms=600,heartbeat_interval_ms=30)
asyncio.run(main())
'''
    marker = tmp_path / 'entered'
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, isolated_redis_url,
        str(tmp_path/'runs.db'), str(marker), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        await eventually(marker.exists, timeout=15)
        await api.cancel('r1','b1')
        await wait_status(api.journal, 'r1', 'stopped')
        assert Path(str(marker)+'.cancelled').exists()
        second = RunAgentInput.model_validate(body('r2','s2'))
        await api.submit(second, parse_intent(second))
        await eventually(lambda: marker.read_text() == 's2')
        process.kill()
        await process.wait()
        await asyncio.sleep(.65)
        await api.journal.recover_expired()
        stopping = asyncio.Event()
        runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
        await eventually(lambda: has_state(queue, 'agui:r2','done'))
        assert worker.orchestrator.calls == 0
        assert (await api.journal.run('r2','b1'))['status'] == 'interrupted'
        await stop_consumer(runner, stopping)
    finally:
        if process.returncode is None:
            process.kill()
        _, error = await process.communicate()
        assert process.returncode == -9, error.decode()
        await api.shutdown()
        await worker.shutdown()


async def test_queue_wait_timeout_keeps_the_same_run_recoverable(tmp_path, real_redis):
    api, worker, queue = await runtime_pair(tmp_path, real_redis)
    api.queue_wait_seconds = 0
    stopping = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(api)), base_url='http://test') as client:
        response = await client.post('/commerce/ag-ui/run', json=body())
        assert 'event: transport-error\n' in response.text
        assert 'RUN_ERROR' not in response.text
        assert (await api.run('r1', 'b1'))['status'] == 'queued'
        runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
        await eventually(lambda: has_state(queue, 'agui:r1', 'done'))
        replay = await client.get('/commerce/ag-ui/runs/r1/events', params={'buyer_id': 'b1'})
        assert frames(replay.text)[-1][1]['type'] == 'RUN_FINISHED'
        assert worker.orchestrator.calls == 1
        await stop_consumer(runner, stopping)
    await api.shutdown()
    await worker.shutdown()


async def test_enqueue_response_lost_retries_same_identity_without_api_execution(tmp_path, real_redis, monkeypatch):
    api, worker, queue = await runtime_pair(tmp_path, real_redis)
    original = queue.enqueue
    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError('synthetic response loss after actual Lua')
    monkeypatch.setattr(queue, 'enqueue', lost)
    from app.domain.queue.ports.task_queue import QueueUnavailable
    request = RunAgentInput.model_validate(body())
    with pytest.raises(QueueUnavailable) as caught:
        await api.submit(request, parse_intent(request))
    assert caught.value.code == 'QUEUE_ACCEPTANCE_UNKNOWN'
    assert await real_redis.xlen(_STREAM) == 1 and api.orchestrator.calls == 0
    monkeypatch.setattr(queue, 'enqueue', original)
    await api.submit(request, parse_intent(request))
    assert await real_redis.xlen(_STREAM) == 1
    stopping = asyncio.Event()
    runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
    try:
        await eventually(lambda: has_state(queue, 'agui:r1', 'done'))
        assert worker.orchestrator.calls == 1 and api.orchestrator.calls == 0
    finally:
        await stop_consumer(runner, stopping)
        await api.shutdown()
        await worker.shutdown()


async def test_expired_queue_has_durable_terminal_and_late_worker_does_not_run(tmp_path, real_redis):
    queue = RedisStreamTaskQueue(real_redis, max_age_seconds=.06)
    await queue.ensure_group()
    api = AGUIRuntime(AGUIJournal(tmp_path/'runs.db'), ControlledOrchestrator(), task_queue=queue)
    worker = AGUIRuntime(AGUIJournal(tmp_path/'runs.db'), ControlledOrchestrator())
    await api.startup(); await worker.startup()
    request = RunAgentInput.model_validate(body())
    await api.submit(request, parse_intent(request))
    await asyncio.sleep(.09)
    await api.recover()
    state = await api.run('r1','b1')
    assert state['status'] == 'error'
    events, _, _ = await api.journal.events('r1','b1')
    assert events[-1]['event']['code'] == 'QUEUE_DEADLINE_EXCEEDED'
    stopping = asyncio.Event()
    runner = consume(queue, lambda task: execute_ag_ui_task(task, worker), stopping)
    try:
        async def empty(): return await queue.depth() == 0
        await eventually(empty)
        assert worker.orchestrator.calls == 0
        assert (await api.submit(request, parse_intent(request)))['status'] == 'error'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(api)), base_url='http://test') as client:
            replay = await client.get('/commerce/ag-ui/runs/r1/events', params={'buyer_id':'b1'})
        assert frames(replay.text)[-1][1]['code'] == 'QUEUE_DEADLINE_EXCEEDED'
    finally:
        await stop_consumer(runner, stopping); await api.shutdown(); await worker.shutdown()


async def test_expiry_materialization_retries_after_journal_failure(tmp_path, real_redis, monkeypatch):
    queue = RedisStreamTaskQueue(real_redis, max_age_seconds=.04)
    api = AGUIRuntime(AGUIJournal(tmp_path/'runs.db'), ControlledOrchestrator(), task_queue=queue)
    await api.startup()
    request = RunAgentInput.model_validate(body())
    await api.submit(request, parse_intent(request)); await asyncio.sleep(.06)
    original = api.journal.fail_unstarted
    async def broken(*a, **kw): raise RuntimeError('synthetic journal unavailable')
    monkeypatch.setattr(api.journal, 'fail_unstarted', broken)
    with pytest.raises(RuntimeError): await api.recover()
    assert len(await queue.expire_due()) == 1
    monkeypatch.setattr(api.journal, 'fail_unstarted', original)
    await api.recover()
    assert await queue.expire_due() == []
    assert (await api.run('r1','b1'))['status'] == 'error'
    await api.shutdown()
