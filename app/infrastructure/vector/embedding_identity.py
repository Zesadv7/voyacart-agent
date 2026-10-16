"""商品向量身份：目录映射、外部身份、文本和模型必须同时可验证。"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from app.domain.catalog.product import Product
from app.infrastructure.settings import Settings


CATALOG_BINDING_SCHEMA = 'product-catalog-binding-v1'
EMBEDDING_SCHEMA = 'product-text-identity-v2'


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def embedding_identity(settings: Settings) -> str:
    url = urlsplit(settings.embedding_base_url.rstrip('/'))
    host = url.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    endpoint = urlunsplit((url.scheme, host + (f':{url.port}' if url.port else ''), url.path, '', ''))
    identity = {'endpoint': endpoint, 'model': settings.embedding_model,
                'version': settings.embedding_version, 'configured_dimension': settings.embedding_dim}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def product_embedding_payload(product: Product, model_key: str) -> dict[str, str]:
    # 价格与库存不参与向量身份；外部身份必须包含站点，不能只比较 ASIN 或内部编号。
    identity_key = _digest({
        'product_id': product.product_id,
        'source_platform': product.source_platform,
        'source_locale': product.source_locale,
        'external_product_id': product.external_product_id,
        'canonical_product_id': product.canonical_product_id,
        'skus': sorted((sku.sku_id, sku.spec) for sku in product.skus),
    })
    text_hash = hashlib.sha256(product.searchable_text().encode()).hexdigest()
    return {'product_id': product.product_id, 'embedding_key': model_key,
            'text_hash': text_hash, 'product_identity': identity_key,
            'product_key': _digest([product.product_id, identity_key, text_hash]),
            'embedding_schema': EMBEDDING_SCHEMA}


def catalog_embedding_identity(payloads: dict[str, dict[str, str]]) -> str:
    """检索目录绑定指纹；不冒充原始压缩文件的字节指纹。"""
    return _digest({'schema': CATALOG_BINDING_SCHEMA,
                    'products': sorted((pid, payload['product_key']) for pid, payload in payloads.items())})
