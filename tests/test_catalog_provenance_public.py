"""M3 来源白名单：保留可解释字段，沿用 M2 审核且不公开任意元数据。"""
import copy
import json

import aiosqlite
import pytest

from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.security.output_guard import REDACTED
from app.infrastructure.security.public_output import public_products, public_state, public_tool_result
from app.infrastructure.tool_product_view import tool_product_view
from tests.test_ag_ui_journal import body
from tests.test_ag_ui_tool_products import product

PROVENANCE = {
    "source": "esci", "synthetic": ["skus.price_major", "skus.stock", "ships_to"],
    "derived": ["category", "material_tags", "origin_country"],
    "category_source": "product-enrich-batch-v1", "category_method": "train_exact_query_majority",
    "category_status": "provisional_unreviewed", "material_status": "unknown",
    "origin_country_method": "source_locale_proxy",
    "source_text_processing": "html_to_text; description_max_1000; bullet_max_200_and_5",
}
OPAQUE = "fixture-private-opaque-model-body"
CANARY = "sk-synthetic_M3c_never_publish_0123456789"


def rich(**changes):
    return product(category="办公学习", source_locale="us", data_provenance=copy.deepcopy(PROVENANCE), **changes)


def test_known_provenance_survives_state_tool_result_and_mini_card_without_mutation():
    value = rich()
    before = copy.deepcopy(value)
    state = public_state({"products": [value]})
    saved = json.loads(public_tool_result("product_search_tool", {"hits": [value]}))
    mini = tool_product_view(json.dumps(saved))["products"][0]
    for card in (state["products"][0], saved["hits"][0], mini):
        assert card["data_provenance"] == PROVENANCE
        assert card["category"] == "办公学习" and card["source_locale"] == "us"
    mini["data_provenance"]["derived"].append("new")
    assert value == before
    assert mini["spec"] == "大号" and mini["price_major"] == 129.5


def test_unknown_fields_and_unsupported_review_claims_are_removed_before_publication():
    value = rich()
    value["data_provenance"].update(raw_response={"body": OPAQUE}, reviewer="human", api_key=CANARY,
        category_status="human_verified", material_status={"claim": OPAQUE},
        origin_country_method="warehouse", category_source={"prompt": OPAQUE})
    value["data_provenance"]["derived"].extend([{"private": OPAQUE}, OPAQUE])
    expected = {key: entry for key, entry in PROVENANCE.items()
                if key not in {"category_status", "material_status", "origin_country_method", "category_source"}}
    for card in (public_products([value])[0], tool_product_view(json.dumps({"hits": [value]}))["products"][0]):
        assert card["data_provenance"] == expected
        assert OPAQUE not in json.dumps(card) and CANARY not in json.dumps(card)


@pytest.mark.parametrize("provenance", [None, [], True, {"source": []}, {"source": "esci", "derived": {}},
    {"source": OPAQUE, "synthetic": [], "derived": []}])
def test_malformed_provenance_never_breaks_valid_product_or_leaks_metadata(provenance):
    value = product(data_provenance=provenance)
    for card in (public_products([value])[0], tool_product_view(json.dumps({"hits": [value]}))["products"][0]):
        assert "data_provenance" not in card
        assert card["product_id"] == "P1001"


def test_mini_card_new_fields_still_use_m2_audit_and_legacy_esci_method_is_not_invented():
    value = product(category="分类 " + CANARY, source_locale="us", data_provenance={
        "source": "esci", "synthetic": ["skus.stock"], "derived": ["origin_country"],
        "category_source": "product-enrich-batch-v1"})
    mini = tool_product_view(json.dumps({"hits": [value]}))["products"][0]
    assert CANARY not in json.dumps(mini) and REDACTED in mini["category"]
    assert "origin_country_method" not in mini["data_provenance"]


def test_new_mini_fields_are_audited_before_truncation():
    value = product(category="x" * 74 + " " + CANARY, source_locale=CANARY)
    mini = tool_product_view(json.dumps({"hits": [value]}))["products"][0]
    assert "sk-" not in mini["category"]
    assert mini["source_locale"] == REDACTED


async def test_sqlite_journal_and_replay_keep_sources_without_opaque_raw_metadata(tmp_path):
    journal = AGUIJournal(tmp_path / "source.db")
    await journal.reserve(body(), "b1", "owner")
    value = rich()
    value["data_provenance"]["raw_model"] = {"body": OPAQUE, "authorization": CANARY}
    await journal.append("r1", "owner", [
        {"type": "TOOL_CALL_START", "toolCallId": "t", "toolCallName": "product_search_tool"},
        {"type": "TOOL_CALL_RESULT", "toolCallId": "t", "messageId": "result", "role": "tool",
         "content": json.dumps({"hits": [value]})},
        {"type": "STATE_SNAPSHOT", "snapshot": {"products": [value]}},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "s1"},
    ])
    restored = AGUIJournal(journal.path)
    view = await restored.tool_products("r1", "t", "b1")
    assert view["products"][0]["data_provenance"] == PROVENANCE
    for saved in (await restored.events("r1", "b1"), await restored.run("r1", "b1"), view):
        assert OPAQUE not in json.dumps(saved) and CANARY not in json.dumps(saved)
    async with aiosqlite.connect(journal.path) as db:
        for table in ("agui_runs", "agui_sessions", "agui_events"):
            rows = await (await db.execute("SELECT * FROM " + table)).fetchall()
            assert OPAQUE not in json.dumps(rows) and CANARY not in json.dumps(rows)
