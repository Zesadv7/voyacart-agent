"""两个独立签名买家的跨入口矩阵；真实临时存储，模型/连接探针固定替身。"""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.application.agent_runtime import AGUIRuntime
from app.application.usecases.order_usecases import QueryOrderUseCase, CancelOrderUseCase
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.automation_store import AutomationStore
from app.infrastructure.buyer_favorites import BuyerFavoriteStore
from app.infrastructure.buyer_skills import BuyerSkillStore
from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore
from tests.test_ag_ui_journal import body as run_body
from tests.test_ag_ui_tool_products import product, start, result
from tests.test_automations import definition
from tests.test_confirmation_routes import order_request, decision
from tests.test_identity import POLICY, auth, secured, intent  # noqa: F401
from tests.test_model_workspace import workspace, client as model_client, draft, create_profile  # noqa: F401
from tests.trade_test_helpers import confirmation_env  # noqa: F401


def endpoint_matrix(buyer='buyer-1', session='session-1'):
    q = f'buyer_id={buyer}'
    both = f'{q}&session_id={session}'
    command = {'buyer_id': buyer, 'request_id': 'r1', 'expected_revision': 1}
    selection = {'buyer_id': buyer, 'profile_id': 'current-gateway', 'expected_revision': 0}
    skill = {'title': '标题', 'description': '用途', 'body': '正文', 'expected_version': '1'}
    connection = draft(buyer_id=buyer)
    return [
        ('POST', '/commerce/intents', intent(buyer, session)),
        ('POST', '/commerce/intents/async', intent(buyer, session)),
        ('GET', f'/commerce/tasks/task-1?{q}', None),
        ('POST', '/commerce/ag-ui/run', run_body('run-1', session, buyer)),
        ('GET', f'/commerce/ag-ui/runs/run-1?{q}', None),
        ('GET', f'/commerce/ag-ui/runs/run-1/events?{q}', None),
        ('POST', f'/commerce/ag-ui/runs/run-1/cancel?{q}', None),
        ('GET', f'/commerce/ag-ui/runs/run-1/tools/tool-1/products?{q}', None),
        ('GET', f'/commerce/ag-ui/sessions?{q}', None),
        ('GET', f'/commerce/ag-ui/sessions/{session}?{q}', None),
        ('GET', f'/commerce/skills?{q}', None),
        ('GET', f'/commerce/favorites?{q}', None),
        ('PUT', f'/commerce/favorites/P1001?{q}', {'product': {'product_id': 'P1001'}}),
        ('DELETE', f'/commerce/favorites/P1001?{q}', None),
        ('POST', f'/commerce/shopping-forms?{q}', {'session_id': session}),
        ('GET', f'/commerce/shopping-forms?{both}', None),
        ('GET', f'/commerce/shopping-forms/form-1?{both}', None),
        ('POST', f'/commerce/shopping-forms/form-1/actions?{q}', {
            'session_id': session, 'expected_revision': 1, 'request_id': 'r1',
            'action': {'name': 'applyShoppingRequirements', 'surfaceId': 'form-1',
                       'sourceComponentId': 'root', 'timestamp': '2026-10-15T00:00:00Z', 'context': {}}}),
        ('GET', f'/commerce/my-skills?{q}', None),
        ('POST', f'/commerce/my-skills?{q}', skill),
        ('PUT', f'/commerce/my-skills/personal-1?{q}', skill),
        ('DELETE', f'/commerce/my-skills/personal-1?{q}&expected_version=1', None),
        ('GET', f'/commerce/preferences?{q}', None),
        ('POST', f'/commerce/preferences?{q}', {'kind': 'like', 'statement': '轻便'}),
        ('DELETE', f'/commerce/preferences?{q}', {'statement': '轻便'}),
        ('GET', f'/commerce/preferences/memory-1/history?{q}', None),
        ('GET', f'/commerce/confirmations?{both}', None),
        ('GET', f'/commerce/confirmations/confirmation-1?{both}', None),
        ('POST', '/commerce/confirmations/orders', order_request(buyer_id=buyer, session_id=session)),
        ('POST', '/commerce/confirmations/confirmation-1/resolve',
            {'buyer_id': buyer, 'session_id': session, 'snapshot_hash': 'hash', 'approved': True}),
        ('GET', f'/commerce/orders?{q}', None),
        ('GET', f'/commerce/orders/order-1?{q}', None),
        ('POST', '/commerce/orders/order-1/cancel', {'buyer_id': buyer, 'session_id': session, 'reason': '调整计划'}),
        ('GET', f'/commerce/automations/templates?{q}', None),
        ('GET', f'/commerce/automations/options?{q}', None),
        ('GET', f'/commerce/automations/session-origins?{q}', None),
        ('POST', '/commerce/automations/preview', {'buyer_id': buyer, 'schedule': {'kind': 'daily', 'time': '10:00'}}),
        ('GET', f'/commerce/automations?{q}', None),
        ('POST', '/commerce/automations', {'buyer_id': buyer, 'request_id': 'r1', 'definition': definition()}),
        ('PATCH', '/commerce/automations/automation-1', {**command, 'definition': definition()}),
        ('POST', '/commerce/automations/automation-1/actions', {**command, 'action': 'pause'}),
        ('POST', '/commerce/automations/automation-1/runs', command),
        ('GET', f'/commerce/automation-runs?{q}', None),
        ('GET', f'/commerce/automation-runs/automation-run-1?{q}', None),
        ('POST', '/commerce/automation-runs/automation-run-1/actions', {**command, 'action': 'stop'}),
        ('GET', f'/commerce/context?{both}', None),
        ('POST', '/commerce/context/compact', {'buyer_id': buyer, 'session_id': session, 'request_id': 'r1', 'expected_revision': 0}),
        ('GET', f'/commerce/context/operations/operation-1?{q}', None),
        ('GET', f'/commerce/models?{both}', None),
        ('PATCH', f'/commerce/sessions/{session}/model', selection),
        ('PATCH', '/commerce/models/current-gateway/preferences', {'buyer_id': buyer, 'visible': False}),
        ('POST', '/commerce/models/default', selection),
        ('POST', '/commerce/model-connections/validate', connection),
        ('POST', '/commerce/model-connections', {**connection, 'validation_ticket': 'ticket', 'config_hash': 'hash'}),
    ]


