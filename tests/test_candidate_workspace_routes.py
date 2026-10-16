"""候选 HTTP、来源与核验：签名双身份、真实 SQLite，外部调用为零。"""
from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.application.usecases.candidate_workspace import CandidateWorkspaceService
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.confirmation_service import ConfirmationService
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.sku import Sku
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.candidate_workspace import CandidateWorkspaceStore
from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.sql.repositories import create_engine
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.sql.trade_tables import SkuInventoryRow, TradeCatalogSkuRow
from app.presentation.candidate_workspace import register_candidate_workspace_routes
from app.presentation.confirmations import register_confirmation_routes
from tests.test_ag_ui_journal import body
from tests.trade_test_helpers import test_address as address

BASE = "/commerce/ag-ui/sessions/s1/candidates"
SOURCE = {"kind": "run", "run_id": "r1", "product_id": "P1001", "sku_id": "P1001-S1"}


@pytest.fixture
async def candidate_env(tmp_path):
    product = Product("P1001", "合成测试背包", "Test", "bag", "CN", "合成描述", ships_to=["CN", "US"],
        skus=[Sku("P1001-S1", "蓝色", Money.of(18900, "CNY"), 5),
              Sku("P1001-S2", "红色", Money.of(19900, "CNY"), 8)],
        source_platform="fixture", source_locale="cn", external_product_id="FIXTURE1", canonical_product_id="FIXTURE1")
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path/'trade.db'}")
    trade = SqlTradeStore(engine)
    await trade.initialize_inventory([product])
    repo = InMemoryProductRepository([product])
    repo.bind_inventory(trade.get_inventory)
    sessions = SqlFencedSessionStore(engine)
    await sessions.assert_owner("s1", "b1", create=True)
    await sessions.assert_owner("s2", "b2", create=True)
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.bind_catalog(await trade.get_catalog_bindings())
    search = CatalogSearchUseCase(repo, embedder=SimpleNamespace(embed=AsyncMock(side_effect=AssertionError("no model"))),
        reranker=SimpleNamespace(rerank=AsyncMock(side_effect=AssertionError("no reranker"))), hybrid_enabled=True)
    await journal.reserve(body(), "b1", "owner")
    card = (await search.execute(ProductSearchSpec(product_id="P1001", ship_to="CN")))["hits"][0]
    await journal.append("r1", "owner", [{"type": "STATE_SNAPSHOT", "snapshot": {"products": [card], "searchCompleted": True}},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "s1"}])
    store = CandidateWorkspaceStore(journal)
    service = CandidateWorkspaceService(store, repo, trade, catalog_search=search)
    confirmation = ConfirmationService(repo, trade)
    policy = IdentityPolicy(mode="hmac", secret="fixture-candidate-signing-key-123456789012")
    api = FastAPI()
    api.state.identity_policy, api.state.session_store = policy, sessions
    register_candidate_workspace_routes(api, lambda: service)
    register_confirmation_routes(api, lambda: confirmation)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://local") as client:
        yield SimpleNamespace(client=client, journal=journal, store=store, service=service, repo=repo, product=product,
                              trade=trade, sessions=sessions, engine=engine, search=search, policy=policy, card=card)
    await engine.dispose()


def auth(env, buyer="b1"):
    return {"Authorization": "Bearer " + env.policy.issue(buyer)}


async def request(env, method="GET", path=BASE, body=None, buyer="b1", token_buyer="b1", **params):
    return await env.client.request(method, path, params={"buyer_id": buyer, **params},
                                    headers=auth(env, token_buyer), json=body)


async def add(env, key="add1", revision=0, source=None):
    response = await request(env, "POST", body={"request_id": key, "expected_revision": revision, "source": source or SOURCE})
    assert response.status_code == 200, response.text
    return response.json()


