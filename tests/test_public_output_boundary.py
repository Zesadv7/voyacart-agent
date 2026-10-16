"""真实 SDK + SQLite + 旧 WS 的先审核后公开边界，不连接模型或读取真实凭据。"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from ag_ui.core import RunAgentInput
from agentscope.event import (
    RequireUserConfirmEvent, TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent,
    ThinkingBlockStartEvent, ThinkingBlockDeltaEvent, ThinkingBlockEndEvent,
    ToolCallStartEvent, ToolCallDeltaEvent, ToolCallEndEvent, ToolResultStartEvent,
    ToolResultTextDeltaEvent, ToolResultDataDeltaEvent, ToolResultEndEvent,
)
from agentscope.message import ToolCallBlock, ToolResultState

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.eventbus import TradeEvent, TradeEventBus
from app.infrastructure.security.output_guard import REDACTED, safe_url
from app.infrastructure.security.public_output import (
    public_messages, public_state, public_tool_arguments, public_tool_result,
    public_trade_event, public_value, safe_text, secret_text,
)
from app.presentation.connection import ConnectionManager
from tests.test_ag_ui_journal import body
from tests.test_ag_ui_tool_products import product

CANARY = "sk-synthetic_M2b_never_publish_0123456789"
STRUCTURED = "fixture-private-authorization-value"
INTERNAL = "http://qdrant:6333/collections/private"


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def encoded(events):
    return [event.model_dump(mode="json", by_alias=True, exclude_none=True) for event in events]


def assert_clean(value):
    text = dump(value)
    assert CANARY not in text and STRUCTURED not in text and INTERNAL not in text


def adapter_for():
    events = []
    return AGUIRunAdapter(RunAgentInput.model_validate(body()), events.append), events


@pytest.mark.parametrize("ending", ["finish", "error", "cancelled"])
def test_native_cross_block_text_thinking_and_partial_content_never_publish_before_audit(ending):
    adapter, events = adapter_for()
    adapter.start()
    # 每个字符分别在一个块中，结束块不是允许公开的审核边界。
    for index, char in enumerate(CANARY):
        block = str(index)
        adapter.on_agent_event(TextBlockStartEvent(reply_id="reply", block_id=block))
        adapter.on_agent_event(TextBlockDeltaEvent(reply_id="reply", block_id=block, delta=char))
        adapter.on_agent_event(TextBlockEndEvent(reply_id="reply", block_id=block))
    adapter.on_agent_event(ThinkingBlockStartEvent(reply_id="reply", block_id="think"))
    for char in CANARY:
        adapter.on_agent_event(ThinkingBlockDeltaEvent(reply_id="reply", block_id="think", delta=char))
    adapter.on_agent_event(ThinkingBlockEndEvent(reply_id="reply", block_id="think"))
    assert not any(event.type == "TEXT_MESSAGE_CONTENT" for event in events)
    assert all(not event.value.get("content") for event in events if event.type == "CUSTOM")
    assert_clean(encoded(events))
    if ending == "finish":
        adapter.finish("可见回答 " + CANARY + " " + INTERNAL)
        final = next(event for event in events if event.type == "MESSAGES_SNAPSHOT")
        assert "可见回答" in final.messages[-1].content and REDACTED in final.messages[-1].content
    else:
        adapter.fail("中断 " + CANARY, cancelled=ending == "cancelled")
        assert not any(event.type == "MESSAGES_SNAPSHOT" for event in events)
    assert_clean(encoded(events))
    assert all(item["content"] == "" for item in adapter.state["activities"] if item["kind"] == "thinking")


def test_native_tool_input_is_whole_audited_and_execution_and_confirmation_truth_stay_unchanged():
    adapter, events = adapter_for()
    arguments = {"statement": "不要红色 " + CANARY, "kind": "dislike", "password": STRUCTURED}
    raw = dump(arguments)
    adapter.on_agent_event(ToolCallStartEvent(reply_id="reply", tool_call_id="call", tool_call_name="remember_preference_tool"))
    for char in raw:
        adapter.on_agent_event(ToolCallDeltaEvent(reply_id="reply", tool_call_id="call", delta=char))
        assert not any(event.type == "TOOL_CALL_ARGS" for event in events)
    adapter.on_agent_event(ToolCallEndEvent(reply_id="reply", tool_call_id="call"))
    args = [event for event in events if event.type == "TOOL_CALL_ARGS"]
    assert len(args) == 1 and json.loads(args[0].delta)["kind"] == "dislike"
    call = ToolCallBlock(id="call", name="remember_preference_tool", input=raw)
    before = call.model_dump()
    adapter.on_agent_event(RequireUserConfirmEvent(reply_id="reply", tool_calls=[call]))
    assert call.model_dump() == before
    confirmation = adapter.state["toolApprovals"][0]
    assert confirmation["tool"] == "remember_preference_tool" and confirmation["toolCallId"] == "r1:tool:call"
    assert "不要红色" in confirmation["arguments"]["statement"]
    assert_clean(encoded(events))


@pytest.mark.parametrize("name", ["product_search_tool", "load_agent_skill_tool", "unknown_tool"])
def test_native_tool_result_text_and_media_do_not_publish_raw_data(name):
    adapter, events = adapter_for()
    adapter.on_agent_event(ToolCallStartEvent(reply_id="reply", tool_call_id="call", tool_call_name=name))
    adapter.on_agent_event(ToolResultStartEvent(reply_id="reply", tool_call_id="call", tool_call_name=name))
    for char in dump({"hits": [product(title=CANARY)], "authorization": STRUCTURED}):
        adapter.on_agent_event(ToolResultTextDeltaEvent(reply_id="reply", tool_call_id="call", delta=char))
    assert not any(event.type == "TOOL_CALL_RESULT" for event in events)
    adapter.on_agent_event(ToolResultEndEvent(reply_id="reply", tool_call_id="call", state=ToolResultState.SUCCESS))
    adapter.on_agent_event(ToolResultStartEvent(reply_id="reply", tool_call_id="media", tool_call_name=name))
    adapter.on_agent_event(ToolResultDataDeltaEvent(reply_id="reply", tool_call_id="media", media_type="image", url=INTERNAL))
    adapter.on_agent_event(ToolResultDataDeltaEvent(reply_id="reply", tool_call_id="media", media_type="image", data=CANARY))
    adapter.on_agent_event(ToolResultEndEvent(reply_id="reply", tool_call_id="media", state=ToolResultState.SUCCESS))
    assert_clean(encoded(events))
    assert [event for event in events if event.type == "TOOL_CALL_START"][0].tool_call_name == name


@pytest.mark.parametrize("url", [INTERNAL, "http://127.0.0.1/a", "https://10.0.0.3/a", "https://service.internal/a",
    "https://user:password@public.example/a", "https://public.example/a?access_token=private",
    "https://public.example/a?%61uthorization=private", "https://public.example/a?X-Amz-Signature=private",
    "https://public.example/" + CANARY, "javascript:alert(1)", "file:///private"])
def test_urls_reject_internal_userinfo_credentials_and_non_http_schemes(url):
    assert safe_url(url) is None


def test_structured_credentials_protocol_ids_confirmation_and_product_contracts():
    source = {"toolCallName": "product_search_tool", "toolCallId": "r1:tool:one", "snapshot_hash": "a" * 64,
              "nested": [{"Authorization": STRUCTURED, "access_token": STRUCTURED, "client-secret": STRUCTURED}],
              "shipping_address": {"recipient_name": "测试买家", "phone": "13800000000", "address_line": "测试路88号"},
              "image_url": "https://images.example.test/good.png", "title": CANARY}
    before = copy.deepcopy(source)
    projected = public_value(source)
    assert source == before
    assert projected["toolCallName"] == source["toolCallName"]
    assert projected["toolCallId"] == source["toolCallId"]
    assert projected["snapshot_hash"] == source["snapshot_hash"]
    assert projected["shipping_address"] == source["shipping_address"]
    assert projected["image_url"] == source["image_url"]
    assert_clean(projected)
    assert safe_text("product_search_tool " + CANARY) == REDACTED + " " + REDACTED
    assert secret_text("product_search_tool " + CANARY) == "product_search_tool " + REDACTED
    rich = product(score=0.5, brand="品牌", category="背包", origin_country="CN", highlights=["轻便"],
                   data_provenance={"source": "catalog", "synthetic": [], "derived": []}, rating_summary={"average": 4.5, "review_count": 3})
    state = public_state({"products": [rich], "resumeDestination": "CN"})
    assert state["products"][0] == rich and state["resumeDestination"] == "CN"


def test_unknown_fields_and_old_cross_message_secret_are_not_public():
    assert public_tool_arguments("unknown_tool", {"private": STRUCTURED}) == {}
    assert_clean(json.loads(public_tool_result("unknown_tool", dump({"private": STRUCTURED}))))
    assert public_state({"private": STRUCTURED}) == {}
    old = [{"id": "a", "role": "assistant", "content": CANARY[:2]},
           {"id": "b", "role": "assistant", "content": CANARY[2:]}]
    assert all(item["content"] == REDACTED for item in public_messages(old))


async def test_journal_audits_before_sqlite_bytes_and_every_read_preserving_product_history(tmp_path):
    journal = AGUIJournal(tmp_path / "public.db")
    await journal.reserve(body(), "b1", "owner")
    await journal.append("r1", "owner", [{"type": "TEXT_MESSAGE_START", "messageId": "a", "role": "assistant"},
        {"type": "TOOL_CALL_START", "toolCallId": "t", "toolCallName": "product_search_tool"}])
    for char in CANARY:
        await journal.append("r1", "owner", [{"type": "TEXT_MESSAGE_CONTENT", "messageId": "a", "delta": char}])
    await journal.append("r1", "owner", [
        {"type": "TOOL_CALL_RESULT", "toolCallId": "t", "messageId": "result", "role": "tool",
         "content": dump({"hits": [product(title="商品 " + CANARY, image_url=INTERNAL)], "password": STRUCTURED})},
        {"type": "MESSAGES_SNAPSHOT", "messages": [{"id": "final", "role": "assistant", "content": "回答 " + CANARY}]},
        {"type": "STATE_SNAPSHOT", "snapshot": {"products": [product(title=CANARY)], "activities": [], "private": STRUCTURED}},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "s1"},
    ])
    async with aiosqlite.connect(journal.path) as db:
        for table in ("agui_events", "agui_runs", "agui_sessions"):
            rows = await (await db.execute("SELECT * FROM " + table)).fetchall()
            assert_clean(rows)
    events, status, cursor = await journal.events("r1", "b1")
    assert status == "completed" and [item["seq"] for item in events] == list(range(1, cursor + 1))
    for read in [await journal.run("r1", "b1"), await journal.session("s1", "b1"), await journal.sessions("b1"), events]:
        assert_clean(read)
    products = await journal.tool_products("r1", "t", "b1")
    assert products["status"] == "ready" and products["count"] == 1
    assert products["products"][0]["product_id"] == "P1001"
    assert products["products"][0]["spec"] == "大号" and "image_url" not in products["products"][0]
    assert_clean(products)


async def test_legacy_journal_cross_chunk_replay_any_cursor_is_safe_without_rewriting_history(tmp_path):
    journal = AGUIJournal(tmp_path / "old.db")
    await journal.reserve(body(), "b1", "owner")
    raw = [{"type": "TEXT_MESSAGE_START", "messageId": "old", "role": "assistant"}]
    raw += [{"type": "TEXT_MESSAGE_CONTENT", "messageId": "old", "delta": char} for char in CANARY]
    raw += [{"type": "TOOL_CALL_START", "toolCallId": "old-tool", "toolCallName": "product_search_tool"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": "old-tool", "messageId": "result", "role": "tool", "content": dump({"hits": [product(title=CANARY)]})}]
    projection = {"messages": [{"id": "a", "role": "assistant", "content": CANARY[:2]}, {"id": "b", "role": "assistant", "content": CANARY[2:]}],
                  "state": {"activities": [{"id": "think", "kind": "thinking", "content": CANARY}], "products": [product(title=CANARY)]}, "openMessages": [], "openTools": []}
    async with aiosqlite.connect(journal.path) as db:
        await db.executemany("INSERT INTO agui_events VALUES('r1',?,?)", [(seq, dump(event)) for seq, event in enumerate(raw, 1)])
        await db.execute("UPDATE agui_runs SET last_seq=?,projection_json=?,status='completed' WHERE run_id='r1'", (len(raw), dump(projection)))
        await db.execute("UPDATE agui_sessions SET messages_json=?,state_json=?,title=?", (dump(projection["messages"]), dump(projection["state"]), CANARY))
        await db.commit()
    all_events, _, _ = await journal.events("r1", "b1")
    for cursor in range(len(raw) + 1):
        replay, _, last = await journal.events("r1", "b1", after=cursor)
        assert replay == all_events[cursor:] and last == len(raw)
        assert_clean(replay)
        assert "".join(item["event"].get("delta", "") for item in replay) == ""
    assert_clean(await journal.run("r1", "b1"))
    assert_clean(await journal.session("s1", "b1"))
    assert_clean(await journal.tool_products("r1", "old-tool", "b1"))
    async with aiosqlite.connect(journal.path) as db:
        unchanged = await (await db.execute("SELECT event_json FROM agui_events ORDER BY seq")).fetchall()
    assert [json.loads(row[0]) for row in unchanged] == raw


async def test_old_websocket_never_forwards_raw_tokens_tools_or_exception(monkeypatch):
    bus = TradeEventBus()
    monkeypatch.setattr("app.presentation.connection.require_buyer", AsyncMock(return_value="b1"))
    monkeypatch.setattr("app.presentation.connection.require_session", AsyncMock())
    raw = [TradeEvent("s1", "token.delta", {"text": char}, "now") for char in CANARY]
    raw += [TradeEvent("s1", "tool.result", {"tool": "load_agent_skill_tool", "body": CANARY, "password": STRUCTURED}, "now"),
            TradeEvent("s1", "error", {"message": CANARY + INTERNAL}, "now"),
            TradeEvent("s1", "final.result", {"text": "安全结论 " + CANARY}, "now")]

    class Socket:
        scope = {"subprotocols": []}
        sent = []
        disconnected = asyncio.Event()
        async def accept(self, **kwargs): pass
        async def receive_json(self): return {"buyer_id": "b1", "shopping_session_id": "s1"}
        async def receive(self):
            await self.disconnected.wait()
            return {"type": "websocket.disconnect"}
        async def send_json(self, payload):
            self.sent.append(payload)
            if payload["type"] == "final.result": self.disconnected.set()

    socket = Socket()
    task = asyncio.create_task(ConnectionManager(bus).serve(socket))
    async with asyncio.timeout(2):
        while not bus._subscribers:
            await asyncio.sleep(0)
        for event in raw:
            bus.deliver_local(event)
        await task
    assert [item["type"] for item in socket.sent] == ["tool.result", "error", "final.result"]
    assert "安全结论" in socket.sent[-1]["payload"]["text"]
    assert_clean(socket.sent)
    assert raw[0].payload == {"text": CANARY[0]}


async def test_runtime_public_projection_does_not_modify_actual_user_execution_input(tmp_path):
    from types import SimpleNamespace
    from app.application.agent_runtime import AGUIRuntime
    from app.presentation.ag_ui import parse_intent
    from tests.test_ag_ui_journal import wait_status
    raw = "请查找 product_search_tool 提到的商品 " + CANARY
    request = RunAgentInput.model_validate(body(query=raw))
    orchestrator = SimpleNamespace(handle_intent=AsyncMock(return_value=SimpleNamespace(final_text="已完成", error=None)))
    runtime = AGUIRuntime(AGUIJournal(tmp_path / "input.db"), orchestrator)
    try:
        await runtime.start(request, parse_intent(request))
        await wait_status(runtime.journal, "r1", "completed")
        actual = orchestrator.handle_intent.await_args.args[0]
        assert actual.raw_query == raw
        assert request.messages[-1].content == raw
        assert_clean(await runtime.journal.run("r1", "b1"))
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("name", ["product_search_tool", "query_order_tool", "load_agent_skill_tool", "unknown"])
@pytest.mark.parametrize("content", ["invalid", '{"hits": []}', '{"hits": [{"product_id": "P1001", "title": "商品", "price_major": 2, "currency": "CNY"}]}'])
def test_tool_public_projection_is_idempotent_for_multiple_boundaries(name, content):
    once = public_tool_result(name, content)
    assert public_tool_result(name, once) == once


def test_tool_result_serialization_is_stable_across_process_hash_seeds():
    import subprocess
    import sys
    script = """from app.infrastructure.security.public_output import public_tool_result
