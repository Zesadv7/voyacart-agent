"""历史批次+商品序号身份绑定：真实 SQLite、SDK 及 HTTP mock，完全离线。"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import (
    Msg, UserMsg, SystemMsg, TextBlock, ToolCallBlock, ToolCallState,
    ToolResultBlock, ToolResultState,
)
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit, ToolChoice

from app.application.tools.conversation_fact_lookup import (
    build_conversation_fact_lookup, lookup_batch_scope, lookup_ordinal_scope,
)
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import ContextAwareAgent, LayeredContextMiddleware
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.infrastructure.visible_evidence import historical_ordinal_identity, historical_ordinal_identity_request
from tests.test_prompt_cache import client_model, completion, streaming


@pytest.fixture(autouse=True)
def context():
    token = ShoppingContext.set(ShoppingContextSnapshot('session', 'buyer', 'zh-CN', 'CNY'))
    yield
    ShoppingContext.reset(token)


@pytest.mark.parametrize('text,batch,position', [
    ('第一批第二件的product_id是什么？', 1, 2),
    ('请问第24轮第3款商品的名称和商品编号是什么？', 24, 3),
    ('第 二十一 批第 十二 个产品的产品ID是什么？', 21, 12),
    ('告诉我第一百零二批第二十件的标题。', 102, 20),
    ('第两千零一批第两件的product_id？', 2001, 2),
    ('第一万批第1项产品的编号', 10000, 1),
    ('历史第一批中的第二件是什么？', 1, 2),
])
def test_parser_accepts_explicit_identity_only(text, batch, position):
    assert historical_ordinal_identity(text) == {'batch': batch, 'position': position, 'fields': 'identity'}


@pytest.mark.parametrize('text', [
    '第二件的product_id是什么？', '第一批的product_id是什么？', '第一批第二件',
    '第一批和第二批第二件的product_id？', '第一批第二件和第三件的product_id？',
    '第一批第二件的sku_id是什么？', '第一批第二件的product_id和库存？',
    '第一批第二件的当前价格？', '第一批第二件的product_id，然后下单。',
    '不要查第一批第二件的product_id。', '不是第一批第二件的product_id。',
    '无需调用工具，第一批第二件的product_id？', '第一批第二件的product_id，别回查。',
    '第一批第二件的product_id还是第三批的？', '第一批第二件的product_id，查最新。',
    '第一批第二件的product_id并发送给朋友', '第一批第二件的product_id与材质',
    '第一批第二件的product_id，按价格排序', '第一批第二件是不是P123？',
    '第0批第二件的product_id？', '第一批第0件的product_id？',
    '第10001批第二件的product_id？', '第999999999999999999999批第二件的product_id？',
    '第一百二批第二件的product_id？', '第十二三批第二件的product_id？',
    '第零十批第二件的product_id？', '第一批第两十件的product_id？',
    '第-1批第二件的product_id？', '第一批第1.5件的product_id？',
    '第1或2批第二件的product_id？', '第一批前两件的product_id？',
    '第一批第二件ID是什么，但不要调用工具。', '第一批第二件ID是什么，只直接答。',
])
def test_parser_does_not_guess_or_intercept_other_intents(text):
    assert historical_ordinal_identity(text) is None


async def evidence(tmp_path, buyer='buyer', session='session'):
    store = ContextEvidenceStore(tmp_path/'ordinal.db')
    hits = [{'product_id': f'P{8400+i}', 'title': f'独立商品{i}', 'price_major': 100+i,
             'skus': [{'sku_id': f'P{8400+i}-S1', 'spec': '规格', 'stock': i}]}
            for i in range(1, 4)]
    refs, sources, messages = [], [], [SystemMsg('system', '保留原始规则'), UserMsg(buyer, '查看商品')]
    for index, order in enumerate(((0, 1, 2), (0, 2, 1), (0, 2, 1)), 1):
        payload = {'hits': [hits[i] for i in order], 'observed_at': f'2026-09-01T00:00:0{index}Z'}
        source = await store.save(buyer, session, 'products', payload)
        sources.append(source)
        payload['result_ref'] = source
        refs.append(await store.save(buyer, session, 'display_batch', payload))
        messages.append(Msg(name='agent', role='assistant', content=[
            ToolCallBlock(id=f'search-{index}', name='product_search_tool', input='{}', state=ToolCallState.FINISHED),
            ToolResultBlock(id=f'search-{index}', name='product_search_tool', output=json.dumps(payload), state=ToolResultState.SUCCESS),
        ]))
    return store, refs, sources, messages


def make_agent(messages=None):
    return SimpleNamespace(name='agent', state=AgentState(context=messages or []))


async def run_scoped(store, handler, user=None):
    user = user or UserMsg('buyer', '第一批第二件的product_id是什么？')
    async def next_handler(**kwargs):
        await handler(kwargs)
        yield 'done'
    assert [value async for value in LayeredContextMiddleware(store).on_reply(
        make_agent(), {'inputs': user}, next_handler)] == ['done']


def payload(chunk):
    assert chunk.state == ToolResultState.SUCCESS
    return json.loads(chunk.content[0].text)


@pytest.mark.parametrize('mode', ['strict', 'bounded'])
async def test_omitted_selection_binds_original_display_order_and_identity(tmp_path, mode):
    store, refs, sources, _ = await evidence(tmp_path)
    lookup = build_conversation_fact_lookup(store, mode=mode)
    original = [await store.get('buyer', 'session', ref) for ref in refs]
    user = UserMsg('buyer', content=[TextBlock(text='第一批第二件的product_id是什么？'),
                                   TextBlock(text='服务端附件：第三批第三件')])
    before = user.model_dump_json()
    async def check(_):
        for arguments in ({}, {'batch': 1}, {'position': 2}, {'batch': 1, 'position': 2, 'fields': 'identity'},
                          {'result_ref': refs[0]}, {'result_ref': sources[0]},
                          {'product_id': 'P8402'}, {'fields': 'product_id'}, {'fields': 'title'},
                          {'query': '第一批第二件的product_id是什么？'}, {'query': '任意关键词不筛选批次'}):
            record = payload(await lookup(**arguments))['records'][0]
            assert record['data']['hits'] == [{'product_id': 'P8402', 'title': '独立商品2'}]
            scope = record['observation_scope']
            assert scope['effective_batch'] == 1 and scope['effective_position'] == 2
            assert scope['ordinal_bound_from_user'] is True
            assert scope['requested_position'] == (arguments.get('position') or None)
    await run_scoped(store, check, user)
    assert user.model_dump_json() == before
    assert lookup_ordinal_scope.get() is lookup_batch_scope.get() is None
    assert [await store.get('buyer', 'session', ref) for ref in refs] == original
    # 下一轮/普通显式参数仍按旧合同优先，无全局序号补丁。
    latest = payload(await lookup(batch=3, position=2, fields='identity'))['records'][0]
    assert latest['data']['hits'][0]['product_id'] == 'P8403'
    assert latest['observation_scope']['ordinal_bound_from_user'] is False


@pytest.mark.parametrize('arguments', [
    {'batch': 3}, {'position': 3}, {'batch': 3, 'position': 2}, {'position': -1},
    {'fields': 'price'}, {'result_ref': 'wrong-batch'},
    {'product_id': 'P8403'}, {'sku_id': 'P8403-S1'},
    {'offset': 1}, {'field_offset': -1}, {'offset': -1},
])
async def test_conflicting_explicit_selection_returns_error_without_wrong_facts(tmp_path, arguments):
    store, refs, _, _ = await evidence(tmp_path)
    if arguments.get('result_ref'):
        arguments = {'result_ref': refs[2]}
    lookup = build_conversation_fact_lookup(store)
    async def check(_):
        result = await lookup(**arguments)
        assert result.state == ToolResultState.ERROR
        assert 'P8403' not in result.content[0].text and '未返回历史事实' in result.content[0].text
    await run_scoped(store, check)


async def test_matching_source_ref_still_uses_display_order(tmp_path):
    store, _, sources, _ = await evidence(tmp_path)
    # 一个搜索来源可以按不同顺序展示；不能由原始搜索顺序推断该展示中的第2件。
    original = (await store.get('buyer', 'session', sources[0]))['data']
    await store.save('buyer', 'session', 'display_batch', {**original, 'result_ref': sources[0],
                                                        'hits': list(reversed(original['hits']))})
    lookup = build_conversation_fact_lookup(store)
    async def check(_):
        record = payload(await lookup(result_ref=sources[0]))['records'][0]
        assert record['data']['hits'][0]['product_id'] == 'P8403'
    await run_scoped(store, check, UserMsg('buyer', '第四批第一件的product_id是什么？'))


async def test_missing_or_out_of_range_selection_never_falls_back(tmp_path):
    store, _, _, _ = await evidence(tmp_path)
    lookup = build_conversation_fact_lookup(store)
    async def missing(_):
        assert payload(await lookup())['records'] == []
    await run_scoped(store, missing, UserMsg('buyer', '第九批第二件的product_id是什么？'))
    async def out_of_range(_):
        assert payload(await lookup())['records'][0]['data']['hits'] == []
    await run_scoped(store, out_of_range, UserMsg('buyer', '第一批第九件的product_id是什么？'))


async def test_scope_is_buyer_session_local_resets_on_exception_and_concurrent_replies(tmp_path):
    store, _, _, _ = await evidence(tmp_path)
    await evidence(tmp_path, 'other', 'session')
    await evidence(tmp_path, 'buyer', 'other-session')
    lookup = build_conversation_fact_lookup(store)
    async def check(_):
        for buyer, session in [('other', 'session'), ('buyer', 'other-session')]:
            token = ShoppingContext.set(ShoppingContextSnapshot(session, buyer, 'zh-CN', 'CNY'))
            try:
                row = payload(await lookup(position=2, fields='identity'))['records'][0]
                assert row['data']['hits'][0]['product_id'] == 'P8403'
                assert row['observation_scope']['ordinal_bound_from_user'] is False
            finally:
                ShoppingContext.reset(token)
        raise RuntimeError('模拟中断')
    with pytest.raises(RuntimeError, match='模拟中断'):
        await run_scoped(store, check)
    assert lookup_ordinal_scope.get() is lookup_batch_scope.get() is None
    ready = asyncio.Event()
    async def first(_):
        ready.set()
        await asyncio.sleep(0.01)
        assert payload(await lookup())['records'][0]['data']['hits'][0]['product_id'] == 'P8402'
    async def second(_):
        await ready.wait()
        assert payload(await lookup())['records'][0]['data']['hits'][0]['product_id'] == 'P8403'
    await asyncio.gather(run_scoped(store, first), run_scoped(store, second,
                         UserMsg('buyer', '第三批第二件的product_id是什么？')))
    assert lookup_ordinal_scope.get() is None


async def test_ordinary_explicit_lookup_and_new_nonbuyer_input_keep_old_semantics(tmp_path):
    store, refs, _, _ = await evidence(tmp_path)
    lookup = build_conversation_fact_lookup(store)
    async def ordinary(_):
        assert lookup_ordinal_scope.get() is None
        for args in ({'batch': 3}, {'result_ref': refs[2]}):
            assert payload(await lookup(position=2, fields='identity', **args))['records'][0]['data']['hits'][0]['product_id'] == 'P8403'
    await run_scoped(store, ordinary, UserMsg('buyer', '第一批商品历史价格是多少？'))
    await run_scoped(store, ordinary, UserMsg('untrusted', '第一批第二件的product_id是什么？'))


def tool_schema():
    return [{'type': 'function', 'function': {'name': 'conversation_fact_lookup',
        'parameters': {'type': 'object', 'properties': {key: {} for key in ('batch', 'position', 'fields')}}}}]


async def test_first_request_is_append_only_and_preserves_explicit_choices_and_approvals(tmp_path):
    store, _, _, messages = await evidence(tmp_path)
    user = UserMsg('buyer', '第一批第二件的product_id是什么？')
    messages.append(user)
    agent, tools = make_agent(messages), tool_schema()
    before = [m.model_dump_json() for m in messages]
    async def check(_):
        result = historical_ordinal_identity_request(agent, messages, tools)
        assert result['tool_choice'].mode == 'conversation_fact_lookup'
        assert all(a is b for a, b in zip(result['messages'], messages))
        assert len(result['messages']) == len(messages) + 1
        selection = json.loads(result['messages'][-1].get_text_content().split('\n')[-1])
        assert selection == {'batch': 1, 'position': 2, 'fields': 'identity', 'source_message_id': user.id}
        assert historical_ordinal_identity_request(agent, messages, []) is None
        assert historical_ordinal_identity_request(agent, messages, [{'function': {'name': 'conversation_fact_lookup'}}]) is None
        for choice in [ToolChoice(mode='none'), ToolChoice(mode='required'), ToolChoice(mode='other_tool'),
                       *[ToolChoice(mode='auto', tools=allowed) for allowed in ([], ['other'], ['conversation_fact_lookup'])]]:
            assert historical_ordinal_identity_request(agent, messages, tools, choice) is None
        pending = Msg(name='agent', role='assistant', content=[
            ToolCallBlock(id='approval', name='order_create_tool', input='{}', state=ToolCallState.ASKING)])
        agent.state.context.append(pending)
        assert historical_ordinal_identity_request(agent, messages, tools) is None
        agent.state.context.remove(pending)
        for state in (ToolResultState.SUCCESS, ToolResultState.ERROR):
            result_message = Msg(name='agent', role='assistant', content=[
                ToolResultBlock(id='read', name='conversation_fact_lookup', output='{}', state=state)])
            assert historical_ordinal_identity_request(agent, [*messages, result_message], tools) is None
        stale = [*messages, UserMsg('buyer', '谢谢')]
        assert historical_ordinal_identity_request(agent, stale, tools) is None
    await run_scoped(store, check, user)
    assert [m.model_dump_json() for m in messages] == before
    assert historical_ordinal_identity_request(agent, messages, tools) is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('layout', ['legacy_system', 'stable_prefix'])
@pytest.mark.parametrize('wrong_arguments', [False, True])
async def test_native_reply_forces_bound_lookup_once_then_releases(tmp_path, stream, layout, wrong_arguments):
    store, _, _, history = await evidence(tmp_path)
    original = [m.model_dump_json() for m in history]
    requests, evidence_results = [], []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        response = completion()
        if len(requests) == 1:
            assert body['stream'] is False
            assert body['tool_choice'] == {'type': 'function', 'function': {'name': 'conversation_fact_lookup'}}
            arguments = {'batch': 3, 'position': 2} if wrong_arguments else {}
            response['choices'][0]['message'] = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'ordinal-read', 'type': 'function', 'function': {'name': 'conversation_fact_lookup',
                 'arguments': json.dumps(arguments)}}]}
            response['choices'][0]['finish_reason'] = 'tool_calls'
        else:
            assert body.get('tool_choice') in (None, 'auto')
            tool_results = [m for m in body['messages'] if m['role'] == 'tool' and m.get('tool_call_id') == 'ordinal-read']
            assert len(tool_results) == 1
            evidence_results.append(tool_results[0]['content'])
        return streaming(response) if body.get('stream') else httpx.Response(200, json=response)
    model = await client_model(tmp_path, handler, stream=stream)
    try:
        middleware = LayeredContextMiddleware(store, prompt_layout=layout, summary=False)
        agent = ContextAwareAgent('agent', '只读身份查证', model,
            toolkit=Toolkit(tools=[FunctionTool(build_conversation_fact_lookup(store), is_read_only=True)]),
            middlewares=[middleware])
        agent.state = AgentState.model_validate_json(AgentState(context=history).model_dump_json())
        await agent.reply(UserMsg('buyer', '第一批第二件的product_id是什么？'))
        assert len(requests) == 2 and requests[0]['tools'] == requests[1]['tools']
        assert requests[1]['stream'] is stream and model.stream is stream
        tool_text = json.dumps(evidence_results[0], ensure_ascii=False)
        if wrong_arguments:
            assert '未返回历史事实' in tool_text and 'P8403' not in tool_text
        else:
            assert 'P8402' in tool_text and 'P8403' not in tool_text
        assert [m.model_dump_json() for m in agent.state.context[:len(history)]] == original
        assert not any(m.name == 'historical_ordinal_selection' for m in agent.state.context)
        assert lookup_ordinal_scope.get() is None
        await agent.reply(UserMsg('buyer', '谢谢'))
        assert len(requests) == 3 and requests[2].get('tool_choice') in (None, 'auto')
    finally:
        await model.client.close()


@pytest.mark.parametrize('stream', [False, True])
async def test_native_forced_lookup_plain_text_is_rejected_before_emission(tmp_path, stream):
    from app.infrastructure.model_protocol import ModelProtocolViolation
    from app.infrastructure.context_usage import context_usage_sink
    store, _, _, _ = await evidence(tmp_path)
    requests, emitted, usage = [], [], []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['stream'] is False
        assert body['tool_choice']['function']['name'] == 'conversation_fact_lookup'
        response = completion()
        response['choices'][0]['message']['content'] = '不应流出的错误身份P8403'
        return httpx.Response(200, json=response)
    model = await client_model(tmp_path, handler, stream=stream)
    usage_token = context_usage_sink.set(usage.append)
    try:
        agent = ContextAwareAgent('agent', '只读身份查证', model,
            toolkit=Toolkit(tools=[FunctionTool(build_conversation_fact_lookup(store), is_read_only=True)]),
            middlewares=[LayeredContextMiddleware(store)])
        with pytest.raises(ModelProtocolViolation) as error:
            async for event in agent.reply_stream(UserMsg('buyer', '第一批第二件的product_id是什么？')):
                emitted.append(event)
        assert error.value.code == 'required_tool_missing'
        assert len(requests) == 1 and model.stream is stream
        assert '不应流出的错误身份' not in str(emitted)
        assert '不应流出的错误身份' not in agent.state.model_dump_json()
        assert lookup_ordinal_scope.get() is None
        assert len(usage) == 1 and usage[0]['input_tokens'] == 1500
    finally:
        context_usage_sink.reset(usage_token)
        await model.client.close()


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('mode', ['auto', 'none'])
async def test_nonforced_lookup_keeps_text_responses_and_streaming(tmp_path, stream, mode):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return streaming(completion()) if body['stream'] else httpx.Response(200, json=completion())
    model = await client_model(tmp_path, handler, stream=stream)
    try:
        response = await model([UserMsg('buyer', '普通文本请求')], tools=tool_schema(), tool_choice=ToolChoice(mode=mode))
        if stream:
            async for response in response:
                pass
        assert response.content and len(requests) == 1
        assert requests[0]['stream'] is stream and model.stream is stream
        assert requests[0].get('tool_choice') in (mode, None)
    finally:
        await model.client.close()


def test_forced_lookup_suppresses_only_unverified_content_in_response_copy():
    from openai.types.chat import ChatCompletion
    from app.infrastructure.model_protocol import ResponseContract
    raw = completion()
    raw['choices'][0]['message'].update(content='未取证的错误身份', reasoning_content='保留原生推理字段', tool_calls=[
        {'id': 'ordinal-read', 'type': 'function', 'function': {'name': 'conversation_fact_lookup', 'arguments': '{}'}}])
    raw['choices'][0]['finish_reason'] = 'tool_calls'
    response = ChatCompletion.model_validate(raw)
    before = response.model_dump()
    for choice in ('auto', {'type': 'function', 'function': {'name': 'conversation_fact_lookup'}}):
        contract = ResponseContract({'tools': tool_schema(), 'tool_choice': choice}, lambda **kwargs: None)
        checked = contract.completion(response)
        assert response.model_dump() == before
        assert checked.usage == response.usage and checked.id == response.id
        assert checked.choices[0].message.tool_calls == response.choices[0].message.tool_calls
        assert checked.choices[0].message.reasoning_content == '保留原生推理字段'
        assert checked.choices[0].message.content == ('未取证的错误身份' if choice == 'auto' else None)


@pytest.mark.parametrize('stream', [False, True])
async def test_native_mixed_text_and_forced_lookup_emits_only_evidenced_followup(tmp_path, stream):
    store, _, _, _ = await evidence(tmp_path)
    requests, emitted = [], []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        response = completion()
        if len(requests) == 1:
            assert body['stream'] is False
            response['choices'][0]['message'] = {'role': 'assistant', 'content': '取证前错误身份P8403', 'tool_calls': [
                {'id': 'ordinal-read', 'type': 'function', 'function': {'name': 'conversation_fact_lookup', 'arguments': '{}'}}]}
            response['choices'][0]['finish_reason'] = 'tool_calls'
        else:
            assert body.get('tool_choice') in (None, 'auto')
            result = next(m['content'] for m in body['messages'] if m.get('tool_call_id') == 'ordinal-read')
            assert 'P8402' in json.dumps(result) and 'P8403' not in json.dumps(result)
            response['choices'][0]['message']['content'] = '已取证身份P8402'
        if not body['stream']:
            return httpx.Response(200, json=response)
        chunks = [
            {'id': 'text', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture', 'choices': [
                {'index': 0, 'delta': {'content': response['choices'][0]['message']['content']}, 'finish_reason': 'stop'}]},
            {'id': 'text', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture', 'choices': [], 'usage': response['usage']},
        ]
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
    model = await client_model(tmp_path, handler, stream=stream)
    try:
        agent = ContextAwareAgent('agent', '只读身份查证', model,
            toolkit=Toolkit(tools=[FunctionTool(build_conversation_fact_lookup(store), is_read_only=True)]),
            middlewares=[LayeredContextMiddleware(store)])
        async for event in agent.reply_stream(UserMsg('buyer', '第一批第二件的product_id是什么？')):
            emitted.append(event)
        assert len(requests) == 2 and model.stream is stream
        assert '取证前错误身份' not in str(emitted) and '已取证身份P8402' in str(emitted)
        assert '取证前错误身份' not in agent.state.model_dump_json()
        assert lookup_ordinal_scope.get() is None
    finally:
        await model.client.close()


@pytest.mark.parametrize('mode', ['strict', 'bounded'])
async def test_large_identity_field_can_continue_fragments_of_same_bound_product(tmp_path, mode):
    store = ContextEvidenceStore(tmp_path/'large-identity.db')
    target = {'product_id': 'P721', 'title': 'x' * 9000}
    await store.save('buyer', 'session', 'display_batch', {'hits': [
        {'product_id': 'P720', 'title': '其他商品'}, target, {'product_id': 'P722', 'title': '末件'}]})
    await store.save('buyer', 'session', 'display_batch', {'hits': [
        target, {'product_id': 'P722', 'title': '末件'}, {'product_id': 'P720', 'title': '其他商品'}]})
    lookup = build_conversation_fact_lookup(store, mode=mode)
    async def check(_):
        fragments, field_offset = [], 0
        while True:
            record = payload(await lookup(field_offset=field_offset))['records'][0]
            page = record['data']
            assert page['fragment_product_id'] == target['product_id']
            assert record['observation_scope']['effective_batch'] == 1
            assert record['observation_scope']['effective_position'] == 2
            fragments.append(page['fragment'])
            if page['next_field_offset'] is None:
                break
            assert page['next_field_offset'] > field_offset
            field_offset = page['next_field_offset']
        assert len(fragments) > 1 and json.loads(''.join(fragments)) == target
    await run_scoped(store, check, UserMsg('buyer', '第一批第二件的名称是什么？'))


@pytest.mark.parametrize('stream', [False, True])
async def test_identity_scope_does_not_override_native_explicit_none(tmp_path, stream):
    store, _, _, _ = await evidence(tmp_path)
    user = UserMsg('buyer', '第一批第二件的product_id是什么？')
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['tool_choice'] == 'none' and body['stream'] is stream
        assert 'historical_ordinal_selection' not in json.dumps(body['messages'])
        return streaming(completion()) if stream else httpx.Response(200, json=completion())
    model = await client_model(tmp_path, handler, stream=stream)
    try:
        agent = ContextAwareAgent('agent', '只读身份查证', model,
            toolkit=Toolkit(tools=[FunctionTool(build_conversation_fact_lookup(store), is_read_only=True)]),
            middlewares=[LayeredContextMiddleware(store)])
        async def check(_):
            response = await agent._call_model([user], tool_schema(), ToolChoice(mode='none'))
            if stream:
                async for response in response:
                    pass
            assert response.content
        await run_scoped(store, check, user)
        assert len(requests) == 1 and lookup_ordinal_scope.get() is None
    finally:
        await model.client.close()
