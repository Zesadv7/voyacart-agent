"""容器工作流的安全合同与探针测试；本机进程测试不声称实际容器已运行。"""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path

import httpx
import pytest
import yaml

from scripts.ci import container_smoke as driver
from scripts.ci import container_probe as probe
from scripts.ci.mock_smoke import free_port, start, stop
from scripts.ci.offline_env import isolated_env

ROOT = Path(__file__).resolve().parents[1]


def test_docker_environment_drops_host_credentials_and_context(monkeypatch, tmp_path):
    for name in ('GITHUB_TOKEN', 'LLM_API_KEY', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'HTTPS_PROXY',
                 'MODEL_MASTER_KEY', 'CI_RUNTIME_IMAGE', 'CI_STATE_SLOT'):
        monkeypatch.setenv(name, 'synthetic-host-value')
    env = driver.isolated_docker_env(tmp_path, 'ci-unit-test')
    assert env['DOCKER_CONFIG'] == str(tmp_path / '.docker')
    assert env['CI_RUNTIME_IMAGE'] == 'ci-unit-test-runtime:local'
    assert env['CI_STATE_SLOT'] == 'initial'
    assert env['LLM_API_KEY'] == 'ci-local-placeholder'
    assert not {'GITHUB_TOKEN', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'HTTPS_PROXY', 'MODEL_MASTER_KEY'} & env.keys()
    assert env['COMPOSE_DISABLE_ENV_FILE'] == '1'


class ComposeLoader(yaml.SafeLoader):
    pass


for tag in ('!reset', '!override'):
    ComposeLoader.add_constructor(tag, lambda loader, node:
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else
        loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else
        loader.construct_scalar(node))


def test_ci_compose_no_ports_internal_network_and_dedicated_volumes():
    value = yaml.load((ROOT / 'docker/docker-compose.ci.yaml').read_text(), Loader=ComposeLoader)
    assert set(value['services']) == {'app', 'mock-llm', 'probe'}
    assert value['networks']['mock-only']['internal'] is True
    assert value['services']['app']['extends']['file'] == 'docker-compose.yaml'
    assert value['services']['app']['ports'] == []
    assert value['services']['app']['environment']['IDENTITY_HMAC_SECRET'] == probe.HMAC_FIXTURE
    assert value['services']['app']['environment']['AUTOMATION_WORKER_MODE'] == 'off'
    for service in value['services'].values():
        assert not service.get('ports') and service['networks'] == ['mock-only']
        assert not service.get('privileged') and not service.get('env_file')
        assert all('docker.sock' not in volume and not volume.startswith('/')
                   for volume in service.get('volumes', []))


def test_workflow_is_read_only_pinned_and_never_requests_real_models():
    raw = (ROOT / '.github/workflows/container-mock.yml').read_text()
    value = yaml.safe_load(raw)
    assert value['permissions'] == {'contents': 'read'}
    assert value['jobs']['container-mock']['runs-on'] == 'ubuntu-24.04'
    # PyYAML 的 YAML 1.1 把 on 当布尔值；这里同时兼容。
    triggers = value.get('on', value.get(True))
    assert 'work/m1-baseline-ci' in triggers['push']['branches']
    assert 'pull_request_target' not in triggers
    steps = value['jobs']['container-mock']['steps']
    assert steps[0]['with']['persist-credentials'] is False
    for step in steps:
        if 'uses' in step:
            assert len(step['uses'].split('@')[1]) == 40
    assert 'secrets.' not in raw and 'model-evaluation' not in raw
    assert steps[-1]['if'] == 'always()'


def test_production_image_keeps_esci_attribution():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'COPY third_party/esci-data ./third_party/esci-data' in dockerfile
    assert (ROOT / 'third_party/esci-data/LICENSE').is_file()
    assert (ROOT / 'third_party/esci-data/NOTICE').is_file()
    assert 'COPY LICENSE ./LICENSE' in dockerfile
    assert 'MIT License' in (ROOT / 'LICENSE').read_text()


def test_identity_extracts_named_state_volume():
    info = {'Id': 'container', 'Image': 'sha256:image', 'State': {'StartedAt': 'time'},
            'Mounts': [{'Destination': '/app/data', 'Name': 'random_initial'}]}
    assert driver.identity(info) == {'container_id': 'container', 'image_id': 'sha256:image',
                                    'data_volume': 'random_initial', 'started_at': 'time'}


def test_stopped_rejects_forced_or_oom_exit(tmp_path, monkeypatch):
    instance = driver.Docker({}, 'ci-unit', tmp_path)
    monkeypatch.setattr(instance, 'compose', lambda *args: '')
    monkeypatch.setattr(instance, 'inspect_app', lambda: {'State': {'Running': False, 'OOMKilled': False, 'ExitCode': 137}})
    with pytest.raises(AssertionError, match='强杀'):
        instance.stopped()
    monkeypatch.setattr(instance, 'inspect_app', lambda: {'State': {'Running': False, 'OOMKilled': True, 'ExitCode': 0}})
    with pytest.raises(AssertionError, match='OOM'):
        instance.stopped()