print(public_tool_result('product_search_tool', {'hits': [{'product_id': 'P1001', 'title': '商品', 'price_major': 2, 'currency': 'CNY', 'score': .5}]}))
"""
    outputs = [subprocess.check_output([sys.executable, "-c", script], env={"PYTHONHASHSEED": seed}) for seed in ("1", "2", "12345")]
    assert len(set(outputs)) == 1


async def test_actual_a2ui_v09_envelopes_survive_adapter_projection(tmp_path):
    from app.infrastructure.shopping_forms import ShoppingFormStore
    store = ShoppingFormStore(tmp_path / "forms.db")
    form = await store.create_clarification("b1", "s1", "补充需求", [{"id": "needs", "type": "text", "label": "需求"}])
    adapter, events = adapter_for()
    adapter.on_trade_event(TradeEvent("s1", "ui.surface", {"buyer_id": "b1", "form": form}, "now"))
    projected = [event.value for event in events if event.type == "CUSTOM" and event.name == "a2ui"]
    assert projected == form["messages"]
    assert [next(key for key in message if key != "version") for message in projected] == ["createSurface", "updateComponents", "updateDataModel"]


def test_context_reason_and_all_usage_counters_keep_their_meaning():
    from app.infrastructure.security.public_output import public_custom
    from app.infrastructure.operational_metrics import begin_request, finish_request
    context = {"reason": "pressure", "before_tokens": 100, "after_tokens": 10, "policy_version": "v1", "archived_results": 2, "summary_changed": True}
    assert public_custom("context.compressed", context) == context
    usage = finish_request(begin_request())
    assert public_custom("usage.summary", usage) == usage
