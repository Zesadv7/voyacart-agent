"""从 ESCI 训练预选输入生成可核验的 10 场景准备计划；此入口不执行模型。"""
from __future__ import annotations

import argparse
import hashlib
from collections import defaultdict
import json
from pathlib import Path

SCENARIOS = (
    ('esci-zh-us', 'crosslingual', 'us', 1, ['exact_product_returned', 'foreign_title_preserved']),
    ('esci-zh-es', 'crosslingual', 'es', 1, ['exact_product_returned', 'foreign_title_preserved']),
    ('esci-zh-jp', 'crosslingual', 'jp', 1, ['exact_product_returned', 'foreign_title_preserved']),
    ('esci-destination', 'destination', 'us', 1, ['destination_constraint_preserved', 'no_constraint_leak']),
    ('esci-budget', 'budget', 'es', 1, ['latest_budget_preserved', 'currency_association', 'no_constraint_leak']),
    ('esci-stock', 'stock', 'jp', 1, ['in_stock_only', 'no_constraint_leak']),
    ('esci-comparison', 'comparison', 'us', 2, ['selected_product_pair', 'sku_price_currency_associations', 'no_order_on_readonly']),
    ('esci-awaiting-confirmation', 'awaiting_confirmation', 'es', 1, ['confirmation_prepared', 'no_order_before_approval', 'inventory_unchanged']),
    ('esci-approved-order', 'approved_order', 'jp', 1, ['confirmation_prepared', 'no_order_before_approval', 'approved_order_created', 'inventory_decreased_once']),
    ('esci-embedding-unavailable', 'embedding_unavailable', 'us', 1, ['remote_embedding_failure_observed', 'local_embedding_used', 'no_constraint_leak']),
)


def build_preparation(prepared: Path) -> dict:
    plan = json.loads((prepared / 'selection-plan.json').read_text())
    queries = sorted(plan['queries']['dev'], key=lambda q: (q['locale'], q['query_id']))
    keys = {(q['locale'], q['query_id']) for q in queries}
    if len(keys) != len(queries) or any(q['split'] != 'train' for q in queries):
        raise ValueError('开发预选查询须唯一且全部来自 train')
    exact = defaultdict(set)
    with (prepared / 'examples.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            key = row['product_locale'], row['query_id']
            if key in keys:
                if row['split'] != 'train':
                    raise ValueError('开发查询的人工标注须来自 train')
                if row['esci_label'] == 'E':
                    exact[key].add(row['product_id'])
    wanted = {(locale, asin) for (locale, _), asins in exact.items() for asin in asins}
    products = {}
    with (prepared / 'products.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            key = row['product_locale'], row['product_id']
            if key in wanted:
                if key in products or not (row['product_title'] or '').strip():
                    raise ValueError('Exact 商品记录须唯一且标题完整')
                products[key] = row
    if wanted - products.keys():
        raise ValueError('Exact 人工标注对应商品文本缺失')
    used, cases = set(), []
    for case_id, scenario, locale, count, checks in SCENARIOS:
        candidates = [q for q in queries if q['locale'] == locale
                      and (locale, q['query_id']) not in used and len(exact[locale, q['query_id']]) >= count]
        if not candidates:
            raise ValueError(f'{case_id} 缺少独立 train 查询及 {count} 件 Exact 商品')
        query = candidates[0]
        used.add((locale, query['query_id']))
        selected = [products[locale, asin] for asin in sorted(exact[locale, query['query_id']])[:count]]
        pending = ['final_query_selection', 'product_id', 'sku_id', 'price', 'stock', 'ships_to']
        if scenario in ('crosslingual', 'embedding_unavailable'):
            pending.append('independent_train_chinese_translation')
        cases.append({'id': case_id, 'scenario': scenario, 'split': 'dev', 'status': 'prepared',
            'source_query': {k: query[k] for k in ('locale', 'query_id', 'query', 'split')},
            'source_products': [{'locale': locale, 'asin': p['product_id'], 'title': p['product_title'],
                                 'esci_label': 'E'} for p in selected],
            'required_checks': checks, 'pending_fields': pending})
    return {'schema_version': 'esci-harness-preparation-v1', 'status': 'prepared', 'runnable': False,
        'profile': 'dev', 'case_count': len(cases), 'model_calls': 0,
        'source_scope': 'ESCI train 预选；最终抽样尚未完成',
        'input_fingerprints': {name: hashlib.blake2b((prepared / name).read_bytes()).hexdigest()
            for name in ('selection-plan.json', 'products.jsonl', 'examples.jsonl')},
        'required_artifacts': ['data/catalog-esci-v1.jsonl.gz', 'data/esci/selection-v1.json',
            'eval/esci/ranking-dev.jsonl', '独立 train 中文译文缓存',
            '显式 esci-v1 开发合同与真实目录工作流'],
        'cases': cases}


def validate_preparation(path: Path, prepared: Path) -> dict:
    if json.loads(path.read_text()) != build_preparation(prepared):
        raise ValueError('准备计划与当前训练输入或内容指纹不同')
    return {'status': 'verified_preparation', 'runnable': False, 'case_count': len(SCENARIOS), 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--output', type=Path, help='新建准备目录；已有证据保持原样')
    action.add_argument('--validate', type=Path, help='核对 preparation.json 及训练输入内容指纹')
    args = parser.parse_args()
    if args.validate:
        result = validate_preparation(args.validate, args.prepared)
    else:
        result = build_preparation(args.prepared)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / 'preparation.json').write_text(json.dumps(result, ensure_ascii=False,
            sort_keys=True, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'runnable', 'case_count', 'model_calls')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
