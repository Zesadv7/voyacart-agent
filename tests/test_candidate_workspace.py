# -*- coding: utf-8 -*-
"""M4 候选持久层合同：临时真实 SQLite，不调用外部服务或模型。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sqlite3
from uuid import UUID

import pytest

from app.application.usecases.catalog_search import ProductCard
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.candidate_workspace import CandidateWorkspaceError, CandidateWorkspaceStore
from app.infrastructure.persistence.catalog_identity import catalog_bindings


def product(number=1):
    return Product(product_id=f"P{number}", title=f"测试旅行包 {number}", brand="测试品牌", category="旅行装备",
        origin_country="US", description="仅合成测试数据", ships_to=["CN", "US"],
        source_platform="fixture-shop", source_locale="US", external_product_id=f"fixture-{number}",
        canonical_product_id=f"canonical-{number}", skus=[
            Sku(f"P{number}-S1", "黑色", Money.of(1000, "USD"), 3),
            Sku(f"P{number}-S2", "白色", Money.of(2000, "USD"), 0)])


def card(value=None):
    value = value or product()
    return ProductCard(product_id=value.product_id, title=value.title, brand=value.brand, category=value.category,
        origin_country=value.origin_country, price_major=71.0, currency="CNY", source_price_major=10.0,
        source_currency="USD", highlights=["测试亮点"], skus=[{"sku_id": sku.sku_id, "spec": sku.spec,
            "price_major": sku.price.to_major_units(), "currency": sku.price.currency, "stock": sku.stock} for sku in value.skus],
        score=1.0, landed_price={"ship_to": "CN", "subtotal_major": 71.0, "freight_major": 25.0,
            "tariff_major": 0.0, "tariff_rate": 0.09, "de_minimis_applied": True,
            "landed_total_major": 96.0, "currency": "CNY"},
        source_platform=value.source_platform, canonical_product_id=value.canonical_product_id,
        material_tags=[], weight_kg=0.5, default_sku_id=value.skus[0].sku_id,
        ships_to=list(value.ships_to), source_locale=value.source_locale, source_language="en",
        external_product_id=value.external_product_id, data_provenance="test-fixture").to_dict()


def bindings(products):
    return {sku: {"product_id": item["product_id"], "version": 1, "identity_hash": item["identity_hash"],
                  "terms_hash": item["terms_hash"], "generation": 1, "active": True}
            for sku, item in catalog_bindings(products).items()}


def source(number=1, sku=1, run="r1"):
    return {"kind": "run", "run_id": run, "product_id": f"P{number}", "sku_id": f"P{number}-S{sku}"}


async def seed(journal, *, run="r1", session="s1", buyer="b1", products=None, cards=None, bound=True):
    products = products or [product()]
    if bound:
        await journal.bind_catalog(bindings(products))
    await journal.reserve({"runId": run, "threadId": session,
        "messages": [{"id": f"user-{run}", "role": "user", "content": "查询合成测试商品"}],
        "forwardedProps": {"buyerId": buyer}}, buyer, "test-owner")
    await journal.append(run, "test-owner", [
        {"type": "STATE_SNAPSHOT", "snapshot": {"products": cards if cards is not None else [card(item) for item in products]}},
        {"type": "RUN_FINISHED", "threadId": session, "runId": run}])


async def setup(tmp_path, **kwargs):
    journal = AGUIJournal(tmp_path / "runs.db")
    await seed(journal, **kwargs)
    return journal, CandidateWorkspaceStore(journal)


async def table_rows(journal, table):
    # 表名由本文件固定提供，不使用用户输入。
    async with journal._db() as db:
        async with db.execute(f"SELECT * FROM {table}") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def replace_products(journal, products, run="r1"):
    async with journal._db(write=True) as db:
        await db.execute("UPDATE agui_runs SET projection_json=?,last_seq=last_seq+1 WHERE run_id=?",
            (json.dumps({"state": {"products": products}, "messages": []}), run))


def error(code, status=None):
    class Expected:
        def __enter__(self):
            self.context = pytest.raises(CandidateWorkspaceError)
            self.result = self.context.__enter__()
            return self.result

        def __exit__(self, *args):
            self.context.__exit__(*args)
            assert self.result.value.code == code
            if status is not None:
                assert self.result.value.status == status
            return True
    return Expected()


async def test_get_is_read_only_and_does_not_claim_unknown_sessions(tmp_path):
    journal, store = await setup(tmp_path)
    empty = await store.list("b1", "s1")
    assert empty == {"schema_version": 1, "buyer_id": "b1", "session_id": "s1", "revision": 0,
        "items": [], "comparison_ids": [], "limits": {"candidates": 30, "comparison": 3}}
    assert await table_rows(journal, "candidate_workspaces") == []
    with error("CANDIDATE_NOT_FOUND", 404):
        await store.list("b1", "absent")
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.list("other", "s1")
    assert await table_rows(journal, "candidate_workspaces") == []


async def test_add_is_bound_to_committed_source_and_deduplicates_without_refresh(tmp_path):
    journal, store = await setup(tmp_path)
    first = await store.add("b1", "s1", "request-1", 0, source())
    item = first["items"][0]
    UUID(item["candidate_id"])
    assert first["revision"] == 1
    assert item["identity_status"] == "bound" and item["quote_status"] == "historical"
    assert item["source"]["kind"] == "run" and item["source"]["run_id"] == "r1"
    assert item["source"]["captured_cursor"] == 2
    assert item["saved_at"].endswith("Z") and item["source"]["captured_at"].endswith("Z")
    assert item["snapshot"] == card()
    private = await store.get_candidate("b1", "s1", item["candidate_id"])
    assert private["catalog_binding"] == bindings([product()])["P1-S1"]
    assert private["workspace_revision"] == 1
    assert "catalog_binding" not in item and "identity_hash" not in json.dumps(first)
    changed = card()
    changed.update(price_major=142, source_price_major=20)
    changed["landed_price"].update(subtotal_major=142, landed_total_major=167)
    await replace_products(journal, [changed])
    again = await store.add("b1", "s1", "request-2", 1, source())
    assert again == first
    assert len(await table_rows(journal, "candidate_requests")) == 2


async def test_same_product_multiple_skus_preserve_coherent_currency_and_quote(tmp_path):
    _, store = await setup(tmp_path)
    first = await store.add("b1", "s1", "one", 0, source())
    second = await store.add("b1", "s1", "two", 1, source(sku=2))
    assert second["revision"] == 2 and len(second["items"]) == 2
    assert second["items"][0] == first["items"][0]
    snapshot = second["items"][1]["snapshot"]
    assert snapshot["default_sku_id"] == "P1-S2"
    assert snapshot["price_major"] == snapshot["source_price_major"] == 20
    assert snapshot["currency"] == snapshot["source_currency"] == "USD"
    assert "landed_price" not in snapshot
    assert second["items"][1]["identity_status"] == "bound"
    # 保存时缺货仍可保留；这不是交易或库存预留。
    assert snapshot["skus"][1]["stock"] == 0


async def test_comparison_order_noops_delete_cleanup_and_new_candidate_id(tmp_path):
    _, store = await setup(tmp_path)
    one = await store.add("b1", "s1", "one", 0, source())
    two = await store.add("b1", "s1", "two", 1, source(sku=2))
    ids = [item["candidate_id"] for item in two["items"]]
    compared = await store.compare("b1", "s1", "compare", 2, ids[::-1])
    assert compared["revision"] == 3 and compared["comparison_ids"] == ids[::-1]
    assert await store.compare("b1", "s1", "noop", 3, ids[::-1]) == compared
    reordered = await store.compare("b1", "s1", "reorder", 3, ids)
    assert reordered["revision"] == 4
    removed = await store.remove("b1", "s1", "remove", 4, ids[0])
    assert removed["revision"] == 5 and removed["comparison_ids"] == [ids[1]]
    restored = await store.add("b1", "s1", "restore", 5, source())
    assert restored["items"][-1]["candidate_id"] != one["items"][0]["candidate_id"]
    assert restored["comparison_ids"] == [ids[1]]
    with error("CANDIDATE_NOT_FOUND", 404):
        await store.remove("b1", "s1", "remove-again", 6, ids[0])


async def test_empty_compare_noop_persists_request_without_bumping_revision(tmp_path):
    journal, store = await setup(tmp_path)
    result = await store.compare("b1", "s1", "empty", 0, [])
    assert result["revision"] == 0 and result["items"] == []
    assert len(await table_rows(journal, "candidate_requests")) == 1
    assert await store.compare("b1", "s1", "empty", 0, []) == result


async def test_replay_after_source_deleted_and_after_candidate_deleted_does_not_resurrect(tmp_path):
    journal, store = await setup(tmp_path)
    added = await store.add("b1", "s1", "add", 0, source())
    candidate_id = added["items"][0]["candidate_id"]
    removed = await store.remove("b1", "s1", "remove", 1, candidate_id)
    async with journal._db(write=True) as db:
        await db.execute("DELETE FROM agui_runs WHERE run_id='r1'")
    assert await store.add("b1", "s1", "add", 0, source()) == added
    assert await store.remove("b1", "s1", "remove", 1, candidate_id) == removed
    assert (await store.list("b1", "s1"))["revision"] == 2
    assert (await store.list("b1", "s1"))["items"] == []
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.add("other", "s1", "add", 0, source())


async def test_request_hash_covers_revision_source_action_and_target(tmp_path):
    _, store = await setup(tmp_path)
    added = await store.add("b1", "s1", "key", 0, source())
    candidate_id = added["items"][0]["candidate_id"]
    for revision, changed_source in ((1, source()), (0, source(sku=2)), (0, source(run="other"))):
        with error("CANDIDATE_REQUEST_CONFLICT", 409):
            await store.add("b1", "s1", "key", revision, changed_source)
    with error("CANDIDATE_REQUEST_CONFLICT", 409):
        await store.compare("b1", "s1", "key", 0, [])
    with error("CANDIDATE_REQUEST_CONFLICT", 409):
        await store.remove("b1", "s1", "key", 0, candidate_id)
    await store.remove("b1", "s1", "delete", 1, candidate_id)
    with error("CANDIDATE_REQUEST_CONFLICT", 409):
        await store.remove("b1", "s1", "delete", 1, "different-candidate")


async def test_independent_connections_serialize_competing_revisions(tmp_path):
    journal, _ = await setup(tmp_path)
    stores = [CandidateWorkspaceStore(AGUIJournal(journal.path)) for _ in range(8)]
    await asyncio.gather(*(store.initialize() for store in stores))
    results = await asyncio.gather(*(store.add("b1", "s1", f"request-{n}", 0, source(sku=1 + n % 2))
                                    for n, store in enumerate(stores)), return_exceptions=True)
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, CandidateWorkspaceError)]
    assert len(successes) == 1 and len(failures) == 7
    assert all(result.code == "CANDIDATE_REVISION_CONFLICT" and result.current_revision == 1 for result in failures)
    assert len(await table_rows(journal, "session_candidates")) == 1
    assert len(await table_rows(journal, "candidate_requests")) == 1


async def test_same_request_concurrent_replay_and_restart(tmp_path):
    journal, store = await setup(tmp_path)
    second = CandidateWorkspaceStore(AGUIJournal(journal.path))
    left, right = await asyncio.gather(store.add("b1", "s1", "shared", 0, source()),
                                     second.add("b1", "s1", "shared", 0, source()))
    assert left == right
    fresh = CandidateWorkspaceStore(AGUIJournal(journal.path))
    assert await fresh.list("b1", "s1") == left
    assert await fresh.add("b1", "s1", "shared", 0, source()) == left
    assert len(await table_rows(journal, "candidate_requests")) == 1


async def test_add_failure_rolls_back_candidate_workspace_and_request(tmp_path, monkeypatch):
    journal, store = await setup(tmp_path)
    record = store._record_request
    async def fail(*args):
        raise sqlite3.OperationalError("synthetic request persistence failure")
    monkeypatch.setattr(store, "_record_request", fail)
    with pytest.raises(sqlite3.OperationalError):
        await store.add("b1", "s1", "rollback", 0, source())
    for table in ("session_candidates", "candidate_workspaces", "candidate_requests"):
        assert await table_rows(journal, table) == []
    monkeypatch.setattr(store, "_record_request", record)
    assert (await store.add("b1", "s1", "rollback", 0, source()))["revision"] == 1


async def test_delete_failure_rolls_back_comparison_and_revision(tmp_path, monkeypatch):
    journal, store = await setup(tmp_path)
    initial = await store.add("b1", "s1", "add", 0, source())
    candidate_id = initial["items"][0]["candidate_id"]
    before = await store.compare("b1", "s1", "compare", 1, [candidate_id])
    async def fail(*args):
        raise sqlite3.OperationalError("synthetic delete persistence failure")
    monkeypatch.setattr(store, "_record_request", fail)
    with pytest.raises(sqlite3.OperationalError):
        await store.remove("b1", "s1", "remove", 2, candidate_id)
    assert await store.list("b1", "s1") == before
    assert len(await table_rows(journal, "candidate_requests")) == 2


async def test_capacity_comparison_constraints_and_duplicate_at_capacity(tmp_path):
    journal, store = await setup(tmp_path, products=[product(number) for number in range(1, 17)])
    current = None
    for offset in range(30):
        current = await store.add("b1", "s1", f"add-{offset}", offset, source(number=1 + offset // 2, sku=1 + offset % 2))
    assert current["revision"] == 30 and len(current["items"]) == 30
    with error("CANDIDATE_LIMIT", 409):
        await store.add("b1", "s1", "over", 30, source(number=16))
    assert await store.add("b1", "s1", "duplicate", 30, source()) == current
    ids = [item["candidate_id"] for item in current["items"]]
    with error("COMPARISON_LIMIT", 409):
        await store.compare("b1", "s1", "over-compare", 30, ids[:4])
    with error("INVALID_CANDIDATE_REQUEST", 422):
        await store.compare("b1", "s1", "duplicate-compare", 30, [ids[0], ids[0]])
    with error("CANDIDATE_NOT_FOUND", 404):
        await store.compare("b1", "s1", "unknown", 30, ["absent"])
    compared = await store.compare("b1", "s1", "three", 30, ids[:3])
    assert compared["comparison_ids"] == ids[:3] and compared["revision"] == 31


async def test_session_source_and_candidate_ownership_are_rechecked(tmp_path):
    journal, store = await setup(tmp_path)
    await seed(journal, run="r2", session="s2")
    await seed(journal, run="r3", session="s3", buyer="b3")
    for other_run in ("r2", "r3"):
        with error("CANDIDATE_FORBIDDEN", 403):
            await store.add("b1", "s1", "foreign-" + other_run, 0, source(run=other_run))
    foreign = await store.add("b1", "s2", "add", 0, source(run="r2"))
    candidate_id = foreign["items"][0]["candidate_id"]
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.compare("b1", "s1", "compare", 0, [candidate_id])
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.remove("b1", "s1", "remove", 0, candidate_id)
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.get_candidate("b1", "s1", candidate_id)
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.get_candidate("b3", "s3", candidate_id)
    assert (await store.list("b1", "s1"))["revision"] == 0


async def test_unknown_legacy_source_never_gets_current_binding(tmp_path):
    journal, store = await setup(tmp_path, bound=False)
    await journal.bind_catalog(bindings([product()]))
    result = await store.add("b1", "s1", "legacy", 0, source())
    candidate = result["items"][0]
    assert candidate["identity_status"] == "unknown"
    assert (await store.get_candidate("b1", "s1", candidate["candidate_id"]))["catalog_binding"] is None


@pytest.mark.parametrize("field,value", [
    ("source_platform", "other-shop"), ("source_locale", "JP"), ("external_product_id", "changed-item"),
    ("canonical_product_id", "different-canonical"), ("title", "旧标题"), ("ships_to", ["JP"]),
    ("origin_country", "JP"), ("spec", "旧规格")])
async def test_resumed_or_changed_card_cannot_inherit_new_catalog_identity(tmp_path, field, value):
    journal, store = await setup(tmp_path)
    changed = card()
    if field == "spec":
        changed["skus"][0][field] = value
    else:
        changed[field] = value
    await replace_products(journal, [changed])
    result = await store.add("b1", "s1", "changed", 0, source())
    item = result["items"][0]
    assert item["identity_status"] == "unknown"
    assert (await store.get_candidate("b1", "s1", item["candidate_id"]))["catalog_binding"] is None


@pytest.mark.parametrize("change", [
    {"product_id": "P2"}, {"version": True}, {"version": 2}, {"active": False},
    {"generation": 0}, {"generation": True}, {"identity_hash": "invalid"}, {"terms_hash": "invalid"}, {"remove_sku": True}])
async def test_malformed_or_wrong_run_binding_fails_closed(tmp_path, change):
    journal, store = await setup(tmp_path)
    mapping = bindings([product()])
    if change.pop("remove_sku", False):
        mapping.pop("P1-S1")
    else:
        mapping["P1-S1"].update(change)
    async with journal._db(write=True) as db:
        await db.execute("UPDATE agui_catalog_snapshots SET bindings_json=?", (json.dumps(mapping),))
    with error("SOURCE_CHANGED", 409):
        await store.add("b1", "s1", "wrong-binding", 0, source())
    assert (await store.list("b1", "s1"))["items"] == []


async def test_source_never_falls_back_to_other_runs_tools_or_session_state(tmp_path):
    journal, store = await setup(tmp_path)
    await seed(journal, run="r2", session="s1")
    await replace_products(journal, [], "r1")
    with error("SOURCE_CHANGED", 409):
        await store.add("b1", "s1", "changed", 0, source())
    async with journal._db(write=True) as db:
        await db.execute("UPDATE agui_runs SET projection_json=? WHERE run_id='r1'", (json.dumps({"state": {}, "messages": []}),))
        for seq in (3, 4):
            await db.execute("INSERT INTO agui_events VALUES(?,?,?)", ("r1", seq, json.dumps({
                "type": "TOOL_CALL_RESULT", "toolCallId": "duplicate", "content": json.dumps({"hits": [card()]})})))
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "unready", 0, source())
    with error("CANDIDATE_NOT_FOUND", 404):
        await store.add("b1", "s1", "missing", 0, source(run="absent"))
    assert (await store.add("b1", "s1", "correct", 0, source(run="r2")))["revision"] == 1


async def test_second_public_filter_removes_legacy_private_data_before_candidate_persistence(tmp_path):
    journal, store = await setup(tmp_path)
    raw = card()
    secret = "sk-syntheticSecretForPrivacyTest12345"
    raw.update(description=f"Bearer {secret}", trace={"secret": secret}, image_url=f"https://shop.example/image?token={secret}",
        catalog_binding={"identity_hash": "forged"}, data_provenance={"source": "test-fixture", "synthetic": [],
            "derived": [], "verified_by_human": True, "private_token": secret})
    raw["skus"][0]["access_token"] = secret
    raw["skus"][0]["dimensions_cm"] = {"private_note": "internal trace"}
    raw["inventory"] = {"private_note": "internal trace"}
    raw["landed_price"].update(private_note=secret, debug="internal trace")
    raw["dimensions_cm"] = {"width": 3, "private_note": secret}
    await replace_products(journal, [raw])
    result = await store.add("b1", "s1", "private", 0, source())
    snapshot = result["items"][0]["snapshot"]
    assert snapshot["image_url"] is None
    assert "已脱敏" in snapshot["description"]
    assert "trace" not in snapshot and "catalog_binding" not in snapshot
    assert snapshot["dimensions_cm"] == {"width": 3}
    public_and_stored = json.dumps(result) + json.dumps(await table_rows(journal, "session_candidates")) + json.dumps(await table_rows(journal, "candidate_requests"))
    for private in (secret, "access_token", "private_note", "private_token", "verified_by_human", "internal trace"):
        assert private not in public_and_stored


@pytest.mark.parametrize("field,value", [
    ("price_major", float("nan")), ("price_major", float("inf")), ("price_major", -1),
    ("price_major", True), ("source_price_major", None), ("weight_kg", float("-inf")),
    ("score", float("nan")), ("currency", "not-currency"), ("source_currency", "usd"),
    ("highlights", "not-a-list"), ("ships_to", None), ("default_sku_id", "missing"),
    ("title", ""), ("description", "x" * 16001), ("dimensions_cm", {"width": float("inf")}),
    ("image_kind", []), ("image_kind", "unverified-custom"), ("image_url", {}),
    ("rating_summary", {"average": float("nan"), "review_count": 1})])
async def test_malformed_product_facts_are_not_completed_or_saved(tmp_path, field, value):
    journal, store = await setup(tmp_path)
    raw = card()
    raw[field] = value
    await replace_products(journal, [raw])
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "invalid", 0, source())
    assert (await store.list("b1", "s1"))["items"] == []


@pytest.mark.parametrize("field,value", [("stock", True), ("stock", -1), ("stock", 1.5), ("stock", float("inf")),
    ("price_major", float("nan")), ("currency", None), ("sku_id", ""), ("spec", ["not", "text"])])
async def test_invalid_sku_is_rejected_before_snapshot_write(tmp_path, field, value):
    journal, store = await setup(tmp_path)
    raw = card()
    raw["skus"][0][field] = value
    await replace_products(journal, [raw])
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "invalid", 0, source())


async def test_missing_required_fields_unknown_sku_and_duplicate_cards_fail_closed(tmp_path):
    journal, store = await setup(tmp_path)
    for field in ("ships_to", "source_price_major", "stock", "skus"):
        raw = card()
        if field == "stock":
            del raw["skus"][0][field]
        else:
            del raw[field]
        await replace_products(journal, [raw])
        with error("SOURCE_NOT_READY", 409):
            await store.add("b1", "s1", "missing-" + field, 0, source())
    await replace_products(journal, [card()])
    with error("INVALID_CANDIDATE_SOURCE", 422):
        await store.add("b1", "s1", "unknown-sku", 0, source(sku=99))
    raw = card()
    raw["skus"].append(deepcopy(raw["skus"][0]))
    await replace_products(journal, [raw])
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "duplicate-sku", 0, source())
    await replace_products(journal, [card(), card()])
    with error("SOURCE_CHANGED", 409):
        await store.add("b1", "s1", "duplicate-product", 0, source())


@pytest.mark.parametrize("revision", [True, False, -1, 1.0, "0", None, 2**53])
async def test_revision_has_strict_integer_contract(tmp_path, revision):
    _, store = await setup(tmp_path)
    with error("INVALID_CANDIDATE_REQUEST", 422):
        await store.add("b1", "s1", "invalid", revision, source())


async def test_store_rejects_client_snapshot_and_invalid_reference_payloads(tmp_path):
    _, store = await setup(tmp_path)
    for raw in ({**source(), "snapshot": card()}, {**source(), "kind": "favorite"}, {**source(), "sku_id": ""}):
        with pytest.raises(CandidateWorkspaceError) as raised:
            await store.add("b1", "s1", "invalid", 0, raw)
        assert raised.value.status == 422
    for request_id in ("", "x" * 129, "Bearer syntheticCredential123", "key\nline"):
        with error("INVALID_CANDIDATE_REQUEST", 422):
            await store.add("b1", "s1", request_id, 0, source())


async def test_source_read_and_candidate_commit_share_the_same_write_transaction(tmp_path, monkeypatch):
    journal, store = await setup(tmp_path)
    replacement_journal = AGUIJournal(journal.path)
    await replacement_journal.initialize()
    read_source, release, replacement_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_source = store._source
    async def hold_after_read(*args):
        result = await actual_source(*args)
        read_source.set()
        await release.wait()
        return result
    monkeypatch.setattr(store, "_source", hold_after_read)
    addition = asyncio.create_task(store.add("b1", "s1", "atomic", 0, source()))
    await asyncio.wait_for(read_source.wait(), 2)
    async def replace():
        replacement_started.set()
        await replace_products(replacement_journal, [])
    replacement = asyncio.create_task(replace())
    await replacement_started.wait()
    # 第二连接的 BEGIN IMMEDIATE 必须等待候选事务，不能在来源读取和保存间穿透。
    await asyncio.sleep(0.05)
    assert not replacement.done()
    release.set()
    saved, _ = await asyncio.gather(addition, replacement)
    assert saved["items"][0]["snapshot"] == card()
    with error("SOURCE_CHANGED", 409):
        await store.add("b1", "s1", "after-change", 1, source())


async def test_replay_keeps_authorization_and_stored_workspace_owner_immutable(tmp_path):
    journal, store = await setup(tmp_path)
    await store.add("b1", "s1", "owner", 0, source())
    async with journal._db(write=True) as db:
        await db.execute("UPDATE candidate_workspaces SET buyer_id='different-owner' WHERE session_id='s1'")
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.add("b1", "s1", "owner", 0, source())
    with error("CANDIDATE_FORBIDDEN", 403):
        await store.list("b1", "s1")
    assert (await table_rows(journal, "candidate_workspaces"))[0]["buyer_id"] == "different-owner"


async def test_canonical_payload_reordering_replays_but_comparison_order_does_not(tmp_path):
    _, store = await setup(tmp_path)
    original = await store.add("b1", "s1", "add", 0, source())
    reversed_source = dict(reversed(list(source().items())))
    assert await store.add("b1", "s1", "add", 0, reversed_source) == original
    second = await store.add("b1", "s1", "add-2", 1, source(sku=2))
    ids = [item["candidate_id"] for item in second["items"]]
    await store.compare("b1", "s1", "compare", 2, ids)
    with error("CANDIDATE_REQUEST_CONFLICT", 409):
        await store.compare("b1", "s1", "compare", 2, ids[::-1])


@pytest.mark.parametrize("landed", [
    {"subtotal_major": float("nan")}, {"landed_total_major": float("inf")}, {"tariff_major": -1},
    {"landed_total_major": 1}, {"subtotal_major": 1}, {"currency": "USD"}, {"de_minimis_applied": "yes"}])
async def test_invalid_default_sku_quote_is_not_preserved(tmp_path, landed):
    journal, store = await setup(tmp_path)
    raw = card()
    raw["landed_price"].update(landed)
    await replace_products(journal, [raw])
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "invalid-quote", 0, source())


@pytest.mark.parametrize("default", [[], {}, 1, None])
async def test_malformed_default_sku_type_is_a_source_error(tmp_path, default):
    journal, store = await setup(tmp_path)
    raw = card()
    raw["default_sku_id"] = default
    await replace_products(journal, [raw])
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "invalid-default", 0, source())


@pytest.mark.parametrize("projection", [[], None, "not-json", {"state": []}, {"state": {"products": {}}}])
async def test_malformed_persisted_projection_reports_source_not_ready(tmp_path, projection):
    journal, store = await setup(tmp_path)
    async with journal._db(write=True) as db:
        await db.execute("UPDATE agui_runs SET projection_json=? WHERE run_id='r1'", (json.dumps(projection),))
    with error("SOURCE_NOT_READY", 409):
        await store.add("b1", "s1", "invalid-projection", 0, source())


async def test_identity_generation_is_preserved_without_public_hashes(tmp_path):
    journal, store = await setup(tmp_path)
    mapping = bindings([product()])
    mapping["P1-S1"]["generation"] = 7
    async with journal._db(write=True) as db:
        await db.execute("UPDATE agui_catalog_snapshots SET bindings_json=?", (json.dumps(mapping),))
    saved = await store.add("b1", "s1", "generation", 0, source())
    private = await store.get_candidate("b1", "s1", saved["items"][0]["candidate_id"])
    assert private["catalog_binding"]["generation"] == 7
    assert "generation" not in json.dumps(saved)
    assert "identity_hash" not in json.dumps(await store.add("b1", "s1", "generation", 0, source()))