def test_runtime_failure_writes_report_and_cleans_only_owned_project(tmp_path, monkeypatch):
    calls = []
    def command(self, args, **kwargs):
        calls.append(args)
        raise RuntimeError('synthetic unavailable daemon')
    def compose(self, *args, **kwargs):
        calls.append(list(args))
        return ''
    monkeypatch.setattr(driver.Docker, 'command', command)
    monkeypatch.setattr(driver.Docker, 'compose', compose)
    with pytest.raises(RuntimeError, match='unavailable daemon'):
        driver.run(tmp_path / 'evidence')
    report = json.loads((tmp_path / 'evidence/report.json').read_text())
    assert report['status'] == 'failed' and report['checks'] == {}
    assert ['down', '--volumes', '--remove-orphans'] in calls
    assert all('prune' not in args for args in calls)


def test_probe_phases_with_real_loopback_http(monkeypatch, tmp_path):
    """真实 HTTP/快照/重建探针自测；这里只重启进程，Docker 生命周期留给 hosted CI。"""
    data, restored = tmp_path / 'data', tmp_path / 'restored'
    data.mkdir()
    monkeypatch.setattr(probe, 'DATA', data)
    monkeypatch.setattr(probe, 'ORIGINAL', data)
    monkeypatch.setattr(probe, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(probe, 'SNAPSHOT', tmp_path / 'snapshot')
    monkeypatch.setattr(probe, 'RESTORED', restored)
    # 容器 catalog 放 /app/catalog，本机公开夹具在项目 data/。
    image_root = tmp_path / 'image'
    (image_root / 'catalog').mkdir(parents=True)
    (image_root / 'catalog/catalog-demo.jsonl.gz').write_bytes((ROOT / 'data/catalog-demo.jsonl.gz').read_bytes())
    monkeypatch.setattr(probe, 'ROOT', image_root)
    monkeypatch.setattr(probe, 'frozen_paths', lambda: {'catalog': image_root / 'catalog', 'application': ROOT / 'app'})
    mock_port, api_port = free_port(), free_port()
    while api_port == mock_port:
        api_port = free_port()
    def environment(directory):
        value = isolated_env(directory, mock_url=f'http://127.0.0.1:{mock_port}/v1')
        value.update(IDENTITY_MODE='hmac', IDENTITY_HMAC_SECRET=probe.HMAC_FIXTURE, HOME=str(tmp_path / 'home'))
        return value
    monkeypatch.setattr(probe, 'client', lambda: httpx.Client(base_url=f'http://127.0.0.1:{api_port}',
        timeout=180, trust_env=False, headers={'Authorization': 'Bearer ' + probe.policy().issue(probe.BUYER)}))
    def counts():
        with httpx.Client(trust_env=False, timeout=10) as http:
            response = http.get(f'http://127.0.0.1:{mock_port}/admin/requests')
            response.raise_for_status()
            return response.json()
    monkeypatch.setattr(probe, 'mock_counts', counts)
    with ExitStack() as stack:
        env = environment(data)
        start(stack, 'mock_llm:app', mock_port, env, tmp_path / 'mock.log')
        api = start(stack, 'app.presentation.server:app', api_port, env, tmp_path / 'api.log')
        assert probe.seed()['prepare_and_resolve_idempotent']
        stop(api)
        assert api.returncode in (0, -15)
        assert probe.index()['points'] == 5700
        assert probe.snapshot()['status'] == 'verified'
        api = start(stack, 'app.presentation.server:app', api_port, env, tmp_path / 'restart.log')
        assert probe.verify()['sse_exact_and_cursor_replay']
        assert probe.mutate()['post_snapshot_order_cancelled']
        stop(api)
        assert probe.restore_snapshot()['status'] == 'verified'
        monkeypatch.setattr(probe, 'DATA', restored)
        api = start(stack, 'app.presentation.server:app', api_port, environment(restored), tmp_path / 'restored.log')
        assert probe.verify()['candidate_and_comparison_exact']
        stop(api)
        assert probe.index()['points'] == 5700
        assert probe.original_preserved()['source_volume_unchanged_after_restore']


def test_resolved_compose_enforces_isolation_with_official_parser(tmp_path):
    from tests.test_compose_delivery import _compose_command
    import subprocess
    environment = driver.isolated_docker_env(tmp_path / 'home', 'ci-resolved-unit')
    result = subprocess.run(_compose_command(environment) + ['--env-file', '/dev/null', '-f', str(driver.COMPOSE),
        '--profile', 'tools', 'config', '--format', 'json'], env=environment,
        capture_output=True, text=True, timeout=30, check=True)
    driver.validate_config(json.loads(result.stdout))


def test_timeout_preserves_partial_log(tmp_path, monkeypatch):
    import subprocess
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b'partial stdout', stderr=b'partial stderr')
    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(RuntimeError, match='timed out'):
        driver.Docker({}, 'ci-unit', tmp_path).command(['build', '.'], timeout=1)
    log = (tmp_path / '01-build.log').read_text()
    assert 'partial stdout' in log and 'partial stderr' in log and 'timed out' in log
