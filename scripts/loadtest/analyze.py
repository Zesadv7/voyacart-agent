"""汇总 AG-UI 原始 CSV；分位数使用 nearest-rank，失败样本保留。"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)] if ordered else None


def analyze(directory):
    with (directory / 'runs.csv').open() as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError('runs.csv 没有完成记录')
    failures = [r for r in rows if r['error_code']]
    duration = max(float(r['finished_at']) for r in rows) - min(float(r['started_at']) for r in rows)
    report = {'requests':len(rows), 'successful':len(rows) - len(failures), 'errors':len(failures),
        'error_rate':len(failures) / len(rows), 'error_codes':dict(Counter(r['error_code'] for r in failures)),
        'reconnections':sum(r['reconnected'] == 'True' for r in rows), 'observed_seconds':duration,
        'successful_runs_per_second':(len(rows) - len(failures)) / duration,
        'latency_ms':{}, 'source':'runs.csv', 'latency_scope':'all observed runs; missing times excluded'}
    for field in ('first_event_ms', 'first_token_ms', 'complete_ms'):
        values = [float(r[field]) for r in rows if r[field] != '']
        report['latency_ms'][field] = {'samples':len(values),
            **{f'p{int(q*100)}':percentile(values, q) for q in (.5, .95, .99)}}
    (directory / 'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2) + '\n')
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    print(json.dumps(analyze(parser.parse_args().directory), ensure_ascii=False, indent=2))
