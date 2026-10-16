"""隔离构造夹具验证 ESCI 开发绑定与交易合同；真实目录与模型效果保持待执行。"""
from dataclasses import asdict
import gzip
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.eval.harness.contracts import ROOT, SUITE, load_suite, write_json
from scripts.eval.harness.esci import build_suite, retrieval_checks, run_scenario
from scripts.eval.run_esci import read_rows
from tests.esci_pairing_fixtures import write_fixture_pairing


@pytest.fixture
def training_inputs(tmp_path):
    records, queries, ranking, translations = [], [], [], []
    for locale, currency, language, count in (('us', 'USD', 'en', 4), ('es', 'EUR', 'es', 3), ('jp', 'JPY', 'ja', 3)):
        for query_id in range(1, count + 1):
            query = f'constructed contract {locale} query {query_id}'
            queries.append({'locale': locale, 'query_id': query_id, 'query': query, 'split': 'train', 'category': '旅行装备'})
            candidates = []
            for index in range(2):
                product_id = f'P{100000 + len(records)}'
                records.append({'product_id': product_id, 'title': f'Contract {locale} {query_id}\u2028{index}',
                    'brand': 'Contract fixture', 'category': '旅行装备', 'origin_country': locale.upper(),
                    'description': '', 'highlights': [], 'ships_to': ['CN'], 'source_platform': 'amazon',
                    'source_locale': locale, 'source_language': language, 'external_product_id': f'B{len(records):09d}',
                    'data_provenance': {'source': 'esci'},
                    'skus': [{'sku_id': product_id + '-S1', 'spec': 'contract', 'price_major': 10 + index,
                              'currency': currency, 'stock': 5}]})
                candidates.append({'product_id': product_id, 'label': 'E'})
            ranking.append({'locale': locale, 'query_id': query_id, 'query': query, 'candidates': candidates})
            translations.append({'locale': locale, 'query_id': query_id, 'query': query, 'split': 'train',
                                 'translation': f'隔离合同夹具 {locale} {query_id}', 'status': 'ok', 'prompt_version': 'contract-train'})
    paths = {'catalog': tmp_path / 'catalog-esci-v1.jsonl.gz', 'selection': tmp_path / 'selection-v1.json',
             'ranking': tmp_path / 'ranking-dev.jsonl', 'translations': tmp_path / 'translations-train.jsonl'}
    with gzip.open(paths['catalog'], 'wt', encoding='utf-8') as stream:
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    write_json(paths['selection'], {'queries': {'dev': queries}})
    for name, rows in (('ranking', ranking), ('translations', translations)):
        paths[name].write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    paths['pairing_manifest'] = write_fixture_pairing(tmp_path, paths['catalog'], selection=paths['selection'])
    return paths


def write_bound_suite(tmp_path, paths):
    suite, cases = build_suite(paths, index_dir=tmp_path / 'index', local_model_cache=tmp_path / 'model', allow_fixture=True)
    folder = tmp_path / 'bound-suite'
    folder.mkdir()
    write_json(folder / 'suite.json', suite)
    write_json(folder / 'cases.json', cases)
    return folder / 'suite.json', suite, cases


def test_ten_cases_bind_distinct_train_queries_and_existing_quotes(tmp_path, training_inputs):
    path, suite, cases = write_bound_suite(tmp_path, training_inputs)
    loaded = load_suite(path)
    assert loaded['_cases'] == cases and set(suite['profiles']) == {'contracts', 'dev'}
    assert len(cases) == 10 and len({(case['source_query']['locale'], case['source_query']['query_id']) for case in cases}) == 10
    assert all(case['source_query']['split'] == 'train' for case in cases)
    records = {row['product_id']: row for row in read_rows(training_inputs['catalog'])}
    for case in cases:
        for expected in case['expected_products']:
            record = records[expected['product_id']]
            assert expected['title'] == record['title'] and '\u2028' in expected['title']
            assert expected['asin'] == record['external_product_id']
            assert expected['sku_id'] == record['skus'][0]['sku_id']
            assert expected['price_major'] == record['skus'][0]['price_major']
    assert len(next(case for case in cases if case['scenario'] == 'comparison')['expected_products']) == 2