@pytest.fixture
async def gate_env(secured, monkeypatch):
    """所有拒绝都必须发生于业务调用之前；访问对象本身也用哨兵断言。"""
    c = secured.container
    denied = Mock(side_effect=AssertionError('身份拒绝前不应访问业务存储'))
    class Guard:
        def __getattr__(self, name):
            denied(name)
    c.ag_ui_runtime = Guard()
    c.context_service = Guard()
    c.automation_service = Guard()
    c.model_service = Guard()
    c.trade_store = Guard()
    c.orchestrator = Guard()
    c.query_order = Guard()
    c.cancel_order = Guard()
    c.confirmations = Guard()
    c.task_queue = Guard()
    # 收藏路由工厂也必须在 require_buyer 后才执行。
    monkeypatch.setattr('app.infrastructure.buyer_favorites.BuyerFavoriteStore', denied)
    original_owner = secured.store.assert_owner
    secured.store.assert_owner = AsyncMock(wraps=original_owner)
    return SimpleNamespace(client=secured.client, denied=denied, owner=secured.store.assert_owner)


@pytest.mark.parametrize('mode', ['missing', 'expired', 'wrong_signature', 'query_only', 'spoofed_claim'])
async def test_all_buyer_routes_reject_untrusted_identity_before_business_calls(gate_env, mode):
    headers = {}
    expected = 403 if mode == 'spoofed_claim' else 401
    if mode == 'expired':
        headers = {'Authorization': 'Bearer ' + replace(POLICY, clock=lambda: 1000).issue('buyer-1')}
    elif mode == 'wrong_signature':
        headers = {'Authorization': 'Bearer ' + replace(POLICY, secret='wrong-signature-synthetic-only-12345').issue('buyer-1')}
    elif mode == 'spoofed_claim':
        headers = auth('buyer-2')
    for method, path, body in endpoint_matrix():
        if mode == 'query_only':
            path += ('&' if '?' in path else '?') + 'token=' + POLICY.issue('buyer-1')
        response = await gate_env.client.request(method, path, json=body, headers=headers)
        assert response.status_code == expected, (mode, method, path.split('?')[0], response.text)
    gate_env.denied.assert_not_called()
    gate_env.owner.assert_not_awaited()


