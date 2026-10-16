"""不调用模型：异常、原生恢复和可读持久化均先审核，旧流无原始增量。"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.event import ReplyStartEvent, TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent, ReplyEndEvent
from agentscope.message import AssistantMsg

from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.model_recovery import IncompleteModelResponse
from app.infrastructure import tracing
from tests.test_ag_ui import EmptyPreferences, Sessions

CANARY = "sk-" + "SYNTHETICONLYABCDEFGHIJ0123456789"


class OutputAgent:
    name = "OutputBoundaryTest"

    def __init__(self, failure=None):
        self.state = SimpleNamespace(summary=None, context=[])
        self.failure = failure

    async def reply_stream(self, inputs, **kwargs):
        yield ReplyStartEvent(session_id="session", reply_id="reply", name=self.name)
        yield TextBlockStartEvent(reply_id="reply", block_id="text")
        for chunk in (CANARY[:5], CANARY[5:]):
            yield TextBlockDeltaEvent(reply_id="reply", block_id="text", delta=chunk)
        if self.failure:
            raise self.failure
        yield TextBlockEndEvent(reply_id="reply", block_id="text")
        yield ReplyEndEvent(session_id="session", reply_id="reply")
        yield AssistantMsg(self.name, "推荐内容 " + CANARY)


def setup(failure=None, guard=False):
    bus = TradeEventBus()
    conversations = SimpleNamespace(touch_session=AsyncMock(), append_turn=AsyncMock(), append_events=AsyncMock())
    evidence = SimpleNamespace(save=AsyncMock(), search=AsyncMock(return_value=[]))
    orchestrator = MainAgentOrchestrator(Sessions(OutputAgent(failure)), bus, EmptyPreferences(),
        conversation_store=conversations, evidence_store=evidence, output_guard_enabled=guard)
    return orchestrator, bus, conversations, evidence


def drain(queue):
    values = []
    while not queue.empty():
        values.append(queue.get_nowait())
    return values


@pytest.mark.parametrize("failure", [None, ValueError("upstream " + CANARY), IncompleteModelResponse(CANARY, "length")])
async def test_final_partial_and_errors_never_bypass_audit_or_leak_legacy_deltas(failure, caplog):
    orchestrator, bus, conversations, evidence = setup(failure)
    queue = bus.subscribe("session")
    result = await orchestrator.handle_intent(SubmitIntentInput("session", "buyer", "zh-CN", "CNY", "test"))
    events = drain(queue)
    assert not any(event.type == "token.delta" for event in events)
    assert CANARY not in result.final_text and CANARY not in (result.error or "")
    assert CANARY not in json.dumps([event.payload for event in events], ensure_ascii=False)
    assert CANARY not in repr(conversations.append_turn.await_args_list)
    assert CANARY not in repr(conversations.append_events.await_args_list)
    assert CANARY not in repr(evidence.save.await_args_list)
    assert CANARY not in caplog.text
    if failure is None:
        assert "推荐内容" in result.final_text
    elif isinstance(failure, IncompleteModelResponse):
        assert result.error_code == "MODEL_RESPONSE_INCOMPLETE"


async def test_native_confirmation_resume_result_passes_same_final_audit(monkeypatch):
    orchestrator, bus, conversations, _ = setup()
    monkeypatch.setattr("app.application.agents.tool_confirmation.confirmation_inputs", lambda *_: ["native-decision"])
    queue = bus.subscribe("session")
    result = await orchestrator.handle_intent(SubmitIntentInput("session", "buyer", "zh-CN", "CNY", "approve",
        confirmations=({"interrupt_id": "pending", "approved": True},)))
    assert CANARY not in result.final_text
    assert CANARY not in repr(conversations.append_turn.await_args_list)
    assert CANARY not in repr([event.payload for event in drain(queue)])


async def test_public_conversation_events_do_not_store_raw_tools():
    orchestrator, bus, conversations, _ = setup()
    queue = bus.subscribe("session")
    bus.publish("session", "tool.invoke", {"tool": "product_search_tool", "arguments": {"api_key": CANARY}})
    bus.publish("session", "tool.result", {"tool": "product_search_tool", "hits": [], "password": CANARY})
    await orchestrator._record_conversation(SubmitIntentInput("session", "buyer", "zh-CN", "CNY", CANARY), CANARY, 1, queue)
    assert CANARY not in repr(conversations.append_events.await_args_list)
    assert CANARY not in repr(conversations.append_turn.await_args_list)


def test_logging_factory_cleans_formatted_args_traceback_and_preserves_tool_name(caplog):
    tracing.install_log_correlation()
    logger = logging.getLogger("output-boundary")
    with caplog.at_level(logging.WARNING):
        logger.warning("provider failure %s", ValueError("OPAQUE_PRIVATE_PROVIDER_RESPONSE"))
        logger.warning("product_search_tool failed %s", CANARY)
        try:
            raise ValueError("OPAQUE_PRIVATE_PROVIDER_RESPONSE " + CANARY)
        except ValueError:
            logger.exception("provider failure")
    assert CANARY not in caplog.text
    assert "OPAQUE_PRIVATE_PROVIDER_RESPONSE" not in caplog.text
    assert "product_search_tool" in caplog.text
    assert "ValueError" in caplog.text
    assert all(CANARY not in str(record.msg) for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_technical_trace_labels_cannot_be_secret_carriers():
    attributes = tracing._sanitize_attributes({"gen_ai.request.model": CANARY,
        "gen_ai.tool.name": "product_search_tool", "gen_ai.response.model": "safe-model"})
    assert "gen_ai.request.model" not in attributes
    assert attributes["gen_ai.tool.name"] == "product_search_tool"
    assert attributes["gen_ai.response.model"] == "safe-model"


async def test_queued_run_projection_cannot_bypass_journal_public_boundary(tmp_path):
    from app.application.agent_runtime import AGUIRuntime
    from app.domain.queue.ports.task_queue import IntentTask
    from app.infrastructure.ag_ui_journal import AGUIJournal
    payload = {"messages": [{"id": "m", "role": "user", "content": CANARY}],
        "forwardedProps": {"password": CANARY}}
    task = IntentTask("agui:queued", "session", "buyer", "zh-CN", "CNY", CANARY, ag_ui_input=payload)
    queue = SimpleNamespace(get_task=AsyncMock(return_value=task),
        get_status=AsyncMock(return_value=SimpleNamespace(state="queued")))
    runtime = AGUIRuntime(AGUIJournal(tmp_path / "runs.db"), SimpleNamespace(), task_queue=queue)
    visible = await runtime.run("queued", "buyer")
    assert visible["status"] == "queued" and CANARY not in json.dumps(visible)
    assert task.raw_query == CANARY, "仅净化公开副本，不能修改真正入队的模型输入"


async def test_old_task_result_read_is_audited_before_legacy_http_reply(monkeypatch):
    # 模块装配要求配置存在；只测试读取函数，绝不连接模型。
    monkeypatch.setenv("LLM_API_KEY", "ci-local-placeholder")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    from app.presentation.server import _await_result
    task = SimpleNamespace(state="done", final_text=CANARY)
    container = SimpleNamespace(bus=TradeEventBus(), settings=SimpleNamespace(queue_wait_seconds=1),
        task_queue=SimpleNamespace(get_status=AsyncMock(return_value=task)))
    assert CANARY not in await _await_result(container, "task", "session")


async def test_enqueue_value_error_is_not_raw_public_conflict(tmp_path):
    import httpx
    from app.application.agent_runtime import AGUIRuntime
    from app.infrastructure.ag_ui_journal import AGUIJournal
    from tests.test_ag_ui_journal import body, application
    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=ValueError("OPAQUE_PRIVATE_QUEUE_ERROR " + CANARY)))
    runtime = AGUIRuntime(AGUIJournal(tmp_path / "runs.db"), SimpleNamespace(), task_queue=queue)
    await runtime.journal.initialize()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(runtime)), base_url="http://test") as client:
        response = await client.post("/commerce/ag-ui/run", json=body())
    assert response.status_code == 409
    assert "OPAQUE_PRIVATE_QUEUE_ERROR" not in response.text and CANARY not in response.text
    assert "不同请求" in response.json()["detail"]
