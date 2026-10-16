"""演示选品遵循 train / E、查询类目众数与全局 ASIN 去重。"""
from scripts.esci.build_demo_catalog import select_products


def test_train_exact_majority_dedup_and_stable_selection():
    products = {(locale, asin): {"product_title": title} for locale, asin, title in (
        ("us", "A", "US original"), ("es", "A", "ES same ASIN"), ("es", "B", "ES original"),
        ("us", "TEST", "holdout only"), ("us", "SUBSTITUTE", "substitute only"), ("us", "EMPTY", ""))}
    queries = {("us", 1): {"query_id": 1, "category": "户外运动", "status": "ok"},
               ("us", 2): {"query_id": 2, "category": "旅行装备", "status": "ok"},
               ("us", 3): {"query_id": 3, "category": "户外运动", "status": "ok"},
               ("es", 4): {"query_id": 4, "category": "家居生活", "status": "ok"}}
    examples = [{"product_locale": locale, "product_id": asin, "query_id": query_id,
                 "split": split, "esci_label": label} for locale, asin, query_id, split, label in (
        ("us", "A", 1, "train", "E"), ("us", "A", 2, "train", "E"), ("us", "A", 3, "train", "E"),
        ("us", "A", 2, "train", "E"), ("es", "A", 4, "train", "E"), ("es", "B", 4, "train", "E"),
        ("us", "TEST", 2, "test", "E"), ("us", "SUBSTITUTE", 2, "train", "S"),
        ("us", "EMPTY", 2, "train", "E"))]
    selected = select_products(products, examples, queries, {"us": 1, "es": 1})
    assert selected == select_products(products, reversed(examples), queries, {"us": 1, "es": 1})
    assert [(item["source"], item["category"]) for item in selected] == [
        ({"product_title": "US original"}, "户外运动"), ({"product_title": "ES original"}, "家居生活")]
    assert len(selected[0]["queries"]) == 3
