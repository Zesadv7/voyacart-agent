"""仅合成落盘证据；不加载密钥、不连接网络、不调用真实模型。"""
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import tarfile

import pytest

from scripts.eval.harness import __main__ as harness_main, functional, reference_regression as reference
from scripts.eval.harness.contracts import ROOT, digest, load_suite

SUITE = ROOT / 'eval/harness/v1/glm-5.3-flash-coding-plan-suite.json'
SHA = 'a' * 40


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def modify(path, change):
    value = json.loads(path.read_text())
    change(value)
    save(path, value)


def lines(path, values):
    path.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')


def change_lines(path, change):
    values = [json.loads(line) for line in path.read_text().splitlines()]
    change(values)
    lines(path, values)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('offline test forbids network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    suite = load_suite(SUITE)
    config = suite['runtime']['model_config']
    environment = {'LLM_MODEL': config['model'], 'LLM_BASE_URL': config['base_url'], 'GITHUB_SHA': SHA}
    files = {'fixture.py': digest(b'pass\n')}
    source = {'sha256': digest(json.dumps(files, sort_keys=True).encode()), 'files': files}
    monkeypatch.setattr(functional, 'source_manifest', lambda: deepcopy(source))
    monkeypatch.setattr(harness_main, 'source_manifest', lambda: deepcopy(source))
    sample = {'kind': 'business', 'input_tokens': None, 'output_tokens': None,
              'elapsed_ms': 50, 'ttft_ms': None,
              'prompt_cache': {'model': config['model'], 'response_model': config['model'],
                               'response_model_matches': True, 'response_model_conflict': False,
                               'protocol_status': 'valid'}}
    shared = {'github_sha': SHA, 'suite_sha256': suite['_suite_sha256'],
              'dataset_sha256': suite['_dataset_sha256'], 'runtime': suite['runtime'],
              'gateway_sha256': digest(config['base_url'].encode()), 'source_stable': True}
    directories = {name: tmp_path / name for name in ('preflight', 'diagnostic', 'smoke')}
    for name, profile in [('diagnostic', 'reference_diagnostic'), ('smoke', 'smoke')]:
        directory = directories[name]
        directory.mkdir()
        (directory / 'traces').mkdir()
        (directory / 'rows').mkdir()
        selected = suite['profiles'][profile]
        rows = []
        ledger = []
        for case in selected['cases']:
            for strategy in ('current', 'candidate'):
                for repetition in range(selected['repetitions']):
                    identity = {'case_id': case, 'strategy': strategy, 'repetition': repetition}
                    trace = f'traces/{case}-{strategy}-{repetition}.jsonl'
                    rows.append({**identity, 'layer': 'context', 'passed': True, 'error': None,
                                 'checks': {'synthetic_check': True}, 'usage': [deepcopy(sample)],
                                 'elapsed_ms': 100, 'round_metrics': [{'elapsed_ms': 100, 'compacted': False}],
                                 'evidence_trace': trace})
                    save(directory / f'rows/{case}-{strategy}-{repetition}.json', rows[-1])
                    ledger.append({**identity, 'sample': deepcopy(sample)})
                    lines(directory / trace, [
                        {**identity, 'sequence': 1, 'kind': 'model_request', 'call_kind': 'business',
                         'payload': {'model': config['model'], 'temperature': config['temperature'],
                                     'messages': [{'role': 'user', 'content': 'synthetic'}]}},
                        {**identity, 'sequence': 2, 'kind': 'model_response_contract', 'call_kind': 'business',
                         'payload': {'requested_model': config['model'], 'response_models': [config['model']],
                                     'protocol_status': 'valid'}},
                    ])
        with tarfile.open(directory / 'source.tar.gz', 'w:gz') as archive:
            member = tarfile.TarInfo('fixture.py')
            member.size = len(b'pass\n')
            archive.addfile(member, io.BytesIO(b'pass\n'))
        manifest = {**deepcopy(shared), 'schema_version': suite['schema_version'], 'run_id': 'synthetic-only',
                    'status': 'completed', 'profile': name, 'requested_profile': profile,
                    'cases': selected['cases'], 'strategies': ['current', 'candidate'],
                    'repetitions': selected['repetitions'], 'model': config['model'],
                    'source': deepcopy(source), 'source_archive_sha256': digest((directory / 'source.tar.gz').read_bytes()),
                    'temperature': config['temperature'], 'physical_calls_started': len(rows),
                    'physical_call_limit': selected['max_model_calls'], 'gates': suite['gates'],
                    'execution_environment': {'python': 'synthetic', 'packages': {}, 'settings': {}},
                    'policy_overrides': {key: value['overrides'] for key, value in suite['strategies'].items()},
                    'pricing': {'currency': None, 'verified': False}, 'limits': ['synthetic-only'],
                    'contracts': {'passed': True, 'tests': 2, 'passed_count': 2, 'failures': 0,
                                  'errors': 0, 'skipped': 0, 'exit_code': 0, 'missing_test_modules': []}}
        save(directory / 'manifest.json', manifest)
        save(directory / 'results.json', rows)
        lines(directory / 'attempts.jsonl', ledger)
    directory = directories['preflight']
    directory.mkdir()
    save(directory / 'summary.json', {**shared, 'requested_model': config['model'], 'source_sha256': source['sha256'],
         'passed': True, 'calls': 6, 'physical_calls_started': 8, 'physical_call_limit': 8, 'tools_executed': 0})
    save(directory / 'results.json', [
        {'stream': stream, 'tool_choice': mode, 'passed': True, 'error': None, 'tools_executed': 0,
         'tool_roundtrip': mode == 'echo_probe', 'usage': [deepcopy(sample) for _ in range(2 if mode == 'echo_probe' else 1)]}
        for stream in (False, True) for mode in ('none', 'auto', 'echo_probe')])
    return directories, environment


def evaluate(evidence, smoke=False):
    directories, environment = evidence
    return reference.evaluate(SUITE, directories['preflight'], directories['diagnostic'],
                              directories['smoke'] if smoke else None, environment=environment)


def test_prior_only_ready_and_complete_smoke_passes_without_benefit(evidence):
    directories, _ = evidence
    ready = evaluate(evidence)
    assert ready['status'] == 'ready_for_smoke' and ready['errors'] == []
    assert ready['total_started'] == 20 and ready['stages']['smoke']['physical_calls_started'] is None
    # verify_run 只允许烟测，且原始证据不被重新渲染覆盖。
    before = {path: path.read_bytes() for directory in directories.values() for path in directory.rglob('*') if path.is_file()}
    result = evaluate(evidence, smoke=True)
    assert result['status'] == 'passed' and result['errors'] == []
    assert (result['scope'], result['diagnostic_kind'], result['holdout_status']) == (
        'reference_diagnostic_then_smoke', 'snapshot_final_turn_only', 'previously_seen_regression')
    assert result['max_model_calls'] == 206 and result['total_started'] == 32
    assert result['cost_benefit_status'] == 'not_evaluated' and result['production_deploy_authorized'] is False
    assert all(stage['status'] == 'passed' for stage in result['stages'].values())
    assert before == {path: path.read_bytes() for path in before}
    assert not (directories['smoke'] / 'summary.json').exists()


@pytest.mark.parametrize('field,value,code', [
    ('github_sha', 'b' * 40, 'source_revision_mismatch'),
    ('source', {'sha256': 'other', 'files': {}}, 'source_mismatch'),
    ('source_stable', False, 'source_mismatch'), ('suite_sha256', 'other', 'suite_mismatch'),
    ('dataset_sha256', 'other', 'dataset_mismatch'), ('runtime', {}, 'runtime_mismatch'),
    ('model', 'other', 'model_mismatch'), ('gateway_sha256', 'other', 'gateway_mismatch'),
    ('temperature', 0, 'runtime_mismatch'), ('policy_overrides', {}, 'runtime_mismatch'),
    ('gates', {}, 'runtime_mismatch'), ('profile', 'smoke', 'profile_mismatch'),
    ('requested_profile', 'smoke', 'profile_mismatch'), ('cases', [], 'matrix_mismatch'),
    ('repetitions', True, 'matrix_mismatch'), ('strategies', ['current'], 'matrix_mismatch'),
    ('status', 'failed', 'stage_incomplete'), ('error', 'provider_rejected', 'stage_incomplete'),
    ('provider_rejected', True, 'stage_incomplete'), ('physical_calls_started', 49, 'call_budget_invalid'),
    ('physical_calls_started', 0, 'call_budget_invalid'), ('physical_calls_started', True, 'call_budget_invalid'),
    ('physical_calls_started', 13, 'call_accounting_mismatch'), ('physical_call_limit', 49, 'call_budget_invalid'),
    ('physical_call_limit', 48.0, 'call_budget_invalid'), ('execution_environment', None, 'execution_environment_mismatch'),
    ('source_archive_sha256', 'wrong', 'source_archive_mismatch'),
])
def test_diagnostic_rejects_changed_manifest(evidence, field, value, code):
    directories, _ = evidence
    modify(directories['diagnostic'] / 'manifest.json', lambda data: data.update({field: value}))
    result = evaluate(evidence)
    assert result['status'] == 'failed' and {'stage': 'diagnostic', 'code': code} in result['errors']


@pytest.mark.parametrize('field,value', [('skipped', 1), ('failures', 1), ('errors', 1), ('exit_code', 1),
                                        ('tests', 0), ('passed_count', True), ('missing_test_modules', ['missing'])])
def test_no_skipped_or_incomplete_contracts(evidence, field, value):
    directories, _ = evidence
    modify(directories['diagnostic'] / 'manifest.json', lambda data: data['contracts'].update({field: value}))
    assert {'stage': 'diagnostic', 'code': 'safety_contract_failed'} in evaluate(evidence)['errors']


@pytest.mark.parametrize('mutation,code', [('missing', 'matrix_mismatch'), ('duplicate', 'matrix_mismatch'),
    ('failed', 'scenario_failed'), ('false_check', 'scenario_failed'), ('error', 'scenario_failed'),
    ('unknown_identity', 'model_identity_invalid'), ('unknown_protocol', 'model_protocol_invalid'),
    ('missing_usage', 'model_observation_missing'), ('wrong_repetition', 'matrix_mismatch')])
def test_rows_are_complete_and_quality_identity_protocol_are_known(evidence, mutation, code):
    directories, _ = evidence
    def change(rows):
        if mutation == 'missing': rows.pop()
        elif mutation == 'duplicate': rows[-1] = deepcopy(rows[0])
        elif mutation == 'failed': rows[0]['passed'] = False
        elif mutation == 'false_check': rows[0]['checks']['synthetic_check'] = False
        elif mutation == 'error': rows[0]['error'] = 'synthetic-canary'
        elif mutation == 'unknown_identity': rows[0]['usage'][0]['prompt_cache']['response_model_matches'] = None
        elif mutation == 'unknown_protocol': rows[0]['usage'][0]['prompt_cache']['protocol_status'] = 'unknown'
        elif mutation == 'wrong_repetition': rows[0]['repetition'] = True
        else: rows[0]['usage'] = []
    modify(directories['diagnostic'] / 'results.json', change)
    assert {'stage': 'diagnostic', 'code': code} in evaluate(evidence)['errors']


@pytest.mark.parametrize('mutation,code', [('sequence', 'trace_invalid'), ('identity', 'trace_invalid'),
    ('unpaired', 'call_accounting_mismatch'), ('double_request', 'trace_invalid'),
    ('response_model', 'model_identity_invalid'), ('protocol', 'model_protocol_invalid'),
    ('temperature', 'runtime_mismatch'), ('call_kind', 'trace_invalid')])
def test_trace_is_complete_paired_and_bound_to_row(evidence, mutation, code):
    directories, _ = evidence
    trace = directories['diagnostic'] / 'traces/h1-hold-first-current-0.jsonl'
    def change(events):
        if mutation == 'sequence': events[0]['sequence'] = 2
        elif mutation == 'identity': events[0]['case_id'] = 'other'
        elif mutation == 'unpaired': events.pop()
        elif mutation == 'double_request': events[1]['kind'] = 'model_request'
        elif mutation == 'response_model': events[1]['payload']['response_models'] = []
        elif mutation == 'protocol': events[1]['payload']['protocol_status'] = 'unknown'
        elif mutation == 'temperature': events[0]['payload']['temperature'] = 0
        else: events[1]['call_kind'] = 'summary'
    change_lines(trace, change)
    assert {'stage': 'diagnostic', 'code': code} in evaluate(evidence)['errors']


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'different_sample', 'different_identity'])
def test_attempt_ledger_matches_each_row_exactly(evidence, mutation):
    directories, _ = evidence
    def change(attempts):
        if mutation == 'missing': attempts.pop()
        elif mutation == 'extra': attempts.append(deepcopy(attempts[0]))
        elif mutation == 'different_sample': attempts[0]['sample']['input_tokens'] = 1
        else: attempts[0]['case_id'] = 'other'
    change_lines(directories['diagnostic'] / 'attempts.jsonl', change)
    assert {'stage': 'diagnostic', 'code': 'ledger_mismatch'} in evaluate(evidence)['errors']


