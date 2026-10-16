# -*- coding: utf-8 -*-
"""持久候选：公开 run 来源、集合版本和成功请求在一个 SQLite 事务中提交。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from typing import Any
from uuid import uuid4

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalForbidden, JournalNotFound
from app.infrastructure.persistence.catalog_identity import catalog_bindings
from app.infrastructure.security.output_guard import audit_output
from app.infrastructure.security.public_output import public_products

CANDIDATE_LIMIT = 30
COMPARISON_LIMIT = 3
_MAX_REVISION = 2**53 - 1
_PRODUCT_CARD_FIELDS = frozenset("product_id title brand category origin_country price_major currency source_price_major source_currency highlights skus score landed_price source_platform canonical_product_id material_tags weight_kg description rating_summary rating_is_live ships_to dimensions_cm updated_at default_sku_id image_url image_kind image_alt source_language source_locale data_provenance external_product_id".split())


class CandidateWorkspaceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409, *, current_revision: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.current_revision = current_revision


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip()
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or not audit_output(value, redact_tools=False)[0]):
        raise CandidateWorkspaceError("INVALID_CANDIDATE_REQUEST", "候选引用格式不正确", 422)
    return value


def _finite(value: Any, *, minimum: float | None = 0) -> bool:
    try:
        return (type(value) in {int, float} and math.isfinite(value)
                and (minimum is None or value >= minimum))
    except OverflowError:
        return False


def _currency(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) is not None


def _text(value: Any, *, maximum: int = 4096, required: bool = False) -> bool:
    return (isinstance(value, str) and len(value) <= maximum and (not required or bool(value.strip()))
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def _strings(value: Any, *, maximum: int = 100) -> bool:
    return isinstance(value, list) and len(value) <= maximum and all(_text(item) for item in value)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _invalid_source() -> CandidateWorkspaceError:
    return CandidateWorkspaceError("SOURCE_NOT_READY", "该来源尚无完整可保留的商品，请重新查询")


def _snapshot(raw: Any, sku_id: str) -> dict:
    """再次执行公开白名单；拒绝不完整小卡，不猜测价格、默认 SKU 或库存。"""
    card = {key: value for key, value in public_products([raw])[0].items() if key in _PRODUCT_CARD_FIELDS}
    for key in ("product_id", "title", "brand", "category", "origin_country", "source_platform", "canonical_product_id"):
        if not _text(card.get(key), required=key in {"product_id", "title"}):
            raise _invalid_source()
    for key in ("price_major", "source_price_major", "weight_kg"):
        if not _finite(card.get(key)):
            raise _invalid_source()
    if (not _finite(card.get("score"), minimum=None) or not _currency(card.get("currency"))
            or not _currency(card.get("source_currency"))
            or not _strings(card.get("highlights")) or not _strings(card.get("material_tags"))):
        raise _invalid_source()
    skus = card.get("skus")
    if not isinstance(skus, list) or not 1 <= len(skus) <= 1000:
        raise _invalid_source()
    seen = set()
    for sku in skus:
        try:
            selected_id = _id(sku.get("sku_id"))
        except CandidateWorkspaceError as exc:
            raise _invalid_source() from exc
        if (selected_id in seen or not _text(sku.get("spec"), maximum=1024)
                or not _finite(sku.get("price_major")) or not _currency(sku.get("currency"))
                or type(sku.get("stock")) is not int or not 0 <= sku["stock"] <= _MAX_REVISION):
            raise _invalid_source()
        seen.add(selected_id)
    if not isinstance(card.get("default_sku_id"), str) or card["default_sku_id"] not in seen:
        raise _invalid_source()
    if sku_id not in seen:
        raise CandidateWorkspaceError("INVALID_CANDIDATE_SOURCE", "所选规格不属于该来源商品", 422)
    for key in ("description", "updated_at", "image_alt", "source_locale", "source_language", "external_product_id"):
        if key in card and not _text(card[key], maximum=16000 if key == "description" else 4096):
            raise _invalid_source()
    if not _strings(card.get("ships_to")):
        raise _invalid_source()
    if "rating_is_live" in card and type(card["rating_is_live"]) is not bool:
        raise _invalid_source()
    if "image_kind" in card and (not isinstance(card["image_kind"], str)
            or card["image_kind"] not in {"illustration", "placeholder"}):
        raise _invalid_source()
    if card.get("image_url") is not None and not isinstance(card["image_url"], str):
        raise _invalid_source()
    card["skus"] = [{key: sku[key] for key in ("sku_id", "spec", "price_major", "currency", "stock")} for sku in skus]
    # 可选嵌套字段只保留其展示合同，不能让未知元数据旁路白名单。
    dimensions = card.get("dimensions_cm")
    if dimensions is not None:
        if not isinstance(dimensions, dict):
            raise _invalid_source()
        card["dimensions_cm"] = {key: dimensions[key] for key in ("length", "width", "height") if key in dimensions}
        if not all(_finite(value) for value in card["dimensions_cm"].values()):
            raise _invalid_source()
    rating = card.get("rating_summary")
    if rating is not None:
        if (not isinstance(rating, dict) or not _finite(rating.get("average")) or rating["average"] > 5
                or type(rating.get("review_count")) is not int or rating["review_count"] < 0):
            raise _invalid_source()
        card["rating_summary"] = {key: rating[key] for key in ("average", "review_count")}
    landed = card.get("landed_price")
    if landed is not None:
        if not isinstance(landed, dict):
            raise _invalid_source()
        if landed.get("unavailable_reason"):
            if not _text(landed["unavailable_reason"], required=True):
                raise _invalid_source()
            card["landed_price"] = {"unavailable_reason": landed["unavailable_reason"]}
        else:
            amounts = ("subtotal_major", "freight_major", "tariff_major", "tariff_rate", "landed_total_major")
            if (not all(_finite(landed.get(key)) for key in amounts)
                    or not _currency(landed.get("currency")) or landed["currency"] != card["currency"]
                    or not _text(landed.get("ship_to"), maximum=2, required=True)
                    or type(landed.get("de_minimis_applied")) is not bool
                    or not math.isclose(landed["subtotal_major"], card["price_major"], rel_tol=1e-9, abs_tol=0.01)
                    or not math.isclose(landed["landed_total_major"], sum(landed[key] for key in
                        ("subtotal_major", "freight_major", "tariff_major")), rel_tol=1e-9, abs_tol=0.01)):
                raise _invalid_source()
            card["landed_price"] = {key: landed[key] for key in (*amounts, "currency", "ship_to", "de_minimis_applied")}
    if sku_id != card["default_sku_id"]:
        selected = next(sku for sku in skus if sku["sku_id"] == sku_id)
        card.update(default_sku_id=sku_id, price_major=selected["price_major"], currency=selected["currency"],
                    source_price_major=selected["price_major"], source_currency=selected["currency"])
        card.pop("landed_price", None)
    # public_products 同时去除未知顶层字段；有限值和大小检查覆盖其剩余可选字段。
    def bounded(value: Any, depth: int = 0) -> bool:
        if depth > 12:
            return False
        if isinstance(value, str):
            return _text(value, maximum=16000)
        if type(value) in {int, float}:
            return _finite(value, minimum=None)
        if isinstance(value, list):
            return len(value) <= 1000 and all(bounded(item, depth + 1) for item in value)
        if isinstance(value, dict):
            return len(value) <= 100 and all(bounded(item, depth + 1) for item in value.values())
        return value is None or type(value) is bool
    if not bounded(card) or len(_json(card).encode()) > 262144:
        raise _invalid_source()
    return card


class CandidateWorkspaceStore:
    def __init__(self, journal: AGUIJournal):
        self.journal = journal
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self):
        async with self._init_lock:
            if self._initialized:
                return
            async with self.journal._db(write=True) as db:
                # execute 而不是 executescript：后者会隐式提交 BEGIN，破坏同库事务。
                for sql in (
                    """CREATE TABLE IF NOT EXISTS candidate_workspaces (
                        session_id TEXT PRIMARY KEY CHECK(length(session_id)>0),
                        buyer_id TEXT NOT NULL CHECK(length(buyer_id)>0),
                        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
                        comparison_json TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS session_candidates (
                        candidate_id TEXT PRIMARY KEY CHECK(length(candidate_id)>0),
                        session_id TEXT NOT NULL CHECK(length(session_id)>0),
                        buyer_id TEXT NOT NULL CHECK(length(buyer_id)>0),
                        product_id TEXT NOT NULL CHECK(length(product_id)>0),
                        sku_id TEXT NOT NULL CHECK(length(sku_id)>0),
                        source_json TEXT NOT NULL, identity_json TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL, saved_at REAL NOT NULL,
                        UNIQUE(session_id, product_id, sku_id))""",
                    "CREATE INDEX IF NOT EXISTS candidate_session ON session_candidates(session_id,buyer_id,saved_at)",
                    """CREATE TABLE IF NOT EXISTS candidate_requests (
                        session_id TEXT NOT NULL CHECK(length(session_id)>0),
                        buyer_id TEXT NOT NULL CHECK(length(buyer_id)>0),
                        request_id TEXT NOT NULL CHECK(length(request_id)>0),
                        request_hash TEXT NOT NULL, action TEXT NOT NULL,
                        result_json TEXT NOT NULL, created_at REAL NOT NULL,
                        PRIMARY KEY(session_id,buyer_id,request_id))""",
                ):
                    await db.execute(sql)
            self._initialized = True

    async def _session(self, db, buyer_id: str, session_id: str):
        row = await self.journal._one(db, "SELECT * FROM agui_sessions WHERE session_id=?", (session_id,))
        self._owned(row, buyer_id, session_id)
        workspace = await self.journal._one(db, "SELECT * FROM candidate_workspaces WHERE session_id=?", (session_id,))
        if workspace is not None:
            self._owned(workspace, buyer_id, session_id)
        return workspace

    def _owned(self, row, buyer_id, session_id):
        try:
            return self.journal._owned(row, buyer_id, session_id)
        except JournalNotFound as exc:
            raise CandidateWorkspaceError("CANDIDATE_NOT_FOUND", "会话、来源或候选不存在", 404) from exc
        except JournalForbidden as exc:
            raise CandidateWorkspaceError("CANDIDATE_FORBIDDEN", "无权访问该会话、来源或候选", 403) from exc

    @staticmethod
    def _item(row) -> dict:
        binding = json.loads(row["identity_json"])
        return {"candidate_id": row["candidate_id"], "product_id": row["product_id"], "sku_id": row["sku_id"],
                "saved_at": _iso(row["saved_at"]), "source": json.loads(row["source_json"]),
                "identity_status": "bound" if binding else "unknown", "quote_status": "historical",
                "snapshot": public_products([json.loads(row["snapshot_json"])])[0]}

    async def _view(self, db, buyer_id: str, session_id: str, workspace=None) -> dict:
        if workspace is None:
            workspace = await self.journal._one(db, "SELECT * FROM candidate_workspaces WHERE session_id=?", (session_id,))
        async with db.execute("SELECT * FROM session_candidates WHERE session_id=? AND buyer_id=? ORDER BY saved_at,rowid",
                              (session_id, buyer_id)) as cursor:
            items = [self._item(row) for row in await cursor.fetchall()]
        return {"schema_version": 1, "buyer_id": buyer_id, "session_id": session_id,
                "revision": workspace["revision"] if workspace else 0, "items": items,
                "comparison_ids": json.loads(workspace["comparison_json"]) if workspace else [],
                "limits": {"candidates": CANDIDATE_LIMIT, "comparison": COMPARISON_LIMIT}}

    async def list(self, buyer_id: str, session_id: str) -> dict:
        _id(buyer_id), _id(session_id)
        await self.initialize()
        async with self.journal._db() as db:
            workspace = await self._session(db, buyer_id, session_id)
            return await self._view(db, buyer_id, session_id, workspace)

    async def get_candidate(self, buyer_id: str, session_id: str, candidate_id: str) -> dict:
        """仅供应用服务核验使用，catalog_binding 不能直接作为 HTTP 响应。"""
        _id(buyer_id), _id(session_id), _id(candidate_id)
        await self.initialize()
        async with self.journal._db() as db:
            workspace = await self._session(db, buyer_id, session_id)
            row = await self.journal._one(db, "SELECT * FROM session_candidates WHERE candidate_id=?", (candidate_id,))
            self._owned(row, buyer_id, session_id)
            return {**self._item(row), "workspace_revision": workspace["revision"],
                    "catalog_binding": json.loads(row["identity_json"]) or None}

    async def add(self, buyer_id: str, session_id: str, request_id: str, expected_revision: int, source: dict) -> dict:
        if not isinstance(source, dict) or set(source) != {"kind", "run_id", "product_id", "sku_id"} or source["kind"] != "run":
            raise CandidateWorkspaceError("INVALID_CANDIDATE_SOURCE", "请使用明确的运行、商品和规格来源", 422)
        for key in ("run_id", "product_id", "sku_id"):
            _id(source[key])
        return await self._mutate(buyer_id, session_id, request_id, expected_revision, "add", {"source": dict(source)})

    async def remove(self, buyer_id: str, session_id: str, request_id: str, expected_revision: int, candidate_id: str) -> dict:
        return await self._mutate(buyer_id, session_id, request_id, expected_revision, "remove", {"candidate_id": _id(candidate_id)})

    async def compare(self, buyer_id: str, session_id: str, request_id: str, expected_revision: int, candidate_ids: list[str]) -> dict:
        if not isinstance(candidate_ids, list):
            raise CandidateWorkspaceError("INVALID_CANDIDATE_REQUEST", "比较项必须为候选编号列表", 422)
        ids = [_id(item) for item in candidate_ids]
        if len(ids) > COMPARISON_LIMIT:
            raise CandidateWorkspaceError("COMPARISON_LIMIT", "最多比较三个候选")
        if len(ids) != len(set(ids)):
            raise CandidateWorkspaceError("INVALID_CANDIDATE_REQUEST", "比较项不能重复", 422)
        return await self._mutate(buyer_id, session_id, request_id, expected_revision, "compare", {"candidate_ids": ids})

    async def _source(self, db, buyer_id: str, session_id: str, source: dict):
        run = await self.journal._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (source["run_id"],))
        self._owned(run, buyer_id, session_id)
        try:
            projection = json.loads(run["projection_json"])
            products = projection.get("state", {}).get("products")
        except (ValueError, AttributeError, TypeError) as exc:
            raise _invalid_source() from exc
        if not isinstance(products, list):
            raise _invalid_source()
        matches = [item for item in products if isinstance(item, dict) and item.get("product_id") == source["product_id"]]
        if len(matches) != 1:
            raise CandidateWorkspaceError("SOURCE_CHANGED", "该运行的商品已变化，请重新查看结果")
        snapshot = _snapshot(matches[0], source["sku_id"])
        binding = None
        catalog = await self.journal._one(db, """SELECT c.snapshot_id,s.bindings_json FROM agui_run_catalog c
            LEFT JOIN agui_catalog_snapshots s ON s.snapshot_id=c.snapshot_id WHERE c.run_id=?""", (source["run_id"],))
        if catalog is not None:
            try:
                bindings = json.loads(catalog["bindings_json"])
                binding = bindings[source["sku_id"]]
                valid = (binding["product_id"] == source["product_id"] and type(binding["version"]) is int
                         and binding["version"] == 1 and binding["active"] is True
                         and type(binding["generation"]) is int and binding["generation"] >= 1
                         and all(isinstance(binding[key], str) and re.fullmatch(r"[0-9a-f]{64}", binding[key])
                                 for key in ("identity_hash", "terms_hash")))
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                raise CandidateWorkspaceError("SOURCE_CHANGED", "来源目录身份不完整或已变化，请重新查询")
            binding = {key: binding[key] for key in ("product_id", "version", "identity_hash", "terms_hash", "generation", "active")}
            # resume 可能继承上一轮商品。只有卡片本身的身份/条款吻合才可绑定，
            # 不能将本轮目录身份补签给旧卡；这里的零金额从不用于报价或返回。
            selected = next(sku for sku in snapshot["skus"] if sku["sku_id"] == source["sku_id"])
            product = Product(product_id=snapshot["product_id"], title=snapshot["title"],
                brand=snapshot["brand"], category=snapshot["category"], origin_country=snapshot["origin_country"],
                description="", ships_to=snapshot["ships_to"],
                source_platform=snapshot["source_platform"], source_locale=snapshot.get("source_locale", ""),
                external_product_id=snapshot.get("external_product_id", ""),
                canonical_product_id=snapshot["canonical_product_id"],
                skus=[Sku(source["sku_id"], selected["spec"], Money.of(0, "CNY"), 0)])
            projected = catalog_bindings([product])[source["sku_id"]]
            if any(projected[key] != binding[key] for key in ("identity_hash", "terms_hash")):
                binding = None
        if (type(run["last_seq"]) is not int or run["last_seq"] < 0
                or not _finite(run["updated_at"]) or run["updated_at"] >= 253402300800):
            raise _invalid_source()
        origin = {"kind": "run", "run_id": run["run_id"], "captured_cursor": run["last_seq"],
                  "captured_at": _iso(run["updated_at"])}
        return snapshot, binding, origin

    async def _mutate(self, buyer_id, session_id, request_id, expected_revision, action, payload):
        _id(buyer_id), _id(session_id), _id(request_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < _MAX_REVISION:
            raise CandidateWorkspaceError("INVALID_CANDIDATE_REQUEST", "候选版本必须为非负整数", 422)
        request_hash = hashlib.sha256(_json({"version": 1, "buyer_id": buyer_id, "session_id": session_id,
            "action": action, "expected_revision": expected_revision, **payload}).encode()).hexdigest()
        await self.initialize()
        async with self.journal._db(write=True) as db:
            workspace = await self._session(db, buyer_id, session_id)
            previous = await self.journal._one(db, "SELECT * FROM candidate_requests WHERE session_id=? AND buyer_id=? AND request_id=?",
                                               (session_id, buyer_id, request_id))
            # 只重放成功事务；先核对 owner，再重放，最后才检查来源及旧版本。
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise CandidateWorkspaceError("CANDIDATE_REQUEST_CONFLICT", "该请求编号已用于其他候选操作")
                return json.loads(previous["result_json"])
            revision = workspace["revision"] if workspace else 0
            if revision != expected_revision:
                raise CandidateWorkspaceError("CANDIDATE_REVISION_CONFLICT", "另一页面已修改候选，请刷新后核对", current_revision=revision)
            comparison = json.loads(workspace["comparison_json"]) if workspace else []
            changed = False
            now = time.time()
            if action == "add":
                source = payload["source"]
                snapshot, binding, origin = await self._source(db, buyer_id, session_id, source)
                duplicate = await self.journal._one(db, "SELECT * FROM session_candidates WHERE session_id=? AND product_id=? AND sku_id=?",
                                                   (session_id, source["product_id"], source["sku_id"]))
                if duplicate is not None:
                    self._owned(duplicate, buyer_id, session_id)
                else:
                    count = await self.journal._one(db, "SELECT count(*) AS n FROM session_candidates WHERE session_id=?", (session_id,))
                    if count["n"] >= CANDIDATE_LIMIT:
                        raise CandidateWorkspaceError("CANDIDATE_LIMIT", "每个会话最多保留三十个候选")
                    await db.execute("INSERT INTO session_candidates VALUES(?,?,?,?,?,?,?,?,?)",
                        (str(uuid4()), session_id, buyer_id, source["product_id"], source["sku_id"],
                         _json(origin), _json(binding), _json(snapshot), now))
                    changed = True
            elif action == "remove":
                candidate_id = payload["candidate_id"]
                item = await self.journal._one(db, "SELECT * FROM session_candidates WHERE candidate_id=?", (candidate_id,))
                self._owned(item, buyer_id, session_id)
                await db.execute("DELETE FROM session_candidates WHERE candidate_id=? AND session_id=? AND buyer_id=?",
                                 (candidate_id, session_id, buyer_id))
                comparison = [item for item in comparison if item != candidate_id]
                changed = True
            elif action == "compare":
                for candidate_id in payload["candidate_ids"]:
                    item = await self.journal._one(db, "SELECT * FROM session_candidates WHERE candidate_id=?", (candidate_id,))
                    self._owned(item, buyer_id, session_id)
                changed = comparison != payload["candidate_ids"]
                comparison = payload["candidate_ids"]
            revision += int(changed)
            await db.execute("""INSERT INTO candidate_workspaces VALUES(?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,
                comparison_json=excluded.comparison_json,updated_at=excluded.updated_at""",
                (session_id, buyer_id, revision, _json(comparison), now if changed or not workspace else workspace["updated_at"]))
            result = await self._view(db, buyer_id, session_id)
            await self._record_request(db, buyer_id, session_id, request_id, request_hash, action, result, now)
            return result

    async def _record_request(self, db, buyer_id, session_id, request_id, request_hash, action, result, now):
        # 成功响应与候选/版本同提交；不设 TTL，响应丢失后仍可用原键恢复。
        await db.execute("INSERT INTO candidate_requests VALUES(?,?,?,?,?,?,?)",
                         (session_id, buyer_id, request_id, request_hash, action, _json(result), now))
