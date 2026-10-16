"""官方客户端读取部署假模型的工具增量、文本与嵌入。"""
import json

import httpx
from openai import AsyncOpenAI
from unittest.mock import AsyncMock
import pytest

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_spec = spec_from_file_location("mock_llm", Path(__file__).parents[1] / "scripts/loadtest/mock_llm.py")
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
app = _module.app


async def test_mock_streaming_and_embeddings_with_sdk(monkeypatch):
    monkeypatch.setenv('MOCK_TTFT_SECONDS','0')
    monkeypatch.setenv('MOCK_TOKEN_SECONDS','0')
    async with AsyncOpenAI(api_key='local',base_url='http://test/v1',
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app))) as client:
        messages = [{'role':'user','content':'查找寄到中国的旅行背包'}]
        stream = await client.chat.completions.create(model='mock-chat',messages=messages,
            tools=[{'type':'function','function':{'name':'product_search_tool','parameters':{'type':'object'}}}],stream=True)
        arguments = ''
        async for chunk in stream:
            if chunk.choices[0].delta.tool_calls:
                arguments += chunk.choices[0].delta.tool_calls[0].function.arguments or ''
        assert json.loads(arguments)['ship_to'] == 'CN'
        stream = await client.chat.completions.create(model='mock-chat',messages=messages,stream=True)
        text = ''
        async for chunk in stream:
            text += chunk.choices[0].delta.content or ''
        assert '商品卡' in text
        first = await client.embeddings.create(model='mock-embedding',input=['相同文本','不同文本'])
        again = await client.embeddings.create(model='mock-embedding',input='相同文本')
        assert len(first.data[0].embedding) == 1024
        assert first.data[0].embedding == again.data[0].embedding
        assert first.data[0].embedding != first.data[1].embedding


async def test_mock_default_text_length_timing_and_usage(monkeypatch):
    for name in ('MOCK_TTFT_SECONDS', 'MOCK_TOKEN_SECONDS', 'MOCK_RESPONSE_TOKENS'):
        monkeypatch.delenv(name, raising=False)
    sleep = AsyncMock()
    monkeypatch.setattr(_module.asyncio, 'sleep', sleep)
    async with AsyncOpenAI(api_key='local', base_url='http://test/v1',
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app))) as client:
        stream = await client.chat.completions.create(model='mock-chat',
            messages=[{'role':'user','content':'压测回复'}], stream=True, stream_options={'include_usage':True})
        chunks = [chunk async for chunk in stream]
    tokens = [chunk.choices[0].delta.content for chunk in chunks if chunk.choices and chunk.choices[0].delta.content]
    assert len(tokens) == 300 and all(len(token) == 1 for token in tokens)
    assert sleep.await_args_list[0].args == (0.8,)
    assert [call.args for call in sleep.await_args_list[1:]] == [(0.03,)] * 299
    assert chunks[-1].usage.completion_tokens == 300


@pytest.mark.parametrize('rate, prepares_confirmation', [(0, False), (1, True)])
async def test_mock_search_then_optional_confirmation_then_answer(monkeypatch, rate, prepares_confirmation):
    monkeypatch.setenv('MOCK_ORDER_RATE', str(rate))
    monkeypatch.setenv('MOCK_TTFT_SECONDS', '0')
    monkeypatch.setenv('MOCK_TOKEN_SECONDS', '0')
    monkeypatch.setenv('MOCK_RESPONSE_TOKENS', '80')
    tools = [{'type':'function','function':{'name':name,'parameters':{'type':'object'}}}
        for name in ['product_search_tool','create_order_tool']]
    messages = [{'role':'user','content':'压测会话：检索并准备确认'}]
    async with AsyncOpenAI(api_key='local', base_url='http://test/v1',
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app))) as client:
        search = (await client.chat.completions.create(model='mock-chat', messages=messages, tools=tools)).choices[0].message
        assert search.tool_calls[0].function.name == 'product_search_tool'
        messages += [search.model_dump(exclude_none=True), {'role':'tool','tool_call_id':search.tool_calls[0].id,
            'content':json.dumps({'hits':[{'product_id':'PTEST'}]})}]
        stream = await client.chat.completions.create(model='mock-chat', messages=messages, tools=tools, stream=True)
        chunks = [chunk async for chunk in stream]
        deltas = [chunk.choices[0].delta for chunk in chunks]
        if prepares_confirmation:
            calls = [delta.tool_calls[0] for delta in deltas if delta.tool_calls]
            args = json.loads(''.join(call.function.arguments or '' for call in calls))
            assert calls[0].function.name == 'create_order_tool'
            assert args['product_id'] == 'PTEST' and 'sku_id' not in args
            assert args['quantity'] == 1 and args['shipping_address']['country'] == 'CN'
            messages += [{'role':'assistant','tool_calls':[{'id':calls[0].id,'type':'function',
                'function':{'name':'create_order_tool','arguments':json.dumps(args)}}]},
                {'role':'tool','tool_call_id':calls[0].id,'content':'{"confirmation_required":true}'}]
            answer = (await client.chat.completions.create(model='mock-chat', messages=messages, tools=tools)).choices[0].message
            assert answer.tool_calls is None and len(answer.content) == 80
            assert '自行批准' in answer.content
        else:
            assert not any(delta.tool_calls for delta in deltas)
            assert len(''.join(delta.content or '' for delta in deltas)) == 80


