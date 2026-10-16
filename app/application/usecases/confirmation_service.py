# -*- coding: utf-8 -*-
"""权威交易确认：工具准备快照，用户 HTTP 决议才可进入原子交易底座。"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Literal

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.order.address import Address
from app.domain.order.ports.trade_store import normalize_prepare_request, prepare_request_hash
from app.application.usecases.order_usecases import OrderItemInput

if TYPE_CHECKING:
    from app.domain.order.ports.trade_store import TradeStore
    from app.infrastructure.eventbus import TradeEventBus

logger = logging.getLogger(__name__)


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    return value


class ConfirmationService:
    def __init__(
        self, product_repo: ProductRepository, trade_store: TradeStore, bus: TradeEventBus | None = None,
        ttl_seconds: int = 300, *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("确认有效期必须为正整数秒")
        self._product_repo = product_repo
        self._trade_store = trade_store
        self._bus = bus
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _identity(buyer_id: str, session_id: str) -> None:
        _required_text(buyer_id, "买家身份")
        _required_text(session_id, "会话标识")

    @staticmethod
    def _envelope(confirmation: dict) -> dict:
        result = {"confirmation_required": confirmation["status"] == "pending", "confirmation": confirmation}
        if confirmation.get("result") is not None:
            result["order"] = confirmation["result"]
        return result

    def _publish(self, session_id: str, event_type: str, confirmation: dict) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(session_id, event_type, {"confirmation": confirmation})
        except Exception as err:  # noqa: BLE001
            # 记录已持久化；事件失败不能把已完成事务误报成失败，HTTP/刷新仍可恢复。
            logger.warning("确认事件投递失败（可从持久存储恢复）：%s", type(err).__name__)

    async def _prepare_replay(self, buyer_id: str, session_id: str, action: Literal["create", "cancel"],
                              request_id: str | None, payload: dict) -> dict | None:
        if request_id is None:
            return None
        return await self._trade_store.find_prepare_request(
            buyer_id=buyer_id, session_id=session_id, action=action, request_id=request_id,
            request_hash=prepare_request_hash(action, payload),
        )

    async def prepare_order(
        self, buyer_id: str, session_id: str, items: list[OrderItemInput], shipping_address: Address,
        *, request_id: str | None = None,
    ) -> dict:
        self._identity(buyer_id, session_id)
        if not isinstance(items, list) or not items:
            raise ValueError("PlaceOrder.items 不能为空")
        if not isinstance(shipping_address, Address):
            raise ValueError("收货地址格式无效")
        if any(not isinstance(item, OrderItemInput) for item in items):
            raise ValueError("订单明细必须为 OrderItemInput")
        request = normalize_prepare_request("create", {
            "items": [asdict(item) for item in items], "shipping_address": asdict(shipping_address),
        })
        replay = await self._prepare_replay(buyer_id, session_id, "create", request_id, request)
        if replay is not None:
            return self._envelope(replay)
        try:
            inventory = await self._trade_store.get_inventory([item["sku_id"] for item in request["items"]])
            lines = []
            for item in request["items"]:
                product_id, sku_id, quantity = item["product_id"], item["sku_id"], item["quantity"]
                product = await self._product_repo.find_by_id(product_id)
                if product is None:
                    raise ValueError(f"商品不存在：{product_id}")
                sku = product.find_sku(sku_id)
                if sku is None:
                    raise ValueError(f"Sku 不存在：{product_id}/{sku_id}")
                if request["shipping_address"]["country"] not in product.ships_to:
                    raise ValueError(f"商品暂不支持配送至 {request['shipping_address']['country']}：{product.title}")
                if inventory.get(sku_id, 0) < quantity:
                    raise ValueError(f"库存不足：{sku_id}，剩余 {inventory.get(sku_id, 0)}，需要 {quantity}")
                lines.append({**item, "title": f"{product.title}（{sku.spec}）",
                    "unit_price_minor": sku.price.amount_in_minor_units, "currency": sku.price.currency})
            operation_id = f"operation-{uuid.uuid4().hex}"
            confirmation = await self._trade_store.prepare_confirmation(
                operation_id=operation_id, buyer_id=buyer_id, session_id=session_id,
                action="create", payload={"items": lines, "shipping_address": request["shipping_address"],
                    "amount_scope": "merchandise_only", "order_kind": "purchase_intent"},
                expires_at=self._clock() + self._ttl, request_id=request_id,
            )
        except Exception:  # noqa: BLE001
            # 并发准备可能已提交，之后的库存变化或丢失响应不能掩盖可恢复的原确认。
            replay = await self._prepare_replay(buyer_id, session_id, "create", request_id, request)
            if replay is not None:
                return self._envelope(replay)
            raise
        # 事务复查可能返回另一并发请求的确认；只由真正创建者投递准备事件。
        if confirmation["operation_id"] == operation_id:
            self._publish(session_id, "confirmation.required", confirmation)
        return self._envelope(confirmation)

    async def prepare_cancel(
        self, buyer_id: str, session_id: str, order_id: str, reason: str,
        *, request_id: str | None = None,
    ) -> dict:
        self._identity(buyer_id, session_id)
        request = normalize_prepare_request("cancel", {"order_id": order_id, "reason": reason})
        replay = await self._prepare_replay(buyer_id, session_id, "cancel", request_id, request)
        if replay is not None:
            return self._envelope(replay)
        try:
            await self._trade_store.get_order(request["order_id"], buyer_id=buyer_id)
            operation_id = f"operation-{uuid.uuid4().hex}"
            confirmation = await self._trade_store.prepare_confirmation(
                operation_id=operation_id, buyer_id=buyer_id, session_id=session_id,
                action="cancel", payload={**request,
                    "amount_scope": "merchandise_only", "order_kind": "purchase_intent"},
                expires_at=self._clock() + self._ttl, request_id=request_id,
            )
        except Exception:  # noqa: BLE001
            replay = await self._prepare_replay(buyer_id, session_id, "cancel", request_id, request)
            if replay is not None:
                return self._envelope(replay)
            raise
        # 事务复查可能返回另一并发请求的确认；只由真正创建者投递准备事件。
        if confirmation["operation_id"] == operation_id:
            self._publish(session_id, "confirmation.required", confirmation)
        return self._envelope(confirmation)

    async def resolve(
        self, confirmation_id: str, buyer_id: str, session_id: str, snapshot_hash: str, approved: bool,
    ) -> dict:
        """仅供用户动作入口调用；不注册为模型工具。底座原子校验、幂等与扣补库存。"""
        self._identity(buyer_id, session_id)
        _required_text(confirmation_id, "确认凭证")
        _required_text(snapshot_hash, "快照校验值")
        if type(approved) is not bool:
            raise ValueError("确认决定必须为布尔值，不能使用模型文字或字符串")
        confirmation = await self._trade_store.resolve_confirmation(
            confirmation_id, buyer_id=buyer_id, session_id=session_id,
            snapshot_hash=snapshot_hash, approved=approved,
        )
        self._publish(session_id, "confirmation.resolved", confirmation)
        return self._envelope(confirmation)

    async def get(self, confirmation_id: str, buyer_id: str, session_id: str) -> dict:
        self._identity(buyer_id, session_id)
        _required_text(confirmation_id, "确认凭证")
        confirmation = await self._trade_store.get_confirmation(
            confirmation_id, buyer_id=buyer_id, session_id=session_id,
        )
        return self._envelope(confirmation)

    async def list(self, buyer_id: str, session_id: str, limit: int = 20) -> dict:
        self._identity(buyer_id, session_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("确认列表数量必须为 1 至 20 的整数")
        return {"confirmations": await self._trade_store.list_confirmations(
            buyer_id=buyer_id, session_id=session_id, limit=limit,
        )}

    async def agent_state(self, buyer_id: str, session_id: str) -> dict:
        """向模型提供精简的权威交易状态，不重复注入收货地址或确认校验值。"""
        self._identity(buyer_id, session_id)
        confirmations = await self._trade_store.list_confirmations(buyer_id=buyer_id, session_id=session_id)
        order_ids = list(dict.fromkeys(
            item["result"]["order_id"] for item in confirmations if item.get("result")
        ))
        orders = [await self._trade_store.get_order(order_id, buyer_id=buyer_id) for order_id in order_ids]
        return {
            "pending_confirmations": [{
                "action": item["action"], "expired": item["expired"],
                "items": [{"product_id": line["product_id"], "sku_id": line["sku_id"], "quantity": line["quantity"]}
                          for line in item["payload"]["items"]],
                "order_id": item["payload"].get("order_id"),
            } for item in confirmations if item["status"] == "pending" and not item["expired"]],
            "orders": [{key: order.get(key) for key in (
                "order_id", "status", "currency", "total_amount_major", "lines", "cancel_reason",
            )} for order in orders],
        }
