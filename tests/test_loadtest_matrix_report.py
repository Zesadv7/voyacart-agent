"""R1 报告保留未收尾的真实测量、未知指标和同时采样的资源峰值。"""
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('matrix_report', ROOT / 'scripts/loadtest/report_matrix.py')
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def test_matrix_report_preserves_partial_results_and_resource_semantics(tmp_path):
    matrix, baseline = tmp_path / 'matrix', tmp_path / 'baseline-summary.json'
    matrix.mkdir()
    (matrix / 'progress.json').write_text(json.dumps({'complete': False, 'configurations': [
        {'mode': 'inline', 'api': 2, 'workers': 1,
         'observed_saturation': {'users': 8, 'reason': 'error_rate_above_5_percent'}, 'stages': []},
    ]}))
    common = {'requests': 20, 'successful_runs_per_second': 1,
        'error_rate': 0, 'latency_ms': {'first_event_ms': {'p50': 10, 'p95': None, 'p99': 30, 'samples': 18}},
        'sqlite_lock_retries': {'api': 1, 'worker': 2}, 'sqlite_lock_error_log_occurrences': {'api': 4}}
    for users, value in [(4, common), (8, {**common, 'error_rate': .1,
        'successful_runs_per_second': .95, 'sqlite_lock_retries': {'api': 0},
        'sqlite_lock_error_log_occurrences': {'api': 0}})]:
        directory = matrix / 'inline-a2-w1' / f'users-{users}'
        directory.mkdir(parents=True)
        (directory / 'report.json').write_text(json.dumps({**value, 'users': users}))
    orphan = matrix / 'inline-a1-w2' / 'users-32'
    orphan.mkdir(parents=True)
    (orphan / 'report.json').write_text(json.dumps({'users': 32,
        'requests': 15, 'successful_runs_per_second': .5, 'error_rate': .5}))
    samples = [
        {'containers': [{'Name': 'test-api-1', 'CPUPerc': '30%'},
            {'Name': 'test-api-2', 'CPUPerc': '60%'}, {'Name': 'test-worker-1', 'CPUPerc': '3%'}],
         'processes': {'test-api-1': 'PID RSS COMMAND\n7 2048 uvicorn\n8 1024 python\n',
            'test-api-2': 'PID RSS COMMAND\n9 2048 uvicorn\n'}},
        {'containers': [{'Name': 'test-api-1', 'CPUPerc': '80%'},
            {'Name': 'test-api-2', 'CPUPerc': '5%'}, {'Name': 'test-worker-1', 'CPUPerc': '4%'}],
         'processes': {'test-api-1': 'PID RSS COMMAND\n7 4096 uvicorn\n',
            'test-api-2': 'PID RSS COMMAND\n9 1024 uvicorn\n'}},
    ]
    resources = matrix / 'inline-a2-w1' / 'users-4' / 'resources.jsonl'
    resources.write_text(''.join(json.dumps(sample) + '\n' for sample in samples))
    baseline.write_text(json.dumps([{'batch': batch, 'users': 64, 'database_lock_log_occurrences': 7,
        'report': {'users': 64, 'successful_runs_per_second': throughput, 'sqlite_lock_retries': {'api': 0}}}
        for batch, throughput in [('baseline-ready', 2), ('baseline-high', 1.9)]]))
    inputs = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}

    result = reporter.summarize(matrix, baseline)
    assert len(result['configurations']) == 18 and result['matrix_complete'] is False
    measured = next(value for value in result['configurations'] if value['name'] == 'inline-a2-w1')
    assert measured['observed_saturation']['users'] == 8
    assert measured['role_cpu_peak_percent'] == {'api': 90, 'worker': 4}
    assert measured['initialization_retries'] == 3 and measured['lock_error_log_occurrences'] == 4
    first = measured['stages'][0]['resources']
    assert first['container_cpu_peak_percent']['test-api-1'] == 80
    assert first['process_rss_peak_kib'] == {'test-api-1 / 7 / uvicorn': 4096,
        'test-api-1 / 8 / python': 1024, 'test-api-2 / 9 / uvicorn': 2048}
    partial = next(value for value in result['configurations'] if value['name'] == 'inline-a1-w2')
    assert partial['status'] == 'unfinished' and partial['stages'][0]['users'] == 32
    assert partial['role_cpu_peak_percent'] == {'api': None, 'worker': None}
    assert partial['initialization_retries'] is None and partial['lock_error_log_occurrences'] is None
    assert [value['successful_runs_per_second'] for value in result['baseline_stages']] == [2, 1.9]
    assert all(value['lock_error_log_occurrences'] == 7 for value in result['baseline_stages'])
    markdown = reporter.render_markdown(result)
    assert '8 用户：错误率 >5%' in markdown and '未知，收尾未记录' in markdown
    assert '| queue-a4-w4 | 未测 | 未测 |' in markdown
    assert '10.0 / 未知 / 30.0' in markdown and '| test-api-1 / 7 / uvicorn | 4.00 |' in markdown
    assert inputs == {path: path.read_bytes() for path in inputs}
    assert set(result['input_fingerprints_blake2b']) == {str(path) for path in inputs}


def test_filtered_run_completion_does_not_claim_full_matrix(tmp_path):
    matrix, baseline = tmp_path / 'matrix', tmp_path / 'baseline-summary.json'
    matrix.mkdir()
    (matrix / 'progress.json').write_text(json.dumps({'complete': True, 'configurations': []}))
    baseline.write_text('[]')
    result = reporter.summarize(matrix, baseline)
    assert result['recorded_run_complete'] is True and result['matrix_complete'] is False
    assert all(value['status'] == 'unmeasured' for value in result['configurations'])
    assert reporter.resource_peaks(tmp_path / 'missing.jsonl')['role_cpu_peak_percent'] == {}
