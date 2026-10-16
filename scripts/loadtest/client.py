"""AG-UI 压测客户端：完整消费 SSE 并校验按游标续传。"""
import json
import time

import requests

FIELDS = ['run_id', 'started_at', 'finished_at', 'first_event_ms', 'first_token_ms',
          'complete_ms', 'status_code', 'error_code', 'events', 'reconnected']


def sse_events(lines, *, deadline=None):
    """按空行分帧；心跳注释不占业务事件序号。"""
    event_id, kind, data = '', '', []
    for line in lines:
        if deadline is not None and time.perf_counter() >= deadline:
            raise requests.Timeout('run_deadline_exceeded')
        if line == '':
            if data:
                yield event_id, kind, json.loads('\n'.join(data))
            event_id, kind, data = '', '', []
        elif line.startswith('id:'):
            event_id = line[3:].lstrip()
        elif line.startswith('event:'):
            kind = line[6:].lstrip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
    if data:
        raise ValueError('incomplete_sse_frame')


def consume_run(session, host, buyer, run_id, *, reconnect=False, timeout=180, row=None):
    started = time.perf_counter()
    deadline = started + timeout
    if row is None:
        row = {}
    row.update(dict.fromkeys(FIELDS, ''))
    row.update(run_id=run_id, started_at=time.time(), events=0, reconnected=False)
    body = {'threadId':run_id, 'runId':run_id,
        'messages':[{'id':run_id+'-user','role':'user','content':'预算300元以内，找一个寄到中国的轻便背包'}],
        'state':{}, 'tools':[], 'context':[],
        'forwardedProps':{'buyerId':buyer,'locale':'zh-CN','currency':'CNY'}}
    method, url, kwargs = 'POST', host + '/commerce/ag-ui/run', {'json':body}
    last_seq, terminal = 0, False
    try:
        while not terminal:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise requests.Timeout('run_deadline_exceeded')
            with session.request(method, url, stream=True, timeout=remaining, **kwargs) as response:
                row['status_code'] = response.status_code
                response.raise_for_status()
                response.encoding = 'utf-8'
                for event_id, event_kind, event in sse_events(
                    response.iter_lines(chunk_size=1, decode_unicode=True), deadline=deadline,
                ):
                    elapsed = (time.perf_counter() - started) * 1000
                    if event_kind == 'transport-error':
                        raise ValueError('transport_error:' + event.get('code', 'unknown'))
                    prefix, sequence = event_id.rsplit(':', 1)
                    if prefix != run_id or int(sequence) != last_seq + 1:
                        raise ValueError('event_sequence_gap_or_duplicate')
                    last_seq += 1
                    row['events'] = last_seq
                    if row['first_event_ms'] == '':
                        row['first_event_ms'] = elapsed
                    if event['type'] == 'TEXT_MESSAGE_CONTENT' and row['first_token_ms'] == '':
                        row['first_token_ms'] = elapsed
                    if event['type'] == 'RUN_ERROR':
                        row['error_code'] = event.get('code') or 'RUN_ERROR'
                        terminal = True
                        break
                    if event['type'] == 'RUN_FINISHED':
                        terminal = True
                        break
                    if reconnect and not row['reconnected'] and row['first_token_ms'] != '':
                        row['reconnected'] = True
                        method, url = 'GET', host + '/commerce/ag-ui/runs/' + run_id + '/events'
                        kwargs = {'params':{'buyer_id':buyer}, 'headers':{'Last-Event-ID':event_id}}
                        break
                else:
                    raise ValueError('stream_ended_before_terminal')
    except (requests.RequestException, ValueError) as err:
        row['error_code'] = (f'http_{row["status_code"]}' if isinstance(err, requests.HTTPError)
                             else f'{type(err).__name__}:{err}')
    except BaseException as err:
        row['error_code'] = f'{type(err).__name__}:{err}'
        raise
    finally:
        row.update(complete_ms=(time.perf_counter() - started) * 1000, finished_at=time.time())
    return row
