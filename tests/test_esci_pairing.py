"""真实冻结输入只做离线配对；错配须在任何运行副作用前拒绝。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.eval import run_esci
from scripts.eval.esci_pairing import ROOT, validate_pairing
from scripts.eval.harness.esci import build_suite, validate_suite
from tests.esci_pairing_fixtures import write_fixture_pairing


def frozen_paths(version='v2'):
    directory = ROOT / ('eval/esci-v2' if version == 'v2' else 'eval/esci')
    return {'catalog': ROOT / f'data/catalog-esci-{version}.jsonl.gz',
            'dataset_dir': directory, 'manifest': directory / 'input-pairing.json',
            'selection': ROOT / f'data/esci/selection-{version}.json'}


def test_defaults_select_coherent_v2_without_changing_runtime_catalog():
    args = run_esci.argument_parser().parse_args(['--output', '/tmp/not-created-esci-default-test'])
    assert args.catalog == ROOT / 'data/catalog-esci-v2.jsonl.gz'
    assert args.dataset_dir == ROOT / 'eval/esci-v2' and args.baseline_version == 'v2'


@pytest.mark.parametrize('version,count', [('v1', 12123), ('v2', 12299)])
def test_frozen_versions_validate_exact_identity_and_development_only(version, count):
    pairing = validate_pairing(**frozen_paths(version), baseline_version=version)
    assert pairing['role'] == 'frozen_esci'
    assert pairing['identity_mapping']['product_count'] == count
    assert set(pairing['verified_artifacts']) == {'catalog', 'selection', 'ranking-dev', 'retrieval-dev', 'constraints-dev'}


def test_development_never_opens_holdout_files(monkeypatch):
    original = Path.open
    def guarded(path, *args, **kwargs):
        assert '-holdout.jsonl' not in path.name and path.name != 'crosslingual-zh.jsonl'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    validate_pairing(**frozen_paths())


@pytest.mark.parametrize('profile', ['dev', 'holdout'])
@pytest.mark.parametrize('mutation', ['catalog', 'dataset', 'baseline', 'selection', 'missing', 'schema', 'identity'])
async def test_mismatch_rejects_before_settings_models_indexes_output_and_holdout_attempt(tmp_path, monkeypatch, profile, mutation):
    paths = frozen_paths()
    version = 'v2'
    if mutation == 'catalog':
        paths['catalog'] = frozen_paths('v1')['catalog']
    elif mutation == 'dataset':
        paths['dataset_dir'] = frozen_paths('v1')['dataset_dir']
    elif mutation == 'selection':
        paths['selection'] = frozen_paths('v1')['selection']
    elif mutation == 'baseline':
        version = 'v1'
    elif mutation == 'missing':
        paths['manifest'] = tmp_path / 'missing.json'
    else:
        content = json.loads(paths['manifest'].read_text())
        for item in content['artifacts'].values():
            item['path'] = str((paths['manifest'].parent / item['path']).resolve())
        if mutation == 'schema':
            content['schema_version'] = 'unknown'
        else:
            content['identity_mapping']['blake2b'] = '0' * 128
        paths['manifest'] = tmp_path / 'altered.json'
        paths['manifest'].write_text(json.dumps(content))
    def forbidden(*args, **kwargs):
        raise AssertionError('配对失败前不应进入配置、模型或索引')
    for name in ('load_settings', 'OpenAIEmbeddingClient', 'QdrantProductIndex', 'create_reranker', 'bootstrap_product_index'):
        monkeypatch.setattr(run_esci, name, forbidden)
    output, index = tmp_path / 'output', tmp_path / 'index'
    # runner 的 selection 来自 manifest；独立 selection 的拒绝由同一校验器覆盖。
    if mutation == 'selection':
        with pytest.raises(ValueError, match='配对'):
            validate_pairing(**paths, profile=profile, baseline_version=version)
    else:
        with pytest.raises(ValueError, match='配对|版本|baseline'):
            await run_esci.run(SimpleNamespace(catalog=paths['catalog'], dataset_dir=paths['dataset_dir'],
                pairing_manifest=paths['manifest'], profile=profile, baseline_version=version,
                strategies=['embedding_only'] if profile == 'dev' else None,
                output=output, index_dir=index, frozen=tmp_path / 'never-read-frozen.json'))
    assert not output.exists() and not index.exists()
    assert not (tmp_path / 'baselines').exists()


def test_explicit_fixture_manifest_required_and_cannot_relabel_frozen_catalog(tmp_path):
    paths = frozen_paths()
    copied = tmp_path / 'catalog.jsonl.gz'
    copied.write_bytes(paths['catalog'].read_bytes())
    (tmp_path / 'ranking-dev.jsonl').write_bytes((paths['dataset_dir'] / 'ranking-dev.jsonl').read_bytes())
    manifest = write_fixture_pairing(tmp_path, copied)
    with pytest.raises(ValueError, match='显式启用'):
        validate_pairing(catalog=copied, ranking=tmp_path / 'ranking-dev.jsonl', manifest=manifest, profile='harness')
    assert validate_pairing(catalog=copied, ranking=tmp_path / 'ranking-dev.jsonl', manifest=manifest,
                            profile='harness', allow_fixture=True)['role'] == 'constructed_fixture'
    manifest = write_fixture_pairing(tmp_path, paths['catalog'])
    with pytest.raises(ValueError, match='不能给正式冻结目录'):
        validate_pairing(catalog=paths['catalog'], ranking=tmp_path / 'ranking-dev.jsonl', manifest=manifest,
                         profile='harness', allow_fixture=True)


def test_v2_ten_case_suite_is_bound_offline_with_four_ai_train_translations(tmp_path):
    paths = frozen_paths()
    suite, cases = build_suite({'catalog': paths['catalog'], 'selection': paths['selection'],
        'ranking': paths['dataset_dir'] / 'ranking-dev.jsonl',
        'translations': ROOT / 'eval/harness/esci-v2/translations-train-zh-v1.jsonl'},
        index_dir=tmp_path / 'index', local_model_cache=tmp_path / 'absent-model')
    validate_suite(suite, cases)
    assert len(cases) == 10 and suite['input_pairing']['dataset_version'] == 'v2'
    assert suite['allow_fixture_manifest'] is False
    chinese = [case for case in cases if case['scenario'] in ('crosslingual', 'embedding_unavailable')]
    assert [(case['source_query']['locale'], case['source_query']['query_id']) for case in chinese] == [
        ('us', 1567), ('es', 2580), ('jp', 3136), ('us', 2775)]
    assert not (tmp_path / 'index').exists() and not (tmp_path / 'absent-model').exists()


@pytest.mark.parametrize('foreign', ['catalog', 'selection', 'ranking'])
def test_harness_rejects_mixed_v1_v2_before_binding(tmp_path, foreign):
    correct, old = frozen_paths(), frozen_paths('v1')
    paths = {'catalog': correct['catalog'], 'selection': correct['selection'],
             'ranking': correct['dataset_dir'] / 'ranking-dev.jsonl',
             'translations': ROOT / 'eval/harness/esci-v2/translations-train-zh-v1.jsonl',
             'pairing_manifest': correct['manifest']}
    paths[foreign] = old['dataset_dir'] / 'ranking-dev.jsonl' if foreign == 'ranking' else old[foreign]
    with pytest.raises(ValueError, match='配对'):
        build_suite(paths, index_dir=tmp_path / 'index', local_model_cache=tmp_path / 'model')
    assert not (tmp_path / 'index').exists()


def test_frozen_manifest_cannot_be_resigned_as_another_pair(tmp_path):
    old, new = frozen_paths('v1'), frozen_paths()
    content = json.loads(old['manifest'].read_text())
    newer = json.loads(new['manifest'].read_text())
    content['artifacts']['catalog'] = newer['artifacts']['catalog']
    content['identity_mapping'] = newer['identity_mapping']
    forged = tmp_path / 'forged.json'
    forged.write_text(json.dumps(content))
    with pytest.raises(ValueError, match='正式配对 manifest 指纹'):
        validate_pairing(catalog=new['catalog'], dataset_dir=old['dataset_dir'], manifest=forged)


def test_preflight_cannot_claim_missing_model_or_credentials_ready(tmp_path):
    from scripts.eval.harness.esci import offline_resource_preflight
    status = offline_resource_preflight(tmp_path / 'missing')
    assert status['suite_bound'] and not status['runnable']
    assert status['remote_configured'] == 'not_checked'
    assert status['local_cache_verified'] is False and status['local_onnx_present'] is False
    assert status['model_calls'] == 0 and status['real_ten_scenarios'] == 'not_executed'
