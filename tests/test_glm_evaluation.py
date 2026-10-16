"""GLM 参数适配仅用 MockTransport；不读取或使用外部模型凭据。"""
from dataclasses import replace
import json

import httpx
import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock, ToolResultState

from scripts.eval.harness.contracts import ROOT, load_suite
from scripts.eval.harness.model_config import (
    GLM_CONFIG, GLM_CODING_CONFIG, GLM_MODEL, GLM_BASE_URL, GLM_CODING_URL, ModelCallBudget, ModelCallLimitExceeded,
    configure_evaluation_model, model_call_budget, validate_model_config,
)
from tests.test_prompt_cache import client_model, completion, messages, settings


SUITE = ROOT/'eval/harness/v1/glm-5.3-flash-suite.json'
CODING_SUITE = ROOT/'eval/harness/v1/glm-5.3-flash-coding-plan-suite.json'


def runtime():
    return load_suite(SUITE)['runtime']


def test_glm_suite_is_explicit_and_uses_identical_frozen_cases():
    original, glm = load_suite(), load_suite(SUITE)
    assert original['_dataset_sha256'] == glm['_dataset_sha256']
    assert glm['runtime']['model_config'] == GLM_CONFIG
    assert set(glm['strategies']) == {'current', 'candidate'}
    for name, rows in [('smoke',12),('dev',60),('release',198)]:
        p=glm['profiles'][name]
        assert len(p['cases'])*p['repetitions']*2 == rows
        assert p['max_model_calls'] > rows
    assert original['runtime']['output_limit'] == 2048
    assert glm['pricing']['verified'] is False
    coding = load_suite(CODING_SUITE)
    assert coding['_dataset_sha256'] == glm['_dataset_sha256']
    assert coding['runtime']['model_config'] == GLM_CODING_CONFIG
    assert coding['pricing']['verified'] is False and coding['pricing']['currency'] is None
    assert 'reference_rates_per_million_tokens' not in coding['pricing']
    assert coding['profiles'] == glm['profiles'] and coding['gates'] == glm['gates']


def test_preflight_workflow_is_separate_and_has_no_implicit_model():
    import yaml
    workflow=yaml.safe_load((ROOT/'.github/workflows/harness-eval.yml').read_text())
    events=workflow.get('on',workflow.get(True))
    assert 'preflight' in events['workflow_dispatch']['inputs']['profile']['options']
    assert events['workflow_dispatch']['inputs']['profile']['default'] == 'config-check'
    assert events['workflow_dispatch']['inputs']['endpoint_mode']['options'] == ['standard','coding_plan']
    diagnostic=workflow['jobs']['configuration']
    assert 'needs' not in diagnostic
    step=next(s for s in diagnostic['steps'] if 'env' in s)
    assert 'LLM_API_KEY' not in step['env']
    assert step['env']['HARNESS_KEY_CONFIGURED'] == "${{ secrets.HARNESS_LLM_API_KEY != '' }}"
    assert 'scripts.eval.endpoint_config' in step['run']
    assert 'config-check' in workflow['jobs']['safety-contracts']['if']
    assert 'config-check' in workflow['jobs']['changes']['if']
    job=workflow['jobs']['model-evaluation']
    assert 'configuration' in job['needs'] and "inputs.profile != 'config-check'" in job['if']
    assert 'workflow_dispatch' in job['if'] and 'approve_model_budget' in job['if']
    assert job['env']['LLM_MODEL']=='${{ vars.HARNESS_LLM_MODEL }}'
    evaluation=next(s for s in job['steps'] if s.get('name')=='Real model evaluation')
    assert evaluation['if']=="inputs.profile != 'preflight'"
    for step in job['steps']:
        if 'suite_args' in step.get('run',''):
            assert 'case "$ENDPOINT_MODE"' in step['run']
            assert 'glm-5.3-flash-coding-plan-suite.json' in step['run']


