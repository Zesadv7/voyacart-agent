"""只读目录迁移预检：不更新数据库、不重新编号、不删除索引或数据卷。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from app.infrastructure.persistence.catalog_identity import CATALOG_IDENTITY_VERSION, catalog_bindings, compare_catalogs
from app.infrastructure.persistence.seed_products import build_seed_products


def inspect_database(path: Path, source_bindings: dict, target_bindings: dict) -> dict:
    path = path.resolve(strict=True)
    # mode=ro 不会把不存在的文件建成一个“干净数据库”；输出不包含买家、地址、订单正文。
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in ("orders", "order_items", "trade_sku_inventory", "trade_confirmations", "trade_catalog_skus") if name in tables}
        registrations = list(connection.execute("SELECT sku_id, identity_hash, product_id, product_identity_hash FROM trade_catalog_skus")) if "trade_catalog_skus" in tables else []
        known = {row[0]: (row[2], row[1]) for row in registrations}
        known_products = {row[2]: row[3] for row in registrations}
        target_products = {row["product_id"]: row["product_identity_hash"] for row in target_bindings.values()}
        product_conflicts = sorted(pid for pid in known_products.keys() & target_products.keys()
            if known_products[pid] != target_products[pid])
        versions = sorted({row[0] for row in connection.execute("SELECT DISTINCT identity_version FROM trade_catalog_skus")}) if "trade_catalog_skus" in tables else []
        referenced = {row[0] for name in ("trade_sku_inventory", "order_items") if name in tables
            for row in connection.execute(f'SELECT DISTINCT sku_id FROM "{name}"')}
        unknown = sorted(referenced - known.keys())
        source_conflicts = sorted(sku for sku in known.keys() & source_bindings.keys()
            if known[sku] != (source_bindings[sku]["product_id"], source_bindings[sku]["identity_hash"]))
        target_conflicts = sorted(sku for sku in known.keys() & target_bindings.keys()
            if known[sku] != (target_bindings[sku]["product_id"], target_bindings[sku]["identity_hash"]))
        unverified = bool(unknown or source_conflicts
            or any(version != CATALOG_IDENTITY_VERSION for version in versions)
            or (not known and (counts.get("trade_confirmations", 0) or counts.get("orders", 0))))
        return {"row_counts": counts, "identity_versions": versions, "unregistered_historical_skus": unknown,
            "source_registry_conflicts": source_conflicts, "target_registry_conflicts": target_conflicts,
            "target_product_registry_conflicts": product_conflicts,
            "legacy_identity_unverified": unverified, "write_performed": False,
            "source_file_alone_proves_legacy_identity": False}


def preflight(source: Path, target: Path, database: Path | None = None) -> dict:
    before, after = build_seed_products(source), build_seed_products(target)
    report = {"schema_version": "catalog-preflight-v1", "mode": "read_only", "model_calls": 0,
        "catalogs": {name: {"file": str(path), "blake2b": hashlib.blake2b(path.read_bytes()).hexdigest()}
            for name, path in (("source", source), ("target", target))}, **compare_catalogs(before, after)}
    if database is not None:
        report["database"] = inspect_database(database, catalog_bindings(before), catalog_bindings(after))
    blocked = not report["safe_identity_update"] or any(report.get("database", {}).get(name)
        for name in ("legacy_identity_unverified", "target_registry_conflicts", "target_product_registry_conflicts"))
    report["status"] = "blocked" if blocked else "identity_compatible_preflight_only"
    report["migration_executed"] = False
    report["limits"] = ["只验证所选数据库和目录；不自动迁移会话、收藏或候选引用", "兼容预检不授权应用迁移，未核验来源的旧编号不能自动登记"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-catalog", type=Path, required=True)
    parser.add_argument("--target-catalog", type=Path, required=True)
    parser.add_argument("--database", type=Path, help="可选的已有 SQLite，仅 mode=ro 读取")
    parser.add_argument("--output", type=Path, help="新建报告路径，不可覆盖历史证据")
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error("报告已存在，请使用新路径")
    result = preflight(args.source_catalog, args.target_catalog, args.database)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered)
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
