"""AG-UI 流式压测；每轮使用新会话，原始时延与续传检查写入 CSV。"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import random
import time
import uuid

from gevent import Timeout
from gevent.lock import Semaphore
from locust import User, between, events, task
import requests

from client import FIELDS, consume_run


@events.init_command_line_parser.add_listener
def options(parser):
    parser.add_argument('--agui-output', required=True, help='新证据目录；已有 runs.csv 时拒绝覆盖')
    parser.add_argument('--agui-reconnect-rate', type=float, default=.1)
    parser.add_argument('--agui-timeout', type=float, default=180)


@events.test_start.add_listener
def begin(environment, **_):
    options = environment.parsed_options
    if not 0 <= options.agui_reconnect_rate <= 1:
        raise ValueError('agui-reconnect-rate 需为 0–1')
    directory = Path(options.agui_output)
    directory.mkdir(parents=True, exist_ok=True)
    environment.agui_file = (directory / 'runs.csv').open('x', newline='')
    environment.agui_writer = csv.DictWriter(environment.agui_file, fieldnames=FIELDS)
    environment.agui_writer.writeheader()
    environment.agui_lock = Semaphore()
    (directory / 'load-config.json').write_text(json.dumps({
        'host':environment.host, 'reconnect_rate':options.agui_reconnect_rate,
        'timeout_seconds':options.agui_timeout, 'think_seconds':[5,15],
        'users':options.num_users, 'spawn_rate':options.spawn_rate,
        'started_at':time.time()}, indent=2) + '\n')


@events.test_stop.add_listener
def end(environment, **_):
    environment.agui_file.close()


class ShoppingUser(User):
    wait_time = between(5, 15)

    def on_start(self):
        self.http = requests.Session()
        token = os.getenv('VOYACART_API_TOKEN', '')
        if token:
            self.http.headers['Authorization'] = 'Bearer ' + token
        self.reconnect = random.random() < self.environment.parsed_options.agui_reconnect_rate

    def on_stop(self):
        self.http.close()

    @task
    def shopping(self):
        run_id = 'load-' + uuid.uuid4().hex
        buyer = os.getenv('VOYACART_BUYER_ID') or run_id
        row = {}
        timeout = self.environment.parsed_options.agui_timeout
        try:
            with Timeout(timeout, requests.Timeout('run_deadline_exceeded')):
                consume_run(self.http, self.environment.host.rstrip('/'), buyer, run_id,
                    reconnect=self.reconnect, timeout=timeout, row=row)
        finally:
            with self.environment.agui_lock:
                self.environment.agui_writer.writerow(row)
                self.environment.agui_file.flush()
            self.environment.events.request.fire(request_type='AGUI', name='run_complete',
                response_time=row['complete_ms'], response_length=0,
                exception=RuntimeError(row['error_code']) if row['error_code'] else None)
            for field in ('first_event_ms', 'first_token_ms'):
                if row[field] != '':
                    self.environment.events.request.fire(request_type='TIMING', name=field,
                        response_time=row[field], response_length=0, exception=None)
