"""已见历史引用 snapshot 回归诊断后才允许普通冒烟；不授予收益或发布结论。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile

from scripts.eval.harness.__main__ import verify_run
from scripts.eval.harness.contracts import digest, load_suite, write_json
from scripts.eval.harness.functional import (
    InvalidEvidence, calls, integer, model_samples, preflight, provenance,
    read_json, require, validate_suite,
)

REFERENCE_CASES = ['h1-hold-first', 'h1-hold-last', 'h1-dev-second']
SMOKE_CASES = ['h1-dev-sku', 'h1-dev-budget', 'h1-dev-stock', 'h1-dev-cost',
               'agent-sku', 'agent-approval']
STAGES = {'preflight': (0, 6, 8), 'diagnostic': (3, 12, 48), 'smoke': (6, 12, 150)}
ERROR_CODES = frozenset({
    'invalid_suite', 'configuration_mismatch', 'source_revision_mismatch', 'source_mismatch',
    'suite_mismatch', 'dataset_mismatch', 'runtime_mismatch', 'model_mismatch', 'gateway_mismatch',
    'call_budget_invalid', 'preflight_failed', 'call_accounting_mismatch', 'model_observation_missing',
    'model_identity_invalid', 'model_protocol_invalid', 'missing_evidence', 'invalid_evidence',
    'profile_mismatch', 'matrix_mismatch', 'scenario_failed', 'stage_incomplete',
    'source_archive_mismatch', 'safety_contract_failed', 'stage_verification_failed',
    'execution_environment_mismatch', 'ledger_mismatch', 'trace_invalid', 'row_evidence_mismatch', 'summary_write_failed',
})


def validate_profiles(suite):
    for name, ids, repetitions, limit in (
        ('reference_diagnostic', REFERENCE_CASES, 2, 48), ('smoke', SMOKE_CASES, 1, 150),
    ):
        profile = suite['profiles'][name]
        require(profile['cases'] == ids and type(profile['repetitions']) is int
                and profile['repetitions'] == repetitions
                and type(profile['max_model_calls']) is int
                and profile['max_model_calls'] == limit, 'invalid_suite')
    cases = {case['id']: case for case in suite['_cases']}
    require(all(cases[name].get('mode') == 'snapshot' and cases[name].get('rounds') == 0
                and cases[name].get('fixture', {}).get('rounds') == 24
                for name in REFERENCE_CASES), 'invalid_suite')


def safe_file(directory, name):
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and '..' not in relative.parts and '\\' not in name,
            'invalid_evidence')
    path = directory / name
    require(not directory.is_symlink() and not any((directory / Path(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)), 'invalid_evidence')
    require(path.is_file(), 'missing_evidence')
    return path


def read_lines(directory, name):
    return [json.loads(line) for line in safe_file(directory, name).read_text(encoding='utf-8').splitlines()
            if line.strip()]


def validate_archive(directory, manifest):
    archive = safe_file(directory, 'source.tar.gz')
    require(digest(archive.read_bytes()) == manifest.get('source_archive_sha256'), 'source_archive_mismatch')
    files = manifest['source']['files']
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        names = [member.name for member in members]
        require(len(names) == len(set(names)) and set(names) == set(files), 'source_archive_mismatch')
        for member in members:
            path = PurePosixPath(member.name)
            require(member.isfile() and not path.is_absolute() and '..' not in path.parts
                    and '\\' not in member.name, 'source_archive_mismatch')
            stream = source.extractfile(member)
            require(stream is not None and digest(stream.read()) == files[member.name], 'source_archive_mismatch')


def identity(row):
    require(isinstance(row, dict) and type(row.get('repetition')) is int
            and isinstance(row.get('case_id'), str) and isinstance(row.get('strategy'), str), 'matrix_mismatch')
    return row['case_id'], row['strategy'], row['repetition']


def validate_row_files(directory, rows):
    # 每个落盘原始行与 trace 恰好归属于矩阵，防止遗漏旧调用或混入别次运行。
    for folder, suffix, code in (('rows', '.json', 'row_evidence_mismatch'),
                                 ('traces', '.jsonl', 'trace_invalid')):
        target = directory / folder
        require(target.is_dir() and not target.is_symlink(), 'missing_evidence')
        expected = {f'{row["case_id"]}-{row["strategy"]}-{row["repetition"]}{suffix}' for row in rows}
        require({path.name for path in target.iterdir()} == expected, code)
    for row in rows:
        name = f'rows/{row["case_id"]}-{row["strategy"]}-{row["repetition"]}.json'
        stored = json.loads(safe_file(directory, name).read_text(encoding='utf-8'))
        require(stored == row, 'row_evidence_mismatch')


def validate_trace(directory, row, model, temperature):
    key = identity(row)
    name = f'traces/{key[0]}-{key[1]}-{key[2]}.jsonl'
    require(re.fullmatch(r'traces/[a-z0-9_-]+\.jsonl', name) is not None
            and row.get('evidence_trace') == name, 'trace_invalid')
    events = read_lines(directory, name)
    pending = None
    responses = 0
    for number, event in enumerate(events, 1):
        require(identity(event) == key and type(event.get('sequence')) is int
                and event['sequence'] == number, 'trace_invalid')
        kind = event.get('kind')
        if kind not in ('model_request', 'model_response_contract'):
            continue
        payload = event.get('payload')
        require(isinstance(payload, dict), 'trace_invalid')
        if kind == 'model_request':
            require(pending is None and payload.get('model') == model, 'trace_invalid')
            require(type(payload.get('temperature')) is type(temperature)
                    and payload['temperature'] == temperature, 'runtime_mismatch')
            require(isinstance(payload.get('messages'), list) and bool(payload['messages']), 'trace_invalid')
            pending = event.get('call_kind')
            require(isinstance(pending, str), 'trace_invalid')
        else:
            require(pending is not None and event.get('call_kind') == pending, 'trace_invalid')
            require(payload.get('requested_model') == model and payload.get('response_models') == [model],
                    'model_identity_invalid')
            require(payload.get('protocol_status') == 'valid', 'model_protocol_invalid')
            require(responses < len(row['usage']) and row['usage'][responses].get('kind') == pending,
                    'call_accounting_mismatch')
            pending = None
            responses += 1
    require(pending is None and responses == len(row['usage']), 'call_accounting_mismatch')
    return responses


def model_stage(directory, profile, suite, expected, state):
    manifest = read_json(directory, 'manifest.json')
    selected = suite['profiles'][profile]
    started = calls(manifest, selected['max_model_calls'], state)
    provenance(manifest, suite, expected)
    require(manifest.get('status') == 'completed' and manifest.get('error') is None
            and manifest.get('provider_rejected', False) is False, 'stage_incomplete')
    actual = 'diagnostic' if profile == 'reference_diagnostic' else 'smoke'
    require(manifest.get('profile') == actual and manifest.get('requested_profile') == profile, 'profile_mismatch')
    require(manifest.get('cases') == selected['cases'] and manifest.get('strategies') == ['current', 'candidate']
            and type(manifest.get('repetitions')) is int
            and manifest['repetitions'] == selected['repetitions'], 'matrix_mismatch')
    require(manifest.get('policy_overrides') == {name: suite['strategies'][name]['overrides']
            for name in ('current', 'candidate')} and manifest.get('gates') == suite['gates']
            and type(manifest.get('temperature')) is type(suite['runtime']['model_config']['temperature'])
            and manifest.get('temperature') == suite['runtime']['model_config']['temperature'], 'runtime_mismatch')
    require(isinstance(manifest.get('execution_environment'), dict), 'execution_environment_mismatch')
    validate_archive(directory, manifest)
    contracts = manifest.get('contracts', {})
    require(contracts.get('passed') is True and integer(contracts.get('tests'), 1)
            and type(contracts.get('passed_count')) is int and contracts['passed_count'] == contracts['tests']
            and all(type(contracts.get(key)) is int and contracts[key] == 0
                    for key in ('exit_code', 'failures', 'errors', 'skipped'))
            and contracts.get('missing_test_modules') == [], 'safety_contract_failed')
    rows = read_json(directory, 'results.json')
    require(isinstance(rows, list), 'matrix_mismatch')
    keys = [identity(row) for row in rows]
    expected_keys = {(case, strategy, repetition) for case in selected['cases']
                     for strategy in ('current', 'candidate') for repetition in range(selected['repetitions'])}
    require(len(keys) == len(expected_keys) and set(keys) == expected_keys, 'matrix_mismatch')
    require(all(row.get('passed') is True and row.get('error') is None
                and isinstance(row.get('checks'), dict) and row['checks']
                and all(value is True for value in row['checks'].values()) for row in rows), 'scenario_failed')
    require(model_samples(rows, expected['model']) == started, 'call_accounting_mismatch')
    validate_row_files(directory, rows)
    ledger = read_lines(directory, 'attempts.jsonl')
    grouped = {key: [] for key in keys}
    for attempt in ledger:
        key = identity(attempt)
        require(key in grouped, 'ledger_mismatch')
        grouped[key].append(attempt.get('sample'))
    require(len(ledger) == started and all(grouped[identity(row)] == row['usage'] for row in rows), 'ledger_mismatch')
    count = sum(validate_trace(directory, row, expected['model'], manifest['temperature']) for row in rows)
    require(count == started, 'call_accounting_mismatch')
    if profile == 'smoke':
        # verify 会重新渲染；在临时副本验证，绝不覆盖原运行与失败证据。
        with tempfile.TemporaryDirectory(prefix='reference-verify-') as temporary:
            target = Path(temporary)
            write_json(target / 'manifest.json', manifest)
            write_json(target / 'results.json', rows)
            require(verify_run(target, minimum='smoke')['passed'] is True, 'stage_verification_failed')
    return manifest['execution_environment']


def evaluate(suite_path, preflight_directory, diagnostic_directory, smoke_directory=None, *, environment=None):
    summary = {'scope': 'reference_diagnostic_then_smoke', 'diagnostic_kind': 'snapshot_final_turn_only',
               'holdout_status': 'previously_seen_regression', 'cost_benefit_status': 'not_evaluated',
               'production_deploy_authorized': False, 'max_model_calls': 206, 'total_started': None,
               'status': 'failed', 'stages': {}, 'errors': []}
    for name, (scenarios, instances, limit) in STAGES.items():
        summary['stages'][name] = {'status': 'not_evaluated', 'scenarios': scenarios, 'instances': instances,
                                   'max_model_calls': limit, 'physical_calls_started': None}
    def record_error(stage, error):
        code = str(error) if isinstance(error, InvalidEvidence) and str(error) in ERROR_CODES else 'invalid_evidence'
        summary['errors'].append({'stage': stage, 'code': code})
    try:
        suite = load_suite(suite_path)
        expected = validate_suite(suite, os.environ if environment is None else environment)
        validate_profiles(suite)
    except InvalidEvidence as error:
        record_error('configuration', error)
        return summary
    except Exception:
        summary['errors'].append({'stage': 'configuration', 'code': 'invalid_suite'})
        return summary
    planned = [('preflight', Path(preflight_directory)), ('diagnostic', Path(diagnostic_directory))]
    if smoke_directory is not None:
        planned.append(('smoke', Path(smoke_directory)))
    environments = {}
    for name, directory in planned:
        state = summary['stages'][name]
        try:
            if name == 'preflight':
                preflight(directory, suite, expected, state)
            else:
                environments[name] = model_stage(directory, 'reference_diagnostic' if name == 'diagnostic' else 'smoke',
                                                 suite, expected, state)
                if name == 'smoke' and 'diagnostic' in environments:
                    require(environments['smoke'] == environments['diagnostic'], 'execution_environment_mismatch')
            state['status'] = 'passed'
        except Exception as error:
            state['status'] = 'failed'
            record_error(name, error)
    counts = [summary['stages'][name]['physical_calls_started'] for name, _ in planned]
    if all(integer(count) for count in counts):
        summary['total_started'] = sum(counts)
    if not summary['errors']:
        summary['status'] = 'passed' if smoke_directory is not None else 'ready_for_smoke'
    return summary


def main(argv=None, *, environment=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--preflight', type=Path, required=True)
    parser.add_argument('--diagnostic', type=Path, required=True)
    parser.add_argument('--smoke', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    summary = evaluate(args.suite, args.preflight, args.diagnostic, args.smoke, environment=environment)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, summary)
    except Exception:
        summary['errors'].append({'stage': 'output', 'code': 'summary_write_failed'})
        summary['status'] = 'failed'
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0 if not summary['errors'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
