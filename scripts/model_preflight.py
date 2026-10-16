"""python -m scripts.model_preflight --output <新目录>：模型身份/工具协议预检，不执行工具。"""
import argparse
import asyncio
from dataclasses import replace
from html import escape
import json
from pathlib import Path
from agentscope.message import SystemMsg, UserMsg, Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.tool import ToolChoice
from app.infrastructure.context_usage import context_usage_sink
from app.infrastructure.llm import create_chat_model
from app.infrastructure.settings import load_settings
from app.infrastructure.throttle import GatewayThrottle
from scripts.eval.harness.contracts import assert_output_path, write_json, source_manifest, load_suite
from scripts.eval.harness.model_config import (
    configure_evaluation_model, validate_model_config, ModelCallBudget, model_call_budget,
)


async def run(output, suite_path=None):
    output=assert_output_path(output);output.mkdir(parents=True)
    settings=replace(load_settings(),llm_fallback_model='',llm_max_retries=0,prompt_cache_mode='passthrough')
    runtime = load_suite(suite_path)['runtime'] if suite_path else {
        'output_limit':256, 'request_timeout_seconds':45}
    validate_model_config(settings, runtime)
    budget = ModelCallBudget(8)
    rows=[];source=source_manifest()['sha256']
    tools=[{'type':'function','function':{'name':'echo_probe','description':'返回传入文字，仅用于协议测试。',
        'parameters':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}}]
    for streamed in (False,True):
        model=create_chat_model(settings,stream=streamed,throttle=GatewayThrottle(1,2))
        configure_evaluation_model(model, runtime)
        budget_token = model_call_budget.set(budget)
        try:
            for mode in ('none','auto','echo_probe'):
                samples=[];token=context_usage_sink.set(samples.append)
                row={'stream':streamed,'tool_choice':mode,'tools_executed':0,'error':None}
                try:
                    messages=[SystemMsg('system','这是协议检查，不执行任何业务动作。'),
                        UserMsg('probe','请调用echo_probe，text为hello。')]
                    result=await model(messages,tools=tools,tool_choice=ToolChoice(mode=mode))
                    if streamed:
                        async for part in result:
                            final = part
                        result = final
                    if mode == 'echo_probe' and runtime.get('model_config'):
                        calls=[b for b in result.content if isinstance(b,ToolCallBlock)]
                        if len(calls) != 1 or calls[0].name != 'echo_probe':
                            raise ValueError('预检未返回唯一的指定工具调用')
                        # 仅提供固定合成工具结果，不执行函数；保留本轮ThinkingBlock。
                        messages.extend([Msg(name='assistant',role='assistant',content=result.content),
                            Msg(name='probe',role='assistant',content=[ToolResultBlock(
                                id=calls[0].id,name=calls[0].name,output='hello',state=ToolResultState.SUCCESS)]),
                            UserMsg('probe','按刚才工具结果回答hello，不再调用工具。')])
                        followup=await model(messages,tools=[],tool_choice=ToolChoice(mode='none'))
                        if streamed:
                            async for part in followup:
                                final=part
                            followup=final
                        text=''.join(b.text for b in followup.content if isinstance(b,TextBlock))
                        if 'hello' not in text.lower():
                            raise ValueError('预检工具结果续答不匹配')
                        row['tool_roundtrip']=True
                except Exception as error:
                    row['error']=getattr(error,'code',type(error).__name__)
                finally:context_usage_sink.reset(token)
                row['usage']=samples
                row['passed']=bool(samples) and not row['error'] and all(
                    s.get('prompt_cache',{}).get('response_model_matches') is True and
                    s.get('prompt_cache',{}).get('protocol_status')=='valid' and
                    type(s.get('input_tokens')) is int and type(s.get('output_tokens')) is int for s in samples)
                rows.append(row);write_json(output/'results.json',rows)
                if row['error']:
                    break
        finally:
            model_call_budget.reset(budget_token)
            await model.client.close()
        if rows[-1]['error']:
            break
    report={'requested_model':settings.llm_model,'passed':len(rows)==6 and all(r['passed'] for r in rows),
        'source_sha256':source,'source_stable':source_manifest()['sha256']==source,'calls':len(rows),
        'physical_calls_started':budget.started,'physical_call_limit':budget.limit,'runtime':runtime,
        'tools_executed':0,'scope':'服务预检；不等于业务质量、BP或真实费用收益验收。'}
    write_json(output/'summary.json',report)
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>模型服务预检</title>'
        '<style>body{font:16px/1.7 system-ui;max-width:1000px;margin:40px auto}pre{white-space:pre-wrap;background:#f3f5f2;padding:20px}</style>'
        '<h1>模型服务身份与工具协议预检</h1><p>无工具执行；失败原样保留。未知或标识不一致禁止宣称同模型收益。</p><pre>'
        +escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre>'+''.join('<details><summary>'+escape(str(r['stream'])+' / '+r['tool_choice'])+'</summary><pre>'+escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre></details>' for r in rows))
    return 0 if report['passed'] and report['source_stable'] else 2


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--suite',type=Path,help='显式冻结模型参数合同；GLM必须指定专用suite')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output,args.suite)))