def test_archive_file_hash_must_match_even_with_matching_archive_hash(evidence):
    directories, _ = evidence
    directory = directories['diagnostic']
    with tarfile.open(directory / 'source.tar.gz', 'w:gz') as archive:
        member = tarfile.TarInfo('fixture.py')
        member.size = 4
        archive.addfile(member, io.BytesIO(b'evil'))
    modify(directory / 'manifest.json', lambda data: data.update(
        source_archive_sha256=digest((directory / 'source.tar.gz').read_bytes())))
    assert {'stage': 'diagnostic', 'code': 'source_archive_mismatch'} in evaluate(evidence)['errors']


@pytest.mark.parametrize('name,filename', [('preflight', 'summary.json'), ('diagnostic', 'manifest.json'),
                                        ('smoke', 'manifest.json')])
def test_missing_stage_is_failed_and_count_remains_unknown(evidence, name, filename):
    directories, _ = evidence
    (directories[name] / filename).unlink()
    result = evaluate(evidence, smoke=True)
    assert result['status'] == 'failed' and result['total_started'] is None
    assert result['stages'][name]['physical_calls_started'] is None
    assert {'stage': name, 'code': 'missing_evidence'} in result['errors']


def test_preflight_failure_cannot_unlock_smoke(evidence):
    directories, _ = evidence
    modify(directories['preflight'] / 'summary.json', lambda data: data.update(passed=False))
    assert evaluate(evidence)['status'] == 'failed'
    assert evaluate(evidence, smoke=True)['status'] == 'failed'