@pytest.mark.parametrize('mutation', ['test_query', 'test_translation', 'unknown_product'])
def test_foreign_split_or_catalog_reference_fails_binding(tmp_path, training_inputs, mutation):
    if mutation == 'test_query':
        selection = json.loads(training_inputs['selection'].read_text())
        selection['queries']['dev'][0]['split'] = 'test'
        write_json(training_inputs['selection'], selection)
    else:
        name = 'translations' if mutation == 'test_translation' else 'ranking'
        rows = read_rows(training_inputs[name])
        if mutation == 'test_translation':
            rows[0]['split'] = 'test'
        else:
            rows[0]['candidates'][0]['product_id'] = 'P999999'
        training_inputs[name].write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(ValueError):
        build_suite(training_inputs, index_dir=tmp_path / 'index', local_model_cache=tmp_path / 'model', allow_fixture=True)


@pytest.mark.parametrize('mutation', ['cases', 'profiles', 'contracts', 'input'])
def test_bound_suite_changes_fail_before_execution(tmp_path, training_inputs, mutation):
    path, suite, cases = write_bound_suite(tmp_path, training_inputs)
    if mutation == 'cases':
        cases[0]['expected_products'][0]['sku_id'] = 'P999999-S1'
        write_json(path.parent / 'cases.json', cases)
    elif mutation == 'profiles':
        suite['profiles']['dev']['cases'].pop()
        write_json(path, suite)
    elif mutation == 'contracts':
        suite['contract_tests'].pop()
        write_json(path, suite)
    else:
        with training_inputs['translations'].open('a') as stream:
            stream.write('\n')
    with pytest.raises(ValueError):
        load_suite(path)


def test_preparation_and_holdout_paths_do_not_start_execution(tmp_path, training_inputs):
    with pytest.raises(ValueError, match='准备态'):
        load_suite(ROOT / 'eval/harness/esci-v1/preparation.json')
    training_inputs['ranking'] = tmp_path / 'ranking-holdout.jsonl'
    with pytest.raises(ValueError, match='ranking-dev'):
        build_suite(training_inputs, index_dir=tmp_path / 'index', local_model_cache=tmp_path / 'model', allow_fixture=True)


def test_missing_train_translation_fails_without_substituting_original_query(tmp_path, training_inputs):
    translations = [row for row in read_rows(training_inputs['translations']) if (row['locale'], row['query_id']) != ('us', 1)]
    training_inputs['translations'].write_text(''.join(json.dumps(row) + '\n' for row in translations))
    with pytest.raises(ValueError, match='缺少独立 train 中文译文：us/1'):
        build_suite(training_inputs, index_dir=tmp_path / 'index', local_model_cache=tmp_path / 'model', allow_fixture=True)


@pytest.mark.parametrize('mutation', ['repetitions', 'holdout'])
def test_original_v1_release_contract_remains_strict(tmp_path, mutation):
    suite = json.loads(SUITE.read_text())
    cases = json.loads((SUITE.parent / suite['cases_file']).read_text())
    if mutation == 'repetitions':
        suite['profiles']['release']['repetitions'] = 2
    else:
        suite['profiles']['release']['cases'].pop()
    write_json(tmp_path / 'suite.json', suite)
    write_json(tmp_path / suite['cases_file'], cases)
    with pytest.raises(ValueError, match='至少 28.*至少 3'):
        load_suite(tmp_path / 'suite.json')


