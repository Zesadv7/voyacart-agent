"""目录身份与可批准事实：内部编号不能充当外部商品身份证明。"""
from __future__ import annotations

import hashlib
import json

from app.domain.catalog.product import Product
from app.domain.order.ports.trade_store import TradeStoreError

CATALOG_IDENTITY_VERSION = 1


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def catalog_bindings(products: list[Product]) -> dict[str, dict]:
    """价格和剩余库存不是身份；同号换站点、ASIN、规格则必须拒绝。"""
    if not products:
        raise TradeStoreError("INVALID_ARGUMENT", "目录不能为空，不能隐式停用全部商品")
    result: dict[str, dict] = {}
    seen: set[str] = set()
    for product in products:
        if product.product_id in seen:
            raise TradeStoreError("INVALID_ARGUMENT", "目录中存在重复商品编号")
        seen.add(product.product_id)
        identity = {
            "kind": "external" if product.source_platform and product.external_product_id else "internal",
            "source_platform": product.source_platform, "source_locale": product.source_locale,
            "external_product_id": product.external_product_id,
            "canonical_product_id": product.canonical_product_id,
        }
        # 无外部事实的旧合成样例只声明内部身份，绝不生成虚构 ASIN。
        if identity["kind"] == "internal":
            identity["internal_product_id"] = product.product_id
        product_hash = digest(identity)
        for sku in product.skus:
            if sku.sku_id in result:
                raise TradeStoreError("INVALID_ARGUMENT", "目录中存在重复 SKU")
            terms = {"title": product.title, "spec": sku.spec,
                "ships_to": sorted(set(product.ships_to)), "origin_country": product.origin_country}
            result[sku.sku_id] = {
                "product_id": product.product_id, "product_identity_hash": product_hash,
                "identity": {**identity, "sku_spec": sku.spec},
                "identity_hash": digest({"product": identity, "sku_spec": sku.spec}),
                "terms": terms, "terms_hash": digest(terms),
            }
    return result


def compare_catalogs(source: list[Product], target: list[Product]) -> dict:
    before, after = catalog_bindings(source), catalog_bindings(target)
    common = before.keys() & after.keys()
    before_products = {row["product_id"]: row["product_identity_hash"] for row in before.values()}
    after_products = {row["product_id"]: row["product_identity_hash"] for row in after.values()}
    product_conflicts = sorted(pid for pid in before_products.keys() & after_products.keys()
        if before_products[pid] != after_products[pid])
    conflicts = sorted(sku for sku in common if any(before[sku][key] != after[sku][key]
        for key in ("product_id", "identity_hash")))
    return {"source_products": len(source), "target_products": len(target),
        "source_skus": len(before), "target_skus": len(after),
        "product_identity_conflicts": product_conflicts, "identity_conflicts": conflicts, "retired_skus": sorted(before.keys() - after.keys()),
        "new_skus": sorted(after.keys() - before.keys()),
        "terms_changed_skus": sorted(sku for sku in common if before[sku]["terms_hash"] != after[sku]["terms_hash"]),
        "source_identity_digest": digest(before), "target_identity_digest": digest(after),
        "safe_identity_update": not conflicts and not product_conflicts}
