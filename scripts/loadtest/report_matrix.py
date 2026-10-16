"""只读汇总 R1 证据；绘制实际容量点，输出目录必须全新。"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re


CONFIGURATIONS = [f'{mode}-a{api}-w{workers}'
    for mode, api, workers in itertools.product(('inline', 'queue'), (1, 2, 4), (1, 2, 4))]
LATENCIES = ('first_event_ms', 'first_token_ms', 'complete_ms')


def resource_peaks(path):
    cpu, containers, processes = {}, {}, {}
    samples = 0
    if path.exists():
        for line in path.read_text().splitlines():
            sample = json.loads(line)
            samples += 1
            totals = {}
            for container in sample['containers']:
                name = container['Name']
                role = re.search(r'-(api|worker)-\d+$', name)
                if role:
                    value = float(container['CPUPerc'].removesuffix('%'))
                    containers[name] = max(containers.get(name, 0), value)
                    totals[role[1]] = totals.get(role[1], 0) + value
            for role, value in totals.items():
                cpu[role] = max(cpu.get(role, 0), value)
            for name, table in sample['processes'].items():
                for row in table.splitlines()[1:]:
                    pid, rss, command = row.split(maxsplit=2)
                    key = f'{name} / {pid} / {command}'
                    processes[key] = max(processes.get(key, 0), int(rss))
    return {'samples': samples, 'role_cpu_peak_percent': cpu,
        'container_cpu_peak_percent': containers, 'process_rss_peak_kib': processes}


def summarize(matrix, baseline):
    fingerprints = {}

    def read_json(path):
        payload = path.read_bytes()
        fingerprints[str(path)] = hashlib.blake2b(payload).hexdigest()
        return json.loads(payload)

    def stage(report, directory, *, baseline_locks=None):
        resources = directory / 'resources.jsonl'
        if resources.exists():
            fingerprints[str(resources)] = hashlib.blake2b(resources.read_bytes()).hexdigest()
        result = {**report, 'resources': resource_peaks(resources), 'evidence': str(directory)}
        retries = report.get('sqlite_lock_retries')
        locks = report.get('sqlite_lock_error_log_occurrences')
        result['initialization_retries'] = sum(retries.values()) if retries is not None else None
        result['lock_error_log_occurrences'] = sum(locks.values()) if locks is not None else baseline_locks
        return result

    progress = read_json(matrix / 'progress.json')
    recorded = {f'{entry["mode"]}-a{entry["api"]}-w{entry["workers"]}': entry
        for entry in progress['configurations']}
    configurations = []
    for name in CONFIGURATIONS:
        reports = sorted((matrix / name).glob('users-*/report.json'),
            key=lambda path: int(path.parent.name.removeprefix('users-')))
        stages = [stage(read_json(path), path.parent) for path in reports]
        entry = recorded.get(name)
        configurations.append({'name': name, 'status': 'recorded' if entry is not None else 'unfinished' if stages else 'unmeasured',
            'observed_saturation': entry['observed_saturation'] if entry is not None else None,
            'stages': stages,
            'role_cpu_peak_percent': {role: max((value['resources']['role_cpu_peak_percent'][role]
                for value in stages if role in value['resources']['role_cpu_peak_percent']), default=None)
                for role in ('api', 'worker')},
            **{key: sum(value[key] for value in stages) if stages and all(value[key] is not None for value in stages) else None
                for key in ('initialization_retries', 'lock_error_log_occurrences')}})
    baseline_stages = [stage({**entry['report'], 'batch': entry['batch']},
        baseline.parent / entry['batch'] / 'inline-a1-w1' / f'users-{entry["users"]}',
        baseline_locks=entry.get('database_lock_log_occurrences')) for entry in read_json(baseline)]
    return {'matrix': str(matrix), 'baseline': str(baseline),
        'recorded_run_complete': progress['complete'],
        'matrix_complete': all(entry['status'] == 'recorded' and entry['stages'] for entry in configurations),
        'configurations': configurations, 'baseline_stages': baseline_stages,
        'source_fingerprint_blake2b': hashlib.blake2b(Path(__file__).read_bytes()).hexdigest(),
        'input_fingerprints_blake2b': fingerprints}


def render_markdown(summary):
    def number(value, digits=3):
        return '未知' if value is None else f'{value:.{digits}f}'

    def latency(stage, field):
        values = stage.get('latency_ms', {}).get(field, {})
        return ' / '.join(number(values.get(quantile), 1) for quantile in ('p50', 'p95', 'p99'))

    def metrics(stage):
        cpu = stage['resources']['role_cpu_peak_percent']
        error = stage.get('error_rate')
        return [str(stage['users']), str(stage.get('requests', '未知')),
            number(stage.get('successful_runs_per_second')),
            '未知' if error is None else f'{error:.2%}',
            *(latency(stage, field) for field in LATENCIES),
            ' / '.join(str(stage.get('latency_ms', {}).get(field, {}).get('samples', '未知')) for field in LATENCIES),
            number(cpu.get('api'), 2), number(cpu.get('worker'), 2),
            number(stage['initialization_retries'], 0), number(stage['lock_error_log_occurrences'], 0)]

    def condition(configuration):
        point = configuration['observed_saturation']
        if point:
            reasons = {'error_rate_above_5_percent': '错误率 >5%',
                'throughput_growth_at_most_10_percent': '相邻吞吐增幅 ≤10%'}
            return f'{point["users"]} 用户：{reasons[point["reason"]]}'
        if configuration['status'] == 'recorded':
            return '截至最后已测档，未观察到饱和'
        return '未知，收尾未记录' if configuration['stages'] else '未测'

    columns = ['用户数', '运行数', '成功运行/秒', '错误率',
        '首事件 P50/P95/P99 ms', '首 token P50/P95/P99 ms', '完成 P50/P95/P99 ms',
        '样本数 事件/token/完成', 'API CPU 峰值 %', 'worker CPU 峰值 %', '初始化重试', '锁异常日志次数']
    lines = ['# R1 容量证据汇总', '',
        f'已记录收尾 {sum(entry["status"] == "recorded" for entry in summary["configurations"])}/18 组；'
        f'完整矩阵：{"已记录" if summary["matrix_complete"] else "未完成"}。', '',
        '配置概览的吞吐和时延展示最后已测档，CPU 为全部已测档的采样峰值，日志计数为各档之和。'
        '各档完整结果在后续表格与 summary.json。'
        '时延来自全部观测运行，缺失时间不参与分位数，样本数逐项保留。', '',
        'CPU 峰值为同一次采样中该角色各容器 CPU 之和的最大值，多核数值可以超过 100%。'
        'RSS 按容器、PID、命令分别取采样峰值，单位 MiB。'
        '初始化显式重试和锁异常日志出现次数分别列示；同一次异常可能出现多条日志。', '',
        '曲线仅连接实际测量点，基线批次分别保留；本汇总不自动推断容量改善。', '',
        '| 配置 | 收尾状态 | 饱和条件 | ' + ' | '.join(columns) + ' |',
        '| ' + ' | '.join(['---'] * (len(columns) + 3)) + ' |']
    for configuration in summary['configurations']:
        label = {'recorded': '已记录', 'unfinished': '未记录', 'unmeasured': '未测'}[configuration['status']]
        if configuration['stages']:
            latest = configuration['stages'][-1]
            overview = {**latest,
                'resources': {**latest['resources'], 'role_cpu_peak_percent': configuration['role_cpu_peak_percent']},
                **{key: configuration[key] for key in ('initialization_retries', 'lock_error_log_occurrences')}}
            values = metrics(overview)
        else:
            values = ['未测'] * len(columns)
        lines.append('| ' + ' | '.join([configuration['name'], label, condition(configuration), *values]) + ' |')
    lines.extend(['', '## 各档与历史基线', '',
        '| 配置/批次 | ' + ' | '.join(columns) + ' |',
        '| ' + ' | '.join(['---'] * (len(columns) + 1)) + ' |'])
    measured = [(entry['name'], value) for entry in summary['configurations'] for value in entry['stages']]
    measured.extend((f'baseline/{value["batch"]}', value) for value in summary['baseline_stages'])
    for name, value in measured:
        lines.append('| ' + ' | '.join([name, *metrics(value)]) + ' |')
    lines.extend(['', '## 每个进程 RSS 采样峰值', '', '| 配置/批次 | 容器 / PID / 命令 | MiB |', '| --- | --- | --- |'])
    peaks = {}
    for name, value in measured:
        for process, rss in value['resources']['process_rss_peak_kib'].items():
            key = (name, process)
            peaks[key] = max(peaks.get(key, 0), rss)
    for (name, process), rss in sorted(peaks.items()):
        lines.append(f'| {name} | {process} | {rss / 1024:.2f} |')
    for name in dict.fromkeys(name for name, _ in measured):
        if not any(key[0] == name for key in peaks):
            lines.append(f'| {name} | 未采集 | 未知 |')
    return '\n'.join(lines) + '\n'


def draw_curves(summary, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(14, 5.5), constrained_layout=True)
    for axis, mode in zip(axes, ('inline', 'queue')):
        measured = 0
        for index, configuration in enumerate(summary['configurations']):
            if configuration['name'].startswith(mode + '-') and configuration['stages']:
                measured += 1
                stages = configuration['stages']
                axis.plot([value['users'] for value in stages],
                    [value.get('successful_runs_per_second') for value in stages],
                    marker='o', color=plt.get_cmap('tab10')(index % 9),
                    label=configuration['name'].removeprefix(mode + '-'))
        for batch in dict.fromkeys(value['batch'] for value in summary['baseline_stages']):
            stages = [value for value in summary['baseline_stages'] if value['batch'] == batch]
            axis.plot([value['users'] for value in stages],
                [value.get('successful_runs_per_second') for value in stages],
                marker='x', linestyle='--', color='gray', label='Old inline / ' + batch)
        axis.set(title='Inline execution' if mode == 'inline' else 'Queued execution',
            xlabel='Concurrent users', ylabel='Successful runs / second', ylim=(0, None))
        axis.set_xscale('log', base=2)
        users = sorted({value['users'] for entry in summary['configurations']
            if entry['name'].startswith(mode + '-') for value in entry['stages']}
            | {value['users'] for value in summary['baseline_stages']})
        axis.set_xticks(users, [str(value) for value in users])
        axis.text(.02, .97, f'{measured}/9 configurations measured', transform=axis.transAxes, va='top', fontsize=9)
        axis.grid(alpha=.25)
        if axis.lines:
            axis.legend(fontsize=8)
    figure.savefig(output / 'capacity.png', dpi=180)
    figure.savefig(output / 'capacity.svg')
    plt.close(figure)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(args.matrix, args.baseline)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'REPORT.md').write_text(render_markdown(summary))
    draw_curves(summary, args.output)
    print(json.dumps({'output': str(args.output), 'matrix_complete': summary['matrix_complete']}, ensure_ascii=False))