def test_smoke_failure_cannot_claim_full_pass(evidence):
    directories, _ = evidence
    modify(directories['smoke'] / 'results.json', lambda rows: rows[0].update(passed=False))
    result = evaluate(evidence, smoke=True)
    assert result['status'] == 'failed' and result['stages']['diagnostic']['status'] == 'passed'


def test_same_job_execution_environment_required(evidence):
    directories, _ = evidence
    modify(directories['smoke'] / 'manifest.json', lambda data: data.update(execution_environment={}))
    assert {'stage': 'smoke', 'code': 'execution_environment_mismatch'} in evaluate(evidence, smoke=True)['errors']


def test_cli_fixed_summary_and_no_exception_or_configuration_leaks(evidence, tmp_path, capsys):
    directories, environment = evidence
    (directories['diagnostic'] / 'manifest.json').write_text('synthetic-secret-canary https://private.invalid/token')
    output = tmp_path / 'summary.json'
    assert reference.main(['--suite', str(SUITE), '--preflight', str(directories['preflight']),
                           '--diagnostic', str(directories['diagnostic']), '--output', str(output)],
                          environment=environment) == 2
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result == json.loads(output.read_text())
    assert set(result) == {'scope', 'diagnostic_kind', 'holdout_status', 'cost_benefit_status',
                           'production_deploy_authorized', 'max_model_calls', 'total_started', 'status', 'stages', 'errors'}
    assert result['errors'] == [{'stage': 'diagnostic', 'code': 'invalid_evidence'}]
    assert 'synthetic-secret' not in text and 'private.invalid' not in text and environment['LLM_BASE_URL'] not in text


