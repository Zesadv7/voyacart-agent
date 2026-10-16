"""本地向量故障切换、硬约束与独立集合的运行时验证。"""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.sku import Sku
from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from tests.test_product_vector_persistence import CountingEmbedding
from tests.test_retrieval import BrokenEmbeddingClient, _settings


def constrained_products():
    good = Product("OK", "backpack", "Test", "旅行装备", "US", "light travel backpack",
        ships_to=["CN"], material_tags=["金属"],
        skus=[Sku("OK-S1", "standard", Money.from_major_units(10, "USD"), 5)])
    return [
        replace(good, product_id="stock", skus=[replace(good.skus[0], stock=0)]),
        replace(good, product_id="destination", ships_to=["US"]),
        replace(good, product_id="budget", skus=[replace(good.skus[0], price=Money.from_major_units(100, "USD"))]),
        replace(good, product_id="category", category="户外运动"),
        replace(good, product_id="required", material_tags=["纸"]),
        replace(good, product_id="excluded", material_tags=["金属", "玻璃"]),
        good,
    ]


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("remote_empty", [False, True])
async def test_local_fallback_preserves_all_hard_constraints(hybrid, remote_empty):
    products = constrained_products()
    local = SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]))
    index = SimpleNamespace(search=AsyncMock(return_value=[VectorHit(p.product_id, 1.0) for p in products]))
    remote = SimpleNamespace(embed=AsyncMock(return_value=[1.0])) if remote_empty else BrokenEmbeddingClient()
    usecase = CatalogSearchUseCase(InMemoryProductRepository(products), embedder=remote,
        vector_index=SimpleNamespace(search=AsyncMock(return_value=[])),
        local_embedder=local, local_vector_index=index, hybrid_enabled=hybrid)
    result = await usecase.execute(ProductSearchSpec(normalized_query="轻便背包", ship_to="CN",
        category="旅行装备", price_max_major=100, target_currency="CNY",
        required_material_tags=["金属"], excluded_material_tags=["玻璃"]))
    assert result["recall_strategy"] == "local_embedding"
    assert result["rerank_applied"] is False
    assert [h["product_id"] for h in result["hits"]] == ["OK"]
    # 诊断沿用现有的三条上限；六种不合格商品均已从结果中排除。
    assert {p["reason"] for p in result["filtered_out"]} == {
        "out_of_stock", "ship_to_unavailable", "over_price_cap",
    }
    local.embed.assert_awaited_once_with("轻便背包")


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("local_empty", [False, True])
async def test_local_failure_preserves_lexical_fallback(hybrid, local_empty):
    product = constrained_products()[-1]
    usecase = CatalogSearchUseCase(InMemoryProductRepository([product]),
        embedder=BrokenEmbeddingClient(), vector_index=SimpleNamespace(),
        local_embedder=CountingEmbedding() if local_empty else BrokenEmbeddingClient(),
        local_vector_index=SimpleNamespace(search=AsyncMock(return_value=[])), hybrid_enabled=hybrid)
    result = await usecase.execute(ProductSearchSpec(normalized_query="backpack"))
    assert result["recall_strategy"] == ("bm25" if hybrid else "keyword_2gram")
    assert [h["product_id"] for h in result["hits"]] == ["OK"]


@pytest.mark.parametrize("hybrid", [False, True])
async def test_healthy_remote_bypasses_local(hybrid):
    product = constrained_products()[-1]
    local = SimpleNamespace(embed=AsyncMock())
    usecase = CatalogSearchUseCase(InMemoryProductRepository([product]),
        embedder=CountingEmbedding(),
        vector_index=SimpleNamespace(search=AsyncMock(return_value=[VectorHit("OK", 1.0)])),
        local_embedder=local, local_vector_index=SimpleNamespace(), hybrid_enabled=hybrid)
    result = await usecase.execute(ProductSearchSpec(normalized_query="backpack"))
    assert result["recall_strategy"] == ("hybrid_only" if hybrid else "embedding_only")
    local.embed.assert_not_awaited()


