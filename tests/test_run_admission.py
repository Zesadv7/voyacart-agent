"""M5准入和持久终态：临时SQLite与可控协程，不访问外部模型。"""
import asyncio
from types import SimpleNamespace
import httpx
import pytest
from ag_ui.core import RunAgentInput
from app.application.agent_runtime import AGUIRuntime
from app.application.run_admission import RunAdmission
from app.domain.queue.ports.task_queue import QueueCapacityExceeded
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalNotFound, JournalConflict
from app.presentation.ag_ui import parse_intent
from tests.test_ag_ui_journal import body, ControlledOrchestrator, application, wait_status
from tests.test_candidate_run_binding import binding, catalog_rows


def request(run='r1', session='s1', **kw):
    value=RunAgentInput.model_validate(body(run,session,**kw))
    return value,parse_intent(value)

async def until(check):
    async with asyncio.timeout(3):
        while not check(): await asyncio.sleep(.005)


def test_gate_is_nonwaiting_and_release_is_idempotent():
    gate=RunAdmission(2); a,b=gate.acquire(),gate.acquire()
    with pytest.raises(QueueCapacityExceeded):gate.acquire()
    a.release();a.release();c=gate.acquire()
    assert gate.snapshot()=={'scope':'process_local_runs','limit':2,'active':2,'peak':2,'accepted':3,'rejected':1}
    b.release();c.release();assert gate.active==0

@pytest.mark.parametrize('limit',[0,-1,True,1.5])
def test_invalid_gate(limit):
    with pytest.raises(ValueError):RunAdmission(limit)

async def test_cap_rejects_without_phantom_and_replay_bypasses_full(tmp_path):
    journal=AGUIJournal(tmp_path/'runs.db');agent=ControlledOrchestrator(True)
    runtime=AGUIRuntime(journal,agent,max_active_runs=2);await runtime.startup()
    try:
        await runtime.start(*request());await runtime.start(*request('r2','s2'))
        await until(lambda:agent.calls==2)
        assert (await runtime.start(*request()))['runId']=='r1'
        with pytest.raises(JournalConflict):await runtime.start(*request(query='不同内容'))
        with pytest.raises(QueueCapacityExceeded):await runtime.start(*request('r3','s3'))
        with pytest.raises(JournalNotFound):await journal.run('r3','b1')
        assert runtime.admission.peak==2 and agent.calls==2
        agent.release.set();await until(lambda:runtime.admission.active==0)
        await runtime.start(*request('r3','s3'));await wait_status(journal,'r3','completed')
    finally:await runtime.shutdown()

async def test_cancel_before_first_schedule_releases_slot(tmp_path):
    runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),ControlledOrchestrator(True),max_active_runs=1)
    await runtime.startup()
    try:
        await runtime.start(*request());runtime.running['r1'].task.cancel()
        await runtime.cancel('r1','b1');await until(lambda:runtime.admission.active==0)
        assert (await runtime.journal.run('r1','b1'))['status']=='stopped'
    finally:await runtime.shutdown()

async def test_reserve_failure_releases_slot(tmp_path,monkeypatch):
    journal=AGUIJournal(tmp_path/'runs.db');runtime=AGUIRuntime(journal,ControlledOrchestrator(),max_active_runs=1)
    async def fail(*a,**kw):raise RuntimeError('fixture')
    monkeypatch.setattr(journal,'reserve',fail)
    with pytest.raises(RuntimeError):await runtime.start(*request())
    assert runtime.admission.active==0

async def test_deadline_is_durable_and_does_not_rerun(tmp_path):
    agent=ControlledOrchestrator(True)
    runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),agent,run_timeout_seconds=.08)
    await runtime.startup()
    try:
        await runtime.start(*request());await wait_status(runtime.journal,'r1','error')
        await until(lambda:runtime.admission.active==0)
        events,status,_=await runtime.journal.events('r1','b1')
        assert status=='error' and events[-1]['event']['code']=='RUN_DEADLINE_EXCEEDED'
        assert (await runtime.start(*request()))['status']=='error' and agent.calls==1
    finally:await runtime.shutdown()

async def test_ignored_cancel_holds_slot_but_cannot_keep_journal_running(tmp_path):
    class Stubborn:
        def __init__(self):self.cancelled,self.release=asyncio.Event(),asyncio.Event()
        async def handle_intent(self,intent,**kw):
            self.guard=kw['persistence_guard']
            try:await self.release.wait()
            except asyncio.CancelledError:self.cancelled.set();await self.release.wait()
            return SimpleNamespace(error=None,final_text='late')
    agent=Stubborn();runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),agent,run_timeout_seconds=.08,max_active_runs=1)
    await runtime.startup()
    try:
        await runtime.start(*request());await wait_status(runtime.journal,'r1','error')
        assert agent.cancelled.is_set() and not agent.guard() and runtime.admission.active==1
        with pytest.raises(QueueCapacityExceeded):await runtime.start(*request('r2','s2'))
        agent.release.set();await until(lambda:runtime.admission.active==0)
        assert (await runtime.journal.run('r1','b1'))['status']=='error'
    finally:agent.release.set();await runtime.shutdown()