def test_verify_exception_is_redacted_and_diagnostic_never_uses_verify(evidence, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic-secret-canary')
    monkeypatch.setattr(reference, 'verify_run', fail)
    assert evaluate(evidence)['status'] == 'ready_for_smoke'
    result = evaluate(evidence, smoke=True)
    assert result['errors'] == [{'stage': 'smoke', 'code': 'invalid_evidence'}]
    assert 'synthetic-secret' not in json.dumps(result)


def test_configuration_mismatch_is_redacted(evidence):
    directories, environment = evidence
    environment['LLM_BASE_URL'] = 'https://private.invalid/synthetic-secret-canary'
    result = evaluate(evidence)
    assert result['errors'] == [{'stage': 'configuration', 'code': 'configuration_mismatch'}]
    assert result['total_started'] is None and 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('field,value', [('repetitions', 1), ('cases', ['h1-dev-second']), ('max_model_calls', 49)])
def test_reference_profile_is_frozen(evidence, monkeypatch, field, value):
    suite = load_suite(SUITE)
    suite['profiles']['reference_diagnostic'][field] = value
    monkeypatch.setattr(reference, 'load_suite', lambda path: suite)
    assert evaluate(evidence)['errors'] == [{'stage': 'configuration', 'code': 'invalid_suite'}]


@pytest.mark.parametrize('folder,code', [('traces', 'trace_invalid'), ('rows', 'row_evidence_mismatch')])
def test_unreferenced_files_are_rejected(evidence, folder, code):
    directories, _ = evidence
    (directories['diagnostic'] / folder / 'unreferenced.jsonl').write_text('{}\n')
    assert {'stage': 'diagnostic', 'code': code} in evaluate(evidence)['errors']


def test_row_checkpoint_matches_results(evidence):
    directories, _ = evidence
    modify(directories['diagnostic'] / 'rows/h1-hold-first-current-0.json', lambda row: row.update(passed=False))
    assert {'stage': 'diagnostic', 'code': 'row_evidence_mismatch'} in evaluate(evidence)['errors']
