"""为指定 SKU 先准备 500 张确认单，再同时发起批准；目标使用隔离压测环境。"""
import argparse
import asyncio
from collections import Counter
import csv
import json
import os
from pathlib import Path
import time
import uuid

import httpx


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    buyer = 'hotspot-' + uuid.uuid4().hex
    token = os.getenv('VOYACART_API_TOKEN', '')
    headers = {'Authorization':'Bearer '+token} if token else {}
    prepare_limit = asyncio.Semaphore(20)
    confirmations, approvals = [], []
    async with httpx.AsyncClient(base_url=args.host.rstrip('/'), headers=headers,
        limits=httpx.Limits(max_connections=args.count, max_keepalive_connections=args.count),
        timeout=180, trust_env=False) as client:
        preparation_log = (args.output/'preparation.jsonl').open('x')
        async def prepare(i):
            async with prepare_limit:
                response = await client.post('/commerce/confirmations/orders', json={
                    'buyer_id':buyer, 'session_id':f'{buyer}-{i}',
                    'items':[{'product_id':args.product, 'sku_id':args.sku, 'quantity':1}],
                    'shipping_address':{'recipient_name':'压测收件人', 'country':'CN', 'state':'测试省',
                        'city':'测试市','address_line':'测试路1号','postal_code':'000000','phone':'00000000000'}})
                preparation_log.write(json.dumps({'index':i, 'status':response.status_code,
                    'body':response.text}, ensure_ascii=False)+'\n')
                preparation_log.flush()
                response.raise_for_status()
                row = {'index':i, 'status':response.status_code, 'body':response.json()}
                confirmations.append(row)
                return row
        preparing = [asyncio.create_task(prepare(i)) for i in range(args.count)]
        try:
            await asyncio.gather(*preparing)
        finally:
            for task in preparing:
                task.cancel()
            await asyncio.gather(*preparing, return_exceptions=True)
            preparation_log.close()
        (args.output/'confirmations.json').write_text(json.dumps(confirmations, ensure_ascii=False, indent=2)+'\n')
        if any(row['status'] != 200 for row in confirmations):
            raise RuntimeError('确认准备失败，未进入批准阶段；详见 confirmations.json')
        before = (await client.get('/commerce/orders', params={'buyer_id':buyer})).json()
        assert before['total'] == 0
        started = time.perf_counter()
        async def approve(row):
            confirmation = row['body']['confirmation']
            begin = time.perf_counter()
            try:
                response = await client.post('/commerce/confirmations/'+confirmation['confirmation_id']+'/resolve', json={
                    'buyer_id':buyer, 'session_id':confirmation['session_id'],
                    'snapshot_hash':confirmation['snapshot_hash'], 'approved':True})
                payload = response.json()
                code = payload.get('detail', {}).get('code', '') if response.status_code != 200 else ''
                approvals.append({'confirmation_id':confirmation['confirmation_id'], 'status':response.status_code,
                    'error_code':code, 'duration_ms':(time.perf_counter()-begin)*1000, 'body':payload})
            except httpx.HTTPError as err:
                approvals.append({'confirmation_id':confirmation['confirmation_id'], 'status':0,
                    'error_code':type(err).__name__, 'duration_ms':(time.perf_counter()-begin)*1000, 'body':str(err)})
        await asyncio.gather(*(approve(row) for row in confirmations))
        elapsed = time.perf_counter()-started
        orders = (await client.get('/commerce/orders', params={'buyer_id':buyer})).json()
    (args.output/'approvals.json').write_text(json.dumps(approvals, ensure_ascii=False, indent=2)+'\n')
    with (args.output/'approvals.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['confirmation_id','status','error_code','duration_ms'], extrasaction='ignore')
        writer.writeheader()
        writer.writerows(approvals)
    successes = sum(row['status'] == 200 for row in approvals)
    errors = dict(Counter(row['error_code'] for row in approvals if row['status'] != 200))
    report = {'buyer_id':buyer,'sku':args.sku,'initial_stock_expected':args.stock,'confirmations':len(confirmations),
        'approval_requests':len(approvals),'approved':successes,'orders':orders['total'],
        'orders_before_approval':before['total'],'errors':errors,'approve_seconds':elapsed,
        'expected_approved':min(args.count,args.stock)}
    (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert successes == orders['total'] == min(args.count,args.stock)
    assert set(errors) <= {'INSUFFICIENT_STOCK'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--product', required=True)
    parser.add_argument('--sku', required=True)
    parser.add_argument('--count', type=int, default=500)
    parser.add_argument('--stock', type=int, default=100)
    asyncio.run(run(parser.parse_args()))
