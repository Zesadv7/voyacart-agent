"""目录换绑、旧库失败关闭与目录批准绑定；仅合成 SQLite。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import delete, select, update

from app.domain.order.ports.trade_store import TradeStoreError
from app.infrastructure.persistence.catalog_identity import catalog_bindings, compare_catalogs, digest
from app.infrastructure.persistence.seed_products import build_seed_products
from app.infrastructure.persistence.sql.repositories import create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.sql.trade_tables import (
    SkuInventoryRow, TradeCatalogSkuRow, TradeConfirmationCatalogRow, TradeConfirmationRow,
)
from scripts.catalog_preflight import inspect_database, preflight
from tests.test_trade_store import NOW, payload, prepare, product, resolve, stores  # noqa: F401


def external(**kwargs):
    return replace(product(**kwargs), source_platform="amazon", source_locale="us",
                   external_product_id="B000000001", canonical_product_id="B000000001", ships_to=["CN"])


async def rows(engine, table):
    async with engine.connect() as connection:
        return [dict(row) for row in (await connection.execute(select(table))).mappings()]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"external_product_id": "B000000002"}, {"source_locale": "jp"}, {"source_platform": "ebay"},
    {"canonical_product_id": "different"},
])
async def test_alias_change_rejected_before_quotes_inventory_or_registration(tmp_path, changed):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path/'trade.db'}")
    store = SqlTradeStore(engine, clock=lambda: NOW)
    old = external()
    try:
        await store.initialize_inventory([old])
        await resolve(store, await prepare(store))
        before = [await rows(engine, table) for table in (SkuInventoryRow, TradeCatalogSkuRow, TradeConfirmationRow)]
        with pytest.raises(TradeStoreError, match="不同") as caught:
            await store.initialize_inventory([replace(old, **changed)])
        assert caught.value.code == "CATALOG_IDENTITY_CONFLICT"
        assert before == [await rows(engine, table) for table in (SkuInventoryRow, TradeCatalogSkuRow, TradeConfirmationRow)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_product_id_cannot_rebind_even_using_new_sku(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path/'trade.db'}")
    store = SqlTradeStore(engine)
    try:
        await store.initialize_inventory([external()])
        new = replace(external(sku_id="new-sku"), external_product_id="B000000002")
        with pytest.raises(TradeStoreError) as caught:
            await store.initialize_inventory([new])
        assert caught.value.code == "CATALOG_IDENTITY_CONFLICT"
        assert await store.get_inventory() == {"sku-1": 5}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_inventory_without_registry_fails_closed_and_preserves_rows(stores):
    store, _, engine, _ = stores
    card = await prepare(store)
    await resolve(store, card)
    async with engine.begin() as connection:
        await connection.execute(delete(TradeCatalogSkuRow))
    before = [await rows(engine, table) for table in (SkuInventoryRow, TradeConfirmationRow)]
    with pytest.raises(TradeStoreError) as caught:
        await store.initialize_inventory([product(stock=900, price=1)])
    assert caught.value.code == "CATALOG_MIGRATION_REQUIRED"
    assert before == [await rows(engine, table) for table in (SkuInventoryRow, TradeConfirmationRow)]
    assert await rows(engine, TradeCatalogSkuRow) == []
    assert (await store.list_orders(buyer_id="buyer-1"))["total"] == 1


@pytest.mark.asyncio
async def test_removed_item_blocks_new_or_pending_but_cancel_and_replay_survive(stores):
    store, other, _, _ = stores
    pending = await prepare(store)
    placed = await resolve(store, await prepare(store, operation_id="placed"))
    await store.initialize_inventory([product(product_id="p-2", sku_id="sku-2")])
    for operation in (prepare(store, operation_id="new"), resolve(store, pending)):
        with pytest.raises(TradeStoreError) as caught:
            await operation
        assert caught.value.code == "CATALOG_ITEM_RETIRED"
    cancel = await prepare(store, operation_id="cancel", action="cancel",
        body={"order_id": placed["result"]["order_id"], "reason": "测试"})
    result = await resolve(store, cancel)
    assert await resolve(other, cancel) == result
    assert await store.get_inventory() == {"sku-1": 5, "sku-2": 5}
    # 重新上架并不复活旧批准事实。
    await store.initialize_inventory([product(), product(product_id="p-2", sku_id="sku-2")])
    with pytest.raises(TradeStoreError) as caught:
        await resolve(store, pending)
    assert caught.value.code == "CATALOG_CONFIRMATION_STALE"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"ships_to": ["US"]}, {"title": "不同规格描述"}])
async def test_material_terms_changed_require_new_approval(stores, change):
    store = stores[0]
    old = await prepare(store)
    await store.initialize_inventory([replace(product(), **change)])
    with pytest.raises(TradeStoreError) as caught:
        await resolve(store, old)
    assert caught.value.code == "CATALOG_CONFIRMATION_STALE"
    assert await store.get_inventory() == {"sku-1": 5}


@pytest.mark.asyncio
async def test_old_pending_without_catalog_sidecar_cannot_be_reapproved(stores):
    store, _, engine, _ = stores
    pending = await prepare(store)
    async with engine.begin() as connection:
        await connection.execute(delete(TradeConfirmationCatalogRow))
    with pytest.raises(TradeStoreError) as caught:
        await resolve(store, pending)
    assert caught.value.code == "CATALOG_CONFIRMATION_STALE"
    assert (await store.get_confirmation(pending["confirmation_id"], buyer_id="buyer-1", session_id="session-1"))["snapshot_hash"] == pending["snapshot_hash"]


@pytest.mark.asyncio
async def test_unknown_identity_version_is_not_upgraded_or_replayed(stores):
    store, _, engine, _ = stores
    async with engine.begin() as connection:
        await connection.execute(update(TradeCatalogSkuRow).values(identity_version=2))
    for call in (store.initialize_inventory([product()]), prepare(store)):
        with pytest.raises(TradeStoreError) as caught:
            await call
        assert caught.value.code == "CATALOG_IDENTITY_VERSION_UNSUPPORTED"


def test_real_demo_to_v2_collision_count_and_same_catalog_identity():
    demo = build_seed_products()
    v2 = build_seed_products(Path("data/catalog-esci-v2.jsonl.gz"))
    report = compare_catalogs(demo, v2)
    assert len(report["identity_conflicts"]) == 2000
    assert report["safe_identity_update"] is False
    assert compare_catalogs(demo, demo)["safe_identity_update"] is True
    assert catalog_bindings([product()])["sku-1"]["identity"]["kind"] == "internal"


def test_preflight_database_is_read_only_and_does_not_assume_source_identity(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE trade_sku_inventory (sku_id TEXT, stock INTEGER)")
        connection.execute("INSERT INTO trade_sku_inventory VALUES ('sku-1', 3)")
    before = database.read_bytes()
    binding = catalog_bindings([product()])
    report = inspect_database(database, binding, binding)
    assert report["legacy_identity_unverified"] is True
    assert report["source_file_alone_proves_legacy_identity"] is False
    assert report["unregistered_historical_skus"] == ["sku-1"]
    assert before == database.read_bytes()
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(FileNotFoundError):
        inspect_database(missing, binding, binding)
    assert not missing.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("seed,expected_error", [(5, None), (1, "INVENTORY_MIGRATION_REQUIRED")])
async def test_registered_identity_missing_inventory_accounts_for_existing_orders(stores, seed, expected_error):
    store, other, engine, _ = stores
    approved = await resolve(store, await prepare(store))
    async with engine.begin() as connection:
        await connection.execute(delete(SkuInventoryRow))
    if expected_error:
        with pytest.raises(TradeStoreError) as caught:
            await store.initialize_inventory([product(stock=seed)])
        assert caught.value.code == expected_error
        assert await store.get_inventory() == {}
    else:
        await store.initialize_inventory([product(stock=seed)])
        assert await other.get_inventory() == {"sku-1": 3}
        cancel = await prepare(store, operation_id="cancel", action="cancel",
            body={"order_id": approved["result"]["order_id"], "reason": "测试"})
        await resolve(other, cancel)
        assert await store.get_inventory() == {"sku-1": 5}


@pytest.mark.asyncio
async def test_catalog_binding_api_exposes_no_buyer_fields_and_updates_generation(stores):
    store = stores[0]
    before = (await store.get_catalog_bindings(["sku-1"]))["sku-1"]
    assert set(before) == {"version", "identity_hash", "terms_hash", "generation", "product_id", "active"}
    await store.initialize_inventory([replace(product(), ships_to=["SG"])])
    after = (await store.get_catalog_bindings())["sku-1"]
    assert after["identity_hash"] == before["identity_hash"]
    assert after["generation"] == before["generation"] + 1
    assert after["terms_hash"] != before["terms_hash"]


def test_default_catalog_cannot_silently_change_even_on_empty_database(tmp_path, monkeypatch):
    from app.infrastructure.persistence import seed_products
    target = tmp_path / "catalog-demo.jsonl.gz"
    target.write_bytes(Path("data/catalog-esci-v2.jsonl.gz").read_bytes())
    monkeypatch.setattr(seed_products, "_CATALOG_FIXTURE", target)
    with pytest.raises(RuntimeError, match="冻结清单"):
        seed_products.build_seed_products()
    assert len(seed_products.build_seed_products(target)) == 12299


def test_runtime_manifest_is_packaged_and_pinned_to_default_bytes():
    import hashlib
    import json
    manifest = json.loads(Path("configs/catalogs/runtime-demo-v1.json").read_text())
    assert manifest["products"] == 5700 and manifest["skus"] == 9105
    assert manifest["blake2b"] == hashlib.blake2b(Path("data/catalog-demo.jsonl.gz").read_bytes()).hexdigest()
    assert manifest["identity_digest"] == digest(catalog_bindings(build_seed_products()))
    assert "COPY configs ./configs" in Path("Dockerfile").read_text()


@pytest.mark.asyncio
async def test_old_process_cannot_bind_current_identity_to_stale_destination(stores):
    store, stale_process, _, _ = stores
    await store.initialize_inventory([replace(product(), ships_to=["US"])])
    # 旧进程的内存目录仍允许 CN，持久层必须独立按当前事实拒绝，不能为旧事实补签。
    with pytest.raises(TradeStoreError) as caught:
        await prepare(stale_process, operation_id="stale-process", body=payload())
    assert caught.value.code == "CATALOG_DESTINATION_CHANGED"
    assert await store.get_inventory() == {"sku-1": 5}


@pytest.mark.asyncio
async def test_legacy_orders_without_lines_do_not_look_like_empty_database(stores):
    from app.infrastructure.persistence.sql.tables import OrderLineRow
    store, _, engine, _ = stores
    await resolve(store, await prepare(store))
    async with engine.begin() as connection:
        await connection.execute(delete(TradeConfirmationCatalogRow))
        await connection.execute(delete(TradeConfirmationRow))
        await connection.execute(delete(TradeCatalogSkuRow))
        await connection.execute(delete(SkuInventoryRow))
        await connection.execute(delete(OrderLineRow))
    with pytest.raises(TradeStoreError) as caught:
        await store.initialize_inventory([product()])
    assert caught.value.code == "CATALOG_MIGRATION_REQUIRED"
    assert await store.get_inventory() == {}


def test_preflight_catches_same_product_identity_change_when_all_skus_are_new():
    old = external()
    new = replace(external(sku_id="new-sku"), external_product_id="B000000002")
    report = compare_catalogs([old], [new])
    assert report["identity_conflicts"] == []
    assert report["product_identity_conflicts"] == ["p-1"]
    assert report["safe_identity_update"] is False


@pytest.mark.asyncio
async def test_database_preflight_catches_product_alias_with_different_sku(tmp_path):
    database = tmp_path / "trade.db"
    engine = create_engine(f"sqlite+aiosqlite:///{database}")
    store = SqlTradeStore(engine)
    try:
        await store.initialize_inventory([external()])
        old = catalog_bindings([external()])
        new = catalog_bindings([replace(external(sku_id="new-sku"), external_product_id="B000000002")])
        result = inspect_database(database, old, new)
        assert result["target_registry_conflicts"] == []
        assert result["target_product_registry_conflicts"] == ["p-1"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_preflight_catches_same_sku_reassigned_to_new_internal_product(tmp_path):
    database = tmp_path / "trade.db"
    engine = create_engine(f"sqlite+aiosqlite:///{database}")
    store = SqlTradeStore(engine)
    try:
        await store.initialize_inventory([external()])
        moved = catalog_bindings([replace(external(), product_id="p-2")])
        result = inspect_database(database, moved, moved)
        assert result["source_registry_conflicts"] == ["sku-1"]
        assert result["target_registry_conflicts"] == ["sku-1"]
        assert result["legacy_identity_unverified"] is True
    finally:
        await engine.dispose()
