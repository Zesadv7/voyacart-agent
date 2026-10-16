"""GitHub hosted runner 容器冒烟驱动；仅使用 stdlib 和现有 Docker/Compose。

独立随机 project + 内部网络 + 新建命名卷；不接触既有项目，不继承 Docker
凭据、应用凭据或 .env，不 push 镜像。证据仅含合成报告、日志和镜像身份。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'docker/docker-compose.ci.yaml'


def isolated_docker_env(home: Path, project: str) -> dict[str, str]:
    # 保留可执行工具查找，不保留 HOME/DOCKER_HOST/DOCKER_CONTEXT/代理或 secret。
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
    env.update(HOME=str(home), DOCKER_CONFIG=str(home / '.docker'),
               COMPOSE_DISABLE_ENV_FILE='1', COMPOSE_ANSI='never',
               CI_RUNTIME_IMAGE=f'{project}-runtime:local', CI_PROBE_IMAGE=f'{project}-probe:local',
               CI_STATE_SLOT='initial', LLM_BASE_URL='http://mock-llm:8081/v1',
               LLM_API_KEY='ci-local-placeholder')
    return env


def validate_config(config: dict) -> None:
    services = config['services']
    assert set(services) == {'app', 'mock-llm', 'probe'}, '只允许专用 mock 服务'
    assert set(config['networks']) == {'mock-only'}
    assert config['networks']['mock-only']['internal'] is True
    assert not config['networks']['mock-only'].get('external')
    assert set(config['volumes']) == {'initial', 'restored', 'snapshot', 'probe-state'}
    assert all(not volume.get('external') for volume in config['volumes'].values())
    for service in services.values():
        assert not service.get('ports'), '测试不发布任何宿主端口'
        assert not service.get('privileged') and not service.get('network_mode')
        assert not service.get('env_file') and not service.get('secrets')
        assert set(service['networks']) == {'mock-only'}
        assert all(mount['type'] == 'volume' for mount in service.get('volumes', [])), '只挂载新命名卷'
    app = services['app']['environment']
    expected = {
        'LLM_BASE_URL': 'http://mock-llm:8081/v1', 'EMBEDDING_BASE_URL': 'http://mock-llm:8081/v1',
        'LLM_API_KEY': 'ci-local-placeholder', 'EMBEDDING_API_KEY': 'ci-local-placeholder',
        'LLM_MODEL': 'mock-chat', 'LLM_FALLBACK_MODEL': '', 'RERANKER_MODE': 'disabled',
        'TAVILY_API_KEY': '', 'RERANKER_API_KEY': '', 'MODEL_MASTER_KEY': '',
        'LANGFUSE_SECRET_KEY': '', 'LANGFUSE_PUBLIC_KEY': '', 'REDIS_URL': '', 'QDRANT_URL': '',
        'OTEL_EXPORTER_OTLP_HEADERS': '', 'OTEL_EXPORTER_OTLP_TRACES_HEADERS': '',
        'OTEL_EXPORTER_OTLP_ENDPOINT': '', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': '',
        'QUEUE_ENABLED': '0', 'AGUI_QUEUE_ENABLED': '0', 'IDENTITY_MODE': 'hmac',
        'PYTHON_DOTENV_DISABLED': '1', 'AUTOMATION_WORKER_MODE': 'off',
    }
    for key, value in expected.items():
        assert app.get(key) == value, (key, 'unexpected configuration')


class Docker:
    def __init__(self, env: dict, project: str, output: Path):
        self.env, self.project, self.output = env, project, output
        self.number = 0

    def command(self, args: list[str], *, timeout: int = 600, check: bool = True) -> str:
        self.number += 1
        log = self.output / f'{self.number:02d}-{args[0]}.log'
        print('Docker phase:', args[0], flush=True)
        # 不传宿主 GITHUB_TOKEN；参数仅为固定命令、独立 project 与本地路径。
        try:
            result = subprocess.run(['docker', *args], cwd=ROOT, env=self.env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
            log.write_text(decoded(error.stdout) + decoded(error.stderr) + '\nDocker command timed out.\n')
            raise RuntimeError(f'Docker command timed out; see {log.name}') from error
        log.write_text(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f'Docker command failed ({result.returncode}); see {log.name}')
        return result.stdout

    def compose(self, *args: str, **kwargs) -> str:
        return self.command(['compose', '--project-name', self.project, '--env-file', os.devnull,
                             '-f', str(COMPOSE), '--profile', 'tools', *args], **kwargs)

    def inspect_app(self) -> dict:
        container = self.compose('ps', '--all', '--quiet', 'app').strip()
        assert container, 'app container missing'
        value = json.loads(self.command(['inspect', container]))[0]
        # 完整 inspect 含公开测试环境；报告只摘录容器/镜像/数据卷身份。
        return value

    def stopped(self) -> dict:
        self.compose('stop', '--timeout', '30', 'app')
        info = self.inspect_app()
        assert info['State']['Running'] is False, '快照前 app 必须完全停止'
        assert not info['State']['OOMKilled'], '不接受 OOM 停机作为一致快照'
        assert info['State']['ExitCode'] in (0, 143), '不接受强杀退出作为一致快照'
        # uvicorn 完成 lifespan shutdown 后可重新抛出 SIGTERM；Docker 记录 128+15。
        return info

    def probe(self, phase: str) -> dict:
        raw = self.compose('run', '--rm', '--no-deps', '-T', 'probe', phase)
        result = json.loads(raw.strip().splitlines()[-1])
        assert result['status'] == 'passed' and result['phase'] == phase, result
        return result['checks']


def identity(info: dict) -> dict:
    data_mount = next(mount for mount in info['Mounts'] if mount['Destination'] == '/app/data')
    return {'container_id': info['Id'], 'image_id': info['Image'],
            'data_volume': data_mount['Name'], 'started_at': info['State']['StartedAt']}


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'mode': 'docker_internal_mock', 'paid_model_calls': 0,
              'checks': {}, 'limits': ['same-image snapshot data rollback, not cross-version code/schema rollback',
              'single API and embedded Qdrant; no Redis workers/frontend/load acceptance',
              'production RPO/RTO and real model quality unknown']}
    started = time.monotonic()
    docker = None
    try:
        # 不读取 .env；拒绝不干净配置源，而不是把真实配置混入构建。
        if (ROOT / '.env').exists() or (ROOT / '.env').is_symlink():
            raise RuntimeError('Run in a clean checkout without .env; existing files will not be read or changed')
        report['git_head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report['source_sha256'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('Dockerfile', '.dockerignore', 'docker/Dockerfile.ci', 'docker/docker-compose.yaml',
                         'docker/docker-compose.ci.yaml', 'scripts/ci/container_smoke.py',
                         'scripts/ci/container_probe.py', 'uv.lock', 'data/catalog-demo.jsonl.gz')}
        with tempfile.TemporaryDirectory(prefix='voyacart-container-home-') as temporary:
            project = 'voyacart-ci-' + uuid.uuid4().hex[:12]
            env = isolated_docker_env(Path(temporary), project)
            docker = Docker(env, project, output)
            try:
                docker.command(['version'], timeout=30)
                docker.compose('version')
                config = json.loads(docker.compose('config', '--format', 'json'))
                validate_config(config)
                report['checks']['isolated_config'] = True
                docker.command(['build', '--pull', '--tag', env['CI_RUNTIME_IMAGE'], '.'], timeout=1200)
                docker.command(['build', '--build-arg', 'RUNTIME_IMAGE=' + env['CI_RUNTIME_IMAGE'],
                                '--file', 'docker/Dockerfile.ci', '--tag', env['CI_PROBE_IMAGE'], '.'], timeout=300)
                report['checks']['runtime_and_probe_images_built'] = True
                docker.compose('up', '-d', '--wait', '--wait-timeout', '300', 'app', 'mock-llm')
                before = identity(docker.inspect_app())
                report['images'] = {'runtime': before['image_id']}
                report['checks']['signed_http'] = docker.probe('seed')

                docker.compose('restart', '--timeout', '30', 'app')
                docker.compose('up', '-d', '--no-recreate', '--wait', '--wait-timeout', '300', 'app')
                restarted = identity(docker.inspect_app())
                assert restarted['container_id'] == before['container_id']
                assert restarted['started_at'] != before['started_at']
                assert restarted['data_volume'] == before['data_volume']
                report['checks']['container_restart'] = docker.probe('verify')

                docker.compose('up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '300', 'app')
                recreated = identity(docker.inspect_app())
                assert recreated['container_id'] != restarted['container_id']
                assert recreated['data_volume'] == before['data_volume']
                assert recreated['image_id'] == before['image_id']
                report['checks']['container_recreate_retains_volume'] = docker.probe('verify')

                docker.stopped()
                report['checks']['source_index'] = docker.probe('index')
                report['checks']['quiesced_snapshot'] = docker.probe('snapshot')
                docker.compose('up', '-d', '--wait', '--wait-timeout', '300', 'app')
                report['checks']['post_snapshot_mutation'] = docker.probe('mutate')
                docker.stopped()
                report['checks']['restore_to_fresh_volume'] = docker.probe('restore')
                env['CI_STATE_SLOT'] = 'restored'
                docker.compose('up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '300', 'app')
                restored = identity(docker.inspect_app())
                assert restored['image_id'] == before['image_id']
                assert restored['data_volume'] != before['data_volume']
                report['checks']['same_image_snapshot_rollback'] = docker.probe('verify')
                docker.stopped()
                report['checks']['restored_index'] = docker.probe('index')
                report['checks']['original_volume_preserved'] = docker.probe('original-preserved')
                report['lifecycle'] = {'initial': before, 'restarted': restarted, 'recreated': recreated, 'restored': restored}
                report['status'] = 'passed'
            finally:
                # 只清理随机 project 的合成容器/卷；不执行全局 prune。
                primary_failure = sys.exc_info()[0] is not None
                try:
                    docker.compose('logs', '--no-color', '--timestamps', check=False, timeout=60)
                except Exception as error:
                    report['log_collection_error'] = type(error).__name__
                try:
                    docker.compose('down', '--volumes', '--remove-orphans', timeout=120)
                    report['cleanup'] = 'passed'
                except Exception as error:
                    report['cleanup'] = {'status': 'failed', 'type': type(error).__name__}
                    if not primary_failure:
                        raise
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
