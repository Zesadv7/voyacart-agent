"""热点准备请求在错误代理环境中直连本机，失败响应先写入证据。"""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

spec = importlib.util.spec_from_file_location('hotspot_load',
    Path(__file__).resolve().parents[1] / 'scripts/loadtest/hotspot.py')
hotspot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hotspot)


async def test_bad_proxy_cannot_redirect_local_preparation_and_failure_body_is_retained(monkeypatch, tmp_path):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    for name in ('NO_PROXY', 'no_proxy', 'VOYACART_API_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    received = []
    body = b'\n  injected gateway failure \r\n'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(503)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args = SimpleNamespace(host=f'http://127.0.0.1:{server.server_port}', output=tmp_path/'run',
        product='P1005', sku='P1005-S1', count=1, stock=100)
    try:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await hotspot.run(args)
        assert error.value.response.status_code == 503
        assert len(received) == 1 and received[0][0] == '/commerce/confirmations/orders'
        row = json.loads((args.output/'preparation.jsonl').read_text())
        assert row == {'index':0, 'status':503, 'body':body.decode()}
        assert not (args.output/'approvals.json').exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