@pytest.fixture
async def matrix(secured, tmp_path):
    c = secured.container
    c.settings.data_dir = tmp_path
    journal = AGUIJournal(tmp_path / 'matrix-runs.db')
    await journal.initialize()
    runtime = AGUIRuntime(journal, c.orchestrator)
    c.ag_ui_runtime = runtime
    skills = BuyerSkillStore(tmp_path / 'skills.db')
    preferences = JsonFilePreferenceStore(tmp_path)
    c.orchestrator._sessions = SimpleNamespace(_main_factory=SimpleNamespace(buyer_skill_store=skills))
    c.orchestrator._preference_store = preferences
    c.orchestrator.available_skills = AsyncMock(return_value={'skills': []})
    c.context_service = SimpleNamespace(store=secured.store, view=AsyncMock(wraps=secured.store.context_view), start=AsyncMock())
    c.trade_store = secured.env.store
    c.query_order = QueryOrderUseCase(c.trade_store)
    c.cancel_order = CancelOrderUseCase(secured.env.service)
    automation_store = AutomationStore(journal.path)
    await automation_store.initialize()
    c.automation_service = SimpleNamespace(store=automation_store, save=AsyncMock())
    for number in (1, 2):
        buyer, session, run = f'buyer-{number}', f'session-{number}', f'run-{number}'
        await secured.store.assert_owner(session, buyer, create=True)
        await journal.reserve(run_body(run, session, buyer), buyer, 'fixture')
        await journal.append(run, 'fixture', [
            {'type': 'RUN_STARTED', 'threadId': session, 'runId': run},
            start('tool-1'), result('tool-1', {'hits': [product('P100' + str(number))]}),
            {'type': 'RUN_FINISHED', 'threadId': session, 'runId': run}])
    runtime.cancel = AsyncMock(wraps=runtime.cancel)
    journal.tool_products = AsyncMock(wraps=journal.tool_products)
    operation, _ = await secured.store.create_context_operation('session-1', 'buyer-1', 'context-1', 0)
    automation = await automation_store.save('buyer-1', definition(), 'save-1')
    automation_run = await automation_store.manual(automation['id'], 'buyer-1', 'manual-1', 1)
    return SimpleNamespace(**vars(secured), journal=journal, runtime=runtime, skills=skills, preferences=preferences,
        operation=operation, automation=automation, automation_run=automation_run, automation_store=automation_store)


async def test_signed_buyers_cannot_read_or_cancel_each_others_session_run_sse_or_products(matrix):
    c = matrix.client
    for actor, owner in [(1, 2), (2, 1)]:
        buyer = f'buyer-{actor}'
        q = f'buyer_id={buyer}'
        for method, path in [
            ('GET', f'/commerce/ag-ui/sessions/session-{owner}?{q}'),
            ('GET', f'/commerce/ag-ui/runs/run-{owner}?{q}'),
            ('GET', f'/commerce/ag-ui/runs/run-{owner}/events?{q}'),
            ('POST', f'/commerce/ag-ui/runs/run-{owner}/cancel?{q}'),
            ('GET', f'/commerce/ag-ui/runs/run-{owner}/tools/tool-1/products?{q}'),
            ('GET', f'/commerce/context?{q}&session_id=session-{owner}'),
        ]:
            response = await c.request(method, path, headers=auth(buyer))
            assert response.status_code == 403, (path, response.text)
        sessions = await c.get(f'/commerce/ag-ui/sessions?{q}', headers=auth(buyer))
        assert [item['id'] for item in sessions.json()['sessions']] == [f'session-{actor}']
        assert (await c.get(f'/commerce/ag-ui/runs/run-{actor}?{q}', headers=auth(buyer))).status_code == 200
        replay = await c.get(f'/commerce/ag-ui/runs/run-{actor}/events?{q}', headers=auth(buyer))
        assert replay.status_code == 200 and 'RUN_FINISHED' in replay.text
    matrix.runtime.cancel.assert_not_awaited()
    matrix.journal.tool_products.assert_not_awaited()
    for actor in (1, 2):
        products = await c.get(f'/commerce/ag-ui/runs/run-{actor}/tools/tool-1/products?buyer_id=buyer-{actor}', headers=auth(f'buyer-{actor}'))
        assert products.status_code == 200 and products.json()['products'][0]['product_id'] == f'P100{actor}'
    matrix.container.context_service.view.assert_not_awaited()
    for path in [f"/commerce/context/operations/{matrix.operation['operation_id']}?buyer_id=buyer-2"]:
        assert (await c.get(path, headers=auth('buyer-2'))).status_code == 403


async def test_cross_session_writes_do_not_start_model_or_context_work(matrix):
    c = matrix.client
    for method, path, body in [
        ('POST', '/commerce/intents', intent('buyer-2', 'session-1')),
        ('POST', '/commerce/ag-ui/run', run_body('new-run', 'session-1', 'buyer-2')),
        ('POST', '/commerce/context/compact', {'buyer_id': 'buyer-2', 'session_id': 'session-1', 'request_id': 'r1', 'expected_revision': 0}),
        ('POST', '/commerce/confirmations/orders', order_request(buyer_id='buyer-2', session_id='session-1')),
    ]:
        assert (await c.request(method, path, json=body, headers=auth('buyer-2'))).status_code == 403
    matrix.container.orchestrator.handle_intent.assert_not_awaited()
    matrix.container.context_service.start.assert_not_awaited()
    assert await matrix.env.store.list_confirmations(buyer_id='buyer-1', session_id='session-1') == []


