"""两阶段完整功能验收；费用收益仍未知，绝不授予生产发布权限。"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re

from scripts.eval.harness.__main__ import verify_run
from scripts.eval.harness.contracts import digest, load_suite, source_manifest, write_json
from scripts.eval.harness.model_config import GLM_CONFIG, GLM_CODING_CONFIG


STAGES = {'preflight': (0, 0, 8), 'dev': (15, 60, 600), 'holdout': (33, 198, 3000)}


class InvalidEvidence(Exception):
    """只携带固定枚举，不能把输入、网关或异常正文带入汇总。"""


def require(condition, code):
    if not condition:
        raise InvalidEvidence(code)


def read_json(directory, filename):
    path = directory / filename
    require(not directory.is_symlink() and not path.is_symlink(), 'invalid_evidence')
    require(path.is_file(), 'missing_evidence')
    return json.loads(path.read_text(encoding='utf-8'))


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def model_samples(rows, model):
    """缺失用量金额允许，缺失身份或协议证据不允许。"""
    count = 0
    for row in rows:
        samples = row.get('usage')
        require(isinstance(samples, list) and bool(samples), 'model_observation_missing')
        for sample in samples:
            cache = sample.get('prompt_cache', {})
            require(cache.get('model') == model and cache.get('response_model') == model
                    and cache.get('response_model_matches') is True
                    and cache.get('response_model_conflict', False) is False, 'model_identity_invalid')
            require(cache.get('protocol_status') == 'valid', 'model_protocol_invalid')
            count += 1
    return count


def validate_suite(suite, environment):
    config = suite['runtime'].get('model_config')
    require(config in (GLM_CONFIG, GLM_CODING_CONFIG), 'invalid_suite')
    require(set(suite['strategies']) == {'current', 'candidate'}, 'invalid_suite')
    require(len(suite['_cases']) == 48, 'invalid_suite')
    for name, scenarios, repetitions, limit in (('dev', 15, 2, 600), ('release', 33, 3, 3000)):
        profile = suite['profiles'][name]
        require(len(profile['cases']) == scenarios and type(profile['repetitions']) is int
                and profile['repetitions'] == repetitions and type(profile['max_model_calls']) is int
                and profile['max_model_calls'] == limit, 'invalid_suite')
    require(not set(suite['profiles']['dev']['cases']) & set(suite['profiles']['release']['cases']), 'invalid_suite')
    require(suite['gates']['ordinary_p95_max_ratio'] == 1.15, 'invalid_suite')
    require(environment.get('LLM_MODEL') == config['model'], 'configuration_mismatch')
    gateway = environment.get('LLM_BASE_URL', '')
    require(isinstance(gateway, str) and gateway.rstrip('/') == config['base_url'], 'configuration_mismatch')
    revision = environment.get('GITHUB_SHA', '')
    require(isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision), 'source_revision_mismatch')
    return {'model': config['model'], 'gateway_sha256': digest(gateway.encode()),
            'github_sha': revision, 'source': source_manifest()}


def provenance(data, suite, expected, *, preflight=False):
    require(data.get('github_sha') == expected['github_sha'], 'source_revision_mismatch')
    source = data.get('source_sha256') if preflight else data.get('source', {}).get('sha256')
    require(source == expected['source']['sha256'] and data.get('source_stable') is True, 'source_mismatch')
    if not preflight:
        require(data.get('source') == expected['source'], 'source_mismatch')
    require(data.get('suite_sha256') == suite['_suite_sha256'], 'suite_mismatch')
    require(data.get('dataset_sha256') == suite['_dataset_sha256'], 'dataset_mismatch')
    require(data.get('runtime') == suite['runtime'], 'runtime_mismatch')
    require(data.get('requested_model' if preflight else 'model') == expected['model'], 'model_mismatch')
    require(data.get('gateway_sha256') == expected['gateway_sha256'], 'gateway_mismatch')


def calls(data, limit, state):
    started = data.get('physical_calls_started')
    if integer(started):
        state['physical_calls_started'] = started
    require(integer(started, 1) and started <= limit, 'call_budget_invalid')
    require(type(data.get('physical_call_limit')) is int and data['physical_call_limit'] == limit, 'call_budget_invalid')
    return started


def preflight(directory, suite, expected, state):
    data = read_json(directory, 'summary.json')
    started = calls(data, 8, state)
    provenance(data, suite, expected, preflight=True)
    require(data.get('passed') is True and type(data.get('calls')) is int and data['calls'] == 6
            and started == 8 and type(data.get('tools_executed')) is int
            and data['tools_executed'] == 0, 'preflight_failed')
    rows = read_json(directory, 'results.json')
    require(isinstance(rows, list) and len(rows) == 6, 'preflight_failed')
    modes = [(stream, mode) for stream in (False, True) for mode in ('none', 'auto', 'echo_probe')]
    require([(r['stream'], r['tool_choice']) for r in rows] == modes, 'preflight_failed')
    require(all(type(r['stream']) is bool and r.get('passed') is True and r.get('error') is None
                and type(r.get('tools_executed')) is int and r['tools_executed'] == 0
                and (r['tool_choice'] != 'echo_probe' or r.get('tool_roundtrip') is True)
                for r in rows), 'preflight_failed')
    require(model_samples(rows, expected['model']) == started, 'call_accounting_mismatch')


def model_stage(directory, profile, suite, expected, state):
    manifest = read_json(directory, 'manifest.json')
    selected = suite['profiles'][profile]
    started = calls(manifest, selected['max_model_calls'], state)
    provenance(manifest, suite, expected)
    require(manifest.get('profile') == profile and manifest.get('requested_profile') == profile, 'profile_mismatch')
    require(manifest.get('cases') == selected['cases'] and manifest.get('strategies') == ['current', 'candidate']
            and type(manifest.get('repetitions')) is int and manifest['repetitions'] == selected['repetitions'], 'matrix_mismatch')
    require(manifest.get('policy_overrides') == {name: suite['strategies'][name]['overrides']
            for name in ('current', 'candidate')} and manifest.get('gates') == suite['gates']
            and manifest.get('temperature') == suite['runtime']['model_config']['temperature'], 'runtime_mismatch')
    archive = directory / 'source.tar.gz'
    require(not archive.is_symlink() and archive.is_file(), 'missing_evidence')
    require(digest(archive.read_bytes()) == manifest.get('source_archive_sha256'), 'source_archive_mismatch')
    contracts = manifest.get('contracts', {})
    require(contracts.get('passed') is True and integer(contracts.get('tests'), 1)
            and contracts.get('passed_count') == contracts['tests']
            and all(type(contracts.get(key)) is int and contracts[key] == 0
                    for key in ('exit_code', 'failures', 'errors', 'skipped'))
            and contracts.get('missing_test_modules') == [], 'safety_contract_failed')
    rows = read_json(directory, 'results.json')
    require(isinstance(rows, list) and len(rows) == state['instances'], 'matrix_mismatch')
    require(all(r.get('passed') is True and r.get('error') is None and r.get('checks')
                and all(value is True for value in r['checks'].values()) for r in rows), 'scenario_failed')
    require(model_samples(rows, expected['model']) == started, 'call_accounting_mismatch')
    # smoke 最低档位保留既有源码/矩阵/完成状态/安全合同校验；release 的收益门禁未被修改。
    require(verify_run(directory, minimum='smoke')['passed'] is True, 'stage_verification_failed')
    report = read_json(directory, 'summary.json')
    ratio = report['gates']['candidate'].get('ordinary_p95_ratio')
    require(type(ratio) in (int, float) and math.isfinite(ratio) and 0 < ratio <= 1.15, 'latency_gate_failed')


def evaluate(suite_path, prior, holdout=None, *, environment=None):
    """只读指定阶段并复用现有报告生成，不搜索其他运行或调用模型。"""
    full = holdout is not None
    summary = {'scope': 'full_functional' if full else 'full_functional_prior',
               'cost_benefit_status': 'not_evaluated', 'production_deploy_authorized': False,
               'scenarios': 48, 'instances': 258, 'max_model_calls': 3608,
               'total_started': None, 'stages': {}, 'errors': []}
    for name, (scenarios, instances, limit) in STAGES.items():
        summary['stages'][name] = {'status': 'not_evaluated', 'scenarios': scenarios,
                                   'instances': instances, 'max_model_calls': limit,
                                   'physical_calls_started': None}
    try:
        suite = load_suite(suite_path)
        expected = validate_suite(suite, os.environ if environment is None else environment)
    except InvalidEvidence as error:
        summary['errors'].append({'stage': 'configuration', 'code': str(error)})
    except Exception:
        summary['errors'].append({'stage': 'configuration', 'code': 'invalid_suite'})
    else:
        planned = [('preflight', Path(prior) / 'ci-preflight'), ('dev', Path(prior) / 'ci-model')]
        if full:
            planned.append(('holdout', Path(holdout)))
        for name, directory in planned:
            state = summary['stages'][name]
            try:
                if name == 'preflight':
                    preflight(directory, suite, expected, state)
                else:
                    model_stage(directory, 'dev' if name == 'dev' else 'release', suite, expected, state)
                state['status'] = 'passed'
            except InvalidEvidence as error:
                state['status'] = 'failed'
                summary['errors'].append({'stage': name, 'code': str(error)})
            except Exception:
                state['status'] = 'failed'
                summary['errors'].append({'stage': name, 'code': 'invalid_evidence'})
        counts = [summary['stages'][name]['physical_calls_started'] for name, _ in planned]
        if all(integer(count) for count in counts):
            summary['total_started'] = sum(counts)
    if full:
        summary['functional_status'] = 'failed' if summary['errors'] else 'passed'
    else:
        summary['status'] = 'failed' if summary['errors'] else 'ready_for_holdout'
    return summary


def main(argv=None, *, environment=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--holdout', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    summary = evaluate(args.suite, args.prior, args.holdout, environment=environment)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, summary)
        except Exception:
            summary['errors'].append({'stage': 'output', 'code': 'summary_write_failed'})
            summary['functional_status' if args.holdout is not None else 'status'] = 'failed'
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 2 if summary['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