async def test_mock_error_injection_is_visible_to_official_sdk(monkeypatch):
    from openai import RateLimitError
    monkeypatch.setenv('MOCK_ERROR_RATE', '1')
    async with AsyncOpenAI(api_key='local', base_url='http://test/v1', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app))) as client:
        with pytest.raises(RateLimitError) as error:
            await client.chat.completions.create(model='mock-chat', messages=[{'role':'user','content':'触发限流'}])
        assert error.value.response.headers['retry-after'] == '1'


@pytest.mark.parametrize('draw, prefix', [(0.099, 'call_order_path_'), (0.1, 'call_search_')])
async def test_mock_default_order_rate_boundary(monkeypatch, draw, prefix):
    from unittest.mock import Mock
    monkeypatch.delenv('MOCK_ORDER_RATE', raising=False)
    monkeypatch.setattr(_module.random, 'random', Mock(side_effect=[0.5, draw]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/chat/completions', json={
            'messages':[{'role':'user','content':'检索'}],
            'tools':[{'function':{'name':'product_search_tool'}}]})
    assert response.status_code == 200
    assert response.json()['choices'][0]['message']['tool_calls'][0]['id'].startswith(prefix)


async def test_mock_fault_control_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv('MOCK_FAULT_CONTROL', raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/admin/requests')).status_code == 404
        assert (await client.post('/admin/fault', json={'model':'primary', 'status':429, 'requests':-1})).status_code == 404


@pytest.mark.parametrize('status', [429, 503])
async def test_mock_finite_faults_affect_only_primary_and_can_be_restored(monkeypatch, status):
    from openai import APIStatusError
    monkeypatch.setenv('MOCK_FAULT_CONTROL', '1')
    monkeypatch.setenv('MOCK_ERROR_RATE', '0')
    monkeypatch.setattr(app.state, 'fault', None)
    monkeypatch.setattr(app.state, 'requests', {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as admin:
        response = await admin.post('/admin/fault', json={'model':'primary','status':status,'requests':2})
        assert response.status_code == 200
        async with AsyncOpenAI(api_key='local', base_url='http://test/v1', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app))) as client:
            for _ in range(2):
                with pytest.raises(APIStatusError) as error:
                    await client.chat.completions.create(model='primary', messages=[{'role':'user','content':'仅存在内存的输入'}])
                assert error.value.status_code == status
            healthy = await client.chat.completions.create(model='fallback', messages=[{'role':'user','content':'正常'}])
            assert healthy.choices[0].finish_reason == 'stop'
            healthy = await client.chat.completions.create(model='primary', messages=[{'role':'user','content':'恢复'}])
            assert healthy.choices[0].finish_reason == 'stop'
        audit = (await admin.get('/admin/requests')).json()
        assert audit['requests'] == {'primary':{str(status):2,'200':1}, 'fallback':{'200':1}}
        assert '仅存在内存的输入' not in json.dumps(audit, ensure_ascii=False)
        assert (await admin.post('/admin/fault', json={'model':'primary','status':status,'requests':0})).status_code == 200
        assert (await admin.get('/admin/requests')).json()['requests'] == {}


async def test_mock_fault_configuration_rejects_unrelated_status_codes(monkeypatch):
    monkeypatch.setenv('MOCK_FAULT_CONTROL', '1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/admin/fault', json={'model':'primary','status':200,'requests':-1})).status_code == 422
