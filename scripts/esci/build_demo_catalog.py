"""从已有 train / E 商品生成 2,000 件 Amazon 演示目录，零模型调用。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import random

from scripts.esci.catalog_records import catalog_record
from scripts.esci.download import ROOT, fingerprint
from scripts.esci.prepare import CATEGORIES, SEED, json_line

LOCALE_COUNTS = {"us": 1000, "es": 500, "jp": 500}
PREPARED = ROOT / "eval/verification/2026-10-14-t2-input-plan"


def select_products(products: dict, examples, queries: dict, targets: dict) -> list[dict]:
    """类目取 train / E 关联查询众数，同票按既有八类顺序；ASIN 全局去重。"""
    references = defaultdict(dict)
    for example in examples:
        if example["split"] != "train" or example["esci_label"] != "E":
            continue
        locale, asin = example["product_locale"], example["product_id"]
        query = queries.get((locale, example["query_id"]))
        if query and query["status"] == "ok" and query["category"] in CATEGORIES:
            product = products[locale, asin]
            if (product["product_title"] or "").strip():
                references[locale, asin][query["query_id"]] = query
    selected = []
    used_asins = set()
    for locale, count in targets.items():
        candidates = sorted(key for key in references if key[0] == locale and key[1] not in used_asins)
        random.Random(f"{SEED}:{locale}").shuffle(candidates)
        if len(candidates) < count:
            raise ValueError(f"train / E 去重商品不足：{locale} 需要 {count}，实际 {len(candidates)}")
        for key in sorted(candidates[:count]):
            votes = Counter(query["category"] for query in references[key].values())
            category = min(votes, key=lambda value: (-votes[value], CATEGORIES.index(value)))
            selected.append({"source": products[key], "category": category,
                             "queries": [references[key][query_id] for query_id in sorted(references[key])]})
            used_asins.add(key[1])
    return selected


def build_demo_catalog(prepared: Path, query_cache: Path, catalog: Path, evidence: Path) -> dict:
    with query_cache.open(encoding="utf-8") as stream:
        queries = {(row["locale"], row["query_id"]): row for line in stream
                   if (row := json.loads(line))["split"] == "train"}
    with (prepared / "products.jsonl").open(encoding="utf-8") as stream:
        products = {(row["product_locale"], row["product_id"]): row for line in stream
                    if (row := json.loads(line))}
    with (prepared / "examples.jsonl").open(encoding="utf-8") as stream:
        selected = select_products(products, (json.loads(line) for line in stream), queries, LOCALE_COUNTS)
    records, selection = [], []
    for number, item in enumerate(selected, 100000):
        source = item["source"]
        record = catalog_record(source, {"category": item["category"], "material_tags": []}, number)
        record["data_provenance"].update(
            category_method="train_exact_query_majority",
            category_status="provisional_unreviewed",
            material_status="unknown",
            origin_country_method="source_locale_proxy",
            source_text_processing="html_to_text; description_max_1000; bullet_max_200_and_5",
        )
        records.append(record)
        selection.append({"product_id": record["product_id"], "locale": source["product_locale"],
                          "asin": source["product_id"], "category": item["category"],
                          "train_exact_queries": [{"query_id": query["query_id"], "query": query["query"],
                                                   "category": query["category"]} for query in item["queries"]]})
    evidence.mkdir(parents=True, exist_ok=False)
    with catalog.open("xb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            compressed.write("".join(json_line(record) for record in records).encode("utf-8"))
    summary = {
        "status": "completed", "seed": SEED, "model_calls": 0,
        "count": len(records), "unique_asins": len({record["external_product_id"] for record in records}),
        "locale_counts": dict(Counter(record["source_locale"] for record in records)),
        "category_counts": dict(Counter(record["category"] for record in records)),
        "category_method": "train_exact_query_majority; ties_follow_existing_category_order",
        "category_status": "provisional_unreviewed", "holdout_labels_used": False,
        "unknown_material_count": len(records),
        "in_stock_cn_count": sum(record["skus"][0]["stock"] > 0 and "CN" in record["ships_to"] for record in records),
        "synthetic_fields": ["skus.price_major", "skus.stock", "ships_to"],
        "source_locale_proxy": ["origin_country"],
        "original_fields": ["title", "brand", "description", "highlights", "skus.spec"],
        "catalog": str(catalog), "catalog_fingerprint": fingerprint(catalog),
        "input_fingerprints": {str(path): fingerprint(path) for path in
                               (prepared / "products.jsonl", prepared / "examples.jsonl", query_cache)},
    }
    (evidence / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (evidence / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, default=PREPARED)
    parser.add_argument("--query-cache", type=Path, default=ROOT / "data/esci/query-class-v1.jsonl")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog-esci-demo-2000.jsonl.gz")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_demo_catalog(args.prepared, args.query_cache, args.catalog, args.evidence), ensure_ascii=False))


if __name__ == "__main__":
    main()