@pytest.mark.parametrize('mutation',['model','url','thinking','cache','config','missing'])
def test_glm_configuration_fails_closed(tmp_path, mutation):
    s=replace(settings(tmp_path),llm_model=GLM_MODEL,llm_base_url=GLM_BASE_URL,prompt_cache_mode='passthrough')
    cfg=runtime()
    validate_model_config(s,cfg)
    if mutation=='model':s=replace(s,llm_model='other')
    elif mutation=='url':s=replace(s,llm_base_url='https://example.invalid/v1')
    elif mutation=='thinking':s=replace(s,llm_enable_thinking=False)
    elif mutation=='cache':s=replace(s,prompt_cache_mode='explicit')
    elif mutation=='config':cfg['model_config']['reasoning_effort']='low'
    else:cfg.pop('model_config')
    with pytest.raises(ValueError):validate_model_config(s,cfg)


@pytest.mark.asyncio
async def test_glm_wire_parameters_reasoning_roundtrip_and_limit(tmp_path):
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content))
        raw=completion();raw['model']=GLM_MODEL
        return httpx.Response(200,json=raw)
    model=await client_model(tmp_path,handler,llm_model=GLM_MODEL)
    configure_evaluation_model(model,runtime())
    budget=ModelCallBudget(1);token=model_call_budget.set(budget)
    thinking='synthetic-reasoning-marker'
    history=messages() + [Msg(name='assistant',role='assistant',content=[
        ThinkingBlock(thinking=thinking),ToolCallBlock(id='probe',name='echo_probe',input='{}')]),
        Msg(name='tool',role='assistant',content=[ToolResultBlock(id='probe',name='echo_probe',output='hello',state=ToolResultState.SUCCESS)])]
    try:
        await model(history,max_completion_tokens=512)
        body=seen[0]
        assert body['model']==GLM_MODEL and body['max_tokens']==512
        assert 'max_completion_tokens' not in body
        assert body['temperature']==1 and body['top_p']==.95
        assert body['thinking']=={'type':'enabled','clear_thinking':False}
        assert body['reasoning_effort']=='max' and 'enable_thinking' not in body
        assistant=next(m for m in body['messages'] if m.get('tool_calls'))
        assert assistant['reasoning_content']==thinking
        assert body['messages'][-1]['tool_call_id']=='probe'
        with pytest.raises(ModelCallLimitExceeded):await model(messages())
        assert len(seen)==1 and budget.started==1 and budget.exhausted
    finally:
        model_call_budget.reset(token);await model.client.close()


@pytest.mark.asyncio
async def test_other_providers_keep_existing_wire_defaults(tmp_path):
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content));return httpx.Response(200,json=completion())
    model=await client_model(tmp_path,handler)
    configure_evaluation_model(model,load_suite()['runtime'])
    try:
        await model(messages())
        body=seen[0]
        assert body['temperature']==0 and body['max_completion_tokens']==2048
        assert 'thinking' not in body and 'reasoning_effort' not in body
    finally:await model.client.close()


