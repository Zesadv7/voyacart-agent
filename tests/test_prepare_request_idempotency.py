"""M2a 请求级幂等：真实 SQLite、独立连接/进程，全部使用合成数据。"""
from __future__ import annotations

import asyncio
import copy
import multiprocessing
import sqlite3
import uuid
from dataclasses import asdict, replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, update

from app.application.usecases.order_usecases import CancelOrderUseCase, OrderItemInput, PlaceOrderUseCase
from app.domain.catalog.money import Money
from app.domain.order.ports.trade_store import (
    PREPARE_REQUEST_VERSION, TradeStoreError, normalize_prepare_request, prepare_request_hash,
)
from app.infrastructure.persistence.sql.repositories import create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.sql.trade_tables import (
    SkuInventoryRow, TradeConfirmationRow, TradePrepareRequestRow,
)
from tests.test_trade_store import NOW, payload, prepare as legacy_prepare, product, resolve, stores  # noqa: F401
from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


async def keyed_prepare(store, *, body=None, action="create", request_id="request-1",
                        buyer_id="buyer-1", session_id="session-1", expiry=None):
    return await store.prepare_confirmation(
        operation_id=f"operation-{uuid.uuid4().hex}", buyer_id=buyer_id, session_id=session_id,
        action=action, payload=body if body is not None else payload(), request_id=request_id,
        expires_at=expiry or NOW + timedelta(minutes=5),
    )


async def mapping_count(engine):
    async with engine.connect() as db:
        return await db.scalar(select(func.count()).select_from(TradePrepareRequestRow))


async def service_prepare(env, *, quantity=1, request_id="request-1", buyer_id="buyer-1",
                          session_id="session-1", shipping_address=None):
    return await env.service.prepare_order(buyer_id, session_id,
        [OrderItemInput("P1001", "P1001-S1", quantity)], shipping_address or address(), request_id=request_id)


async def service_resolve(env, prepared, approved=True):
    confirmation = prepared["confirmation"]
    return await env.service.resolve(confirmation["confirmation_id"], "buyer-1", "session-1",
        confirmation["snapshot_hash"], approved)


def test_canonical_request_is_versioned_sorted_aggregated_trimmed_and_ignores_quote():
    first = payload(quantity=2)
    first["items"].extend(payload(quantity=3, product_id="p-2", sku_id="sku-2")["items"])
    second = copy.deepcopy(first)
    second["items"][0]["quantity"] = 1
    second["items"].append(dict(second["items"][0]))
    second["items"].reverse()
    for item in second["items"]:
        item.update(title="新标题", unit_price_minor=9999, currency="USD", stock=0)
        item["product_id"] = " " + item["product_id"] + " "
    second["shipping_address"] = {key: " " + value + " " for key, value in first["shipping_address"].items()}
    second["expires_at"] = "invalid-ignored"
    assert prepare_request_hash("create", first) == prepare_request_hash("create", second)
    normalized = normalize_prepare_request("create", second)
    assert normalized["items"] == [
        {"product_id": "p-1", "sku_id": "sku-1", "quantity": 2},
        {"product_id": "p-2", "sku_id": "sku-2", "quantity": 3},
    ]
    assert prepare_request_hash("cancel", {"order_id": " order ", "reason": " reason "}) == prepare_request_hash(
        "cancel", {"order_id": "order", "reason": "reason"})
    assert PREPARE_REQUEST_VERSION == 1


@pytest.mark.parametrize("change", [
    {"product_id": "another-product"}, {"quantity": True}, {"quantity": 2**63},
])
def test_canonical_request_rejects_cross_product_sku_or_bad_quantity(change):
    body = payload(quantity=1)
    body["items"].append({**body["items"][0], **change})
    with pytest.raises(TradeStoreError) as caught:
        prepare_request_hash("create", body)
    assert caught.value.code == "INVALID_ARGUMENT"


