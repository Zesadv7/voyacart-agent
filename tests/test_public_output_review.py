"""M2b 独立审查发现的 URL 数字主机与活动正文旁路回归。"""
import json

import aiosqlite
import pytest

from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.security.output_guard import safe_url
from app.infrastructure.security.public_output import public_activity, public_agui_event, public_state, public_value, secret_text
from tests.test_ag_ui_journal import body

RAW_BODY = "fixture-unstructured-private-tool-output"
ALIASES = [
    "0x7f.0.0.1", "0x7f.0.0.0x1", "0X7F.0.0.1", "0177.0.0.1", "127.1",
    "127.0.1", "2130706433", "0x7f000001", "017700000001", "0x7f.1",
    "0.0x7f000001", "0xa9.0xfe.0xa9.0xfe", "0x0a.0.0.1", "127.000.000.001",
    "１２７.０.０.１", "127。0。0。1", "%30x7f.0.0.1", "0x7f.0.0.1.", "0x7f.0.0.0x", "0x7f.0.0.0X",
]


@pytest.mark.parametrize("host", ALIASES)
def test_noncanonical_numeric_hosts_cannot_bypass_public_url_policy(host):
    assert safe_url("http://" + host + "/admin") is None


@pytest.mark.parametrize("url", ["https://8.8.8.8/image.png", "https://images.example.test/item.png", "/products/image.webp"])
def test_canonical_public_urls_remain_usable(url):
    assert safe_url(url) == url


@pytest.mark.parametrize("kind", ["tool", "unknown", "thinking"])
def test_activity_content_is_never_a_generic_tool_result_channel(kind):
    activity = {"id": "r1:tool:one", "runId": "r1", "kind": kind, "content": RAW_BODY,
                "toolName": "product_search_tool", "sequence": 1, "status": "completed",
                "inputSummary": "最多 3 件", "resultSummary": "检索返回 3 件商品。"}
    for projected in (public_activity(activity), public_state({"activities": [activity]}),
                      public_agui_event({"type": "CUSTOM", "name": "activity.upsert", "value": activity})):
        assert RAW_BODY not in json.dumps(projected)
    projected = public_activity(activity)
    if kind == "thinking":
        assert projected["content"] == ""
    else:
        assert "content" not in projected
    if kind == "tool":
        assert projected["toolName"] == "product_search_tool"
        assert projected["inputSummary"] == activity["inputSummary"]
        assert projected["resultSummary"] == activity["resultSummary"]
    if kind == "unknown":
        assert "inputSummary" not in projected and "resultSummary" not in projected


async def test_new_and_legacy_activity_journal_boundaries_never_return_tool_body(tmp_path):
    journal = AGUIJournal(tmp_path / "review.db")
    await journal.reserve(body(), "b1", "owner")
    activity = {"id": "r1:tool:one", "runId": "r1", "kind": "tool", "content": RAW_BODY,
                "sequence": 1, "status": "completed", "toolName": "product_search_tool"}
    event = {"type": "CUSTOM", "name": "activity.upsert", "value": activity}
    await journal.append("r1", "owner", [event])
    async with aiosqlite.connect(journal.path) as db:
        saved = await (await db.execute("SELECT event_json FROM agui_events")).fetchall()
        assert RAW_BODY not in json.dumps(saved)
        # 用旧版本原始事件/投影模拟现有库，确认只读兼容层也封住旁路。
        await db.execute("UPDATE agui_events SET event_json=?", (json.dumps(event),))
        projection = {"messages": [], "state": {"activities": [activity]}, "openMessages": [], "openTools": []}
        await db.execute("UPDATE agui_runs SET projection_json=?", (json.dumps(projection),))
        await db.execute("UPDATE agui_sessions SET state_json=?", (json.dumps(projection["state"]),))
        await db.commit()
    for restored in (await journal.run("r1", "b1"), await journal.events("r1", "b1"), await journal.session("s1", "b1")):
        assert RAW_BODY not in json.dumps(restored)
    async with aiosqlite.connect(journal.path) as db:
        raw = await (await db.execute("SELECT event_json FROM agui_events")).fetchone()
        assert RAW_BODY in raw[0]


@pytest.mark.parametrize("key", ["private_key", "Set-Cookie", "Cookie", "access_key", "credentials", "credential", "Authorization", "Proxy-Authorization", "api_key", "OPENAI_API_KEY", "client_secret", "password", "access_token", "refresh_token"])
def test_structured_and_labeled_prose_credentials_share_opaque_secret_coverage(key):
    opaque = "OPAQUE_CANARY_VALUE_1234"
    assert opaque not in secret_text(f"{key}={opaque}")
    assert opaque not in secret_text(f'"{key}": "{opaque}"')
    assert opaque not in secret_text(f"{key}: synthetic_session={opaque}; second_session={opaque}")
    assert opaque not in json.dumps(public_value({key: opaque}))


def test_ordinary_labeled_business_text_is_not_erased():
    assert secret_text("product_id=P1001 quantity=2 currency=CNY") == "product_id=P1001 quantity=2 currency=CNY"


from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


async def test_authoritative_order_scope_survives_confirmation_and_tool_views(confirmation_env):
    from app.application.usecases.order_usecases import OrderItemInput
    from app.infrastructure.security.public_output import public_confirmation, public_tool_result
    prepared = await confirmation_env.service.prepare_order("b1", "s1", [OrderItemInput("P1001", "P1001-S1", 1)], address())
    pending = prepared["confirmation"]
    resolved = await confirmation_env.service.resolve(pending["confirmation_id"], "b1", "s1", pending["snapshot_hash"], True)
    confirmation = resolved["confirmation"]
    order = confirmation["result"]
    assert order["amount_scope"] == "merchandise_only" and order["order_kind"] == "purchase_intent"
    assert public_confirmation(confirmation) == confirmation
    assert json.loads(public_tool_result("query_order_tool", order)) == order
    assert json.loads(public_tool_result("create_order_tool", resolved))["confirmation"] == confirmation


@pytest.mark.parametrize("strategy,reranked", [("keyword_2gram", False), ("embedding_only", False), ("embedding_rerank", True)])
def test_search_result_retains_actual_retrieval_evidence_without_raw_errors(strategy, reranked):
    from app.infrastructure.security.public_output import public_tool_result
    result = json.loads(public_tool_result("product_search_tool", {"hits": [], "recall_strategy": strategy,
        "rerank_applied": reranked, "reranker_error": RAW_BODY,
        "retrieval_diagnostics": {"candidate_limit": 50, "reranker_error": RAW_BODY, "private": RAW_BODY}}))
    assert result["recall_strategy"] == strategy and result["rerank_applied"] is reranked
    assert result["reranker_error"] == "RERANK_UNAVAILABLE"
    assert result["retrieval_diagnostics"] == {"candidate_limit": 50, "reranker_error": "RERANK_UNAVAILABLE"}
    assert RAW_BODY not in json.dumps(result)
    assert json.loads(public_tool_result("product_search_tool", result)) == result
