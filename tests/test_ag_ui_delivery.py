"""SSE 读锁、租约恢复、增量合并与信号丢失的实际行为。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from ag_ui.core import RunAgentInput

from app.application.agent_runtime import AGUIRuntime
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.ag_ui_signals import AGUISignals
from app.infrastructure.queue.redis_stream_queue import RedisEventBackplane
from app.presentation.ag_ui import parse_intent
from tests.test_ag_ui_journal import (
    ControlledOrchestrator, application, body, controlled_journal_time, frames, wait_status,
)
from tests.test_queue_reliability import eventually, isolated_redis_url, real_redis


async def test_event_and_run_reads_complete_while_another_connection_owns_write_lock(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "owner")
    async with aiosqlite.connect(journal.path) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        run, (events, status, _) = await asyncio.wait_for(asyncio.gather(
            journal.run("r1", "b1"), journal.events("r1", "b1"),
        ), .5)
        assert run["status"] == status == "running" and events == []
        await writer.rollback()
    async with journal._db() as reader:
        with pytest.raises(aiosqlite.OperationalError, match="readonly"):
            await reader.execute("UPDATE agui_runs SET stop_requested=1")


async def test_periodic_recovery_finishes_expired_run_without_reader_writes(tmp_path, monkeypatch):
    clock = controlled_journal_time(monkeypatch)
    monkeypatch.setattr("app.application.agent_runtime.RECOVERY_INTERVAL_SECONDS", .02)
    journal = AGUIJournal(tmp_path / "runs.db")
    runtime = AGUIRuntime(journal, ControlledOrchestrator())
    await runtime.startup()
    try:
        await journal.reserve(body(), "b1", "dead-process", lease_seconds=30)
        clock.now += 30
        await wait_status(journal, "r1", "interrupted")
        events, _, _ = await journal.events("r1", "b1")
        assert events[-1]["event"]["code"] == "SERVER_RESTART"
    finally:
        await runtime.shutdown()


class DeltaOrchestrator:
    async def handle_intent(self, intent, event_observer, **kwargs):
        event_observer(SimpleNamespace(type="REPLY_START"))
        event_observer(SimpleNamespace(type="TEXT_BLOCK_START", block_id="text"))
        for delta in ("轻", "便", "背", "包"):
            event_observer(SimpleNamespace(type="TEXT_BLOCK_DELTA", block_id="text", delta=delta))
            await asyncio.sleep(.004)
        event_observer(SimpleNamespace(type="TEXT_BLOCK_END", block_id="text"))
        event_observer(SimpleNamespace(type="TOOL_CALL_START", tool_call_id="tool", tool_call_name="product_search_tool"))
        for delta in ('{"normalized_query":', '"背包"', '}'):
            event_observer(SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id="tool", delta=delta))
            await asyncio.sleep(.004)
        event_observer(SimpleNamespace(type="TOOL_CALL_END", tool_call_id="tool"))
        return SimpleNamespace(final_text="轻便背包", error=None)


@pytest.mark.parametrize("window_ms", [0, 50])
async def test_coalescing_preserves_final_snapshot_whole_arguments_and_sequence(tmp_path, window_ms):
    runtime = AGUIRuntime(AGUIJournal(tmp_path / "runs.db"), DeltaOrchestrator(), delta_coalesce_ms=window_ms)
    request = RunAgentInput.model_validate(body())
    try:
        await runtime.start(request, parse_intent(request))
        await wait_status(runtime.journal, "r1", "completed")
        saved, _, _ = await runtime.journal.events("r1", "b1")
        events = [item["event"] for item in saved]
        text = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]
        arguments = [event for event in events if event["type"] == "TOOL_CALL_ARGS"]
        assert not text
        assert next(event for event in events if event["type"] == "MESSAGES_SNAPSHOT")["messages"][-1]["content"] == "轻便背包"
        assert json.loads("".join(event["delta"] for event in arguments)) == {"normalized_query": "背包"}
        assert len(text) == 0
        assert len(arguments) == 1
        assert [item["seq"] for item in saved] == list(range(1, len(saved) + 1))
        assert events[-1]["type"] == "RUN_FINISHED"
    finally:
        await runtime.shutdown()


async def test_signals_wake_only_matching_run_and_release_subscriptions():
    signals = AGUISignals()
    with signals.subscribe("one") as first, signals.subscribe("one") as same, signals.subscribe("two") as other:
        await signals.publish("one", "session")
        assert first.is_set() and same.is_set() and not other.is_set()
    assert not signals._subscribers


async def test_real_redis_backplane_delivers_worker_signal(real_redis):
    receiving = RedisEventBackplane(real_redis, origin="api")
    sending = AGUISignals(RedisEventBackplane(real_redis, origin="worker"))
    signals = AGUISignals()

    async def forward():
        async for event in receiving.listen():
            signals.notify(event.payload["run_id"])

    listener = asyncio.create_task(forward())
    try:
        await eventually(real_redis.pubsub_numpat)
        with signals.subscribe("r1") as signal:
            await sending.publish("r1", "s1")
            await asyncio.wait_for(signal.wait(), 1)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.parametrize("redis_failed", [False, True])
async def test_sse_replays_durable_events_when_signals_are_lost_or_redis_fails(tmp_path, monkeypatch, redis_failed):
    client = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError("Redis unavailable")))
    runtime = AGUIRuntime(AGUIJournal(tmp_path / "runs.db"), DeltaOrchestrator(),
                          backplane=RedisEventBackplane(client) if redis_failed else None)
    monkeypatch.setattr(runtime.signals, "notify", lambda run_id: None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(application(runtime)), base_url="http://test") as http:
            response = await asyncio.wait_for(http.post("/commerce/ag-ui/run", json=body()), 2)
            events = frames(response.text)
            assert events[-1][1]["type"] == "RUN_FINISHED"
            assert [identity for identity, _ in events] == [f"r1:{i}" for i in range(1, len(events) + 1)]
            resumed = await http.get("/commerce/ag-ui/runs/r1/events?buyer_id=b1", headers={"Last-Event-ID": events[2][0]})
            assert frames(resumed.text) == events[3:]
    finally:
        await runtime.shutdown()