async def test_http_full_flow_compare_recheck_prepare_approve_and_restart(candidate_env):
    env = candidate_env
    empty = await request(env)
    assert empty.status_code == 200 and empty.json()["revision"] == 0
    assert env.card["source_locale"] == "cn" and "source_language" not in env.card
    first = await add(env)
    second = await add(env, "add2", 1, {**SOURCE, "sku_id": "P1001-S2"})
    ids = [item["candidate_id"] for item in second["items"]]
    compared = await request(env, "PUT", BASE + "/comparison", {"request_id": "compare", "expected_revision": 2, "candidate_ids": ids})
    assert compared.status_code == 200 and compared.json()["comparison_ids"] == ids
    current = await request(env, path=BASE + f"/{ids[0]}/verification", ship_to="CN")
    assert current.status_code == 200 and current.json()["status"] == "available"
    assert current.json()["product"]["default_sku_id"] == second["items"][0]["sku_id"]
    assert "catalog_binding" not in str(compared.json()) and "identity_hash" not in str(compared.json())
    prepared = await env.client.post("/commerce/confirmations/orders", headers=auth(env), json={
        "buyer_id": "b1", "session_id": "s1", "request_id": "purchase1",
        "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}], "shipping_address": asdict(address())})
    assert prepared.status_code == 200
    confirmation = prepared.json()["confirmation"]
    assert confirmation["payload"]["order_kind"] == "purchase_intent"
    assert confirmation["payload"]["amount_scope"] == "merchandise_only"
    decision = {"buyer_id": "b1", "session_id": "s1", "snapshot_hash": confirmation["snapshot_hash"], "approved": True}
    approved = await env.client.post(f"/commerce/confirmations/{confirmation['confirmation_id']}/resolve", headers=auth(env), json=decision)
    repeated = await env.client.post(f"/commerce/confirmations/{confirmation['confirmation_id']}/resolve", headers=auth(env), json=decision)
    assert approved.status_code == repeated.status_code == 200 and approved.json() == repeated.json()
    assert (await env.trade.get_inventory())["P1001-S1"] == 4
    restored = CandidateWorkspaceStore(AGUIJournal(env.journal.path))
    assert await restored.list("b1", "s1") == compared.json()
    assert (await request(env)).json() == compared.json()
    env.search._embedder.embed.assert_not_awaited()
    env.search._reranker.rerank.assert_not_awaited()


@pytest.mark.parametrize("method,suffix,body_value", [
    ("GET", "", None), ("POST", "", {"request_id": "add", "expected_revision": 0, "source": SOURCE}),
    ("PUT", "/comparison", {"request_id": "cmp", "expected_revision": 0, "candidate_ids": []}),
    ("DELETE", "/missing", {"request_id": "delete", "expected_revision": 0}), ("GET", "/missing/verification", None),
])
async def test_signed_identity_and_owner_matrix(candidate_env, method, suffix, body_value):
    env = candidate_env
    unsigned = await env.client.request(method, BASE + suffix, params={"buyer_id": "b1"}, json=body_value)
    assert unsigned.status_code == 401
    assert (await request(env, method, BASE + suffix, body_value, buyer="b2")).status_code == 403
    assert (await request(env, method, BASE + suffix, body_value, buyer="b2", token_buyer="b2")).status_code == 403
    absent = await request(env, method, BASE.replace("s1", "absent") + suffix, body_value)
    assert absent.status_code == 404


async def test_two_tabs_stale_revision_and_response_loss_replay_are_deterministic(candidate_env):
    env = candidate_env
    original = await add(env)
    identifier = original["items"][0]["candidate_id"]
    conflict = await request(env, "PUT", BASE + "/comparison", {"request_id": "tab-b", "expected_revision": 0, "candidate_ids": [identifier]})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "CANDIDATE_REVISION_CONFLICT"
    removed = await request(env, "DELETE", BASE + f"/{identifier}", {"request_id": "remove", "expected_revision": 1})
    assert removed.status_code == 200 and removed.json()["revision"] == 2
    assert await add(env) == original  # 旧响应不代表现集合；客户端必须拒绝回退。
    assert (await request(env)).json()["items"] == []
    different = await request(env, "POST", body={"request_id": "add1", "expected_revision": 0, "source": {**SOURCE, "sku_id": "P1001-S2"}})
    assert different.status_code == 409 and different.json()["detail"]["code"] == "CANDIDATE_REQUEST_CONFLICT"


async def test_cross_session_source_and_candidate_cannot_be_borrowed(candidate_env):
    env = candidate_env
    original = await add(env)
    await env.sessions.assert_owner("s3", "b1", create=True)
    await env.journal.reserve(body("r3", "s3"), "b1", "owner3")
    other = BASE.replace("s1", "s3")
    forged = await request(env, "POST", other, {"request_id": "x", "expected_revision": 0, "source": SOURCE})
    assert forged.status_code == 403
    candidate_id = original["items"][0]["candidate_id"]
    assert (await request(env, path=other + f"/{candidate_id}/verification")).status_code in {403, 404}
    assert (await request(env)).json() == original


@pytest.mark.parametrize("change", [{"expected_revision": True}, {"expected_revision": -1}, {"product": {"price_major": 0}}, {"buyer_id": "b2"}])
async def test_write_schema_rejects_authority_and_snapshot_injection(candidate_env, change):
    payload = {"request_id": "bad", "expected_revision": 0, "source": SOURCE, **change}
    assert (await request(candidate_env, "POST", body=payload)).status_code == 422
    assert (await request(candidate_env)).json()["items"] == []


async def test_legacy_run_and_resumed_changed_card_remain_unknown(candidate_env):
    env = candidate_env
    legacy = AGUIJournal(env.journal.path)
    await legacy.reserve(body("legacy"), "b1", "old")
    await legacy.append("legacy", "old", [{"type": "STATE_SNAPSHOT", "snapshot": {"products": [env.card]}},
        {"type": "RUN_FINISHED", "runId": "legacy", "threadId": "s1"}])
    result = await add(env, source={**SOURCE, "run_id": "legacy"})
    item = result["items"][0]
    assert item["identity_status"] == "unknown"
    checked = await request(env, path=BASE + f"/{item['candidate_id']}/verification")
    assert checked.json()["status"] == "unknown_identity" and "product" not in checked.json()
    await legacy.bind_catalog(await env.trade.get_catalog_bindings())
    await legacy.reserve(body("legacy"), "b1", "replay")
    assert (await request(env, path=BASE + f"/{item['candidate_id']}/verification")).json()["status"] == "unknown_identity"
    assert (await request(env)).json() == result


async def test_fresh_binding_cannot_bless_old_resume_product_facts(candidate_env):
    env = candidate_env
    await env.journal.reserve(body("resume"), "b1", "owner-resume")
    await env.journal.append("resume", "owner-resume", [{"type": "STATE_SNAPSHOT", "snapshot": {
        "products": [{**env.card, "title": "另一件旧商品"}]}}, {"type": "RUN_FINISHED", "runId": "resume", "threadId": "s1"}])
    result = await add(env, source={**SOURCE, "run_id": "resume"})
    assert result["items"][0]["identity_status"] == "unknown"


async def test_price_update_rechecks_current_catalog_without_rewriting_history(candidate_env):
    env = candidate_env
    saved = await add(env)
    identifier = saved["items"][0]["candidate_id"]
    env.product.skus[0].price = Money.of(22500, "CNY")
    await env.trade.initialize_inventory([env.product])
    checked = await request(env, path=BASE + f"/{identifier}/verification", ship_to="CN")
    assert checked.json()["status"] == "available" and checked.json()["product"]["source_price_major"] == 225
    assert (await request(env)).json() == saved
    assert saved["items"][0]["snapshot"]["price_major"] == 189


async def test_out_of_stock_unknown_and_changed_terms_preserve_candidate(candidate_env):
    env = candidate_env
    saved = await add(env)
    path = BASE + f"/{saved['items'][0]['candidate_id']}/verification"
    async with env.engine.begin() as connection:
        await connection.execute(update(SkuInventoryRow).where(SkuInventoryRow.sku_id == "P1001-S1").values(stock=0))
    assert (await request(env, path=path)).json()["status"] == "out_of_stock"
    async with env.engine.begin() as connection:
        await connection.execute(update(SkuInventoryRow).where(SkuInventoryRow.sku_id == "P1001-S1").values(stock=2))
    assert (await request(env, path=path)).json()["status"] == "available"
    env.product.ships_to = ["CN"]
    await env.trade.initialize_inventory([env.product])
    assert (await request(env, path=path)).json()["status"] == "terms_changed"
    async with env.engine.begin() as connection:
        await connection.execute(update(TradeCatalogSkuRow).where(TradeCatalogSkuRow.sku_id == "P1001-S1").values(identity_version=99))
    assert (await request(env, path=path)).json()["status"] == "unknown_identity"
    assert (await request(env)).json() == saved


async def test_current_process_catalog_mismatch_is_not_given_registry_identity(candidate_env):
    env = candidate_env
    saved = await add(env)
    env.product.external_product_id = "DIFFERENT"
    checked = await request(env, path=BASE + f"/{saved['items'][0]['candidate_id']}/verification")
    assert checked.json()["status"] == "identity_mismatch" and "product" not in checked.json()


@pytest.mark.parametrize("params", [{"target_currency": "QQQ"}, {"target_currency": "cny"}, {"ship_to": "China"}])
async def test_bad_verification_parameters_and_store_failure_do_not_clear_state(candidate_env, params):
    env = candidate_env
    saved = await add(env)
    assert (await request(env, path=BASE + f"/{saved['items'][0]['candidate_id']}/verification", **params)).status_code == 422
    assert (await request(env)).json() == saved


async def test_storage_failure_does_not_claim_empty_success_or_leak_details(candidate_env, monkeypatch):
    env = candidate_env
    monkeypatch.setattr(env.store, "list", AsyncMock(side_effect=sqlite3.OperationalError("fixture-private-path")))
    response = await request(env)
    assert response.status_code == 503
    assert "fixture-private-path" not in response.text and "items" not in response.json()


async def test_later_search_and_no_result_do_not_modify_candidates_or_comparison(candidate_env):
    env = candidate_env
    saved = await add(env)
    identifier = saved["items"][0]["candidate_id"]
    compared = (await request(env, "PUT", BASE + "/comparison", {
        "request_id": "compare", "expected_revision": 1, "candidate_ids": [identifier]})).json()
    for run_id, products in (("next-search", [env.card]), ("no-results", [])):
        await env.journal.reserve(body(run_id), "b1", run_id)
        await env.journal.append(run_id, run_id, [{"type": "STATE_SNAPSHOT", "snapshot": {
            "products": products, "searchCompleted": True}}, {"type": "RUN_FINISHED", "runId": run_id, "threadId": "s1"}])
        assert (await request(env)).json() == compared
    assert (await request(env, path=BASE + f"/{identifier}/verification")).json()["status"] == "available"


async def test_verification_rejects_catalog_change_during_exact_lookup(candidate_env):
    env = candidate_env
    saved = await add(env)
    original = env.search.execute
    async def changed_during_lookup(spec):
        result = await original(spec)
        async with env.engine.begin() as connection:
            await connection.execute(update(TradeCatalogSkuRow).where(TradeCatalogSkuRow.sku_id == "P1001-S1")
                                     .values(generation=TradeCatalogSkuRow.generation + 1))
        return result
    env.search.execute = changed_during_lookup
    checked = await request(env, path=BASE + f"/{saved['items'][0]['candidate_id']}/verification")
    assert checked.json()["status"] == "terms_changed" and "product" not in checked.json()
    assert (await request(env)).json() == saved


async def test_retired_sku_and_unsupported_destination_are_not_deleted(candidate_env):
    env = candidate_env
    saved = await add(env)
    path = BASE + f"/{saved['items'][0]['candidate_id']}/verification"
    assert (await request(env, path=path, ship_to="GB")).json()["status"] == "unavailable"
    async with env.engine.begin() as connection:
        await connection.execute(update(TradeCatalogSkuRow).where(TradeCatalogSkuRow.sku_id == "P1001-S1").values(active=False))
    assert (await request(env, path=path)).json()["status"] == "unavailable"
    assert (await request(env)).json() == saved
