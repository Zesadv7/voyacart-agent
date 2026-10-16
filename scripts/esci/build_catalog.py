"""T2 查询预选和增强输入准备；候选分类完整后才能冻结本轮选择。"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random

import pyarrow.parquet as pq

from scripts.esci.catalog_records import plain_text
from scripts.esci.download import FILES, ROOT, fingerprint, verify_files
from scripts.esci.prepare import CATEGORIES, DEV_QUERIES, DISTRACTOR_QUERIES, HOLDOUT_QUERIES, PROMPT_VERSION, SEED, candidate_pool, json_line, read_cache


TARGETS = {'dev':DEV_QUERIES, 'holdout':HOLDOUT_QUERIES, 'distractors':DISTRACTOR_QUERIES}


def stratified(rows: list[dict], count: int) -> list[dict]:
    """类目轮转抽样；每层先按查询号排序，再用固定种子打乱。"""
    groups = defaultdict(list)
    for row in rows:
        groups[row['category']].append(row)
    for group in groups.values():
        group.sort(key=lambda row: row['query_id'])
        random.Random(SEED).shuffle(group)
    if sum(map(len, groups.values())) < count:
        raise ValueError(f'合格查询不足：目标 {count}，实际 {sum(map(len,groups.values()))}')
    result = []
    cursor = 0
    while len(result) < count:
        for category in CATEGORIES:
            group = groups[category]
            if cursor < len(group):
                result.append(group[cursor])
                if len(result) == count:
                    return result
        cursor += 1
    return result


def preselect(candidates: list[dict], cache: dict) -> dict:
    selected = []
    for row in candidates:
        saved = cache.get((row['locale'],row['query_id'],PROMPT_VERSION))
        if saved is None:
            raise ValueError('T1 候选仍有缺失分类；完成后再冻结预选')
        if saved['query'] != row['query'] or saved['split'] != row['split']:
            raise ValueError('缓存与原始查询不一致')
        if saved['status']=='ok' and saved['category'] in CATEGORIES:
            selected.append(saved)
    result = {}
    for locale, ratio in (('us',2),('es',1),('jp',1)):
        used = set()
        for dataset, target in TARGETS.items():
            split = 'test' if dataset=='holdout' else 'train'
            group = [r for r in selected if r['locale']==locale and r['split']==split and r['query_id'] not in used]
            rows = stratified(group, math.ceil(target*ratio/4*1.3))
            result.setdefault(dataset,[]).extend(rows)
            used.update(row['query_id'] for row in rows)
    return result


def prepare_inputs(raw: Path, candidates: list[dict], cache: dict, output: Path) -> dict:
    selection = preselect(candidates, cache)
    queries = [row for group in selection.values() for row in group]
    keys = {(r['locale'],r['query_id']) for r in queries}
    examples = pq.read_table(raw/FILES[0], columns=['query_id','product_locale','product_id','esci_label','split'],
        filters=[('small_version','=',1),('query_id','in',sorted({q for _,q in keys}))]).to_pylist()
    examples = [r for r in examples if (r['product_locale'],r['query_id']) in keys]
    products = {}
    wanted = {(r['product_locale'],r['product_id']) for r in examples}
    for batch in pq.ParquetFile(raw/FILES[1]).iter_batches(batch_size=32768):
        for product in batch.to_pylist():
            key = product['product_locale'],product['product_id']
            if key in wanted:
                if key in products:
                    raise ValueError(f'同站点商品记录重复：{key}')
                products[key] = product
    missing = wanted - products.keys()
    if missing:
        raise ValueError(f'标注商品缺少文本：{len(missing)} 件')
    product_path = output/'enrichment-inputs.jsonl'
    product_path.write_text(''.join(json_line({'locale':locale,'asin':asin,'title':p['product_title'],
        'brand':p['product_brand'] or '', 'description':plain_text(p['product_description'])[:1000],
        'bullet_points':plain_text(p['product_bullet_point'])[:1000]}) for (locale,asin),p in sorted(products.items())))
    (output/'products.jsonl').write_text(''.join(json_line(p) for _,p in sorted(products.items())))
    (output/'examples.jsonl').write_text(''.join(json_line(row) for row in sorted(examples,key=lambda r:(r['product_locale'],r['query_id'],r['product_id']))))
    manifest = {'schema_version':1,'seed':SEED,'targets':TARGETS,'reserve_multiplier':1.3,'queries':selection,
        'preselected_counts':{key:len(value) for key,value in selection.items()},'products_to_enrich':len(products),
        'source_fingerprint':fingerprint(ROOT/'data/esci/source.json'),
        'query_cache_fingerprint':fingerprint(ROOT/'data/esci/query-class-v1.jsonl'),
        'enrichment_input_fingerprint':fingerprint(product_path),
        'catalog_status':'awaiting_product_enrichment', 'model_calls':0}
    (output/'selection-plan.json').write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    raw=ROOT/'data/raw/esci'
    verify_files(raw,json.loads((ROOT/'data/esci/source.json').read_text()))
    result=prepare_inputs(raw,candidate_pool(raw/FILES[0]),read_cache(ROOT/'data/esci/query-class-v1.jsonl'),args.output)
    print(json.dumps({key:result[key] for key in ('targets','preselected_counts','products_to_enrich','model_calls')},ensure_ascii=False))


if __name__=='__main__':
    main()