async def test_favorites_preferences_and_private_skills_remain_buyer_scoped(matrix):
    c = matrix.client
    skill = {'title': '我的选购', 'description': '个人方案', 'body': '仅买家一的正文'}
    created = (await c.post('/commerce/my-skills?buyer_id=buyer-1', json=skill, headers=auth())).json()['skill']
    favorite = {'product': {'product_id': 'P1001', 'title': '收藏一'}}
    assert (await c.put('/commerce/favorites/P1001?buyer_id=buyer-1', json=favorite, headers=auth())).status_code == 200
    pref = {'kind': 'like', 'statement': '仅买家一的偏好'}
    assert (await c.post('/commerce/preferences?buyer_id=buyer-1', json=pref, headers=auth())).status_code == 200
    for path, field in [('favorites', 'products'), ('preferences', 'preferences'), ('my-skills', 'skills')]:
        response = await c.get(f'/commerce/{path}?buyer_id=buyer-2', headers=auth('buyer-2'))
        assert response.status_code == 200 and response.json()[field] == []
    path = f"/commerce/my-skills/{created['id']}?buyer_id=buyer-2"
    assert (await c.put(path, json={**skill, 'expected_version': created['version']}, headers=auth('buyer-2'))).status_code == 404
    assert (await c.delete(path + '&expected_version=' + created['version'], headers=auth('buyer-2'))).status_code == 404
    assert (await c.delete('/commerce/favorites/P1001?buyer_id=buyer-2', headers=auth('buyer-2'))).status_code == 200
    assert (await c.request('DELETE', '/commerce/preferences?buyer_id=buyer-2', json={'statement': pref['statement']}, headers=auth('buyer-2'))).json()['deleted'] is False
    assert len((await c.get('/commerce/favorites?buyer_id=buyer-1', headers=auth())).json()['products']) == 1
    assert len((await c.get('/commerce/preferences?buyer_id=buyer-1', headers=auth())).json()['preferences']) == 1
    assert matrix.skills.list('buyer-1', body=True)[0]['body'] == skill['body']


async def test_foreign_confirmation_and_order_ids_cannot_reveal_or_mutate_trade(matrix):
    c = matrix.client
    prepared = await c.post('/commerce/confirmations/orders', json=order_request(), headers=auth())
    assert prepared.status_code == 200, prepared.text
    confirmation = prepared.json()['confirmation']
    matrix.env.store._create_order = AsyncMock(wraps=matrix.env.store._create_order)
    matrix.env.store._cancel_order = AsyncMock(wraps=matrix.env.store._cancel_order)
    path = f"/commerce/confirmations/{confirmation['confirmation_id']}"
    foreign = await c.get(path + '?buyer_id=buyer-2&session_id=session-2', headers=auth('buyer-2'))
    assert foreign.status_code == 409 and foreign.json()['detail']['code'] == 'OWNER_MISMATCH'
    denied = await c.post(path + '/resolve', json=decision(confirmation, buyer_id='buyer-2', session_id='session-2'), headers=auth('buyer-2'))
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'OWNER_MISMATCH'
    assert (await matrix.env.store.get_inventory())['P1001-S1'] == 50
    matrix.env.store._create_order.assert_not_awaited()
    matrix.env.store._cancel_order.assert_not_awaited()
    resolved = await c.post(path + '/resolve', json=decision(confirmation), headers=auth())
    assert resolved.status_code == 200
    order_id = resolved.json()['order']['order_id']
    assert (await c.get(f'/commerce/orders/{order_id}?buyer_id=buyer-2', headers=auth('buyer-2'))).status_code == 404
    assert (await c.post(f'/commerce/orders/{order_id}/cancel', json={'buyer_id': 'buyer-2', 'session_id': 'session-2', 'reason': '越权'}, headers=auth('buyer-2'))).status_code == 409
    assert (await matrix.env.store.get_inventory())['P1001-S1'] == 49
    matrix.env.store._create_order.assert_awaited_once()
    matrix.env.store._cancel_order.assert_not_awaited()
    mine = await c.get(f'/commerce/orders/{order_id}?buyer_id=buyer-1', headers=auth())
    assert mine.status_code == 200 and mine.json()['status'] == 'CONFIRMED'
    assert (await c.get('/commerce/orders?buyer_id=buyer-2', headers=auth('buyer-2'))).json()['total'] == 0


