"""候选只保存历史；当前目录核验不生成确认，也不调用模型。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.money import Money
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.catalog_identity import CATALOG_IDENTITY_VERSION, catalog_bindings
from app.infrastructure.security.public_output import public_products


class CandidateWorkspaceService:
    def __init__(self, store, product_repo, trade_store, *, catalog_search=None):
        self.store = store
        self.product_repo = product_repo
        self.trade_store = trade_store
        # 明确精确 ID 路径；不装配 embedding、向量或重排客户端。
        self.catalog_search = catalog_search or CatalogSearchUseCase(product_repo)

    async def verify(self, buyer_id: str, session_id: str, candidate_id: str, *,
                     target_currency: str = "CNY", ship_to: str | None = None) -> dict:
        Money.of(0, target_currency)
        item = await self.store.get_candidate(buyer_id, session_id, candidate_id)
        response = {"candidate_id": candidate_id, "session_id": session_id,
                    "workspace_revision": item["workspace_revision"],
                    "checked_at": datetime.now(timezone.utc).isoformat(), "source": "local_catalog"}

        def result(status, reason, product=None):
            return {**response, "status": status, "reason": reason,
                    **({"product": product} if product is not None else {})}

        saved = item.get("catalog_binding")
        if (not isinstance(saved, dict) or type(saved.get("version")) is not int
                or saved["version"] != CATALOG_IDENTITY_VERSION):
            return result("unknown_identity", "此历史记录没有可验证的目录来源，请重新搜索后保留。")
        current = (await self.trade_store.get_catalog_bindings([item["sku_id"]])).get(item["sku_id"])
        if not current or current.get("active") is not True:
            return result("unavailable", "该规格已不在当前可售目录，历史候选仍保留。")
        if type(current.get("version")) is not int or current["version"] != CATALOG_IDENTITY_VERSION:
            return result("unknown_identity", "当前目录身份版本无法验证，请重新搜索或联系维护者。")
        if (saved.get("product_id") != item["product_id"] or current.get("product_id") != item["product_id"]
                or saved.get("identity_hash") != current.get("identity_hash")):
            return result("identity_mismatch", "目录编号对应的商品或规格身份已变化，请重新搜索。")
        if any(saved.get(key) != current.get(key) for key in ("terms_hash", "generation", "active")):
            return result("terms_changed", "商品或配送事实已变化，请重新搜索后重新保留候选。")
        product = await self.product_repo.find_by_id(item["product_id"])
        sku = product.find_sku(item["sku_id"]) if product is not None else None
        if product is None or sku is None:
            return result("unavailable", "当前目录没有这件商品或规格，历史信息仍保留。")
        # 多进程目录不一致时不能把 registry 的新身份贴给本进程旧商品。
        observed = catalog_bindings([product])[item["sku_id"]]
        if observed["identity_hash"] != current["identity_hash"]:
            return result("identity_mismatch", "当前服务与登记目录的商品身份不一致，暂不能准备。")
        if observed["terms_hash"] != current["terms_hash"]:
            return result("terms_changed", "当前服务与登记目录的商品事实不一致，请稍后重新核验。")
        if sku.stock <= 0:
            return result("out_of_stock", "当前规格库存不足，可稍后重新核验；保存时信息仍保留。")
        if ship_to is not None and ship_to not in product.ships_to:
            return result("unavailable", "当前目录不支持配送到所选国家或地区。")
        exact = await self.catalog_search.execute(ProductSearchSpec(
            product_id=item["product_id"], sku_id=item["sku_id"], ship_to=ship_to,
            target_currency=target_currency, top_k=1,
        ))
        cards = public_products(exact.get("hits"))
        card = next((card for card in cards if card.get("product_id") == item["product_id"]
                     and card.get("default_sku_id") == item["sku_id"]), None)
        if card is None:
            reasons = {entry.get("reason") for entry in exact.get("filtered_out", []) if isinstance(entry, dict)}
            status = "out_of_stock" if reasons & {"out_of_stock", "requested_sku_out_of_stock"} else "unavailable"
            return result(status, "目录核验未得到可用规格，请稍后重新查询。")
        # 核验期间另一连接更新目录时拒绝陈旧结果；不宣称读操作预留库存。
        after = (await self.trade_store.get_catalog_bindings([item["sku_id"]])).get(item["sku_id"])
        if after != current:
            return result("terms_changed", "核验期间目录已变化，请重新核验。")
        return result("available", "这是当前本地目录核验，未预留库存；生成确认单时会再次检查。", card)