def response_for(body, include_usage=True):
    raw=completion();raw['model']=GLM_MODEL
    forced=isinstance(body.get('tool_choice'),dict)
    msg={'role':'assistant','content':'hello','reasoning_content':'synthetic-roundtrip-reasoning'}
    reason='stop'
    if forced:
        msg.update(content=None,tool_calls=[{'id':'probe','type':'function',
            'function':{'name':'echo_probe','arguments':'{"text":"hello"}'}}]);reason='tool_calls'
    raw['choices']=[{'index':0,'message':msg,'finish_reason':reason}]
    if not body['stream']:
        if not include_usage:raw.pop('usage')
        return httpx.Response(200,json=raw)
    delta={k:v for k,v in msg.items() if k!='role'}
    if forced:delta['tool_calls'][0]['index']=0
    chunks=[{'id':'probe','object':'chat.completion.chunk','created':1,'model':GLM_MODEL,
        'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
        {'id':'probe','object':'chat.completion.chunk','created':1,'model':GLM_MODEL,
        'choices':[{'index':0,'delta':{},'finish_reason':reason}]}]
    if include_usage:chunks.append({'id':'probe','object':'chat.completion.chunk','created':1,'model':GLM_MODEL,
        'choices':[],'usage':raw['usage']})
    return httpx.Response(200,headers={'content-type':'text/event-stream'},
        content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')


@pytest.mark.asyncio
@pytest.mark.parametrize('suite,base_url',[(SUITE,GLM_BASE_URL),(CODING_SUITE,GLM_CODING_URL)])
@pytest.mark.parametrize('include_usage',[True,False])
async def test_preflight_uses_eight_bounded_requests_and_retains_reasoning(tmp_path, monkeypatch,suite,base_url,include_usage):
    from scripts import model_preflight
    from app.infrastructure.llm import create_chat_model
    from app.infrastructure.throttle import GatewayThrottle
    s=replace(settings(tmp_path),llm_model=GLM_MODEL,llm_base_url=base_url,prompt_cache_mode='passthrough')
    seen=[]
    def handler(request):
        assert str(request.url).startswith(base_url+'/')
        body=json.loads(request.content);seen.append(body);return response_for(body,include_usage)
    def factory(config,*,stream,throttle):
        model=create_chat_model(config,stream=stream,throttle=GatewayThrottle(1,0))
        model.client._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return model
    monkeypatch.setattr(model_preflight,'load_settings',lambda:s)
    monkeypatch.setattr(model_preflight,'create_chat_model',factory)
    out=tmp_path/'preflight'
    assert await model_preflight.run(out,suite)==0
    summary=json.loads((out/'summary.json').read_text())
    assert summary['physical_calls_started']==8 and summary['tools_executed']==0
    assert summary['usage_known'] is include_usage and summary['billing_verified'] is False
    roundtrips=[b for b in seen if any(m['role']=='tool' for m in b['messages'])]
    assert len(roundtrips)==2
    for body in roundtrips:
        assistant=next(m for m in body['messages'] if m.get('tool_calls'))
        assert assistant['reasoning_content']=='synthetic-roundtrip-reasoning'
    assert all(b['tool_stream'] for b in seen if b['stream'])
    assert 'synthetic-roundtrip-reasoning' not in (out/'report.html').read_text()


@pytest.mark.parametrize('mode,url',[('standard',GLM_BASE_URL),('coding_plan',GLM_CODING_URL)])
def test_config_check_is_offline_and_redacted(tmp_path,monkeypatch,capsys,mode,url):
    import socket
    from scripts.eval.endpoint_config import main
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('diagnostic attempted network'))
    env={'LLM_BASE_URL':url,'LLM_MODEL':GLM_MODEL,'HARNESS_KEY_CONFIGURED':'true',
         'LLM_API_KEY':'synthetic-key-canary','GITHUB_STEP_SUMMARY':str(tmp_path/'summary')}
    assert main(['--endpoint-mode',mode],env)==0
    report=json.loads(capsys.readouterr().out)
    assert report['endpoint_class']==mode and report['configuration_matches'] and report['model_calls']==0
    assert 'synthetic-key-canary' not in (tmp_path/'summary').read_text()
    assert url not in (tmp_path/'summary').read_text()


@pytest.mark.parametrize('url',[
    'https://user:synthetic-password@open.bigmodel.cn/api/paas/v4',
    GLM_BASE_URL+'?token=synthetic-query',GLM_BASE_URL+'#synthetic-fragment',
    'https://open.bigmodel.cn.evil.invalid/api/paas/v4','\n'+GLM_BASE_URL,
])
def test_config_check_unknown_urls_never_echo_input(url):
    from scripts.eval.endpoint_config import inspect_configuration
    env={'LLM_BASE_URL':url,'LLM_MODEL':'synthetic-model-canary','HARNESS_KEY_CONFIGURED':'false'}
    report=inspect_configuration(env)
    rendered=json.dumps(report)
    assert report['endpoint_class']=='unknown' and not report['configuration_matches']
    assert url not in rendered and 'synthetic' not in rendered


def test_endpoint_selection_is_explicit_without_fallback(tmp_path):
    from scripts.eval.endpoint_config import inspect_configuration
    env={'LLM_BASE_URL':GLM_CODING_URL,'LLM_MODEL':GLM_MODEL,'HARNESS_KEY_CONFIGURED':'true'}
    assert not inspect_configuration(env,'standard','preflight')['configuration_matches']
    assert inspect_configuration(env,'coding_plan','preflight')['configuration_matches']
    s=replace(settings(tmp_path),llm_model=GLM_MODEL,llm_base_url=GLM_CODING_URL,prompt_cache_mode='passthrough')
    with pytest.raises(ValueError,match='期望=standard，实际=coding_plan'):validate_model_config(s,runtime())
    validate_model_config(s,load_suite(CODING_SUITE)['runtime'])
    with pytest.raises(ValueError):validate_model_config(replace(s,llm_model='other'),load_suite()['runtime'])
    assert s.llm_base_url == GLM_CODING_URL


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[401,403,429])
async def test_coding_preflight_stops_on_provider_rejection(tmp_path,monkeypatch,status):
    from scripts import model_preflight
    from app.infrastructure.llm import create_chat_model
    from app.infrastructure.throttle import GatewayThrottle
    s=replace(settings(tmp_path),llm_model=GLM_MODEL,llm_base_url=GLM_CODING_URL,prompt_cache_mode='passthrough')
    seen=[]
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status,json={'error':{'code':'synthetic-code-canary','message':'synthetic-secret-canary'}})
    def factory(config,*,stream,throttle):
        model=create_chat_model(config,stream=stream,throttle=GatewayThrottle(1,0))
        model.client._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return model
    monkeypatch.setattr(model_preflight,'load_settings',lambda:s)
    monkeypatch.setattr(model_preflight,'create_chat_model',factory)
    out=tmp_path/'rejected'
    assert await model_preflight.run(out,CODING_SUITE)==2
    summary=json.loads((out/'summary.json').read_text())
    assert summary['physical_calls_started']==1 and len(seen)==1
    assert seen[0].startswith(GLM_CODING_URL+'/')
    assert 'synthetic-secret-canary' not in (out/'report.html').read_text()
    assert 'synthetic-code-canary' not in (out/'report.html').read_text()


