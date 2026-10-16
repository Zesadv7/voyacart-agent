"""独立压测环境运行：流式协议断线续传与报告统计。"""
import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('agui_load', ROOT / 'scripts/loadtest/client.py')
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)
spec = importlib.util.spec_from_file_location('agui_analyze', ROOT / 'scripts/loadtest/analyze.py')
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)


class Response:
    status_code = 200

    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    def iter_lines(self, **_):
        yield ': heartbeat'
        yield ''
        for sequence, kind in self.events:
            yield f'id: run:{sequence}'
            yield 'data: ' + json.dumps({'type':kind})
            yield ''


def test_reconnect_uses_last_id_and_retains_original_timings():
    from unittest.mock import Mock
    session = Mock()
    session.request.side_effect = [Response([(1,'RUN_STARTED'),(2,'TEXT_MESSAGE_CONTENT'),(3,'RUN_FINISHED')]),
                                  Response([(3,'TEXT_MESSAGE_CONTENT'),(4,'RUN_FINISHED')])]
    result = load.consume_run(session, 'http://test', 'buyer', 'run', reconnect=True)
    assert result['error_code'] == '' and result['reconnected'] and result['events'] == 4
    assert result['first_event_ms'] <= result['first_token_ms'] <= result['complete_ms']
    resume = session.request.call_args_list[1]
    assert resume.args == ('GET', 'http://test/commerce/ag-ui/runs/run/events')
    assert resume.kwargs['headers'] == {'Last-Event-ID':'run:2'}
    assert 'json' not in resume.kwargs


@pytest.mark.parametrize('events,error', [
    ([(1,'RUN_STARTED'),(1,'RUN_FINISHED')], 'event_sequence_gap_or_duplicate'),
    ([(1,'RUN_STARTED'),(3,'RUN_FINISHED')], 'event_sequence_gap_or_duplicate'),
    ([(1,'RUN_STARTED')], 'stream_ended_before_terminal'),
    ([(1,'RUN_ERROR')], 'RUN_ERROR'),
])
def test_protocol_and_runtime_failures_remain_failed_samples(events, error):
    from unittest.mock import Mock
    result = load.consume_run(Mock(request=Mock(return_value=Response(events))), 'http://test', 'buyer', 'run')
    assert error in result['error_code']


def test_transport_failure_is_recorded():
    from unittest.mock import Mock
    session = Mock(request=Mock(side_effect=requests.ConnectionError('connection lost')))
    result = load.consume_run(session, 'http://test', 'buyer', 'run')
    assert result['error_code'] == 'ConnectionError:connection lost'
    assert result['events'] == 0 and result['first_token_ms'] == ''


def test_total_deadline_counts_heartbeat_lines_without_business_events(monkeypatch):
    from unittest.mock import Mock
    clock = 0
    monkeypatch.setattr(load.time, 'perf_counter', lambda: clock)

    class Heartbeats(Response):
        def iter_lines(self, **_):
            nonlocal clock
            for _ in range(10):
                clock += .05
                yield ': heartbeat'
                yield ''

    result = load.consume_run(Mock(request=Mock(return_value=Heartbeats([]))),
        'http://test', 'buyer', 'run', timeout=.2)
    assert result['error_code'] == 'Timeout:run_deadline_exceeded'
    assert result['events'] == 0 and result['complete_ms'] == pytest.approx(200)


def test_reconnect_consumes_remaining_run_deadline(monkeypatch):
    from unittest.mock import Mock
    clock = 0
    monkeypatch.setattr(load.time, 'perf_counter', lambda: clock)

    class First(Response):
        def iter_lines(self, **_):
            nonlocal clock
            clock = .15
            yield from super().iter_lines()

    class Resumed(Response):
        def iter_lines(self, **_):
            nonlocal clock
            clock = .21
            yield ': heartbeat'
            yield ''

    session = Mock(request=Mock(side_effect=[
        First([(1, 'RUN_STARTED'), (2, 'TEXT_MESSAGE_CONTENT')]), Resumed([]),
    ]))
    result = load.consume_run(session, 'http://test', 'buyer', 'run', reconnect=True, timeout=.2)
    assert result['error_code'] == 'Timeout:run_deadline_exceeded'
    assert result['events'] == 2 and result['reconnected']
    assert result['first_token_ms'] == pytest.approx(150)
    assert session.request.call_args_list[1].kwargs['timeout'] == pytest.approx(.05)