async def test_foreign_automation_ids_cannot_edit_trigger_or_stop_owner_work(matrix):
    c = matrix.client
    command = {'buyer_id': 'buyer-2', 'request_id': 'foreign', 'expected_revision': 1}
    aid, rid = matrix.automation['id'], matrix.automation_run['id']
    for method, path, body in [
        ('PATCH', f'/commerce/automations/{aid}', {**command, 'definition': definition()}),
        ('POST', f'/commerce/automations/{aid}/actions', {**command, 'action': 'pause'}),
        ('POST', f'/commerce/automations/{aid}/runs', command),
        ('GET', f'/commerce/automation-runs/{rid}?buyer_id=buyer-2', None),
        ('POST', f'/commerce/automation-runs/{rid}/actions', {**command, 'action': 'stop'}),
    ]:
        response = await c.request(method, path, json=body, headers=auth('buyer-2'))
        assert response.status_code == 404, (path, response.text)
    matrix.container.automation_service.save.assert_not_awaited()
    assert (await matrix.automation_store.get(aid, 'buyer-1'))['revision'] == 1
    assert (await matrix.automation_store.runs('buyer-1'))['total'] == 1
    assert (await c.get('/commerce/automations?buyer_id=buyer-2', headers=auth('buyer-2'))).json()['total'] == 0
    assert (await c.get('/commerce/automation-runs?buyer_id=buyer-2', headers=auth('buyer-2'))).json()['total'] == 0


async def test_signed_model_admin_permissions_preferences_defaults_and_private_profiles(workspace):
    workspace.app.state.identity_policy = POLICY
    workspace.settings.identity_mode = 'hmac'
    async with model_client(workspace, host='api.example.invalid', peer='198.51.100.5') as c:
        for buyer in ('alice', 'bob'):
            response = await c.get(f'/commerce/models?buyer_id={buyer}&session_id={buyer}', headers=auth(buyer))
            assert response.status_code == 200 and response.json()['management_allowed'] == (buyer == 'alice')
        for path in ['/commerce/model-connections/validate', '/commerce/model-connections']:
            response = await c.post(path, json=draft(buyer_id='bob'), headers=auth('bob'))
            assert response.status_code == 403
        assert not workspace.calls
        async with model_client(workspace, host='api.example.invalid', peer='198.51.100.5', headers=auth('alice')) as admin:
            profile = await create_profile(admin)
        assert len(workspace.calls) == 1
        own = (await c.get('/commerce/models?buyer_id=alice&session_id=alice', headers=auth('alice'))).json()
        other = (await c.get('/commerce/models?buyer_id=bob&session_id=bob', headers=auth('bob'))).json()
        assert any(item['profile_id'] == profile['profile_id'] for item in own['models'])
        assert not any(item['profile_id'] == profile['profile_id'] for item in other['models'])
        foreign = {'buyer_id': 'bob', 'profile_id': profile['profile_id'], 'expected_revision': 0}
        assert (await c.patch('/commerce/sessions/alice/model', json={**foreign, 'profile_id': 'current-gateway'}, headers=auth('bob'))).status_code == 403
        assert (await c.patch('/commerce/sessions/bob/model', json=foreign, headers=auth('bob'))).status_code == 422
        assert (await c.patch(f"/commerce/models/{profile['profile_id']}/preferences", json={'buyer_id': 'bob', 'visible': False}, headers=auth('bob'))).status_code == 422
        assert (await c.post('/commerce/models/default', json=foreign, headers=auth('bob'))).status_code == 422
        assert len(workspace.calls) == 1
        # 普通买家可维护自己的内置模型偏好/默认，不能改变另一个买家。
        assert (await c.patch('/commerce/models/current-gateway/preferences', json={'buyer_id': 'bob', 'visible': False}, headers=auth('bob'))).status_code == 200
        value = {'buyer_id': 'bob', 'profile_id': 'current-gateway', 'expected_revision': 0, 'window_id': 'budget-32000'}
        assert (await c.post('/commerce/models/default', json=value, headers=auth('bob'))).status_code == 200
        after = (await c.get('/commerce/models?buyer_id=alice&session_id=alice-new', headers=auth('alice'))).json()
        assert after['default_revision'] == 0
        assert next(item for item in after['models'] if item['profile_id'] == 'current-gateway')['visible'] is True