@pytest.mark.asyncio
async def test_runner_stops_after_provider_rejection(tmp_path,monkeypatch):
    from scripts.eval.harness import runner
    from scripts.eval.harness.model_config import ProviderRejected
    s=replace(settings(tmp_path),llm_model=GLM_MODEL,llm_base_url=GLM_CODING_URL,prompt_cache_mode='passthrough')
    monkeypatch.setattr('app.infrastructure.settings.load_settings',lambda **kwargs:s)
    async def denied(*args):
        budget=model_call_budget.get();budget.reserve();budget.provider_rejected=True
        with pytest.raises(ProviderRejected):budget.reserve()
        raise ProviderRejected('stopped')
    monkeypatch.setattr(runner,'execute_case',denied)
    out=tmp_path/'denied'
    await runner.run_profile('smoke',out,suite_path=CODING_SUITE,skip_contracts=True)
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['status']=='failed' and manifest['error']=='provider_rejected'
    assert manifest['physical_calls_started']==1
    assert len(json.loads((out/'results.json').read_text()))==1


@pytest.mark.asyncio
async def test_runner_stops_at_physical_limit_and_keeps_partial_evidence(tmp_path,monkeypatch):
    from scripts.eval.harness import runner
    from app.infrastructure.settings import load_settings
    s=replace(settings(tmp_path),llm_model=GLM_MODEL,llm_base_url=GLM_BASE_URL,prompt_cache_mode='passthrough')
    monkeypatch.setattr('app.infrastructure.settings.load_settings',lambda **kwargs:s)
    suite=load_suite(SUITE);suite['profiles']['smoke']['max_model_calls']=1
    monkeypatch.setattr(runner,'load_suite',lambda *args:suite)
    async def limited(*args):
        budget=model_call_budget.get();budget.reserve();budget.reserve()
    monkeypatch.setattr(runner,'execute_case',limited)
    out=tmp_path/'limit'
    await runner.run_profile('smoke',out,suite_path=SUITE,skip_contracts=True)
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['status']=='failed' and manifest['error']=='model_call_limit_exceeded'
    assert manifest['physical_calls_started']==1
    assert len(json.loads((out/'results.json').read_text()))==1
    assert model_call_budget.get() is None
