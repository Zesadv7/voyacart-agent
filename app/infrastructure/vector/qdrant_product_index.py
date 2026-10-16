# -*- coding: utf-8 -*-
"""Qdrant 商品向量：先绑定完整权威目录，再同步或查询；不删除历史集合/旧点。"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, HasIdCondition, MatchAny, MatchValue,
    PointStruct, VectorParams,
)

from app.domain.catalog.ports.retrieval_ports import ProductVectorIndex, VectorHit
from app.domain.catalog.product import Product
from app.infrastructure.settings import Settings
from app.infrastructure.vector.embedding_identity import (
    CATALOG_BINDING_SCHEMA, EMBEDDING_SCHEMA, catalog_embedding_identity, embedding_identity,
    product_embedding_payload,
)


def _point_id(product_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"voyacart/product/{product_id}"))


def _valid_vector(vector: Any, dimension: int) -> bool:
    return (isinstance(vector, list) and len(vector) == dimension and dimension > 0
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    and math.isfinite(x) for x in vector))


class QdrantProductIndex(ProductVectorIndex):
    def __init__(self, settings: Settings) -> None:
        if settings.qdrant_url:
            self._client = AsyncQdrantClient(url=settings.qdrant_url)
        else:
            local_path = settings.data_dir / "qdrant"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self._client = AsyncQdrantClient(path=str(local_path))
        self._collection = settings.qdrant_collection
        self.embedding_key = embedding_identity(settings)
        self.catalog_key: str | None = None
        self._expected: dict[str, dict[str, str]] = {}
        self._vector_dim: int | None = None

    def bind_catalog(self, products: list[Product]) -> None:
        """绑定完整目录快照，失败也清除旧绑定；不能把同步批次当成完整目录。"""
        self.catalog_key = None
        self._expected = {}
        payloads = {p.product_id: product_embedding_payload(p, self.embedding_key) for p in products}
        if len(payloads) != len(products):
            raise ValueError('目录存在重复 product_id，禁止绑定向量索引')
        self.catalog_key = catalog_embedding_identity(payloads)
        self._expected = payloads

    async def _collection_dimension(self) -> int:
        config = (await self._client.get_collection(self._collection)).config.params.vectors
        if not isinstance(config, VectorParams) or config.distance != Distance.COSINE or config.size < 1:
            raise ValueError('已有 Qdrant 集合的向量配置或距离类型不匹配；请配置新集合，禁止覆盖旧向量')
        return config.size

    def _matches(self, point: Any, *, require_catalog: bool = True) -> bool:
        payload = point.payload or {}
        expected = self._expected.get(payload.get('product_id'))
        return bool(expected and str(point.id) == _point_id(expected['product_id'])
                    and all(payload.get(key) == value for key, value in expected.items())
                    and (not require_catalog or payload.get('catalog_key') == self.catalog_key)
                    and payload.get('vector_dimension') == self._vector_dim)

    async def products_needing_embeddings(self, products: list[Product]) -> list[Product]:
        self.bind_catalog(products)
        if not await self._client.collection_exists(self._collection):
            return products
        self._vector_dim = await self._collection_dimension()
        reusable: set[str] = set()
        for start in range(0, len(products), 256):
            points = await self._client.retrieve(
                collection_name=self._collection,
                ids=[_point_id(p.product_id) for p in products[start:start + 256]],
                with_payload=True, with_vectors=True,
            )
            rebind = []
            for point in points:
                if self._matches(point, require_catalog=False) and _valid_vector(point.vector, self._vector_dim):
                    reusable.add(point.payload['product_id'])
                    if point.payload.get('catalog_key') != self.catalog_key:
                        rebind.append((point.id, point.payload['product_key']))
            if rebind:
                # 仅同外部身份/文本/模型的已验证点可重绑；旧格式不能凭同编号补签。
                # 用已验证语义键做条件写入，防止 retrieve 后同 ID 被并发换绑再误贴目录标签。
                selector = Filter(must=[
                    HasIdCondition(has_id=[point_id for point_id, _ in rebind]),
                    FieldCondition(key='product_key', match=MatchAny(any=[key for _, key in rebind])),
                    FieldCondition(key='embedding_key', match=MatchValue(value=self.embedding_key)),
                    FieldCondition(key='embedding_schema', match=MatchValue(value=EMBEDDING_SCHEMA)),
                ])
                await self._client.set_payload(self._collection, {'catalog_key': self.catalog_key},
                                               points=selector, wait=True)
        return [p for p in products if p.product_id not in reusable]

    async def ensure_ready(self, vector_dim: int) -> None:
        if vector_dim < 1:
            raise ValueError('向量维度必须为正整数')
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
            )
        elif await self._collection_dimension() != vector_dim:
            raise ValueError('已有 Qdrant 集合的维度或距离类型不匹配；请配置新集合，禁止覆盖旧向量')
        self._vector_dim = vector_dim

    def _model_filter(self, product_ids: list[str] | None = None) -> Filter:
        # catalog_key 承诺完整 ID→身份/文本映射，重绑只允许条件写入已验证点。
        # 普通查询无需重复传完整目录 ID/语义键，避免本地过滤变成 O(N²)。
        conditions = [
            FieldCondition(key='embedding_key', match=MatchValue(value=self.embedding_key)),
            FieldCondition(key='catalog_key', match=MatchValue(value=self.catalog_key or 'unbound')),
        ]
        if product_ids is not None:
            conditions.append(HasIdCondition(has_id=[_point_id(pid) for pid in product_ids if pid in self._expected]))
        return Filter(must=conditions)

    async def upsert_products(self, products: list[Product], embeddings: list[list[float]]) -> None:
        if len(products) != len(embeddings):
            raise ValueError("products 与 embeddings 数量不一致")
        if not products:
            return
        # 兼容已有的一次性全量写入接口；后续批次必须属于这份显式绑定。
        if self.catalog_key is None:
            self.bind_catalog(products)
        if self._vector_dim is None or not all(_valid_vector(v, self._vector_dim) for v in embeddings):
            raise ValueError('未验证集合维度或 embedding 无效，未写入本批商品')
        points = []
        for product, embedding in zip(products, embeddings):
            payload = product_embedding_payload(product, self.embedding_key)
            if payload != self._expected.get(product.product_id):
                raise ValueError('商品不属于已绑定目录快照；请重新绑定完整目录后同步')
            points.append(PointStruct(id=_point_id(product.product_id), vector=embedding,
                                      payload={**payload, 'catalog_key': self.catalog_key,
                                               'vector_dimension': self._vector_dim}))
        await self._client.upsert(collection_name=self._collection, points=points, wait=True)

    async def verify_catalog(self) -> dict[str, Any]:
        """枚举全部点；当前目录完整与集合无额外旧点分别报告，不做删除修复。"""
        result: dict[str, Any] = {
            'catalog_binding_schema': CATALOG_BINDING_SCHEMA, 'catalog_key': self.catalog_key,
            'model_key': self.embedding_key, 'collection': self._collection,
            'expected_products': len(self._expected), 'collection_points': 0,
            'indexed_products': 0, 'stale_points': 0, 'invalid_points': 0,
            'missing_products': len(self._expected), 'catalog_complete': False,
            'complete': False, 'index_verified': False, 'readiness': 'unbound',
            'index_fingerprint_schema': 'point-identity-payload-v1',
        }
        if self.catalog_key is None:
            return result
        if not await self._client.collection_exists(self._collection):
            result.update(index_verified=True, readiness='empty')
            return result
        self._vector_dim = await self._collection_dimension()
        result.update(vector_dimension=self._vector_dim, distance='Cosine')
        expected_ids = {_point_id(pid) for pid in self._expected}
        valid: set[str] = set()
        fingerprints: list[tuple[str, str]] = []
        offset = None
        while True:
            points, offset = await self._client.scroll(self._collection, limit=256, offset=offset,
                                                       with_payload=True, with_vectors=True)
            for point in points:
                result['collection_points'] += 1
                if str(point.id) not in expected_ids:
                    result['stale_points'] += 1
                elif self._matches(point) and _valid_vector(point.vector, self._vector_dim):
                    valid.add(point.payload['product_id'])
                else:
                    result['invalid_points'] += 1
                # COSINE 本地点重载前后的归一化表现不同；身份指纹不把数值表示差异当成目录变化。
                # 实际向量仍逐点检查存在、维度及有限数值；这里绑定点位/完整 payload/集合配置。
                fingerprint = hashlib.sha256(json.dumps(
                    {'payload': point.payload}, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                fingerprints.append((str(point.id), fingerprint))
            if offset is None:
                break
        catalog_complete = bool(self._expected) and len(valid) == len(self._expected)
        complete = catalog_complete and not result['stale_points'] and not result['invalid_points']
        result.update(indexed_products=len(valid), missing_products=len(self._expected) - len(valid),
                      catalog_complete=catalog_complete, complete=complete, index_verified=True,
                      readiness='complete' if complete else ('stale' if result['stale_points'] else 'partial'),
                      index_fingerprint=hashlib.sha256(json.dumps({
                          'schema': result['index_fingerprint_schema'], 'points': sorted(fingerprints),
                          'vector_dimension': self._vector_dim, 'distance': result['distance'],
                      }, sort_keys=True).encode()).hexdigest())
        return result

    async def _search(self, embedding: list[float], top_n: int,
                      product_ids: list[str] | None = None) -> list[VectorHit]:
        if self.catalog_key is None or not self._expected or top_n < 1:
            return []
        if product_ids is not None and not any(pid in self._expected for pid in product_ids):
            return []
        result = await self._client.query_points(
            collection_name=self._collection, query=embedding, limit=top_n,
            query_filter=self._model_filter(product_ids), with_payload=True,
        )
        return [VectorHit(product_id=point.payload['product_id'], score=point.score)
                for point in result.points if self._matches(point)]

    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        return await self._search(embedding, top_n)

    async def search_filtered(self, embedding: list[float], top_n: int, *, product_ids: list[str]) -> list[VectorHit]:
        return await self._search(embedding, top_n, product_ids)

    async def close(self) -> None:
        await self._client.close()