@pytest.mark.asyncio
async def test_explicit_esci_case_uses_existing_workflow_dispatch(tmp_path, training_inputs, monkeypatch):
    from scripts.eval.harness.runner import execute_case
    from app.infrastructure.settings import load_settings
    _, suite, cases = write_bound_suite(tmp_path, training_inputs)
    workflow = AsyncMock(return_value={'case_id': cases[0]['id'], 'layer': 'esci_agent', 'checks': {}, 'error': None})
    monkeypatch.setattr('scripts.eval.harness.workflows.run_workflow', workflow)
    await execute_case(cases[0], 'current', 0, load_settings(require_llm_key=False), suite, tmp_path / 'work', None, None)
    workflow.assert_awaited_once()
    with pytest.raises(ValueError, match='显式独立'):
        await execute_case(cases[0], 'current', 0, load_settings(require_llm_key=False), {}, tmp_path / 'work', None, None)


@pytest.mark.asyncio
async def test_bound_approval_uses_real_confirmation_and_inventory_once(tmp_path, training_inputs):
    from app.application.usecases.catalog_search import CatalogSearchUseCase
    from app.application.usecases.confirmation_service import ConfirmationService
    from app.application.usecases.order_usecases import OrderItemInput
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    from app.domain.order.address import Address
    from app.infrastructure.eventbus import TradeEventBus
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.infrastructure.persistence.seed_products import _product_from_record
    from app.infrastructure.persistence.sql.repositories import create_engine
    from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
    _, _, cases = write_bound_suite(tmp_path, training_inputs)
    case = next(case for case in cases if case['scenario'] == 'approved_order')
    expected = case['expected_products'][0]
    repo = InMemoryProductRepository([_product_from_record(row) for row in read_rows(training_inputs['catalog'])])
    engine = create_engine('sqlite+aiosqlite:///' + str(tmp_path / 'isolated-contract.db'))
    try:
        trade = SqlTradeStore(engine)
        await trade.initialize_inventory(await repo.list_all())
        repo.bind_inventory(trade.get_inventory)
        confirmations = ConfirmationService(repo, trade, bus=TradeEventBus())
        initial = await trade.get_inventory()
        observed = []
        async def reply(prompt):
            if '订单确认卡' in prompt:
                await confirmations.prepare_order('contract-buyer', 'contract-session',
                    [OrderItemInput(expected['product_id'], expected['sku_id'], 1)],
                    Address('合同夹具', case['constraints']['ship_to'], '评测省', '评测市', '评测街1号', '200000', '00000000000'))
            else:
                spec = ProductSearchSpec(product_id=expected['product_id'], **case['constraints'])
                result = await CatalogSearchUseCase(repo).execute(spec)
                observed.append({'spec': asdict(spec), 'result': result})
            return '隔离合同回复，无模型调用'
        checks = await run_scenario(case, reply, observed, trade, confirmations, 'contract-buyer', 'contract-session', initial)
        assert all(checks.values())
        assert (await trade.list_orders(buyer_id='contract-buyer'))['total'] == 1
        assert (await trade.get_inventory())[expected['sku_id']] == initial[expected['sku_id']] - 1
    finally:
        await engine.dispose()


def test_embedding_failure_cannot_pass_as_keyword_or_unobserved_failure(tmp_path, training_inputs):
    _, _, cases = write_bound_suite(tmp_path, training_inputs)
    case = next(case for case in cases if case['scenario'] == 'embedding_unavailable')
    expected = case['expected_products'][0]
    hit = {'product_id': expected['product_id'], 'title': expected['title'], 'price_major': 1, 'currency': 'CNY',
           'skus': [{'sku_id': expected['sku_id'], 'stock': 5}]}
    observed = [{'spec': case['constraints'], 'result': {'hits': [hit], 'recall_strategy': 'keyword_2gram'}},
                {'remote_embedding_failure': True}]
    checks = retrieval_checks(case, observed)
    assert checks['remote_embedding_failure_observed'] and not checks['local_embedding_used']
    observed[0]['result']['recall_strategy'] = 'local_embedding'
    observed.pop()
    checks = retrieval_checks(case, observed)
    assert checks['local_embedding_used'] and not checks['remote_embedding_failure_observed']
