"""商品级增强之前的代理类目：取商品关联预选查询的 T1 预分类众数，材质留空，零模型调用。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from scripts.esci.catalog_records import CATEGORIES
from scripts.esci.download import ROOT, fingerprint
from scripts.esci.finalize_catalog import load_rows
from scripts.esci.prepare import json_line

VERSION = 'query-class-proxy-v1'


def proxy_rows(prepared: Path, query_cache: Path) -> list[dict]:
    """同票按既有八类顺序；与 2,000 件演示目录的类目规则一致，但覆盖全部已标注商品与标签。"""
    classes = {(row['locale'], row['query_id']): row for row in load_rows(query_cache)}
    plan = json.loads((prepared/'selection-plan.json').read_text())
    preselected = {(q['locale'], q['query_id']) for rows in plan['queries'].values() for q in rows}
    votes = defaultdict(Counter)
    for example in load_rows(prepared/'examples.jsonl'):
        key = example['product_locale'], example['query_id']
        if key not in preselected:
            raise ValueError(f'标注不属于预选查询：{key}')
        query = classes[key]
        if query['status'] != 'ok' or query['category'] not in CATEGORIES:
            raise ValueError(f'预选查询缺少八类预分类：{key}')
        votes[example['product_locale'], example['product_id']][query['category']] += 1
    products = {(p['product_locale'], p['product_id']) for p in load_rows(prepared/'products.jsonl')}
    if products != set(votes):
        raise ValueError('商品与标注不能一一覆盖')
    return [{'locale': locale, 'asin': asin, 'prompt_version': VERSION, 'status': 'ok',
             'category': min(votes[locale, asin], key=lambda c: (-votes[locale, asin][c], CATEGORIES.index(c))),
             'material_tags': []} for locale, asin in sorted(votes)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, default=ROOT/'eval/verification/2026-10-14-t2-input-plan')
    parser.add_argument('--query-cache', type=Path, default=ROOT/'data/esci/query-class-v1.jsonl')
    parser.add_argument('--output', type=Path, required=True, help='代理类目缓存文件')
    args = parser.parse_args()
    rows = proxy_rows(args.prepared, args.query_cache)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(''.join(json_line(row) for row in rows))
    print(json.dumps({'products': len(rows), 'categories': dict(Counter(r['category'] for r in rows)), 'model_calls': 0,
                      'output_fingerprint': fingerprint(args.output),
                      'input_fingerprints': {str(p): fingerprint(p) for p in (args.prepared/'examples.jsonl',
                                             args.prepared/'products.jsonl', args.query_cache)}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