async def test_independent_connections_atomically_create_one_confirmation(stores):
    first, second, engine, _ = stores
    results = await asyncio.gather(*(keyed_prepare(store) for store in (first, second, first, second)))
    assert all(result == results[0] for result in results)
    assert await mapping_count(engine) == 1
    assert len(await first.list_confirmations(buyer_id="buyer-1", session_id="session-1")) == 1
    assert await first.get_inventory() == {"sku-1": 5}


@pytest.mark.parametrize("change", [
    lambda body: body["items"][0].update(quantity=3),
    lambda body: body["items"][0].update(product_id="p-other"),
    lambda body: body["shipping_address"].update(address_line="另一地址"),
    lambda body: body["shipping_address"].update(phone="13900000000"),
])
async def test_changed_request_conflicts_before_inventory_or_catalog_checks(stores, change):
    first, second, engine, _ = stores
    await keyed_prepare(first)
    async with engine.begin() as db:
        await db.execute(update(SkuInventoryRow).values(stock=0))
    body = payload()
    change(body)
    with pytest.raises(TradeStoreError) as caught:
        await keyed_prepare(second, body=body)
    assert caught.value.code == "PREPARE_REQUEST_CONFLICT"
    assert await mapping_count(engine) == 1


async def test_store_replay_precedes_stale_quote_validation_and_expiry(stores):
    first, second, engine, _ = stores
    prepared = await keyed_prepare(first)
    await second.initialize_inventory([product(price=1)])
    async with engine.begin() as db:
        await db.execute(update(SkuInventoryRow).values(stock=0))
    stale = payload(price=999999)
    stale["items"][0]["title"] = "现在不同的标题"
    assert await keyed_prepare(second, body=stale, expiry=NOW - timedelta(days=1)) == prepared
    # 已有请求不需要重建报价字段；只校验稳定的用户意图。
    for field in ("unit_price_minor", "currency", "title"):
        stale["items"][0].pop(field)
    assert await keyed_prepare(second, body=stale) == prepared
    with pytest.raises(TradeStoreError) as caught:
        await keyed_prepare(second, request_id="fresh", body=payload(price=999999))
    assert caught.value.code == "PRICE_CHANGED"


async def test_request_scope_is_buyer_session_and_action(stores):
    first, second, engine, _ = stores
    original = await keyed_prepare(first)
    other_buyer = await keyed_prepare(second, buyer_id="buyer-2")
    other_session = await keyed_prepare(second, session_id="session-2")
    placed = await resolve(first, original)
    cancellation = await keyed_prepare(second, action="cancel", body={
        "order_id": placed["result"]["order_id"], "reason": "测试取消"})
    assert len({row["confirmation_id"] for row in (original, other_buyer, other_session, cancellation)}) == 4
    assert await mapping_count(engine) == 4


@pytest.mark.parametrize("decision", [None, True, False])
async def test_restart_replays_expired_approved_and_rejected_without_extending_ttl(stores, decision):
    first, _, engine, _ = stores
    original = await keyed_prepare(first)
    if decision is not None:
        original = await resolve(first, original, approved=decision)
    await engine.dispose()
    restarted_engine = create_engine(str(engine.url))
    restarted = SqlTradeStore(restarted_engine, clock=lambda: NOW + timedelta(days=1))
    try:
        await restarted.initialize_inventory([product(price=99, stock=999)])
        replay = await keyed_prepare(restarted, body=payload(price=99), expiry=NOW + timedelta(days=2))
        assert replay["confirmation_id"] == original["confirmation_id"]
        assert replay["snapshot_hash"] == original["snapshot_hash"]
        assert replay["expires_at"] == original["expires_at"]
        assert replay["status"] == original["status"]
        assert replay["expired"] is (decision is None)
        assert await restarted.get_inventory() == {"sku-1": 3 if decision is True else 5}
        if decision is None:
            with pytest.raises(TradeStoreError) as caught:
                await resolve(restarted, replay)
            assert caught.value.code == "CONFIRMATION_EXPIRED"
    finally:
        await restarted_engine.dispose()


