"""演练边界与实际传输协议使用内存响应验证，保持本机服务运行状态。"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def drills(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/loadtest'))
    spec = importlib.util.spec_from_file_location('r2_faults', ROOT / 'scripts/loadtest/faults.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('project,port', [
    ('voyacart-scale-review', 8086), ('voyacart-r1-current', 8086), ('voyacart-r2-../x', 8086),
    ('voyacart-r2-test', 8080), ('voyacart-r2-test', 8085), ('voyacart-r2-test', 8000),
])
def test_existing_application_and_capacity_targets_are_rejected(drills, project, port):
    with pytest.raises(ValueError):
        drills.validate_target(project, port)


def test_new_project_settings_use_only_local_mock_credentials(drills, monkeypatch, tmp_path):
    monkeypatch.setattr(drills, 'ROOT', tmp_path)
    (tmp_path / 'docker').mkdir()
    (tmp_path / 'docker/nginx.scale.conf').write_text('add_header X-Accel-Buffering no;')
    monkeypatch.setenv('LLM_API_KEY', 'private-upstream-key')
    monkeypatch.setenv('EMBEDDING_BASE_URL', 'https://external.example/v1')
    drill = drills.Drill(SimpleNamespace(project='voyacart-r2-unit', port=8086,
        scenario='redis', output=tmp_path / 'eval/verification/run', model_cache=None))
    assert drill.env['LLM_API_KEY'] == drill.env['EMBEDDING_API_KEY'] == 'r2-local'
    assert drill.env['EMBEDDING_BASE_URL'] == 'http://mock-embedding:8081/v1'
    assert '--env-file' in drill.compose
    assert 'private-upstream-key' not in json.dumps(drill.env)
    drill.dc = Mock(return_value='already-present')
    with pytest.raises(ValueError, match='项目已存在'):
        drill.prepare()
    assert not drill.owns_project
    assert drill.dc.call_args.args == ('ps', '-aq')


class Response:
    status_code = 200
    encoding = 'utf-8'

    def __init__(self, records, upstream, error=None):
        self.records, self.error = records, error
        self.headers = {'X-R2-Upstream': upstream}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, **_):
        yield b':  preserved  \r\n\r\n'
        for seq, kind in self.records:
            yield (f'id: run:{seq}\ndata: ' + json.dumps({'type': kind}) + '\n\n').encode()
        if self.error:
            raise self.error


def test_stopped_api_resumes_with_original_cursor_and_keeps_raw_failures(drills, monkeypatch, tmp_path):
    monkeypatch.setattr(drills.time, 'sleep', Mock())
    action = Mock()
    session = Mock()
    session.request.side_effect = [
        Response([(1, 'RUN_STARTED'), (2, 'TEXT_MESSAGE_CONTENT')], 'api-one:8000',
            requests.exceptions.ChunkedEncodingError('API stopped')),
        Response([(3, 'TEXT_MESSAGE_CONTENT'), (4, 'RUN_FINISHED')], 'api-two:8000')]
    result = drills.consume(session, 'http://test', 'buyer', 'run', tmp_path, action=action)
    assert result['fault_injected'] and result['upstreams'] == ['api-one:8000', 'api-two:8000']
    action.assert_called_once()
    resume = session.request.call_args_list[1]
    assert resume.args == ('GET', 'http://test/commerce/ag-ui/runs/run/events')
    assert resume.kwargs['headers'] == {'Last-Event-ID': 'run:2'}
    assert 'json' not in resume.kwargs
    assert result['transport_errors'][0]['type'] == 'ChunkedEncodingError'
    assert (tmp_path / 'sse-run-1.log').read_text().endswith('\n\n')
    assert (tmp_path / 'sse-run-1.log').read_bytes().startswith(b':  preserved  \r\n\r\n')
    assert (tmp_path / 'transport-run.json').exists()
    assert [row['id'] for row in result['records']] == ['run:1', 'run:2', 'run:3', 'run:4']


def test_duplicate_event_after_fault_is_an_explicit_failure(drills, monkeypatch, tmp_path):
    monkeypatch.setattr(drills.time, 'sleep', Mock())
    session = Mock()
    session.request.side_effect = [
        Response([(1, 'RUN_STARTED'), (2, 'TEXT_MESSAGE_CONTENT')], 'api-one:8000',
            requests.ConnectionError('API stopped')),
        Response([(2, 'TEXT_MESSAGE_CONTENT'), (3, 'RUN_FINISHED')], 'api-two:8000')]
    with pytest.raises(AssertionError, match='事件序号不连续'):
        drills.consume(session, 'http://test', 'buyer', 'run', tmp_path, action=Mock())
    assert 'run:2' in (tmp_path / 'sse-run-2.log').read_text()


def test_uninjected_connection_loss_stops_the_drill(drills, tmp_path):
    session = Mock(request=Mock(side_effect=requests.ConnectionError('initial request failed')))
    with pytest.raises(requests.ConnectionError):
        drills.consume(session, 'http://test', 'buyer', 'run', tmp_path)
    assert json.loads((tmp_path / 'transport-run.json').read_text())[0]['attempt'] == 1
