import json

import pytest

from scripts.esci.finalize_catalog import selected_inputs
from scripts.esci.prepare import json_line
from scripts.esci.proxy_categories import VERSION, proxy_rows


def write(path, rows):
    path.write_text(''.join(json_line(row) for row in rows))


def fixture(tmp_path, extra_product=False):
    queries = [{'locale': 'us', 'query_id': 1, 'category': '厨房餐饮', 'status': 'ok'},
               {'locale': 'us', 'query_id': 2, 'category': '家居生活', 'status': 'ok'},
               {'locale': 'us', 'query_id': 3, 'category': '家居生活', 'status': 'ok'}]
    write(tmp_path/'query-class.jsonl', queries)
    (tmp_path/'selection-plan.json').write_text(json.dumps({'queries': {'dev': queries}}))
    labels = [(1, 'A', 'E'), (2, 'A', 'I'), (3, 'A', 'S'), (1, 'B', 'E'), (2, 'B', 'E')]
    write(tmp_path/'examples.jsonl', [{'product_locale': 'us', 'query_id': q, 'product_id': asin, 'esci_label': label}
                                      for q, asin, label in labels])
    write(tmp_path/'products.jsonl', [{'product_locale': 'us', 'product_id': asin} for asin in ('A', 'B', 'C')[:3 if extra_product else 2]])


def test_majority_of_linked_query_classes_with_category_order_ties(tmp_path):
    fixture(tmp_path)
    rows = proxy_rows(tmp_path, tmp_path/'query-class.jsonl')
    # A：家居生活 2 票胜出；B：厨房餐饮与家居生活同票，按既有八类顺序（家居生活在前）取家居生活。
    assert [(row['asin'], row['category']) for row in rows] == [('A', '家居生活'), ('B', '家居生活')]
    assert all(row['prompt_version'] == VERSION and row['material_tags'] == [] and row['status'] == 'ok' for row in rows)


def test_products_without_labels_are_rejected(tmp_path):
    fixture(tmp_path, extra_product=True)
    with pytest.raises(ValueError, match='一一覆盖'):
        proxy_rows(tmp_path, tmp_path/'query-class.jsonl')


def test_finalize_rejects_mixed_category_sources(tmp_path):
    rows = [{'locale': 'us', 'asin': 'A', 'prompt_version': VERSION, 'status': 'ok', 'category': '家居生活', 'material_tags': []},
            {'locale': 'us', 'asin': 'B', 'prompt_version': 'product-enrich-v1', 'status': 'ok', 'category': '家居生活', 'material_tags': []}]
    write(tmp_path/'enrichment.jsonl', rows)
    write(tmp_path/'products.jsonl', [{'product_locale': 'us', 'product_id': asin} for asin in ('A', 'B')])
    write(tmp_path/'examples.jsonl', [])
    from scripts.esci.build_catalog import TARGETS
    from scripts.esci.catalog_records import SEED
    (tmp_path/'selection-plan.json').write_text(json.dumps({'seed': SEED, 'targets': TARGETS, 'queries': {}}))
    with pytest.raises(ValueError, match='一种类目来源'):
        selected_inputs(tmp_path, tmp_path/'enrichment.jsonl')