async def test_cancel_replays_after_order_cancelled_and_changed_reason_or_order_conflicts(stores):
    first, second, engine, _ = stores
    placed = await resolve(first, await keyed_prepare(first))
    body = {"order_id": placed["result"]["order_id"], "reason": "测试取消"}
    results = await asyncio.gather(*(keyed_prepare(store, action="cancel", body=body) for store in (first, second)))
    assert results[0] == results[1]
    committed = await resolve(first, results[0])
    assert await keyed_prepare(second, action="cancel", body=body) == committed
    for change in ({"reason": "另一个原因"}, {"order_id": "different-order"}):
        with pytest.raises(TradeStoreError) as caught:
            await keyed_prepare(second, action="cancel", body={**body, **change})
        assert caught.value.code == "PREPARE_REQUEST_CONFLICT"
    with pytest.raises(TradeStoreError) as caught:
        await keyed_prepare(second, request_id="fresh", action="cancel", body=body)
    assert caught.value.code == "ORDER_CHANGED"
    assert await first.get_inventory() == {"sku-1": 5}
    assert await mapping_count(engine) == 2


@pytest.mark.parametrize("fail_on", ["INSERT INTO trade_confirmations", "INSERT INTO trade_prepare_requests", "commit"])
async def test_confirmation_and_mapping_roll_back_together_without_reserving_request(stores, fail_on):
    first, second, engine, _ = stores
    def fail_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(fail_on):
            raise RuntimeError("合成准备写入故障")
    def fail_commit(_conn):
        raise RuntimeError("合成准备写入故障")
    name, callback = ("commit", fail_commit) if fail_on == "commit" else ("before_cursor_execute", fail_sql)
    event.listen(engine.sync_engine, name, callback)
    try:
        with pytest.raises(RuntimeError, match="合成准备写入故障"):
            await keyed_prepare(first)
    finally:
        event.remove(engine.sync_engine, name, callback)
    assert await mapping_count(engine) == 0
    assert await second.list_confirmations(buyer_id="buyer-1", session_id="session-1") == []
    assert await second.get_inventory() == {"sku-1": 5}
    assert (await keyed_prepare(second))["status"] == "pending"
    assert await mapping_count(engine) == 1


async def test_stock_failure_does_not_claim_request_and_later_same_key_can_succeed(stores):
    first, second, engine, _ = stores
    with pytest.raises(TradeStoreError) as caught:
        await keyed_prepare(first, body=payload(quantity=6))
    assert caught.value.code == "INSUFFICIENT_STOCK"
    assert await mapping_count(engine) == 0
    assert await second.list_confirmations(buyer_id="buyer-1", session_id="session-1") == []
    async with engine.begin() as db:
        await db.execute(update(SkuInventoryRow).values(stock=6))
    assert (await keyed_prepare(second, body=payload(quantity=6)))["status"] == "pending"
    assert await second.get_inventory() == {"sku-1": 6}


async def test_additive_migration_preserves_legacy_rows_hashes_and_resolution(stores):
    first, _, engine, _ = stores
    existing = await legacy_prepare(first)
    approved = await resolve(first, existing)
    pending = await legacy_prepare(first, operation_id="legacy-pending")
    async with engine.begin() as db:
        before = (await db.execute(select(TradeConfirmationRow.__table__))).mappings().all()
        # 旧版完整 schema 与当前唯一差异就是尚不存在请求映射表。
        await db.run_sync(TradePrepareRequestRow.__table__.drop)
    await engine.dispose()
    restarted_engine = create_engine(str(engine.url))
    restarted = SqlTradeStore(restarted_engine, clock=lambda: NOW)
    try:
        await restarted.initialize_inventory([product(stock=999)])
        async with restarted_engine.connect() as db:
            after = (await db.execute(select(TradeConfirmationRow.__table__))).mappings().all()
        assert after == before
        assert await mapping_count(restarted_engine) == 0
        assert await restarted.get_inventory() == {"sku-1": 3}
        assert await resolve(restarted, existing) == approved
        assert (await resolve(restarted, pending))["status"] == "approved"
        assert await restarted.get_inventory() == {"sku-1": 1}
        assert await keyed_prepare(restarted, body=payload(quantity=1))
        assert await mapping_count(restarted_engine) == 1
    finally:
        await restarted_engine.dispose()


