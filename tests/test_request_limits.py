"""直连 ASGI 请求边界，无服务端口、模型或真实凭据。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request

from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.security.request_limits import RequestLimitsMiddleware, request_limit_options


def public_settings():
    return SimpleNamespace(identity_mode='hmac', identity_hmac_secret='synthetic-only-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ',
        session_owner_binding=True, output_guard_enabled=True, public_deployment=True,
        cors_origins=['https://shop.example.invalid'], token_budget_total=4000,
        http_max_request_bytes=1048576, http_body_timeout_seconds=30, http_requests_per_minute=120)


@pytest.mark.parametrize('field,value', [
    ('identity_mode', 'demo'), ('identity_hmac_secret', 'a' * 64), ('identity_hmac_secret', 'short'),
    ('session_owner_binding', False), ('output_guard_enabled', False), ('cors_origins', []),
    ('cors_origins', ['*']), ('cors_origins', ['https://*.example.invalid']),
    ('cors_origins', ['https://shop.example.invalid/path']), ('cors_origins', ['https://shop.example.invalid/']),
    ('cors_origins', ['http://shop.example.invalid']), ('cors_origins', ['https://u:p@shop.example.invalid']),
    ('cors_origins', ['https://shop.example.invalid?secret=canary']),
    ('cors_origins', ['https://@shop.example.invalid']), ('cors_origins', ['https://bad%40host.invalid']),
    ('cors_origins', ['https://bad_host.invalid']),
    ('cors_origins', ['https://shop.example.invalid#fragment']), ('cors_origins', ['https://shop.example.invalid:bad']),
    ('cors_origins', ['https://shop.example.invalid:0']), ('cors_origins', ['https://shop.example.invalid\n']),
    ('token_budget_total', 0), ('token_budget_total', -1), ('token_budget_total', True),
    ('http_requests_per_minute', 0), ('http_requests_per_minute', -1),
    ('http_requests_per_minute', 100001), ('http_max_request_bytes', 0), ('http_max_request_bytes', 16777217),
    ('http_body_timeout_seconds', float('inf')), ('http_body_timeout_seconds', float('nan')),
])
def test_public_configuration_rejects_unsafe_values(field, value):
    settings = public_settings()
    setattr(settings, field, value)
    with pytest.raises(ValueError):
        IdentityPolicy.from_settings(settings)


def test_public_configuration_is_explicit_and_valid_example_is_accepted():
    assert IdentityPolicy.from_settings(public_settings()).mode == 'hmac'
    assert request_limit_options(SimpleNamespace()) == {
        'max_request_bytes': 1048576, 'body_timeout_seconds': 30.0, 'requests_per_minute': 0}
    assert IdentityPolicy.from_settings(SimpleNamespace(identity_mode='demo', identity_hmac_secret='',
        session_owner_binding=False)).mode == 'demo'


async def raw_request(middleware, *, chunks=(b'ab',), headers=(), delay=0):
    messages = [{'type': 'http.request', 'body': chunk, 'more_body': index < len(chunks) - 1}
                for index, chunk in enumerate(chunks)]
    async def receive():
        if delay:
            await asyncio.sleep(delay)
        return messages.pop(0) if messages else {'type': 'http.disconnect'}
    sent = []
    async def send(message):
        sent.append(message)
    await middleware({'type': 'http', 'method': 'POST', 'path': '/commerce/example', 'headers': headers}, receive, send)
    return sent


@pytest.mark.parametrize('chunks,headers,status', [
    ((b'123456',), (), 413),
    ((b'12', b'34', b'56'), (), 413),
    ((b'123456',), ((b'content-length', b'1'),), 413),
    ((b'',), ((b'content-length', b'999'),), 413),
    ((b'',), ((b'content-length', b'9' * 5000),), 413),
    ((b'',), ((b'content-length', b'-1'),), 400),
    ((b'',), ((b'content-length', b'bad'),), 400),
    ((b'',), ((b'content-length', b'2'), (b'content-length', b'2')), 400),
])
async def test_oversized_or_invalid_body_never_enters_business_route(chunks, headers, status):
    business = AsyncMock()
    sent = await raw_request(RequestLimitsMiddleware(business, max_request_bytes=5), chunks=chunks, headers=headers)
    assert sent[0]['status'] == status
    business.assert_not_awaited()


async def test_body_timeout_and_disconnect_do_not_enter_route():
    business = AsyncMock()
    sent = await raw_request(RequestLimitsMiddleware(business, body_timeout_seconds=0.001), delay=0.03)
    assert sent[0]['status'] == 408
    business.assert_not_awaited()
    sent = await raw_request(RequestLimitsMiddleware(business), chunks=())
    assert not sent
    business.assert_not_awaited()


async def test_exact_boundary_and_replayed_body_are_preserved():
    body = []
    async def business(scope, receive, send):
        body.append(await receive())
        assert (await receive())['type'] == 'http.disconnect'
    await raw_request(RequestLimitsMiddleware(business, max_request_bytes=5), chunks=(b'12', b'345'))
    assert body == [{'type': 'http.request', 'body': b'12345', 'more_body': False}]


async def test_rate_limit_is_process_wide_bounded_and_expires_without_spoofed_ip_bypass():
    clock = [100.0]
    app = FastAPI()
    calls = []
    @app.post('/commerce/example')
    async def endpoint(request: Request):
        calls.append(await request.body())
        return {'ok': True}
    @app.get('/health')
    async def health():
        return {'ok': True}
    middleware = RequestLimitsMiddleware(app, requests_per_minute=2, clock=lambda: clock[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url='http://local') as client:
        assert (await client.post('/commerce/example', content='1')).status_code == 200
        assert (await client.post('/commerce/example', content='2')).status_code == 200
        for _ in range(3):
            response = await client.post('/commerce/example', headers={'X-Forwarded-For': 'different'})
            assert response.status_code == 429 and response.headers['retry-after'] == '60'
        assert len(middleware._arrivals) == 2 and len(calls) == 2
        assert (await client.get('/health')).status_code == 200
        clock[0] += 60
        assert (await client.post('/commerce/example', content='3')).status_code == 200
        assert len(middleware._arrivals) == 1


def test_settings_read_explicit_request_boundary_environment(monkeypatch):
    from app.infrastructure.settings import load_settings
    monkeypatch.setenv('LLM_API_KEY', '')
    monkeypatch.setenv('PUBLIC_DEPLOYMENT', '1')
    monkeypatch.setenv('HTTP_MAX_REQUEST_BYTES', '4096')
    monkeypatch.setenv('HTTP_BODY_TIMEOUT_SECONDS', '2.5')
    monkeypatch.setenv('HTTP_REQUESTS_PER_MINUTE', '50')
    settings = load_settings(require_llm_key=False)
    assert settings.public_deployment is True
    assert request_limit_options(settings) == {
        'max_request_bytes': 4096, 'body_timeout_seconds': 2.5, 'requests_per_minute': 50}


@pytest.mark.parametrize('value', ['yes', 'enabled', '', 'treu'])
def test_public_deployment_typo_never_silently_disables_guards(monkeypatch, value):
    from app.infrastructure.settings import load_settings
    monkeypatch.setenv('LLM_API_KEY', '')
    monkeypatch.setenv('PUBLIC_DEPLOYMENT', value)
    with pytest.raises(ValueError, match='PUBLIC_DEPLOYMENT'):
        load_settings(require_llm_key=False)
