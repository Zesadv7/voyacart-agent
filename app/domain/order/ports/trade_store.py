"""交易确认与库存、订单的原子持久化端口。"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import fields
from datetime import datetime
from typing import Literal

from app.domain.catalog.product import Product
from app.domain.order.address import Address


PREPARE_REQUEST_VERSION = 1
_MAX_QUANTITY = 2**63 - 1


class TradeStoreError(ValueError):
    """可向调用方展示的交易拒绝；code 用于稳定的接口错误处理。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def normalize_prepare_request(action: str, payload: dict) -> dict:
    """请求身份只绑定用户意图；报价、库存、标题和确认有效期不参与摘要。"""
    def required(value: object, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise TradeStoreError("INVALID_ARGUMENT", f"{label} 必须为非空字符串，最长 {maximum} 字符")
        return value.strip()

    if not isinstance(payload, dict):
        raise TradeStoreError("INVALID_ARGUMENT", "请求内容必须为对象")
    if action == "cancel":
        return {"order_id": required(payload.get("order_id"), "order_id", 32),
                "reason": required(payload.get("reason"), "reason", 255)}
    if action != "create":
        raise TradeStoreError("INVALID_ARGUMENT", "不支持的交易操作")
    address = payload.get("shipping_address")
    if not isinstance(address, dict):
        raise TradeStoreError("INVALID_ARGUMENT", "缺少完整收货地址")
    normalized_address = {}
    for field in fields(Address):
        value = address.get(field.name, "")
        if not isinstance(value, str):
            raise TradeStoreError("INVALID_ARGUMENT", "地址字段必须为字符串")
        normalized_address[field.name] = value.strip()
    try:
        Address(**normalized_address)
    except ValueError as exc:
        raise TradeStoreError("INVALID_ARGUMENT", str(exc)) from exc
    if not isinstance(payload.get("items"), list) or not payload["items"]:
        raise TradeStoreError("INVALID_ARGUMENT", "订单至少需要一个商品")
    items: dict[str, dict] = {}
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise TradeStoreError("INVALID_ARGUMENT", "商品明细格式错误")
        product_id = required(item.get("product_id"), "product_id", 64)
        sku_id = required(item.get("sku_id"), "sku_id", 64)
        quantity = item.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= _MAX_QUANTITY:
            raise TradeStoreError("INVALID_ARGUMENT", "订单数量必须为正整数且不能溢出")
        if sku_id in items:
            if items[sku_id]["product_id"] != product_id:
                raise TradeStoreError("INVALID_ARGUMENT", "相同 SKU 不可对应不同商品")
            quantity += items[sku_id]["quantity"]
            if quantity > _MAX_QUANTITY:
                raise TradeStoreError("INVALID_ARGUMENT", "订单数量不能溢出")
        items[sku_id] = {"product_id": product_id, "sku_id": sku_id, "quantity": quantity}
    return {"items": sorted(items.values(), key=lambda line: (line["product_id"], line["sku_id"])),
            "shipping_address": normalized_address}


def prepare_request_hash(action: str, payload: dict) -> str:
    normalized = {"request_version": PREPARE_REQUEST_VERSION, "action": action,
                  "payload": normalize_prepare_request(action, payload)}
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class TradeStore(ABC):
    @abstractmethod
    async def initialize_inventory(self, products: list[Product]) -> None:
        """绑定完整当前目录；拒绝身份换绑，停用移出项，绝不重置已有库存。"""

    @abstractmethod
    async def get_inventory(self, sku_ids: list[str] | None = None) -> dict[str, int]: ...

    async def get_catalog_bindings(self, sku_ids: list[str] | None = None) -> dict[str, dict]:
        """当前目录的服务端身份版本；不得用当前值替旧历史来源补签。"""
        raise NotImplementedError

    @abstractmethod
    async def find_prepare_request(
        self, *, buyer_id: str, session_id: str, action: Literal["create", "cancel"],
        request_id: str, request_hash: str, request_version: int = PREPARE_REQUEST_VERSION,
    ) -> dict | None:
        """报价前查已提交请求；相同键不同请求必须报 PREPARE_REQUEST_CONFLICT。"""

    @abstractmethod
    async def prepare_confirmation(
        self, *, operation_id: str, buyer_id: str, session_id: str,
        action: Literal["create", "cancel"], payload: dict, expires_at: datetime,
        request_id: str | None = None,
    ) -> dict: ...

    @abstractmethod
    async def get_confirmation(
        self, confirmation_id: str, *, buyer_id: str, session_id: str,
    ) -> dict: ...

    @abstractmethod
    async def list_confirmations(
        self, *, buyer_id: str, session_id: str, limit: int = 20,
    ) -> list[dict]: ...

    @abstractmethod
    async def resolve_confirmation(
        self, confirmation_id: str, *, buyer_id: str, session_id: str,
        snapshot_hash: str, approved: bool,
    ) -> dict: ...

    @abstractmethod
    async def get_order(self, order_id: str, *, buyer_id: str) -> dict: ...

    async def list_orders(self, *, buyer_id: str, status: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        """仅列出当前买家订单，分页与状态过滤由存储执行。"""
        raise NotImplementedError