async def test_unknown_mapping_version_stops_startup_and_replay(stores):
    first, second, engine, _ = stores
    await keyed_prepare(first)
    async with engine.begin() as db:
        await db.execute(update(TradePrepareRequestRow).values(request_version=2))
    for task in (second.initialize_inventory([product(price=5)]), keyed_prepare(second)):
        with pytest.raises(TradeStoreError) as caught:
            await task
        assert caught.value.code == "PREPARE_REQUEST_VERSION_UNSUPPORTED"
    async with engine.connect() as db:
        assert await db.scalar(select(SkuInventoryRow.unit_price_minor)) == 12900


async def test_request_lookup_is_digest_only_and_legacy_calls_remain_distinct(stores):
    first, _, engine, _ = stores
    one = await keyed_prepare(first, request_id=None)
    two = await keyed_prepare(first, request_id=None)
    assert one["confirmation_id"] != two["confirmation_id"]
    assert await mapping_count(engine) == 0
    keyed = await keyed_prepare(first)
    row = await first.find_prepare_request(buyer_id="buyer-1", session_id="session-1", action="create",
        request_id="request-1", request_hash=prepare_request_hash("create", payload()))
    assert row == keyed
    assert set(TradePrepareRequestRow.__table__.columns.keys()) == {
        "buyer_id", "session_id", "action", "request_id", "request_version", "request_hash", "confirmation_id", "created_at"}


def _process_prepare(database_url, gate, results):
    """spawn 子进程，不继承连接；各自建表、种子和准备事务。"""
    async def run():
        engine = create_engine(database_url)
        store = SqlTradeStore(engine, clock=lambda: NOW)
        try:
            await store.initialize_inventory([product()])
            return await keyed_prepare(store)
        finally:
            await engine.dispose()
    try:
        gate.wait(timeout=30)
        results.put(("ok", asyncio.run(run())))
    except Exception as exc:
        results.put(("error", type(exc).__name__ + ": " + str(exc)))


