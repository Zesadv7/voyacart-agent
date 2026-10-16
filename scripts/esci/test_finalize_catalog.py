"""缓存齐全时目录与评测集逐字节一致，标签继承原始人工判定。"""
import csv
import gzip
import json

import pytest

from scripts.esci.build_catalog import TARGETS, preselect
from scripts.esci.catalog_records import SEED
from scripts.esci.finalize_catalog import finalize, load_rows, translation_inputs
from scripts.esci.prepare import json_line
from scripts.esci.test_build_catalog import classified_pool
from scripts.esci.test_catalog_records import product


@pytest.mark.parametrize('separator', ['\u2028', '\u2029', '\u0085'])
def test_jsonl_reading_preserves_unicode_line_separators(tmp_path, separator):
    rows = [{'title': f'原文{separator}标题', 'query': f'中文{separator}查询'}, {'title': '第二件商品'}]
    path = tmp_path / 'products.jsonl'
    path.write_text(json_line(rows[0]) + json.dumps(rows[1], ensure_ascii=False))
    assert load_rows(path) == rows


def test_final_catalog_and_evaluations_are_reproducible(tmp_path):
    rows,cache=classified_pool(); queries=preselect(rows,cache)
    prepared=tmp_path/'prepared';prepared.mkdir()
    (prepared/'selection-plan.json').write_text(json.dumps({'seed':SEED,'targets':TARGETS,'queries':queries}))
    sources=[]; examples=[]; enhanced=[]
    for dataset,group in queries.items():
        for row in group:
            asin=str(row['query_id'])
            sources.append({**product(row['locale'],asin), 'product_title': 'Travel\u2028Backpack\u0085Original\u2029Title'})
            examples.append({'product_locale':row['locale'],'query_id':row['query_id'],'product_id':asin,'esci_label':'E'})
            enhanced.append({'locale':row['locale'],'asin':asin,'prompt_version':'product-enrich-v1',
                'category':row['category'],'material_tags':[]})
    for filename,records in [('products.jsonl',sources),('examples.jsonl',examples),('enriched.jsonl',enhanced)]:
        (prepared/filename).write_text(''.join(json_line(r) for r in records))
    inputs=translation_inputs(prepared,prepared/'enriched.jsonl')
    assert len(inputs)==200
    translated=[{**row,'prompt_version':'translate-zh-v1','translation':'中文查询'+str(row['query_id'])} for row in inputs]
    (prepared/'translations.jsonl').write_text(''.join(json_line(r) for r in translated))
    reports=[]
    for destination in (tmp_path/'first',tmp_path/'second'):
        reports.append(finalize(prepared,prepared/'enriched.jsonl',prepared/'translations.jsonl',destination,'v2'))
    assert reports[0]==reports[1]
    assert reports[0]['queries']==TARGETS
    assert reports[0]['products']==500 and reports[0]['chinese_queries']==200
    first,second=tmp_path/'first',tmp_path/'second'
    for path in first.rglob('*'):
        if path.is_file():assert path.read_bytes()==(second/path.relative_to(first)).read_bytes()
    assert (first/'selection-v2.json').exists()
    with gzip.open(first/'catalog-esci-v2.jsonl.gz', 'rt', encoding='utf-8') as stream:
        products=[json.loads(line) for line in stream]
    assert len(products)==500 and products[0]['product_id']=='P100000'
    assert all(p['title']=='Travel\u2028Backpack\u0085Original\u2029Title' for p in products)
    for dataset,n in (('dev',100),('holdout',300)):
        with (first/'eval'/f'ranking-{dataset}.jsonl').open(encoding='utf-8') as stream:
            ranking=[json.loads(line) for line in stream]
        with (first/'eval'/f'constraints-{dataset}.jsonl').open(encoding='utf-8') as stream:
            constraints=[json.loads(line) for line in stream]
        assert len(ranking)==n and all(c['label']=='E' for row in ranking for c in row['candidates'])
        assert sum(row['expected_empty'] for row in constraints)==n//10
        assert all(row['relevant_product_ids']==[] for row in constraints if row['expected_empty'])
    with (first/'audit'/'category-sample-200.csv').open() as stream:
        audit=list(csv.DictReader(stream));assert len(audit)==200 and all(row['正确']=='' for row in audit)
    with (first/'audit'/'translation-sample-50.csv').open() as stream:assert len(list(csv.DictReader(stream)))==50
