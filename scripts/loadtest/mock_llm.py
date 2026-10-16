"""压测假模型：检索、可选下单确认、定长流式回复及确定性嵌入。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI()
app.state.fault = None
app.state.requests = {}


class Fault(BaseModel):
    model: str = Field(min_length=1)
    status: int
    requests: int = Field(ge=-1)


@app.post('/admin/fault')
async def inject_fault(body: Fault):
    if os.getenv('MOCK_FAULT_CONTROL', '0') != '1':
        raise HTTPException(404)
    if body.status not in {429, 500, 502, 503}:
        raise HTTPException(422, '故障状态需为 429、500、502 或 503')
    app.state.fault = body.model_dump()
    app.state.requests = {}
    return app.state.fault


@app.get('/admin/requests')
async def request_counts():
    if os.getenv('MOCK_FAULT_CONTROL', '0') != '1':
        raise HTTPException(404)
    return {'fault': app.state.fault, 'requests': app.state.requests}


@app.get('/health')
async def health():
    return {'status': 'ok', 'mode': 'mock'}


@app.post('/v1/embeddings')
async def embeddings(body: dict):
    values = body.get('input')
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or any(not isinstance(t, str) for t in values):
        raise HTTPException(422, 'input 需要文本或文本列表')
    dimension = int(body.get('dimensions', 1024))
    if dimension < 1 or dimension > 4096:
        raise HTTPException(422, 'dimensions 需在 1–4096 之间')
    data = []
    for index, value in enumerate(values):
        rng = random.Random(hashlib.blake2b(value.encode()).digest())
        vector = [rng.uniform(-1, 1) for _ in range(dimension)]
        norm = math.sqrt(sum(v*v for v in vector))
        data.append({'object': 'embedding', 'index': index, 'embedding': [v/norm for v in vector]})
    return {'object': 'list', 'model': body.get('model','mock-embedding'), 'data': data,
            'usage': {'prompt_tokens': sum(len(v) for v in values), 'total_tokens': sum(len(v) for v in values)}}


@app.post('/v1/chat/completions')
async def chat(body: dict):
    model = body.get('model', 'mock-chat')
    fault = app.state.fault if os.getenv('MOCK_FAULT_CONTROL', '0') == '1' else None
    status = 200
    if fault and fault['model'] == model and fault['requests'] != 0:
        status = fault['status']
        if fault['requests'] > 0:
            fault['requests'] -= 1
    elif random.random() < float(os.getenv('MOCK_ERROR_RATE', '0')):
        status = 429
    counts = app.state.requests.setdefault(model, {})
    counts[str(status)] = counts.get(str(status), 0) + 1
    if status != 200:
        raise HTTPException(status, '注入的模型故障', headers={'Retry-After':'1'} if status == 429 else None)
    messages = body.get('messages', [])
    if not messages:
        raise HTTPException(422, 'messages 不能为空')
    tools = {tool['function']['name'] for tool in body.get('tools', [])}
    last_user = max((i for i, item in enumerate(messages) if item['role']=='user'), default=-1)
    results = [item for item in messages[last_user+1:] if item['role']=='tool']
    name, args = None, None
    if 'product_search_tool' in tools and not results:
        name = 'product_search_tool'
        args = {'normalized_query':'旅行 背包', 'category':'旅行装备', 'ship_to':'CN','target_currency':'CNY','top_k':3,'price_max_major':300}
    elif ('create_order_tool' in tools and len(results) == 1
          and results[0].get('tool_call_id', '').startswith('call_order_path_')):
        try:
            found = json.loads(results[0]['content'])
        except json.JSONDecodeError as err:
            raise HTTPException(422, '检索工具结果需要 JSON 对象') from err
        if found.get('hits'):
            product = found['hits'][0]
            name = 'create_order_tool'
            args = {'product_id':product['product_id'], 'quantity':1,
                'shipping_address':{'recipient_name':'压测收件人','country':'CN','state':'测试省',
                    'city':'测试市','address_line':'测试路1号','postal_code':'000000','phone':'00000000000'}}
    text = ('已检索旅行商品。请查看商品卡中的规格、库存与到手价，选择商品后可继续比较或准备订单确认。'
            '不同站点保留原币价格，预算筛选使用目标币种。请核对配送目的地、材质和数量。'
            '确认卡准备后，订单仍需用户点击批准才能创建。此回复用于检验流式传输、页面恢复和并发执行。')
    if results and any(item.get('tool_call_id', '').startswith('call_confirmation_') for item in results):
        text = '本轮已执行下单确认准备，请以确认卡显示的状态为准，核对规格、数量、地址与金额后自行批准。' + text
    response_tokens = int(os.getenv('MOCK_RESPONSE_TOKENS', '300'))
    if response_tokens < 1:
        raise HTTPException(422, 'MOCK_RESPONSE_TOKENS 需为正整数')
    # 压测以一个 Unicode 字符作为一个模拟 token，明确控制帧数与延迟。
    text = (text * ((response_tokens + len(text) - 1) // len(text)))[:response_tokens]
    if name:
        prefix = 'call_confirmation_' if name == 'create_order_tool' else 'call_search_'
        if name == 'product_search_tool' and random.random() < float(os.getenv('MOCK_ORDER_RATE', '0.1')):
            prefix = 'call_order_path_'
        call = {'id':prefix+uuid.uuid4().hex,'type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}
        message = {'role':'assistant','content':None,'tool_calls':[call]}
        reason = 'tool_calls'
    else:
        message = {'role':'assistant','content':text}
        reason = 'stop'
    base = {'id':'chatcmpl-'+uuid.uuid4().hex,'created':int(time.time()),'model':body.get('model','mock-chat')}
    completion_tokens = len(call['function']['arguments']) if name else response_tokens
    usage = {'prompt_tokens':100,'completion_tokens':completion_tokens,'total_tokens':100+completion_tokens}
    if not body.get('stream'):
        return {**base,'object':'chat.completion','choices':[{'index':0,'message':message,'finish_reason':reason}],'usage':usage}

    async def stream():
        def frame(delta, finish=None):
            return 'data: '+json.dumps({**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':finish}]},ensure_ascii=False)+'\n\n'
        await asyncio.sleep(float(os.getenv('MOCK_TTFT_SECONDS','0.8')))
        yield frame({'role':'assistant'})
        if name:
            yield frame({'tool_calls':[{'index':0,**call,'function':{'name':name,'arguments':''}}]})
            for start in range(0,len(call['function']['arguments']),12):
                await asyncio.sleep(float(os.getenv('MOCK_TOKEN_SECONDS','0.03')))
                yield frame({'tool_calls':[{'index':0,'function':{'arguments':call['function']['arguments'][start:start+12]}}]})
        else:
            for index, char in enumerate(text):
                if index:
                    await asyncio.sleep(float(os.getenv('MOCK_TOKEN_SECONDS','0.03')))
                yield frame({'content':char})
        yield frame({},reason)
        if body.get('stream_options',{}).get('include_usage'):
            yield 'data: '+json.dumps({**base,'object':'chat.completion.chunk','choices':[],'usage':usage})+'\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(stream(), media_type='text/event-stream')
