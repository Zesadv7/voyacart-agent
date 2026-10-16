"""完整功能门禁仅用合成落盘证据，不加载配置凭据、不调用模型。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.eval.harness import __main__ as harness_main
from scripts.eval.harness import functional
from scripts.eval.harness.contracts import ROOT, digest, load_suite


SUITE = ROOT / 'eval/harness/v1/glm-5.3-flash-coding-plan-suite.json'
SHA = 'a' * 40


def save(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')


def modify(path, change):
    value = json.loads(path.read_text())
    change(value)
    save(path, value)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    suite = load_suite(SUITE)
    config = suite['runtime']['model_config']
    environment = {'LLM_MODEL': config['model'], 'LLM_BASE_URL': config['base_url'], 'GITHUB_SHA': SHA}
    source = {'sha256': digest(b'synthetic-source'), 'files': {'fixture.py': digest(b'pass')}}
    monkeypatch.setattr(functional, 'source_manifest', lambda: deepcopy(source))
    monkeypatch.setattr(harness_main, 'source_manifest', lambda: deepcopy(source))
    sample = {'kind': 'business', 'input_tokens': None, 'output_tokens': None,
              'elapsed_ms': 50, 'ttft_ms': None,
              'prompt_cache': {'model': config['model'], 'response_model': config['model'],
                               'response_model_matches': True, 'response_model_conflict': False,
                               'protocol_status': 'valid'}}
    prior, holdout = tmp_path / 'prior', tmp_path / 'holdout'
    shared = {'github_sha': SHA, 'suite_sha256': suite['_suite_sha256'],
              'dataset_sha256': suite['_dataset_sha256'], 'runtime': suite['runtime'],
              'gateway_sha256': digest(config['base_url'].encode()), 'source_stable': True}
    for profile, directory in [('dev', prior / 'ci-model'), ('release', holdout)]:
        directory.mkdir(parents=True)
        selected = suite['profiles'][profile]
        rows = [{'case_id': case, 'strategy': strategy, 'repetition': repetition,
                 'layer': 'context', 'passed': True, 'checks': {'synthetic_check': True},
                 'error': None, 'elapsed_ms': 100, 'usage': [deepcopy(sample)],
                 'round_metrics': [{'elapsed_ms': 100, 'compacted': False}]}
                for case in selected['cases'] for strategy in ('current', 'candidate')
                for repetition in range(selected['repetitions'])]
        archive = b'synthetic archive bytes; no repository or secrets included'
        (directory / 'source.tar.gz').write_bytes(archive)
        manifest = {**deepcopy(shared), 'schema_version': suite['schema_version'], 'run_id': 'synthetic-only',
                    'status': 'completed', 'profile': profile, 'requested_profile': profile,
                    'cases': selected['cases'], 'strategies': ['current', 'candidate'],
                    'repetitions': selected['repetitions'], 'model': config['model'],
                    'source': deepcopy(source), 'source_archive_sha256': digest(archive),
                    'temperature': config['temperature'], 'physical_calls_started': len(rows),
                    'physical_call_limit': selected['max_model_calls'], 'gates': suite['gates'],
                    'policy_overrides': {name: value['overrides'] for name, value in suite['strategies'].items()},
                    'pricing': {'currency': None, 'verified': False}, 'limits': ['synthetic-only'],
                    'contracts': {'passed': True, 'tests': 2, 'passed_count': 2, 'failures': 0,
                                  'errors': 0, 'skipped': 0, 'exit_code': 0, 'missing_test_modules': []}}
        save(directory / 'manifest.json', manifest)
        save(directory / 'results.json', rows)
    preflight = prior / 'ci-preflight'
    preflight.mkdir()
    save(preflight / 'summary.json', {**shared, 'requested_model': config['model'],
         'source_sha256': source['sha256'], 'passed': True, 'calls': 6,
         'physical_calls_started': 8, 'physical_call_limit': 8, 'tools_executed': 0,
         'usage_known': False, 'billing_verified': False})
    save(preflight / 'results.json', [
        {'stream': stream, 'tool_choice': mode, 'passed': True, 'error': None,
         'tools_executed': 0, 'tool_roundtrip': mode == 'echo_probe',
         'usage': [deepcopy(sample) for _ in range(2 if mode == 'echo_probe' else 1)]}
        for stream in (False, True) for mode in ('none', 'auto', 'echo_probe')])
    return prior, holdout, environment


def test_complete_functional_success_allows_unknown_cost_and_keeps_benefit_blocked(evidence):
    prior, holdout, environment = evidence
    summary = functional.evaluate(SUITE, prior, holdout, environment=environment)
    assert summary['scope'] == 'full_functional'
    assert summary['functional_status'] == 'passed' and summary['errors'] == []
    assert summary['cost_benefit_status'] == 'not_evaluated'
    assert summary['production_deploy_authorized'] is False
    assert (summary['scenarios'], summary['instances'], summary['max_model_calls']) == (48, 258, 3608)
    assert summary['total_started'] == 266
    assert all(stage['status'] == 'passed' for stage in summary['stages'].values())
    # 原收益门禁没有被改写或冒充已验收。
    report = json.loads((holdout / 'summary.json').read_text())
    assert report['gates']['candidate']['status'] == 'BLOCKED'
    assert report['strategies']['current']['input_tokens']['total'] is None


def test_prior_only_persists_ready_state_without_full_success(evidence, tmp_path, capsys):
    prior, _, environment = evidence
    output = tmp_path / 'prior-summary.json'
    assert functional.main(['--suite', str(SUITE), '--prior', str(prior), '--output', str(output)],
                           environment=environment) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == json.loads(output.read_text())
    assert result['scope'] == 'full_functional_prior' and result['status'] == 'ready_for_holdout'
    assert 'functional_status' not in result and result['stages']['holdout']['status'] == 'not_evaluated'
    assert result['total_started'] == 68


@pytest.mark.parametrize('stage,filename', [('preflight', 'summary.json'), ('dev', 'results.json'),
                                         ('holdout', 'manifest.json')])
def test_missing_phase_is_failed_and_never_found_in_neighboring_run(evidence, stage, filename):
    prior, holdout, environment = evidence
    directory = {'preflight': prior / 'ci-preflight', 'dev': prior / 'ci-model', 'holdout': holdout}[stage]
    (directory / filename).rename(directory / ('other-run-' + filename))
    result = functional.evaluate(SUITE, prior, holdout, environment=environment)
    assert result['functional_status'] == 'failed'
    assert {'stage': stage, 'code': 'missing_evidence'} in result['errors']


@pytest.mark.parametrize('field,value,code', [
    ('github_sha', 'b' * 40, 'source_revision_mismatch'),
    ('source', {'sha256': 'wrong', 'files': {}}, 'source_mismatch'),
    ('source_stable', False, 'source_mismatch'),
    ('suite_sha256', 'wrong', 'suite_mismatch'),
    ('dataset_sha256', 'wrong', 'dataset_mismatch'),
    ('model', 'wrong', 'model_mismatch'),
    ('gateway_sha256', 'wrong', 'gateway_mismatch'),
    ('runtime', {}, 'runtime_mismatch'),
    ('profile', 'release', 'profile_mismatch'),
    ('requested_profile', 'smoke', 'profile_mismatch'),
    ('cases', [], 'matrix_mismatch'),
    ('strategies', ['current'], 'matrix_mismatch'),
    ('repetitions', True, 'matrix_mismatch'),
    ('physical_calls_started', 601, 'call_budget_invalid'),
    ('physical_calls_started', True, 'call_budget_invalid'),
    ('physical_calls_started', 60.0, 'call_budget_invalid'),
    ('physical_calls_started', 0, 'call_budget_invalid'),
    ('physical_call_limit', 601, 'call_budget_invalid'),
    ('physical_call_limit', 600.0, 'call_budget_invalid'),
    ('source_archive_sha256', 'wrong', 'source_archive_mismatch'),
    ('contracts', {'passed': True, 'tests': 1, 'skipped': 1}, 'safety_contract_failed'),
])
def test_prior_rejects_wrong_stage_provenance_shape_or_budget(evidence, field, value, code):
    prior, _, environment = evidence
    modify(prior / 'ci-model/manifest.json', lambda data: data.update({field: value}))
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert result['status'] == 'failed'
    assert {'stage': 'dev', 'code': code} in result['errors']


@pytest.mark.parametrize('field,value,code', [
    ('physical_calls_started', 3001, 'call_budget_invalid'),
    ('gateway_sha256', 'other-run', 'gateway_mismatch'),
    ('github_sha', 'b' * 40, 'source_revision_mismatch'),
])
def test_holdout_has_separate_limit_and_same_run_provenance(evidence, field, value, code):
    prior, holdout, environment = evidence
    modify(holdout / 'manifest.json', lambda data: data.update({field: value}))
    result = functional.evaluate(SUITE, prior, holdout, environment=environment)
    assert result['functional_status'] == 'failed'
    assert {'stage': 'holdout', 'code': code} in result['errors']


@pytest.mark.parametrize('field,value,code', [
    ('passed', False, 'preflight_failed'), ('source_stable', False, 'source_mismatch'),
    ('physical_calls_started', 7, 'preflight_failed'),
    ('physical_calls_started', 9, 'call_budget_invalid'),
    ('physical_call_limit', True, 'call_budget_invalid'),
    ('gateway_sha256', 'wrong', 'gateway_mismatch'),
])
def test_preflight_must_be_successful_eight_call_contract(evidence, field, value, code):
    prior, _, environment = evidence
    modify(prior / 'ci-preflight/summary.json', lambda data: data.update({field: value}))
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert {'stage': 'preflight', 'code': code} in result['errors']


@pytest.mark.parametrize('field,value,code', [
    ('response_model_matches', None, 'model_identity_invalid'),
    ('response_model_matches', False, 'model_identity_invalid'),
    ('response_model', 'other-model', 'model_identity_invalid'),
    ('response_model_conflict', True, 'model_identity_invalid'),
    ('protocol_status', None, 'model_protocol_invalid'),
    ('protocol_status', 'unknown', 'model_protocol_invalid'),
    ('protocol_status', 'tools_forbidden', 'model_protocol_invalid'),
])
def test_unknown_identity_and_protocol_never_pass(evidence, field, value, code):
    prior, _, environment = evidence
    modify(prior / 'ci-model/results.json', lambda rows: rows[0]['usage'][0]['prompt_cache'].update({field: value}))
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert {'stage': 'dev', 'code': code} in result['errors']


@pytest.mark.parametrize('mutation,code', [('failed', 'scenario_failed'), ('duplicate', 'stage_verification_failed'),
                                        ('slow', 'latency_gate_failed'), ('unknown_latency', 'latency_gate_failed'),
                                        ('missing_usage', 'model_observation_missing')])
def test_quality_integrity_and_latency_still_gate_functionality(evidence, mutation, code):
    prior, _, environment = evidence
    def change(rows):
        if mutation == 'failed':
            rows[0]['passed'] = False
        elif mutation == 'duplicate':
            rows[-1] = deepcopy(rows[0])
        elif mutation == 'missing_usage':
            rows[0]['usage'] = []
        else:
            for row in rows:
                if row['strategy'] == 'candidate':
                    row['round_metrics'][0]['elapsed_ms'] = None if mutation == 'unknown_latency' else 116
    modify(prior / 'ci-model/results.json', change)
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert {'stage': 'dev', 'code': code} in result['errors']


def test_failure_cli_saves_redacted_summary_and_returns_nonzero(evidence, tmp_path, capsys):
    prior, holdout, environment = evidence
    (holdout / 'manifest.json').write_text('invalid synthetic-secret-canary https://private.invalid/token')
    output = tmp_path / 'failed.json'
    assert functional.main(['--suite', str(SUITE), '--prior', str(prior), '--holdout', str(holdout),
                            '--output', str(output)], environment=environment) == 2
    text = capsys.readouterr().out
    saved = json.loads(output.read_text())
    assert json.loads(text) == saved and saved['functional_status'] == 'failed'
    assert saved['errors'] == [{'stage': 'holdout', 'code': 'invalid_evidence'}]
    assert 'synthetic-secret' not in text and 'private.invalid' not in text
    assert environment['LLM_BASE_URL'] not in text


def test_runtime_environment_mismatch_does_not_echo_inputs(evidence):
    prior, _, environment = evidence
    environment['LLM_BASE_URL'] = 'https://private.invalid/synthetic-secret-canary'
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert result['errors'] == [{'stage': 'configuration', 'code': 'configuration_mismatch'}]
    assert 'private.invalid' not in json.dumps(result) and 'synthetic-secret' not in json.dumps(result)


def test_verify_exceptions_are_redacted(evidence, monkeypatch):
    prior, _, environment = evidence
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic-secret-canary')
    monkeypatch.setattr(functional, 'verify_run', fail)
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert result['errors'] == [{'stage': 'dev', 'code': 'invalid_evidence'}]
    assert 'synthetic-secret' not in json.dumps(result)


def test_non_glm_suite_cannot_enter_full_functional(evidence):
    prior, _, environment = evidence
    result = functional.evaluate(ROOT / 'eval/harness/v1/suite.json', prior, environment=environment)
    assert result['errors'] == [{'stage': 'configuration', 'code': 'invalid_suite'}]


def test_standard_glm_suite_uses_its_own_explicit_gateway(evidence):
    prior, holdout, environment = evidence
    path = ROOT / 'eval/harness/v1/glm-5.3-flash-suite.json'
    suite = load_suite(path)
    environment['LLM_BASE_URL'] = suite['runtime']['model_config']['base_url']
    updates = {'suite_sha256': suite['_suite_sha256'], 'runtime': suite['runtime'],
               'gateway_sha256': digest(environment['LLM_BASE_URL'].encode())}
    for filename in (prior / 'ci-preflight/summary.json', prior / 'ci-model/manifest.json', holdout / 'manifest.json'):
        modify(filename, lambda data: data.update(deepcopy(updates)))
    result = functional.evaluate(path, prior, holdout, environment=environment)
    assert result['functional_status'] == 'passed'


def test_missing_current_sha_cannot_validate_otherwise_valid_prior(evidence):
    prior, _, environment = evidence
    environment.pop('GITHUB_SHA')
    result = functional.evaluate(SUITE, prior, environment=environment)
    assert result['errors'] == [{'stage': 'configuration', 'code': 'source_revision_mismatch'}]