@pytest.mark.parametrize('broken',[False,True])
async def test_queue_outage_returns_503_without_inline_execution(tmp_path,broken):
    class Broken:
        async def enqueue(self,*a,**kw):raise ConnectionError('synthetic unavailable')
        async def get_task(self,*a):raise ConnectionError('synthetic unavailable')
    runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),ControlledOrchestrator(),task_queue=Broken() if broken else None,queue_enabled=True)
    await runtime.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(runtime)),base_url='http://test') as c:
            response=await c.post('/commerce/ag-ui/run',json=body())
        assert response.status_code==503 and response.headers['retry-after']=='3'
        assert response.json()['detail']['code']==('QUEUE_ACCEPTANCE_UNKNOWN' if broken else 'QUEUE_UNAVAILABLE')
        assert runtime.orchestrator.calls==0 and runtime.admission.active==0
        with pytest.raises(JournalNotFound):await runtime.journal.run('r1','b1')
    finally:await runtime.shutdown()

async def test_queue_unavailable_still_replays_existing_journal(tmp_path):
    runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),ControlledOrchestrator());await runtime.startup()
    try:
        await runtime.start(*request());await wait_status(runtime.journal,'r1','completed');runtime.queue_enabled=True
        assert (await runtime.submit(*request()))['status']=='completed' and runtime.orchestrator.calls==1
    finally:await runtime.shutdown()

async def test_unstarted_terminal_preserves_new_session_and_never_signs_catalog(tmp_path):
    journal=AGUIJournal(tmp_path/'runs.db');await journal.bind_catalog(binding())
    await journal.reserve(body('new','s1'),'b1','owner')
    assert (await journal.fail_unstarted(body('old','s1'),'b1'))['status']=='error'
    assert (await journal.session('s1','b1'))['run']['runId']=='new'
    assert [r[0] for r in await catalog_rows(journal)]==['new']
    assert (await journal.fail_unstarted(body('new','s1'),'b1'))['status']=='running'
    with pytest.raises(JournalConflict):await journal.fail_unstarted(body('old','s1',query='changed'),'b1')
    assert (await journal.existing(body('old','s1'),'b1'))['status']=='error'

async def test_legacy_and_agui_share_same_gate(tmp_path,monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "ci-local-placeholder")
    from app.presentation.server import _run_inline
    runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),ControlledOrchestrator(True),max_active_runs=1);await runtime.startup()
    c=SimpleNamespace(ag_ui_runtime=runtime,settings=SimpleNamespace(run_timeout_seconds=1),orchestrator=runtime.orchestrator)
    try:
        await runtime.start(*request())
        with pytest.raises(QueueCapacityExceeded):await _run_inline(c,request('legacy','other')[1])
        assert runtime.admission.peak==1
    finally:runtime.orchestrator.release.set();await runtime.shutdown()

@pytest.mark.parametrize('seconds',[0,-1,True,float('inf'),float('nan')])
def test_invalid_run_deadline(tmp_path,seconds):
    with pytest.raises(ValueError):
        AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),ControlledOrchestrator(),run_timeout_seconds=seconds)

async def test_legacy_queue_recovers_even_when_agui_ingress_is_inline(tmp_path):
    calls=[]
    class Queue:
        async def expire_due(self,*,limit):calls.append(limit);return []
    runtime=AGUIRuntime(AGUIJournal(tmp_path/'runs.db'),ControlledOrchestrator(),recovery_queue=Queue())
    await runtime.recover()
    assert calls==[100] and runtime.task_queue is None and not runtime.queue_enabled

async def test_worker_legacy_waits_inside_consumer_slot_and_releases_on_timeout(monkeypatch):
    from app import worker
    from app.domain.queue.ports.task_queue import IntentTask
    monkeypatch.setattr(worker,'current_execution_lease',lambda:SimpleNamespace(is_valid=lambda:True))
    class Blocked:
        calls=0
        async def handle_intent(self,*a,**kw):
            self.calls+=1
            await asyncio.sleep(1)
    gate=RunAdmission(1);held=gate.acquire();agent=Blocked()
    task=IntentTask('legacy','s1','b1','zh-CN','CNY','合成请求')
    bus=SimpleNamespace(publish=lambda *a:None)
    with pytest.raises(TimeoutError):
        await worker.execute_intent_task(task,agent,bus,admission=gate,timeout_seconds=.04)
    assert agent.calls==0 and gate.active==1
    held.release()
    with pytest.raises(TimeoutError):
        await worker.execute_intent_task(task,agent,bus,admission=gate,timeout_seconds=.04)
    assert agent.calls==1 and gate.active==0

async def test_worker_legacy_deadline_blocks_late_persistence(monkeypatch):
    from app import worker
    from app.domain.queue.ports.task_queue import IntentTask
    monkeypatch.setattr(worker,'current_execution_lease',lambda:SimpleNamespace(is_valid=lambda:True))
    class Late:
        async def handle_intent(self,*a,**kw):
            try:await asyncio.sleep(1)
            except asyncio.CancelledError:pass
            assert not kw['persistence_guard']()
            return SimpleNamespace(error=None,final_text='late')
    gate=RunAdmission(1)
    with pytest.raises(TimeoutError):
        await worker.execute_intent_task(IntentTask('legacy','s1','b1','zh-CN','CNY','合成请求'),Late(),
            SimpleNamespace(publish=lambda *a:None),admission=gate,timeout_seconds=.04)
    assert gate.active==0