@pytest.mark.skipif(importlib.util.find_spec('locust') is None, reason='需要独立 Locust 环境')
@pytest.mark.parametrize('interruption', ['deadline', 'stalled', 'stop'])
def test_locust_interrupted_stream_is_written_to_csv(tmp_path, interruption):
    command = [sys.executable, '-c', '''
import csv, json, sys, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path.cwd() / 'scripts/loadtest'))
import locustfile as loadtest
import gevent
from locust.env import Environment

class Response:
    status_code = 200
    def __init__(self, run_id): self.run_id = run_id
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def raise_for_status(self): pass
    def iter_lines(self, **_):
        yield 'id: ' + self.run_id + ':1'
        yield 'data: ' + json.dumps({'type':'RUN_STARTED'})
        yield ''
        while True:
            gevent.sleep(1 if interruption == 'stalled' else .01)
            yield ': heartbeat'
            yield ''

class Session:
    def request(self, *_, **kwargs): return Response(kwargs['json']['runId'])
    def close(self): pass

directory, interruption = sys.argv[1:]
environment = Environment(user_classes=[loadtest.ShoppingUser])
environment.host = 'http://test'
environment.parsed_options = SimpleNamespace(agui_output=directory,
    agui_reconnect_rate=0, agui_timeout=180 if interruption == 'stop' else .08,
    num_users=1, spawn_rate=1)
loadtest.begin(environment)
user = loadtest.ShoppingUser(environment)
user.http, user.reconnect = Session(), False
observations = []
environment.events.request.add_listener(lambda **event: observations.append(event))
started = time.monotonic()
runner = gevent.spawn(user.shopping)
if interruption == 'stop':
    gevent.sleep(.03)
    runner.kill()
runner.join(timeout=1)
assert runner.ready(), '整轮截止时间未生效'
assert time.monotonic() - started < 1
assert runner.exception is None
loadtest.end(environment)
rows = list(csv.DictReader((Path(directory)/'runs.csv').open()))
assert len(rows) == 1
row = rows[0]
assert row['error_code'] == ('GreenletExit:' if interruption == 'stop' else 'Timeout:run_deadline_exceeded')
assert row['events'] == '1' and row['first_event_ms'] != ''
assert row['first_token_ms'] == '' and row['finished_at'] != ''
assert float(row['complete_ms']) > 0
assert len([event for event in observations if event['request_type']=='AGUI' and event['exception']]) == 1
''', str(tmp_path), interruption]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sse_multiline_json_and_incomplete_frame():
    assert list(load.sse_events(['id: run:1','data: {','data: "type":"RUN_STARTED"}',''])) == [
        ('run:1','',{'type':'RUN_STARTED'})]
    with pytest.raises(ValueError, match='incomplete_sse_frame'):
        list(load.sse_events(['data: {}']))


def test_analysis_reports_failures_missing_tokens_and_nearest_rank(tmp_path):
    rows = [dict(zip(load.FIELDS, ['a',0,2,10,20,2000,200,'',3,True])),
            dict(zip(load.FIELDS, ['b',1,4,30,'',3000,503,'http_503',0,False]))]
    with (tmp_path/'runs.csv').open('w') as file:
        writer = csv.DictWriter(file, fieldnames=load.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    report = analyze.analyze(tmp_path)
    assert report['successful_runs_per_second'] == .25
    assert report['error_rate'] == .5 and report['error_codes'] == {'http_503':1}
    assert report['latency_ms']['first_token_ms'] == {'samples':1,'p50':20,'p95':20,'p99':20}
    assert report['latency_ms']['complete_ms']['p95'] == 3000
    assert report['reconnections'] == 1
    assert json.loads((tmp_path/'failures.json').read_text())[0]['run_id'] == 'b'