def test_multiprocess_first_startup_and_same_key_commit_only_once(tmp_path):
    path = tmp_path / "multiprocess.db"
    context = multiprocessing.get_context("spawn")
    gate, results = context.Event(), context.Queue()
    processes = [context.Process(target=_process_prepare, args=(f"sqlite+aiosqlite:///{path}", gate, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        gate.set()
        responses = [results.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        assert all(process.exitcode == 0 for process in processes)
        assert all(status == "ok" for status, _ in responses), responses
        assert responses[0][1] == responses[1][1]
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM trade_prepare_requests").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM trade_confirmations").fetchone()[0] == 1
            assert db.execute("SELECT stock FROM trade_sku_inventory").fetchone()[0] == 5
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()
        results.join_thread()


async def test_service_replays_before_product_stock_or_order_reads(confirmation_env, monkeypatch):
    env = confirmation_env
    prepared = await service_prepare(env, quantity=50)
    approved = await service_resolve(env, prepared)
    assert (await env.store.get_inventory())["P1001-S1"] == 0
    cancellation = await env.service.prepare_cancel("buyer-1", "session-1", approved["order"]["order_id"],
        "不需要了", request_id="cancel-request")
    cancelled = await service_resolve(env, cancellation)
    for owner, attribute in ((env.products, "find_by_id"), (env.store, "get_inventory"), (env.store, "get_order")):
        monkeypatch.setattr(owner, attribute, AsyncMock(side_effect=AssertionError("重放不能查询实时状态")))
    assert await service_prepare(env, quantity=50) == approved
    assert await env.service.prepare_cancel("buyer-1", "session-1", approved["order"]["order_id"],
        " 不需要了 ", request_id="cancel-request") == cancelled


async def test_service_new_key_refreshes_quote_and_legacy_no_key_creates_again(confirmation_env):
    env = confirmation_env
    original = await service_prepare(env)
    catalog = await env.products.find_by_id("P1001")
    catalog.find_sku("P1001-S1").price = Money.of(12345, "CNY")
    await env.store.initialize_inventory([catalog])
    assert await service_prepare(env) == original
    fresh = await service_prepare(env, request_id="fresh")
    assert fresh["confirmation"]["payload"]["items"][0]["unit_price_minor"] == 12345
    legacy1 = await service_prepare(env, request_id=None)
    legacy2 = await service_prepare(env, request_id=None)
    assert len({row["confirmation"]["confirmation_id"] for row in (original, fresh, legacy1, legacy2)}) == 4
    assert await mapping_count(env.engine) == 2


@pytest.mark.parametrize("action", ["create", "cancel"])
async def test_service_committed_prepare_response_loss_is_recoverable(confirmation_env, monkeypatch, action):
    env = confirmation_env
    placed = await service_resolve(env, await service_prepare(env, request_id="seed")) if action == "cancel" else None
    original = env.store.prepare_confirmation
    async def commit_then_lose_response(**kwargs):
        await original(**kwargs)
        raise ConnectionError("合成响应丢失")
    monkeypatch.setattr(env.store, "prepare_confirmation", commit_then_lose_response)
    async def prepare():
        if action == "create":
            return await service_prepare(env)
        return await env.service.prepare_cancel("buyer-1", "session-1", placed["order"]["order_id"],
            "测试取消", request_id="request-1")
    first = await prepare()
    assert await prepare() == first
    assert first["confirmation"]["status"] == "pending"
    assert await mapping_count(env.engine) == (2 if action == "cancel" else 1)


@pytest.mark.parametrize("action", ["create", "cancel"])
async def test_service_preflight_failure_rechecks_concurrent_committed_mapping(confirmation_env, monkeypatch, action):
    env = confirmation_env
    placed = await service_resolve(env, await service_prepare(env, request_id="seed")) if action == "cancel" else None
    original_prepare = env.store.prepare_confirmation
    result = []
    async def concurrent_commit_then_fail(*args, **kwargs):
        body = ({"items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1,
                "title": "旧报价", "unit_price_minor": 18900, "currency": "CNY"}],
                "shipping_address": asdict(address())} if action == "create" else
                {"order_id": placed["order"]["order_id"], "reason": "测试取消"})
        result.append(await original_prepare(operation_id="concurrent-operation", buyer_id="buyer-1",
            session_id="session-1", action=action, payload=body, request_id="request-1",
            expires_at=env.now[0] + timedelta(minutes=5)))
        raise ValueError("并发提交后的库存或订单预检失败")
    monkeypatch.setattr(env.store, "get_inventory" if action == "create" else "get_order", concurrent_commit_then_fail)
    if action == "create":
        replay = await service_prepare(env)
    else:
        replay = await env.service.prepare_cancel("buyer-1", "session-1", placed["order"]["order_id"],
            "测试取消", request_id="request-1")
    assert replay["confirmation"] == result[0]


async def test_usecases_forward_optional_request_identity(confirmation_env):
    env = confirmation_env
    create = PlaceOrderUseCase(env.service)
    args = ("buyer-1", [OrderItemInput("P1001", "P1001-S1", 1)], address())
    first = await create.execute(*args, session_id="session-1", request_id="from-usecase")
    assert await create.execute(*args, session_id="session-1", request_id="from-usecase") == first
    placed = await service_resolve(env, first)
    cancel = CancelOrderUseCase(env.service)
    cancel_args = (placed["order"]["order_id"], "测试取消")
    pending = await cancel.execute(*cancel_args, buyer_id="buyer-1", session_id="session-1", request_id="from-usecase")
    assert await cancel.execute(*cancel_args, buyer_id="buyer-1", session_id="session-1", request_id="from-usecase") == pending


async def test_service_changed_address_conflicts_even_after_catalog_disappears(confirmation_env, monkeypatch):
    env = confirmation_env
    await service_prepare(env)
    lookup = AsyncMock(side_effect=AssertionError("冲突应先于目录查询"))
    monkeypatch.setattr(env.products, "find_by_id", lookup)
    with pytest.raises(TradeStoreError) as caught:
        await service_prepare(env, shipping_address=replace(address(), city="北京"))
    assert caught.value.code == "PREPARE_REQUEST_CONFLICT"
    lookup.assert_not_called()


async def test_same_key_different_payload_race_has_one_winner(stores):
    first, second, engine, _ = stores
    results = await asyncio.gather(
        keyed_prepare(first, body=payload(quantity=1)), keyed_prepare(second, body=payload(quantity=2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, TradeStoreError)]
    assert len(errors) == 1 and errors[0].code == "PREPARE_REQUEST_CONFLICT"
    assert await mapping_count(engine) == 1
    assert len(await first.list_confirmations(buyer_id="buyer-1", session_id="session-1")) == 1
    assert await first.get_inventory() == {"sku-1": 5}


async def test_service_equivalent_aggregated_trimmed_request_replays(confirmation_env, monkeypatch):
    env = confirmation_env
    original = await service_prepare(env, quantity=2)
    monkeypatch.setattr(env.products, "find_by_id", AsyncMock(side_effect=AssertionError("规范等价应直接重放")))
    trimmed_address = type(address())(**{key: " " + value + " " for key, value in asdict(address()).items()})
    replay = await env.service.prepare_order("buyer-1", "session-1", [
        OrderItemInput("P1001", " P1001-S1 ", 1), OrderItemInput(" P1001 ", "P1001-S1", 1),
    ], trimmed_address, request_id="request-1")
    assert replay == original


async def test_new_request_cannot_rebind_legacy_operation(stores):
    first, _, engine, _ = stores
    original = await legacy_prepare(first)
    with pytest.raises(TradeStoreError) as caught:
        await first.prepare_confirmation(operation_id="operation-1", buyer_id="buyer-1", session_id="session-1",
            action="create", payload=payload(), request_id="new-key", expires_at=NOW + timedelta(minutes=5))
    assert caught.value.code == "OPERATION_CONFLICT"
    assert await mapping_count(engine) == 0
    assert await legacy_prepare(first) == original


@pytest.mark.parametrize("action", ["create", "cancel"])
async def test_service_concurrent_replay_publishes_only_one_prepare_event(confirmation_env, monkeypatch, action):
    env = confirmation_env
    placed = await service_resolve(env, await service_prepare(env, request_id="seed")) if action == "cancel" else None
    queue = env.bus.subscribe("session-1")
    barrier = asyncio.Barrier(2)
    attribute = "get_inventory" if action == "create" else "get_order"
    original = getattr(env.store, attribute)
    async def synchronized_preflight(*args, **kwargs):
        response = await original(*args, **kwargs)
        await asyncio.wait_for(barrier.wait(), timeout=5)
        return response
    monkeypatch.setattr(env.store, attribute, synchronized_preflight)
    async def prepare():
        if action == "create":
            return await service_prepare(env)
        return await env.service.prepare_cancel("buyer-1", "session-1", placed["order"]["order_id"],
            "测试取消", request_id="request-1")
    first, second = await asyncio.gather(prepare(), prepare())
    assert first == second
    assert queue.qsize() == 1
    assert queue.get_nowait().type == "confirmation.required"
