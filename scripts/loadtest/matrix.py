"""运行独立 R1 容量实验；默认 API 1/2/4 × worker 1/2/4 × 两种模式。"""
import argparse
import csv
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

from analyze import analyze


def execute(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/'progress.json').exists():
        raise ValueError('请使用新的输出目录，已有实验记录保留')
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, 'LLM_BASE_URL':'http://mock-llm:8081/v1',
        'LLM_API_KEY':'local-model', 'LLM_MODEL':'mock-chat',
        'EMBEDDING_BASE_URL':'http://mock-llm:8081/v1', 'EMBEDDING_API_KEY':'local-model',
        'LLM_FALLBACK_MODEL':'', 'MOCK_TTFT_SECONDS':'0.8', 'MOCK_TOKEN_SECONDS':'0.03',
        'MOCK_RESPONSE_TOKENS':'300', 'MOCK_ORDER_RATE':'0.1', 'MOCK_ERROR_RATE':'0'}
    for name in ('VOYACART_API_TOKEN', 'VOYACART_BUYER_ID'):
        environment.pop(name, None)
    project = 'voyacart-r1-baseline' if args.baseline else 'voyacart-r1-matrix'
    environment['VOYACART_LOAD_IMAGE'] = 'voyacart-runtime:r1-baseline' if args.baseline else 'voyacart-runtime:r1'
    environment['VOYACART_LOAD_PORT'] = '8085'
    compose = ['docker','compose','-p',project,'-f','docker/docker-compose.load.yaml','--profile','mock']
    sources = [root/p for p in ('scripts/loadtest/client.py','scripts/loadtest/locustfile.py',
        'scripts/loadtest/analyze.py','scripts/loadtest/mock_llm.py','scripts/loadtest/matrix.py',
        'docker/docker-compose.load.yaml','app/infrastructure/ag_ui_journal.py',
        'app/infrastructure/persistence/sql/repositories.py',
        'app/infrastructure/persistence/sql/session_store.py',
        'app/infrastructure/cache/redis_cache.py')]
    (output/'manifest.json').write_text(json.dumps({'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'image':environment['VOYACART_LOAD_IMAGE'],
        'image_id':subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',environment['VOYACART_LOAD_IMAGE']],text=True).strip(),
        'project':project,'load_seconds':args.seconds,
        'users':args.users, 'plateau_relative_gain':.1, 'max_error_rate':.05,
        'source_fingerprints_blake2b':{str(p.relative_to(root)):hashlib.blake2b(p.read_bytes()).hexdigest() for p in sources},
        'model_concurrency_limit':10000,'model_min_interval_seconds':0,'worker_concurrency':2,
        'mock':{k:v for k,v in environment.items() if k.startswith('MOCK_')}},indent=2)+'\n')
    configurations = [('inline',1,1)] if args.baseline else list(itertools.product(('inline','queue'),(1,2,4),(1,2,4)))
    if args.configuration:
        configurations = [entry for entry in configurations if f'{entry[0]}-a{entry[1]}-w{entry[2]}' in args.configuration]
    completed = []
    try:
        for mode, api, workers in configurations:
            directory = output/f'{mode}-a{api}-w{workers}'
            directory.mkdir()
            environment['AGUI_QUEUE_ENABLED'] = '1' if mode == 'queue' else '0'
            with (directory/'compose.json').open('w') as file:
                subprocess.run(compose+['config','--format','json'], env=environment, stdout=file, check=True)
            with (directory/'start.log').open('w') as file:
                subprocess.run(compose+['up','-d','--no-build','--force-recreate','--scale',f'api={api}','--scale',f'worker={workers}'],
                    cwd=root, env=environment, stdout=file, stderr=subprocess.STDOUT, check=True)
            with httpx.Client(timeout=10) as health:
                for attempt in range(60):
                    try:
                        health.get('http://127.0.0.1:8085/health').raise_for_status()
                        break
                    except httpx.ConnectError:
                        if attempt == 59:
                            raise
                        time.sleep(1)
            container_ids = subprocess.check_output(['docker','ps','--filter',f'label=com.docker.compose.project={project}',
                '--format','{{.Names}}'],text=True).splitlines()
            stages, saturation = [], None
            for users in args.users:
                stage = directory/f'users-{users}'
                stage.mkdir()
                command = [args.locust,'-f','scripts/loadtest/locustfile.py','--headless',
                    '--host','http://127.0.0.1:8085','--users',str(users),'--spawn-rate',str(users),
                    '--run-time',f'{args.seconds}s','--stop-timeout','190',
                    '--agui-output',str(stage),'--csv',str(stage/'locust'),'--csv-full-history']
                (stage/'command.json').write_text(json.dumps(command,indent=2)+'\n')
                started = time.time()
                with (stage/'locust.log').open('w') as log, (stage/'resources.jsonl').open('w') as resources:
                    process = subprocess.Popen(command,cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT)
                    while process.poll() is None:
                        sample = subprocess.check_output(['docker','stats','--no-stream','--format','{{json .}}',*container_ids],text=True)
                        processes = {name:subprocess.check_output(['docker','top',name,'-eo','pid,rss,comm'],text=True)
                            for name in container_ids if '-api-' in name or '-worker-' in name}
                        resources.write(json.dumps({'time':time.time(),'containers':[json.loads(line) for line in sample.splitlines()],
                            'processes':processes})+'\n')
                        resources.flush()
                        time.sleep(2)
                (stage/'exit.json').write_text(json.dumps({'exit_code':process.returncode,'started_at':started,'finished_at':time.time()})+'\n')
                report = analyze(stage)
                retry_counts, lock_errors, redis_errors = {}, {}, {}
                for container in container_ids:
                    logs = subprocess.check_output(['docker','logs','--since',str(int(started)),container],stderr=subprocess.STDOUT)
                    (stage/(container+'.log')).write_bytes(logs)
                    retry_counts[container] = logs.count(b'sqlite_lock_retry component=')
                    lock_errors[container] = logs.count(b'database is locked')
                    redis_errors[container] = logs.count(b'Too many connections') + logs.count(b'No connection available')
                report.update(users=users,sqlite_lock_retries=retry_counts,
                    sqlite_lock_error_log_occurrences=lock_errors,redis_pool_error_log_occurrences=redis_errors)
                (stage/'report.json').write_text(json.dumps(report,indent=2)+'\n')
                stages.append(report)
                print(json.dumps({'configuration':directory.name,'users':users,'report':report}),flush=True)
                (output/'progress.json').write_text(json.dumps({'configurations':completed,
                    'current':{'mode':mode,'api':api,'workers':workers,'stages':stages},'complete':False},indent=2)+'\n')
                if report['error_rate'] > .05:
                    saturation = {'users':users,'reason':'error_rate_above_5_percent'}
                    break
                if len(stages) > 1 and report['successful_runs_per_second'] <= stages[-2]['successful_runs_per_second'] * 1.1:
                    saturation = {'users':users,'previous_users':stages[-2]['users'],'reason':'throughput_growth_at_most_10_percent'}
                    break
                # 客户端超时后明确取消本次测试运行，等待在途工作收口后再切档。
                with (stage/'runs.csv').open() as file:
                    failed = [row for row in csv.DictReader(file) if row['error_code']]
                with httpx.Client(base_url='http://127.0.0.1:8085',timeout=15) as client:
                    for row in failed:
                        url='/commerce/ag-ui/runs/'+row['run_id']
                        response=client.get(url,params={'buyer_id':row['run_id']})
                        if response.status_code == 404:
                            continue
                        response.raise_for_status()
                        if response.json()['status'] in {'running','queued'}:
                            client.post(url+'/cancel',params={'buyer_id':row['run_id']}).raise_for_status()
                            for _ in range(60):
                                state=client.get(url,params={'buyer_id':row['run_id']})
                                state.raise_for_status()
                                if state.json()['status'] not in {'running','queued'}:
                                    break
                                time.sleep(1)
                            else:
                                raise RuntimeError('测试运行取消后仍在执行：'+row['run_id'])
            completed.append({'mode':mode,'api':api,'workers':workers,'stages':stages,'observed_saturation':saturation})
            (output/'progress.json').write_text(json.dumps({'configurations':completed,'complete':len(completed)==len(configurations)},indent=2)+'\n')
            with (directory/'stop.log').open('w') as file:
                subprocess.run(compose+['stop'],env=environment,stdout=file,stderr=subprocess.STDOUT,check=True)
    finally:
        with (output/'stop.log').open('w') as file:
            subprocess.run(compose+['stop'],env=environment,stdout=file,stderr=subprocess.STDOUT,check=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--locust',default='locust')
    parser.add_argument('--users',type=lambda value:[int(n) for n in value.split(',')],default=[4,8,16,32,64])
    parser.add_argument('--seconds',type=int,default=45)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--configuration',action='append',choices=[f'{mode}-a{api}-w{workers}'
        for mode,api,workers in itertools.product(('inline','queue'),(1,2,4),(1,2,4))])
    execute(parser.parse_args())
