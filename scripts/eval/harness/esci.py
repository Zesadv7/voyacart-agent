"""ESCI 十场景开发合同：绑定正式训练输入并核对生产工具的实际结果。"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import re
import runpy

from scripts.eval.harness.contracts import ROOT
from scripts.eval.run_esci import read_rows, validate_cases
from scripts.eval.esci_pairing import MANIFEST_NAME, validate_pairing

SUITE_TYPE = 'esci-dev-v1'
SCENARIOS = runpy.run_path(str(ROOT / 'eval/harness/esci-v1/prepare.py'))['SCENARIOS']
INPUTS = ('catalog', 'selection', 'ranking', 'translations', 'pairing_manifest')


def bind_cases(paths, *, index_dir, local_model_cache, allow_fixture=False):
    from app.domain.catalog.exchange_rate import ExchangeRateTable
    from app.infrastructure.persistence.seed_products import _product_from_record

    if Path(paths['ranking']).name != 'ranking-dev.jsonl':
        raise ValueError('ESCI Harness 仅接受 ranking-dev.jsonl，留出输入保持独立')
    if Path(paths['translations']).name == 'crosslingual-zh.jsonl':
        raise ValueError('ESCI Harness 使用独立 train 译文缓存，中文留出派生集保持独立')
    pairing = validate_pairing(catalog=paths['catalog'], selection=paths['selection'], ranking=paths['ranking'],
        manifest=paths.get('pairing_manifest'), profile='harness', allow_fixture=allow_fixture)
    records = read_rows(Path(paths['catalog']))
    products = [_product_from_record(row) for row in records]
    ranking = read_rows(Path(paths['ranking']))
    validate_cases({'ranking': ranking}, products)
    if any(row['data_provenance']['source'] != 'esci' or row['source_platform'] != 'amazon'
           or not row['external_product_id'] or row['source_locale'] not in ('us', 'es', 'jp') for row in records):
        raise ValueError('Harness 商品须绑定正式 ESCI 目录的来源、站点与 ASIN')
    by_id = {row['product_id']: row for row in records}
    queries = json.loads(Path(paths['selection']).read_text())['queries']['dev']
    selected = {(row['locale'], row['query_id']): row for row in queries}
    if len(selected) != len(queries) or any(row['split'] != 'train' for row in queries):
        raise ValueError('ESCI Harness 开发查询须唯一且来自 train')
    ranked = {(row['locale'], row['query_id']): row for row in ranking}
    if set(ranked) != set(selected) or any(row['query'] != selected[key]['query'] for key, row in ranked.items()):
        raise ValueError('ranking-dev 须与正式开发 selection 的查询完全对应')
    translations, translation_provenance = {}, {}
    for row in read_rows(Path(paths['translations'])):
        key = row['locale'], row['query_id']
        if (row['split'] != 'train' or key not in selected or key in translations
                or row['query'] != selected[key]['query'] or row['status'] != 'ok'
                or not row['prompt_version'] or not re.search(r'[\u4e00-\u9fff]', row['translation'])):
            raise ValueError('中文开发译文须为独立、成功且对应原文的 train 缓存')
        if pairing['role'] == 'frozen_esci':
            provenance = row.get('provenance', {})
            if (provenance.get('author_type') not in ('ai', 'human') or not provenance.get('provider')
                    or not provenance.get('generation_method')
                    or provenance.get('human_review_status') not in ('pending', 'completed')
                    or provenance.get('source_selection_blake2b') != pairing['verified_artifacts']['selection']['blake2b']):
                raise ValueError('正式 train 译文须明确作者、来源、selection 指纹及人工审核状态')
        translations[key] = row['translation']
        translation_provenance[key] = row.get('provenance', {'author_type': 'constructed_fixture'})
    used, cases = set(), []
    for identity, scenario, locale, count, checks in SCENARIOS:
        choice = None
        for key in sorted(selected):
            if key[0] != locale or key in used:
                continue
            exact = sorted(candidate['product_id'] for candidate in ranked[key]['candidates'] if candidate['label'] == 'E')
            eligible = [by_id[product_id] for product_id in exact
                        if by_id[product_id]['source_locale'] == locale
                        and any(sku['stock'] > 0 for sku in by_id[product_id]['skus'])]
            if len(eligible) < count:
                continue
            chosen = eligible[:count]
            destinations = set.intersection(*(set(row['ships_to']) for row in chosen))
            if not destinations:
                continue
            choice = key, chosen, 'CN' if 'CN' in destinations else sorted(destinations)[0]
            break
        if choice is None:
            raise ValueError(f'{identity} 缺少独立 train 查询及满足目录约束的 Exact 商品')
        key, chosen, destination = choice
        used.add(key)
        chinese = scenario in ('crosslingual', 'embedding_unavailable')
        if chinese and key not in translations:
            raise ValueError(f'{identity} 缺少独立 train 中文译文：{key[0]}/{key[1]}')
        expected = []
        for row in chosen:
            sku = next(sku for sku in row['skus'] if sku['stock'] > 0)
            expected.append({'product_id': row['product_id'], 'sku_id': sku['sku_id'],
                'title': row['title'], 'asin': row['external_product_id'], 'locale': locale,
                'price_major': sku['price_major'], 'currency': sku['currency'], 'stock': sku['stock']})
        budget = math.ceil(max(sku['price_major'] * ExchangeRateTable().rate(sku['currency'], 'CNY')
                               for sku in expected) * 100) / 100
        cases.append({'id': identity, 'title': identity, 'scenario': scenario, 'layer': 'esci_agent',
            'split': 'dev', 'mode': 'ordinary', 'critical': True, 'required_checks': checks,
            'source_query': {key_: selected[key][key_] for key_ in ('locale', 'query_id', 'query', 'split')},
            'query': translations[key] if chinese else selected[key]['query'], 'expected_products': expected,
            'query_translation_provenance': translation_provenance[key] if chinese else None,
            'constraints': {'ship_to': destination, 'price_max_major': budget, 'target_currency': 'CNY'},
            'catalog_file': str(Path(paths['catalog']).resolve()), 'index_dir': str(Path(index_dir).resolve()),
            'local_model_cache': str(Path(local_model_cache).resolve())})
    return cases


def build_suite(paths, *, index_dir, local_model_cache, allow_fixture=False):
    paths = {**paths, 'pairing_manifest': paths.get('pairing_manifest', Path(paths['ranking']).parent / MANIFEST_NAME)}
    paths = {name: Path(paths[name]).resolve() for name in INPUTS}
    cases = bind_cases(paths, index_dir=index_dir, local_model_cache=local_model_cache, allow_fixture=allow_fixture)
    pairing = validate_pairing(catalog=paths['catalog'], selection=paths['selection'], ranking=paths['ranking'],
        manifest=paths['pairing_manifest'], profile='harness', allow_fixture=allow_fixture)
    template = json.loads((ROOT / 'eval/harness/v1/suite.json').read_text())
    suite = {key: template[key] for key in ('schema_version', 'runtime', 'gates', 'pricing')}
    suite.update(suite_type=SUITE_TYPE, cases_file='cases.json', dataset_version=f"esci-{pairing['dataset_version']}-dev",
        dataset_provenance=('构造夹具，仅合同测试' if allow_fixture else 'ESCI 正式目录和 train 人工相关性标注；中文查询作者与人工审核状态见每场景来源；价格、库存、配送为演示字段；买家与交易库隔离'),
        input_pairing=pairing, allow_fixture_manifest=allow_fixture,
        strategies={name: template['strategies'][name] for name in ('current', 'candidate')},
        profiles={'contracts': {'cases': [], 'repetitions': 1},
                  'dev': {'cases': [case['id'] for case in cases], 'repetitions': 1}},
        contract_tests=list(dict.fromkeys([*template['contract_tests'], 'tests/test_esci_harness.py', 'tests/test_esci_pairing.py'])),
        inputs={name: {'path': str(path), 'blake2b': hashlib.blake2b(path.read_bytes()).hexdigest()}
                for name, path in paths.items()},
        index_dir=str(Path(index_dir).resolve()), local_model_cache=str(Path(local_model_cache).resolve()),
        limits=['仅开发训练场景；留出与发布档保持未执行', '使用真实目录和生产工具、隔离买家与 SQLite；支付及物流未执行'])
    return suite, cases


def offline_resource_preflight(local_model_cache):
    """不读取凭据或加载模型；仅证明静态文件观察，不能宣布真实执行就绪。"""
    from importlib.util import find_spec
    cache = Path(local_model_cache)
    files = {str(path.relative_to(cache)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(cache.rglob('*')) if path.is_file() and path.suffix in ('.onnx', '.json')}
    return {'suite_bound': True, 'runnable': False, 'remote_configured': 'not_checked',
            'local_dependency_available': find_spec('fastembed') is not None,
            'local_cache_exists': cache.is_dir(), 'local_onnx_present': any(name.endswith('.onnx') for name in files),
            'local_cache_verified': False, 'local_model_files_sha256': files,
            'local_model_load': 'not_attempted', 'external_calls': 0, 'model_calls': 0,
            'real_ten_scenarios': 'not_executed',
            'reason': '仅离线绑定与静态文件预检；凭据、模型加载、索引和真实服务可用性仍须单独验收'}


def validate_suite(suite, cases):
    if set(suite['profiles']) != {'contracts', 'dev'} or suite['profiles']['contracts'] != {'cases': [], 'repetitions': 1}:
        raise ValueError('ESCI 独立合同仅提供 contracts 与 dev')
    if suite['profiles']['dev'] != {'cases': [row[0] for row in SCENARIOS], 'repetitions': 1}:
        raise ValueError('ESCI 开发档须完整覆盖冻结的十场景，各运行一次')
    if set(suite['inputs']) != set(INPUTS):
        raise ValueError('ESCI 正式目录、selection、ranking-dev 与训练译文输入须齐全')
    template = json.loads((ROOT / 'eval/harness/v1/suite.json').read_text())
    if suite['contract_tests'] != list(dict.fromkeys([*template['contract_tests'], 'tests/test_esci_harness.py', 'tests/test_esci_pairing.py'])):
        raise ValueError('ESCI 开发合同须保留已有安全回归与独立绑定检查')
    paths = {name: item['path'] for name, item in suite['inputs'].items()}
    for name, item in suite['inputs'].items():
        if hashlib.blake2b(Path(item['path']).read_bytes()).hexdigest() != item['blake2b']:
            raise ValueError(f'ESCI 输入内容指纹已变化：{name}')
    pairing = validate_pairing(catalog=paths['catalog'], selection=paths['selection'], ranking=paths['ranking'],
        manifest=paths['pairing_manifest'], profile='harness', allow_fixture=suite.get('allow_fixture_manifest', False))
    if suite.get('input_pairing') != pairing:
        raise ValueError('ESCI 套件配对证明与输入不同')
    expected = bind_cases(paths, index_dir=suite['index_dir'], local_model_cache=suite['local_model_cache'],
        allow_fixture=suite.get('allow_fixture_manifest', False))
    if cases != expected:
        raise ValueError('ESCI 场景与正式训练输入绑定不同')


async def build_search(repo, settings, case, resources, observed):
    from app.application.usecases.catalog_search import CatalogSearchUseCase
    from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
    from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
    from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex

    arguments = {}
    if case['scenario'] in ('crosslingual', 'embedding_unavailable'):
        if not settings.embedding_api_key or not settings.embedding_base_url or not settings.embedding_model:
            raise ValueError('ESCI 中文场景需要 EMBEDDING_BASE_URL、EMBEDDING_API_KEY、EMBEDDING_MODEL')
        index_settings = replace(settings, data_dir=Path(case['index_dir']), qdrant_collection='voyacart_products_esci_harness')
        embedder, index = OpenAIEmbeddingClient(index_settings), QdrantProductIndex(index_settings)
        resources.push_async_callback(index.close)
        if not await bootstrap_product_index(repo, embedder, index):
            raise RuntimeError('ESCI 远端向量索引未完成')
        arguments.update(embedder=embedder, vector_index=index)
        if case['scenario'] == 'embedding_unavailable':
            from app.infrastructure.embedding.local_multilingual import LocalMultilingualEmbedding
            from app.domain.catalog.ports.retrieval_ports import EmbeddingClient

            local = await asyncio.to_thread(LocalMultilingualEmbedding, Path(case['local_model_cache']), local_files_only=True)
            local_settings = replace(index_settings, qdrant_collection='voyacart_products_esci_harness_local',
                embedding_base_url='local://fastembed', embedding_model=local.model_name,
                embedding_dim=local.dimension, embedding_version=local.version)
            local_index = QdrantProductIndex(local_settings)
            resources.push_async_callback(local_index.close)
            if not await bootstrap_product_index(repo, local, local_index):
                raise RuntimeError('ESCI 本地向量索引未完成')
            class UnavailableEmbedding(EmbeddingClient):
                async def embed(self, text):
                    observed.append({'remote_embedding_failure': True})
                    raise ConnectionError('ESCI 诊断注入远端嵌入不可用')
                async def embed_batch(self, texts):
                    raise ConnectionError('ESCI 诊断注入远端嵌入不可用')
            arguments.update(embedder=UnavailableEmbedding(), local_embedder=local, local_vector_index=local_index)
    usecase = CatalogSearchUseCase(repo, **arguments)
    original = usecase.execute
    async def recorded(spec):
        result = await original(spec)
        observed.append({'spec': asdict(spec), 'result': result})
        return result
    usecase.execute = recorded
    return usecase


def retrieval_checks(case, observed):
    calls = [item for item in observed if 'result' in item]
    hits = [hit for item in calls for hit in item['result']['hits']]
    expected = {row['product_id']: row for row in case['expected_products']}
    constraints = case['constraints']
    from scripts.eval.hard_constraints import find_hit_constraint_violations
    records = {row['product_id']: row for row in read_rows(Path(case['catalog_file']))}
    leaks = find_hit_constraint_violations(hits, records, **constraints)
    checks = {'product_tool_called': bool(calls), 'no_constraint_leak': bool(hits) and not leaks}
    scenario = case['scenario']
    if scenario == 'crosslingual':
        matched = [hit for hit in hits if hit['product_id'] in expected]
        checks.update(exact_product_returned=bool(matched),
            foreign_title_preserved=bool(matched) and all(hit['title'] == expected[hit['product_id']]['title'] for hit in matched))
    if scenario == 'destination':
        checks['destination_constraint_preserved'] = bool(calls) and all(item['spec']['ship_to'] == constraints['ship_to'] for item in calls)
    if scenario == 'budget':
        checks['latest_budget_preserved'] = bool(calls) and all(item['spec']['price_max_major'] == constraints['price_max_major'] for item in calls)
        quoted = {sku['sku_id']: sku for row in records.values() for sku in row['skus']}
        checks['currency_association'] = bool(hits) and all(hit['currency'] == 'CNY'
            and all(sku['currency'] == quoted[sku['sku_id']]['currency']
                    and sku['price_major'] == quoted[sku['sku_id']]['price_major'] for sku in hit['skus']) for hit in hits)
    if scenario == 'stock':
        checks['in_stock_only'] = bool(hits) and not any('out_of_stock' in reasons for reasons in leaks.values())
    if scenario == 'comparison':
        checks['selected_product_pair'] = set(expected) <= {hit['product_id'] for hit in hits}
    if scenario == 'embedding_unavailable':
        checks.update(remote_embedding_failure_observed=any(item.get('remote_embedding_failure') for item in observed),
            local_embedding_used=bool(calls) and all(item['result']['recall_strategy'] == 'local_embedding' for item in calls))
    return checks


async def run_scenario(case, reply, observed, trade, confirmations, buyer, session, initial_inventory):
    from scripts.eval.harness.workflows import check_sku_quotes

    scenario, constraints = case['scenario'], case['constraints']
    expected = case['expected_products']
    budget = constraints['price_max_major'] * 2 if scenario == 'budget' else constraints['price_max_major']
    prompt = (f"检索需求：{case['query']}。寄到{constraints['ship_to']}，只选有货商品。"
              f"商品价格上限{budget} CNY，调用商品工具落实约束。只读选购，保留商品原文标题。")
    boundary = 0
    if scenario == 'budget':
        await reply(prompt)
        boundary = len(observed)
        prompt = f"预算更新为{constraints['price_max_major']} CNY，目的地仍为{constraints['ship_to']}。重新调用商品工具核对，保留有货约束。"
    if scenario == 'comparison':
        prompt += ' 比较这两件正式目录商品：' + '、'.join(row['product_id'] for row in expected)
        prompt += '。分别调用工具核对，逐项回答“SKU编号 单价 数额 原币币种”，保留 SKU 原币报价。'
    text = await reply(prompt)
    checks = retrieval_checks(case, observed[boundary:])
    if scenario == 'comparison':
        checks['sku_price_currency_associations'] = check_sku_quotes(text, expected)
    if scenario in ('awaiting_confirmation', 'approved_order'):
        row = expected[0]
        await reply(f"请为商品{row['product_id']}、规格{row['sku_id']}准备1件订单确认卡。"
                    f"评测收件人，国家{constraints['ship_to']}，省市评测省/评测市，地址评测街1号，邮编200000，电话00000000000。只准备，等待页面批准。")
        pending = await trade.list_confirmations(buyer_id=buyer, session_id=session)
        wanted = [item for item in pending if item['status'] == 'pending' and item['action'] == 'create'
                  and item['payload']['shipping_address']['country'] == constraints['ship_to']
                  and [(line['product_id'], line['sku_id'], line['quantity']) for line in item['payload']['items']]
                  == [(row['product_id'], row['sku_id'], 1)]]
        checks.update(confirmation_prepared=bool(wanted), no_order_before_approval=(await trade.list_orders(buyer_id=buyer))['total'] == 0,
            inventory_unchanged=await trade.get_inventory() == initial_inventory)
        if scenario == 'approved_order':
            checks.update(approved_order_created=False, inventory_decreased_once=False)
            if wanted and checks['no_order_before_approval'] and checks['inventory_unchanged']:
                envelope = wanted[0]
                result = await confirmations.resolve(envelope['confirmation_id'], buyer, session, envelope['snapshot_hash'], True)
                repeated = await confirmations.resolve(envelope['confirmation_id'], buyer, session, envelope['snapshot_hash'], True)
                orders = await trade.list_orders(buyer_id=buyer)
                after = await trade.get_inventory()
                wanted_inventory = {**initial_inventory, row['sku_id']: initial_inventory[row['sku_id']] - 1}
                checks.update(approved_order_created=orders['total'] == 1 and result == repeated,
                    inventory_decreased_once=after == wanted_inventory)
    else:
        checks.update(no_order_on_readonly=(await trade.list_orders(buyer_id=buyer))['total'] == 0,
            no_confirmation_on_readonly=not await trade.list_confirmations(buyer_id=buyer, session_id=session),
            inventory_unchanged=await trade.get_inventory() == initial_inventory)
    return {name: bool(checks.get(name, False)) for name in {*case['required_checks'], *checks}}
