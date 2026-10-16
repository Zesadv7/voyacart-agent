"""在全新 voyacart-r2-* Docker 项目逐项演练；证据目录保留服务与原始传输日志。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import requests

from client import sse_events

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ('worker', 'api', 'redis', 'embedding', 'model', 'hotspot')


def validate_target(project, port):
    if not re.fullmatch(r'voyacart-r2-[a-z0-9][a-z0-9-]*', project):
        raise ValueError('project 需为独立的 voyacart-r2-* 项目')
    if not 1024 < port < 65536 or port in {8000, 8080, 8085}:
        raise ValueError('使用独立的本机端口，避开当前应用和容量矩阵')


def verify_sequence(records, run_id):
    ids = [row['id'] for row in records]
    if ids != [f'{run_id}:{n}' for n in range(1, len(ids) + 1)]:
        raise AssertionError('事件序号存在缺口或重复')
    if not records or records[-1]['event']['type'] not in {'RUN_FINISHED', 'RUN_ERROR'}:
        raise AssertionError('缺少运行终态')


def consume(session, host, buyer, run_id, output, *, action=None):
    body = {'threadId': run_id, 'runId': run_id,
        'messages': [{'id': run_id + '-user', 'role': 'user',
            'content': '预算300元以内，找一个寄到中国的轻便背包'}],
        'state': {}, 'tools': [], 'context': [],
        'forwardedProps': {'buyerId': buyer, 'locale': 'zh-CN', 'currency': 'CNY'}}
    records, upstreams, transport_errors = [], [], []
    method, url, options = 'POST', host + '/commerce/ag-ui/run', {'json': body}
    deadline, performed = time.monotonic() + 180, False
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with session.request(method, url, stream=True, timeout=90, **options) as response:
                upstreams.append(response.headers.get('X-R2-Upstream', ''))
                response.raise_for_status()
                response.encoding = 'utf-8'
                with (output / f'sse-{run_id}-{attempt}.log').open('xb') as log:
                    def lines():
                        pending = b''
                        for chunk in response.iter_content(chunk_size=None):
                            log.write(chunk)
                            log.flush()
                            pending += chunk
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                yield line.removesuffix(b'\r').decode('utf-8')
                        if pending:
                            yield pending.decode('utf-8')
                    for event_id, kind, event in sse_events(lines()):
                        if kind == 'transport-error':
                            raise RuntimeError('运行传输等待超时')
                        expected = f'{run_id}:{len(records) + 1}'
                        if event_id != expected:
                            raise AssertionError(f'事件序号不连续：{event_id}，预期 {expected}')
                        records.append({'id': event_id, 'event': event, 'observed_at': time.time()})
                        if action and not performed and event['type'] == 'TEXT_MESSAGE_CONTENT':
                            action(response)
                            performed = True
                        if event['type'] in {'RUN_FINISHED', 'RUN_ERROR'}:
                            verify_sequence(records, run_id)
                            if action and not performed:
                                raise AssertionError('运行结束前未执行故障注入')
                            result = {'run_id': run_id, 'buyer': buyer, 'records': records,
                                'upstreams': upstreams, 'transport_errors': transport_errors,
                                'fault_injected': performed}
                            (output / f'run-{run_id}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                            return result
                raise requests.ConnectionError('终态前连接关闭')
        except requests.RequestException as error:
            transport_errors.append({'attempt': attempt, 'type': type(error).__name__,
                'message': str(error), 'observed_at': time.time()})
            (output / f'transport-{run_id}.json').write_text(json.dumps(transport_errors, indent=2) + '\n')
            if not performed:
                raise
            method, url = 'GET', host + '/commerce/ag-ui/runs/' + run_id + '/events'
            options = {'params': {'buyer_id': buyer},
                'headers': {'Last-Event-ID': records[-1]['id']}}
            time.sleep(.5)
    raise TimeoutError('故障后续传未在 180 秒内结束')


class Drill:
    def __init__(self, args):
        validate_target(args.project, args.port)
        self.args, self.output = args, args.output.resolve()
        self.output.relative_to(ROOT / 'eval/verification')
        self.output.mkdir(parents=True, exist_ok=False)
        self.host = f'http://127.0.0.1:{args.port}'
        self.env = {key: value for key, value in os.environ.items()
            if key in {'PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'TMPDIR'}}
        self.env.update(LLM_BASE_URL='http://mock-llm:8081/v1', LLM_API_KEY='r2-local',
            LLM_MODEL='mock-primary', LLM_FALLBACK_MODEL='mock-fallback',
            EMBEDDING_BASE_URL='http://mock-embedding:8081/v1', EMBEDDING_API_KEY='r2-local',
            EMBEDDING_MODEL='mock-embedding', LLM_MAX_CONCURRENCY='8', LLM_MIN_INTERVAL_SECONDS='0',
            LLM_MAX_RETRIES='1', WORKER_CONCURRENCY='1', SEMANTIC_CACHE_ENABLED='0',
            MOCK_ORDER_RATE='0', VOYACART_SCALE_PORT=str(args.port))
        (self.output / 'empty.env').touch()
        nginx = (ROOT / 'docker/nginx.scale.conf').read_text().replace(
            'add_header X-Accel-Buffering no;',
            'add_header X-Accel-Buffering no;\n        add_header X-R2-Upstream $upstream_addr always;')
        (self.output / 'nginx.conf').write_text(nginx)
        nginx_source = ROOT / '.cache/r2' / (args.project + '.conf')
        nginx_source.parent.mkdir(parents=True, exist_ok=True)
        with nginx_source.open('x') as file:
            file.write(nginx)
        local = args.scenario == 'embedding'
        runtime = args.project + '-runtime'
        services = {name: {'image': runtime, 'environment': {'OPENSSL_armcap': '0',
                'LOCAL_EMBEDDING_FALLBACK': '1' if local else '0'}}
            for name in ('api', 'worker', 'automation-worker')}
        services['mock-llm'] = {'image': runtime, 'environment': {'MOCK_FAULT_CONTROL': '1'}}
        services['mock-embedding'] = {'image': runtime, 'command':
            ['uvicorn', '--app-dir', '/app/scripts/loadtest', 'mock_llm:app', '--host', '0.0.0.0', '--port', '8081']}
        services['nginx'] = {'image': args.project + '-frontend', 'build': {'args':
            {'NGINX_CONFIG': str(nginx_source.relative_to(ROOT))}}}
        (self.output / 'compose.override.json').write_text(json.dumps({'services': services}, indent=2) + '\n')
        self.compose = ['docker', 'compose', '--env-file', str(self.output / 'empty.env'),
            '-p', args.project, '-f', str(ROOT / 'docker/docker-compose.scale.yaml'),
            '-f', str(self.output / 'compose.override.json'), '--profile', 'mock']
        self.command_index = 0
        self.owns_project = False

    def command(self, *args):
        self.command_index += 1
        prefix = f'command-{self.command_index:03d}'
        (self.output / f'{prefix}.json').write_text(json.dumps(list(args), ensure_ascii=False) + '\n')
        result = subprocess.run(args, cwd=ROOT, env=self.env, capture_output=True, timeout=900)
        (self.output / f'{prefix}.stdout.log').write_bytes(result.stdout)
        (self.output / f'{prefix}.stderr.log').write_bytes(result.stderr)
        (self.output / f'{prefix}.exit.json').write_text(json.dumps({'returncode': result.returncode}) + '\n')
        result.check_returncode()
        return result.stdout.decode()

    def dc(self, *args):
        return self.command(*self.compose, *args)

    def container(self, service):
        ids = self.dc('ps', '-q', service).split()
        if len(ids) != 1:
            raise RuntimeError(f'{service} 需要恰好一个运行实例')
        return ids[0]

    def owned_container(self, container):
        project = self.command('docker', 'inspect', '--format',
            '{{index .Config.Labels "com.docker.compose.project"}}', container).strip()
        if project != self.args.project:
            raise ValueError('容器不属于本次专用项目')

    def admin(self, path, body=None):
        script = "import json,sys,urllib.request; data=None if sys.argv[2]=='null' else sys.argv[2].encode(); req=urllib.request.Request('http://127.0.0.1:8081'+sys.argv[1],data=data,headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req,timeout=5).read().decode())"
        return json.loads(self.dc('exec', '-T', 'mock-llm', 'python', '-c', script,
            path, json.dumps(body)))

    def query(self, database, sql, parameters=()):
        script = "import sqlite3,json,sys; db=sqlite3.connect('/app/data/'+sys.argv[1]); rows=db.execute(sys.argv[2],json.loads(sys.argv[3])).fetchall(); db.commit(); print(json.dumps(rows))"
        return json.loads(self.dc('exec', '-T', '--index', '1', 'api', 'python', '-c', script,
            database, sql, json.dumps(parameters)))

    def prepare(self):
        if self.dc('ps', '-aq').strip():
            raise ValueError('项目已存在，使用全新的 voyacart-r2-* 名称')
        if self.command('docker', 'volume', 'ls', '--filter',
            'label=com.docker.compose.project=' + self.args.project, '--format', '{{.Name}}').strip():
            raise ValueError('项目数据卷已存在，使用全新的 voyacart-r2-* 名称')
        self.owns_project = True
        fingerprints = {str(path.relative_to(ROOT)): hashlib.blake2b(path.read_bytes()).hexdigest()
            for directory in ('app', 'scripts/loadtest') for path in sorted((ROOT / directory).rglob('*.py'))}
        (self.output / 'source.json').write_text(json.dumps({'commit': self.command('git', 'rev-parse', 'HEAD').strip(),
            'content_fingerprints_blake2b': fingerprints, 'scenario': self.args.scenario,
            'project': self.args.project, 'host': self.host, 'settings':
            {key: value for key, value in self.env.items() if key.startswith(('LLM_', 'EMBEDDING_', 'MOCK_', 'WORKER_', 'SEMANTIC_', 'VOYACART_'))}}, indent=2) + '\n')
        self.dc('build', 'api', 'worker', 'automation-worker', 'mock-llm', 'nginx')
        if self.args.scenario == 'embedding':
            if not self.args.model_cache or not (self.args.model_cache / 'models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q').exists():
                raise ValueError('嵌入演练需要提前准备 MiniLM 缓存；本脚本只复制本机缓存')
            seed = self.args.project + '-model-seed'
            self.dc('run', '-d', '--no-deps', '--name', seed, 'api', 'sleep', '300')
            try:
                self.owned_container(seed)
                self.command('docker', 'exec', seed, 'mkdir', '-p', '/app/data/local_embedding/model')
                self.command('docker', 'cp', str(self.args.model_cache.resolve()) + '/.',
                    seed + ':/app/data/local_embedding/model/')
            finally:
                self.command('docker', 'rm', '-f', seed)
        self.dc('up', '-d', '--no-build', '--scale', 'api=2', '--scale', 'worker=1')
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                response = requests.get(self.host + '/health', timeout=3)
                if response.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        raise TimeoutError('专用环境启动超时；保留启动日志')

    def one_run(self, name, action=None):
        run_id = 'r2-' + name + '-' + uuid.uuid4().hex
        with requests.Session() as session:
            result = consume(session, self.host, run_id, run_id, self.output, action=action)
            response = session.get(self.host + '/commerce/ag-ui/runs/' + run_id,
                params={'buyer_id': run_id}, timeout=10)
            response.raise_for_status()
            result['snapshot'] = response.json()
            response = session.get(self.host + '/commerce/orders', params={'buyer_id': run_id}, timeout=10)
            response.raise_for_status()
            assert response.json()['total'] == 0, '未批准会话产生了订单'
        return result

    def run(self):
        scenario = self.args.scenario
        details = {}
        self.admin('/admin/fault', {'model': 'mock-primary', 'status': 429, 'requests': 0})
        if scenario == 'worker':
            worker = self.container('worker')
            self.owned_container(worker)
            def kill_worker(response):
                details['calls_before_kill'] = self.admin('/admin/requests')
                self.command('docker', 'kill', '--signal', 'KILL', worker)
                details['killed_at'] = time.time()
                details['lease_until'] = self.query('ag_ui_runs.db', 'SELECT lease_until FROM agui_runs')[0][0]
                self.command('docker', 'start', worker)
            result = self.one_run(scenario, kill_worker)
            assert result['snapshot']['status'] == 'interrupted'
            assert result['records'][-1]['event']['code'] == 'SERVER_RESTART'
            details['recovered_at_server'] = result['snapshot']['updatedAt'] / 1000
            delay = details['recovered_at_server'] - details['lease_until']
            details['recovery_after_lease_seconds'] = delay
            (self.output / 'worker-check.json').write_text(json.dumps(details, indent=2) + '\n')
            assert 0 <= delay <= 5.5, '运行中断恢复超出租约加 5 秒'
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status = self.dc('exec', '-T', 'redis', 'redis-cli', '--raw', 'GET',
                    'voyacart:task:agui:' + result['run_id']).strip()
                details['redelivery_status'] = json.loads(status)
                if details['redelivery_status']['state'] == 'done':
                    break
                time.sleep(1)
            else:
                raise TimeoutError('worker 重投任务未完成确认')
            details['calls_after_redelivery'] = self.admin('/admin/requests')
            assert details['calls_before_kill']['requests'] == details['calls_after_redelivery']['requests']
            assert sum(row['event']['type'] == 'RUN_STARTED' for row in result['records']) == 1
            details['run'] = result
        elif scenario == 'api':
            def stop_api(response):
                upstream = response.headers['X-R2-Upstream'].split(':')[0]
                ids = self.dc('ps', '-q', 'api').split()
                for container in ids:
                    networks = json.loads(self.command('docker', 'inspect', '--format',
                        '{{json .NetworkSettings.Networks}}', container))
                    if any(network['IPAddress'] == upstream for network in networks.values()):
                        self.owned_container(container)
                        details['stopped_upstream'] = response.headers['X-R2-Upstream']
                        self.command('docker', 'stop', '--time', '0', container)
                        return
                raise ValueError('SSE 上游未匹配到本次 API 容器')
            result = self.one_run(scenario, stop_api)
            assert result['records'][-1]['event']['type'] == 'RUN_FINISHED'
            assert result['transport_errors']
            assert result['upstreams'][-1] != details['stopped_upstream']
            details['run'] = result
        elif scenario == 'redis':
            self.dc('stop', 'redis')
            before = self.admin('/admin/requests')
            result = self.one_run('redis-down')
            assert result['records'][-1]['event']['type'] == 'RUN_ERROR'
            assert any(row['event'].get('name') == 'runtime.degraded' and
                row['event']['value']['fallback'] == 'in_process' for row in result['records'])
            assert self.admin('/admin/requests')['requests'] == before['requests'], '共享限流停机期间发送了模型请求'
            self.dc('start', 'redis')
            redis = self.container('redis')
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self.command('docker', 'inspect', '--format', '{{.State.Health.Status}}', redis).strip() == 'healthy':
                    break
                time.sleep(1)
            else:
                raise TimeoutError('Redis 重启后未恢复健康状态')
            recovered = self.one_run('redis-restored')
            assert recovered['records'][-1]['event']['type'] == 'RUN_FINISHED'
            assert not any(row['event'].get('name') == 'runtime.degraded' for row in recovered['records'])
            details.update(during_outage=result, recovered=recovered)
        elif scenario == 'embedding':
            self.dc('stop', 'mock-embedding')
            result = self.one_run('embedding-down')
            assert result['records'][-1]['event']['type'] == 'RUN_FINISHED'
            results = [json.loads(row['event']['content']) for row in result['records']
                if row['event']['type'] == 'TOOL_CALL_RESULT']
            products = [payload for payload in results if 'hits' in payload]
            assert products and products[0]['hits'] and products[0]['recall_strategy'] == 'local_embedding'
            details['run'] = result
        elif scenario == 'model':
            for status in (429, 503):
                self.admin('/admin/fault', {'model': 'mock-primary', 'status': status, 'requests': -1})
                result = self.one_run('model-' + str(status))
                assert result['records'][-1]['event']['type'] == 'RUN_FINISHED'
                assert any(row['event'].get('name') == 'model.fallback' for row in result['records'])
                audit = self.admin('/admin/requests')
                assert audit['requests']['mock-primary'][str(status)] >= 2
                assert audit['requests']['mock-fallback']['200'] >= 1
                details[str(status)] = {'run': result, 'audit': audit}
            self.admin('/admin/fault', {'model': 'mock-primary', 'status': 503, 'requests': 0})
            recovered = self.one_run('model-restored')
            assert recovered['records'][-1]['event']['type'] == 'RUN_FINISHED'
            assert not any(row['event'].get('name') == 'model.fallback' for row in recovered['records'])
            details['recovered'] = recovered
            circuit = self.dc('exec', '-T', 'redis', 'redis-cli', '--raw', 'GET',
                'voyacart:breaker:product_search_tool').strip()
            assert circuit == '', '成功检索后工具熔断状态仍未清除'
            details['circuit_scope'] = '工具熔断在本轨迹保持关闭：检索工具成功，主模型故障由重试和备用模型处理。本证据未覆盖工具熔断触发。'
        else:
            self.query('voyacart.db', 'UPDATE trade_sku_inventory SET stock=100 WHERE sku_id=?', ('P1005-S1',))
            before = self.query('voyacart.db', 'SELECT stock FROM trade_sku_inventory WHERE sku_id=?', ('P1005-S1',))
            assert before == [[100]]
            self.command(sys.executable, str(ROOT / 'scripts/loadtest/hotspot.py'), '--host', self.host,
                '--output', str(self.output / 'hotspot'), '--product', 'P1005', '--sku', 'P1005-S1',
                '--count', '500', '--stock', '100')
            after = self.query('voyacart.db', 'SELECT stock FROM trade_sku_inventory WHERE sku_id=?', ('P1005-S1',))
            assert after == [[0]]
            assert self.query('voyacart.db', 'SELECT COUNT(*) FROM trade_sku_inventory WHERE stock<0') == [[0]]
            details.update(stock_before=before, stock_after=after,
                hotspot=json.loads((self.output / 'hotspot/report.json').read_text()))
        (self.output / 'report.json').write_text(json.dumps({'scenario': scenario, 'passed': True,
            'details': details}, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=SCENARIOS)
    parser.add_argument('--project', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--model-cache', type=Path)
    args = parser.parse_args()
    drill = Drill(args)
    try:
        drill.prepare()
        drill.run()
    except BaseException as error:
        (drill.output / 'failure.json').write_text(json.dumps({'type': type(error).__name__,
            'message': str(error)}, ensure_ascii=False, indent=2) + '\n')
        raise
    finally:
        if drill.owns_project:
            drill.dc('logs', '--no-color', '--timestamps')
            drill.dc('ps', '-a', '--format', 'json')
            drill.dc('stop')


if __name__ == '__main__':
    main()