async def test_local_refills_until_an_eligible_product_is_found():
    good = constrained_products()[-1]
    products = [replace(good, product_id=f"rejected-{i}", ships_to=["US"]) for i in range(40)] + [good]
    index = SimpleNamespace(search=AsyncMock(side_effect=lambda vector, top_n: [
        VectorHit(p.product_id, 1.0) for p in products[:top_n]]))
    result = await CatalogSearchUseCase(InMemoryProductRepository(products),
        embedder=BrokenEmbeddingClient(), vector_index=SimpleNamespace(),
        local_embedder=CountingEmbedding(), local_vector_index=index).execute(
            ProductSearchSpec(normalized_query="轻便背包", ship_to="CN", top_k=1))
    assert [h["product_id"] for h in result["hits"]] == ["OK"]
    assert index.search.await_count == 2


@pytest.mark.parametrize("enabled", [False, True])
async def test_composition_bootstraps_and_closes_independent_local_index(monkeypatch, tmp_path, enabled):
    from app import composition
    from app.infrastructure.embedding import local_multilingual

    settings = replace(_settings(tmp_path), llm_api_key="unit-test", embedding_api_key="unit-test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'voyacart.db'}",
        local_embedding_fallback=enabled, automation_worker_mode="off")
    local = CountingEmbedding(vector=[1.0, 0.0])
    local.model_name, local.dimension, local.version = "test-local", 2, "fixture-v1"
    factory = Mock(return_value=local)
    monkeypatch.setattr(local_multilingual, "LocalMultilingualEmbedding", factory)
    monkeypatch.setattr(composition, "load_settings", lambda: settings)
    monkeypatch.setattr(composition, "InMemoryProductRepository", lambda: InMemoryProductRepository([constrained_products()[-1]]))
    monkeypatch.setattr(composition, "OpenAIEmbeddingClient", lambda _: CountingEmbedding(vector=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(composition, "build_category_knowledge_base", lambda _: None)
    monkeypatch.setattr(composition, "bootstrap_category_knowledge", AsyncMock())
    container = await composition.build_container()
    try:
        await container.startup()
        info = await container.vector_index._client.get_collection(settings.qdrant_collection)
        assert info.points_count == 1 and info.config.params.vectors.size == 3
        if enabled:
            factory.assert_called_once_with(tmp_path / "local_embedding" / "model", local_files_only=True)
            local_info = await container.local_vector_index._client.get_collection(settings.qdrant_local_collection)
            assert local_info.points_count == 1 and local_info.config.params.vectors.size == 2
            assert container.vector_index.embedding_key != container.local_vector_index.embedding_key
            assert len(await container.local_vector_index.search([1.0, 0.0], 5)) == 1
            assert (tmp_path / "qdrant").is_dir()
            assert (tmp_path / "local_embedding" / "qdrant").is_dir()
        else:
            factory.assert_not_called()
            assert container.local_vector_index is None and container.local_embedder is None
    finally:
        await container.shutdown()
    assert container.vector_index._client._client.closed
    if enabled:
        assert container.local_vector_index._client._client.closed


def test_local_settings_are_opt_in(monkeypatch):
    from app.infrastructure.settings import load_settings
    monkeypatch.delenv("LOCAL_EMBEDDING_FALLBACK", raising=False)
    monkeypatch.delenv("QDRANT_LOCAL_COLLECTION", raising=False)
    assert load_settings().local_embedding_fallback is False
    assert load_settings().qdrant_local_collection == "voyacart_products_local"
    monkeypatch.setenv("LOCAL_EMBEDDING_FALLBACK", "1")
    monkeypatch.setenv("QDRANT_LOCAL_COLLECTION", "separate_local_products")
    assert load_settings().local_embedding_fallback is True
    assert load_settings().qdrant_local_collection == "separate_local_products"


@pytest.mark.parametrize("collection", ["test_products", "test_category_kb", " "])
async def test_local_collection_configuration_preserves_existing_indexes(monkeypatch, tmp_path, collection):
    from app import composition
    from app.infrastructure.embedding import local_multilingual

    factory = Mock()
    monkeypatch.setattr(local_multilingual, "LocalMultilingualEmbedding", factory)
    monkeypatch.setattr(composition, "load_settings", lambda: replace(_settings(tmp_path),
        local_embedding_fallback=True, qdrant_local_collection=collection))
    with pytest.raises(ValueError, match="独立的非空集合名"):
        await composition.build_container()
    factory.assert_not_called()
