"""M3d：真实本地 Qdrant + 确定性向量，验证目录隔离与中断安全，不调用模型。"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.vector.embedding_identity import product_embedding_payload
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex, _point_id
from tests.test_product_vector_persistence import CountingEmbedding, products
from tests.test_retrieval import _settings


async def test_unbound_restart_never_exposes_prior_catalog(tmp_path):
    settings = _settings(tmp_path)
    first = QdrantProductIndex(settings)
    assert await bootstrap_product_index(InMemoryProductRepository(products()), CountingEmbedding(), first)
    await first.close()
    second = QdrantProductIndex(settings)
    try:
        assert await second.search([1, 0.25, 0.5], 5) == []
        assert (await second.verify_catalog())['readiness'] == 'unbound'
        report = {}
        assert await bootstrap_product_index(InMemoryProductRepository(products()), CountingEmbedding(fail_batch=1), second, report=report)
        assert report['complete'] and report['index_verified'] and report['catalog_complete']
        assert report['collection_points'] == 5
        assert len(report['index_fingerprint']) == 64
    finally:
        await second.close()


async def test_same_id_new_external_identity_is_excluded_during_interrupted_sync(tmp_path):
    original = products()[:3]
    current = [replace(p, source_platform='amazon', source_locale='es', external_product_id=f'NEW-{i}')
               for i, p in enumerate(original)]
    assert [p.searchable_text() for p in original] == [p.searchable_text() for p in current]
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository(original), CountingEmbedding(), index)
        old_key = index.catalog_key
        report = {}
        assert not await bootstrap_product_index(InMemoryProductRepository(current), CountingEmbedding(fail_batch=2), index,
                                                 batch_size=1, report=report)
        assert index.catalog_key != old_key
        assert report['readiness'] == 'partial' and report['indexed_products'] == 1
        assert report['missing_products'] == 2 and report['invalid_points'] == 2
        assert [hit.product_id for hit in await index.search([1, .25, .5], 10)] == [current[0].product_id]
        assert await index.search_filtered([1, .25, .5], 3, product_ids=[current[1].product_id]) == []
        resumed = CountingEmbedding(); completed = {}
        assert await bootstrap_product_index(InMemoryProductRepository(current), resumed, index, report=completed)
        assert sum(map(len, resumed.batches)) == 2
        assert completed['complete'] and completed['reused_products'] == 1
    finally:
        await index.close()


async def test_same_asin_different_locale_requires_new_vector_even_when_text_unchanged(tmp_path):
    original = replace(products()[0], source_platform='amazon', source_locale='us', external_product_id='SAME-ASIN')
    current = replace(original, source_locale='jp')
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository([original]), CountingEmbedding(), index)
        offline = CountingEmbedding(fail_batch=1)
        assert not await bootstrap_product_index(InMemoryProductRepository([current]), offline, index)
        assert len(offline.batches) == 1
        assert await index.search([1, .25, .5], 1) == []
    finally:
        await index.close()


async def test_removed_products_are_preserved_but_collection_is_not_complete(tmp_path):
    items = products()
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository(items), CountingEmbedding(), index)
        original_key = index.catalog_key
        report = {}; no_model = CountingEmbedding(fail_batch=1)
        assert not await bootstrap_product_index(InMemoryProductRepository(items[:2]), no_model, index, report=report)
        assert no_model.batches == [] and index.catalog_key != original_key
        assert not report['complete'] and report['catalog_complete'] and report['index_verified']
        assert report['readiness'] == 'stale' and report['stale_points'] == 3
        assert report['collection_points'] == 5 and report['missing_products'] == 0
        assert {hit.product_id for hit in await index.search([1, .25, .5], 10)} == {p.product_id for p in items[:2]}
        assert (await index._client.get_collection(index._collection)).points_count == 5
    finally:
        await index.close()


async def test_foreign_and_stale_points_cannot_crowd_out_current_top_n(tmp_path):
    item = products()[0]
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository([item]), CountingEmbedding(vector=[0, 1, 0]), index)
        foreign = replace(item, product_id='P999999', external_product_id='OLD')
        # 旧目录点的相似度更高，但目录边界必须在 top_n 截断之前将其排除。
        stale_payload = {**product_embedding_payload(foreign, index.embedding_key),
                         'catalog_key': 'previous-catalog', 'vector_dimension': 3}
        await index._client.upsert(index._collection, [PointStruct(id=_point_id(foreign.product_id),
                                                                   payload=stale_payload, vector=[1, 0, 0])])
        hits = await index.search([1, 0, 0], 1)
        assert [hit.product_id for hit in hits] == [item.product_id]
        report = await index.verify_catalog()
        assert report['stale_points'] == 1 and not report['complete']
        # 故障客户端绕过查询过滤时，返回项仍不可被映射到错误的现目录商品。
        original_query = index._client.query_points
        index._client.query_points = AsyncMock(return_value=SimpleNamespace(points=[
            SimpleNamespace(id=_point_id(item.product_id), payload=stale_payload, score=1.0)]))
        assert await index.search([1, 0, 0], 1) == []
        index._client.query_points = original_query
    finally:
        await index.close()


async def test_legacy_text_and_model_match_cannot_self_attest_external_identity(tmp_path):
    item = products()[0]
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        await index.ensure_ready(3)
        old = product_embedding_payload(item, index.embedding_key)
        for key in ['product_identity', 'product_key']:
            del old[key]
        old['embedding_schema'] = 'product-text-v1'
        await index._client.upsert(index._collection, [PointStruct(id=_point_id(item.product_id), payload=old, vector=[1, 0, 0])])
        report = {}; offline = CountingEmbedding(fail_batch=1)
        assert not await bootstrap_product_index(InMemoryProductRepository([item]), offline, index, report=report)
        assert report['reused_products'] == 0 and report['invalid_points'] == 1
        assert await index.search([1, 0, 0], 1) == []
        assert (await index._client.get_collection(index._collection)).points_count == 1
    finally:
        await index.close()


async def test_warm_verification_rejects_distance_without_embedding_or_deleting(tmp_path):
    item = products()[0]
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        await index._client.create_collection(index._collection, vectors_config=VectorParams(size=3, distance=Distance.DOT))
        payload = product_embedding_payload(item, index.embedding_key)
        await index._client.upsert(index._collection, [PointStruct(id=_point_id(item.product_id), payload=payload, vector=[1, 0, 0])])
        report = {}; offline = CountingEmbedding(fail_batch=1)
        assert not await bootstrap_product_index(InMemoryProductRepository([item]), offline, index, report=report)
        assert offline.batches == [] and report['error_type'] == 'ValueError'
        info = await index._client.get_collection(index._collection)
        assert info.points_count == 1 and info.config.params.vectors.distance == Distance.DOT
    finally:
        await index.close()


async def test_duplicate_or_empty_catalog_clears_prior_binding(tmp_path):
    item = products()[0]
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository([item]), CountingEmbedding(), index)
        with pytest.raises(ValueError, match='重复'):
            index.bind_catalog([item, item])
        assert await index.search([1, .25, .5], 1) == []
        assert not await bootstrap_product_index(InMemoryProductRepository([]), CountingEmbedding(), index)
        assert await index.search([1, .25, .5], 1) == []
    finally:
        await index.close()


async def test_mutated_product_cannot_be_written_under_stale_bound_snapshot(tmp_path):
    item = deepcopy(products()[0])
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        index.bind_catalog([item])
        await index.ensure_ready(3)
        item.external_product_id = 'CHANGED-AFTER-BIND'
        with pytest.raises(ValueError, match='快照'):
            await index.upsert_products([item], [[1, 0, 0]])
        assert (await index._client.get_collection(index._collection)).points_count == 0
    finally:
        await index.close()


async def test_demo_5700_cold_warm_point_set_exact_and_no_new_warm_embeddings(tmp_path):
    repo = InMemoryProductRepository()
    items = await repo.list_all()
    assert len(items) == 5700
    settings = _settings(tmp_path)
    first = QdrantProductIndex(settings)
    cold = {}
    try:
        assert await bootstrap_product_index(repo, CountingEmbedding(), first, batch_size=256, report=cold)
        assert cold['complete'] and cold['collection_points'] == cold['indexed_products'] == 5700
        assert cold['embedded_products'] == 5700 and cold['stale_points'] == 0
    finally:
        await first.close()
    second = QdrantProductIndex(settings)
    warm = {}; offline = CountingEmbedding(fail_batch=1)
    try:
        assert await bootstrap_product_index(repo, offline, second, report=warm)
        assert warm['complete'] and warm['reused_products'] == 5700 and offline.batches == []
        assert warm['index_fingerprint'] == cold['index_fingerprint']
        assert warm['catalog_key'] == cold['catalog_key']
    finally:
        await second.close()


async def test_changed_id_point_with_current_catalog_tag_is_still_excluded(tmp_path):
    original = products()[0]
    current = replace(original, external_product_id='REBOUND-IDENTITY')
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository([original]), CountingEmbedding(), index)
        assert await index.products_needing_embeddings([current]) == [current]
        # 模拟绕过适配器的外部数据损坏；返回 payload 校验依然禁止错误商品映射。
        await index._client.set_payload(index._collection, {'catalog_key': index.catalog_key},
                                        points=[_point_id(current.product_id)], wait=True)
        assert await index.search([1, .25, .5], 1) == []
        assert await index.search_filtered([1, .25, .5], 1, product_ids=[current.product_id]) == []
        report = await index.verify_catalog()
        assert report['invalid_points'] == report['missing_products'] == 1
        assert not report['complete']
    finally:
        await index.close()


def test_model_key_keeps_configured_dimension_in_identity(tmp_path):
    from app.infrastructure.vector.embedding_identity import embedding_identity
    settings = _settings(tmp_path)
    assert embedding_identity(settings) != embedding_identity(replace(settings, embedding_dim=384))


async def test_conditional_rebind_cannot_relabel_concurrently_replaced_identity(tmp_path):
    original, added = products()[:2]
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository([original]), CountingEmbedding(), index)
        set_payload = index._client.set_payload
        replaced = False

        async def race_replace(*args, **kwargs):
            nonlocal replaced
            if not replaced:
                replaced = True
                foreign = replace(original, external_product_id='CONCURRENT-FOREIGN-IDENTITY')
                payload = {**product_embedding_payload(foreign, index.embedding_key),
                           'catalog_key': 'other-process-catalog', 'vector_dimension': 3}
                await index._client.upsert(index._collection, [PointStruct(
                    id=_point_id(original.product_id), vector=[1, 0, 0], payload=payload)])
            return await set_payload(*args, **kwargs)

        index._client.set_payload = race_replace
        report = {}
        assert not await bootstrap_product_index(InMemoryProductRepository([original, added]), CountingEmbedding(), index, report=report)
        assert replaced and report['invalid_points'] == 1 and report['missing_products'] == 1
        point = (await index._client.retrieve(index._collection, [_point_id(original.product_id)]))[0]
        assert point.payload['catalog_key'] == 'other-process-catalog'
        assert [hit.product_id for hit in await index.search([1, 0, 0], 1)] == [added.product_id]
    finally:
        await index.close()
