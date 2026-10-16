"""容器内合成 HTTP/恢复探针；只有报告聚合进入 stdout，不导出数据库或密钥。

Docker 生命周期由 container_smoke 控制。本模块没有 Docker socket，只访问
专用 internal 网络内 app/mock-llm，复用既有 M6 seed_http 和快照工具。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile

import httpx

from app.infrastructure.identity import IdentityPolicy
from scripts.ci.mock_smoke import frames
from scripts.ops.mock_recovery import BUYER, SESSION, IDENTITY, HMAC_FIXTURE, inventory, seed_http
from scripts.ops.state_snapshot import backup, restore, fingerprint_files, summary

DATA = Path('/app/data')
STATE = Path('/ci-state')
SNAPSHOT = Path('/ci-snapshot/baseline')
RESTORED = Path('/ci-restored')
ROOT = Path('/app')
ORIGINAL = Path('/ci-original')


def policy() -> IdentityPolicy:
    return IdentityPolicy(mode='hmac', secret=HMAC_FIXTURE)


def client() -> httpx.Client:
    return httpx.Client(base_url='http://app:8000', timeout=180, trust_env=False,
                        headers={'Authorization': 'Bearer ' + policy().issue(BUYER)})


def mock_counts() -> dict:
    with httpx.Client(trust_env=False, timeout=10) as http:
        response = http.get('http://mock-llm:8081/admin/requests')
        response.raise_for_status()
        return response.json()


def frozen_paths() -> dict[str, Path]:
    return {name: ROOT / path for name, path in {
        'application': 'app', 'config': 'configs', 'knowledge': 'knowledge',
        'catalog': 'catalog', 'uv_lock': 'uv.lock', 'project': 'pyproject.toml',
    }.items()}


def save_fixture(value: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / 'fixture.json'
    with path.open('x') as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False)


def load_fixture() -> dict:
    return json.loads((STATE / 'fixture.json').read_text())


def assert_identity(http: httpx.Client) -> None:
    response = http.get('/commerce/identity', headers={'Authorization': ''})
    assert response.status_code == 401, response.status_code
    assert http.get('/commerce/identity').json()['buyer_id'] == BUYER
    assert http.get('/commerce/identity/config').json()['mode'] == 'hmac'
    assert http.get('/commerce/orders', params={'buyer_id': 'ci-other-buyer'}).status_code == 403


def assert_restored(http: httpx.Client, data: Path, fixture: dict) -> dict:
    """同一组 HTTP 契约应用到 restart/recreate/snapshot restore。"""
    query = {'buyer_id': BUYER}
    original = fixture['original_inventory']
    assert inventory(data) == original - 1
    response = http.get('/commerce/orders', params=query)
    response.raise_for_status()
    assert response.json()['total'] == 1
    response = http.get('/commerce/orders/' + fixture['order']['order_id'], params=query)
    response.raise_for_status()
    assert response.json()['status'] == 'CONFIRMED'
    response = http.post('/commerce/confirmations/orders', json=fixture['prepare'])
    response.raise_for_status()
    assert response.json()['order']['order_id'] == fixture['order']['order_id']
    response = http.post('/commerce/confirmations/' + fixture['card']['confirmation_id'] + '/resolve',
                         json=fixture['resolve'])
    response.raise_for_status()
    assert response.json()['order']['order_id'] == fixture['order']['order_id']
    assert inventory(data) == original - 1
    response = http.get('/commerce/ag-ui/runs/m6-search/events', params=query)
    response.raise_for_status()
    assert frames(response.text) == fixture['events']
    middle = len(fixture['events']) // 2
    response = http.get('/commerce/ag-ui/runs/m6-search/events', params=query,
                        headers={'Last-Event-ID': fixture['events'][middle - 1]['id']})
    response.raise_for_status()
    assert frames(response.text) == fixture['events'][middle:]
    url = f'/commerce/ag-ui/sessions/{SESSION}/candidates'
    assert http.get(url, params=query).json() == fixture['candidates']
    assert http.put(url + '/comparison', params=query, json=fixture['comparison']).json() == fixture['candidates']
    candidate = fixture['candidates']['items'][0]['candidate_id']
    response = http.get(url + f'/{candidate}/verification', params={**query, 'ship_to': 'CN'})
    response.raise_for_status()
    assert response.json()['status'] == 'available'
    other = {'Authorization': 'Bearer ' + policy().issue('ci-other-buyer')}
    for endpoint in (url, '/commerce/ag-ui/runs/m6-search/events', '/commerce/orders/' + fixture['order']['order_id']):
        assert http.get(endpoint, params={'buyer_id': 'ci-other-buyer'}, headers=other).status_code in {403, 404}
    assert mock_counts() == fixture['mock_counts'], '重放/候选核验不得重新调用 mock chat'
    return {'signed_identity': True, 'other_buyer_denied': True,
            'order_and_inventory': True, 'prepare_and_resolve_idempotent': True,
            'sse_exact_and_cursor_replay': True, 'candidate_and_comparison_exact': True,
            'replay_without_model_call': True, 'event_count': len(fixture['events'])}


def seed() -> dict:
    with client() as http:
        assert_identity(http)
        fixture = seed_http(http, DATA)
        fixture['mock_counts'] = mock_counts()
        save_fixture(fixture)
        return assert_restored(http, DATA, fixture)


def verify() -> dict:
    with client() as http:
        assert_identity(http)
        return assert_restored(http, DATA, load_fixture())


def snapshot() -> dict:
    # 调用前 driver 已 inspect app 确认停止；mock 不写应用状态。
    manifest = backup(DATA, SNAPSHOT, quiesced=True,
                      include_model_key=True,
                      database_url=f'sqlite+aiosqlite:///{DATA / "voyacart.db"}',
                      catalog_paths={'catalog': ROOT / 'catalog'}, fingerprint_paths=frozen_paths())
    return summary(manifest)


def restore_snapshot() -> dict:
    manifest = restore(SNAPSHOT, RESTORED, catalog_target=STATE / 'restored-catalog',
                       expected_fingerprints=fingerprint_files(frozen_paths()))
    return summary(manifest)


def mutate() -> dict:
    fixture = load_fixture()
    cancel = {**IDENTITY, 'request_id': 'container-post-snapshot-cancel', 'reason': '合成快照回退验证'}
    with client() as http:
        url = '/commerce/orders/' + fixture['order']['order_id'] + '/cancel'
        response = http.post(url, json=cancel)
        response.raise_for_status()
        card = response.json()['confirmation']
        response = http.post('/commerce/confirmations/' + card['confirmation_id'] + '/resolve',
                             json={**IDENTITY, 'snapshot_hash': card['snapshot_hash'], 'approved': True})
        response.raise_for_status()
        assert response.json()['order']['status'] == 'CANCELLED'
        assert inventory(DATA) == fixture['original_inventory']
        assert http.post(url, json=cancel).json() == response.json()
        assert inventory(DATA) == fixture['original_inventory']
    return {'post_snapshot_order_cancelled': True, 'inventory_replenished_once': True}


def index() -> dict:
    from qdrant_client import QdrantClient
    index_client = QdrantClient(path=str(DATA / 'qdrant'))
    try:
        collection = index_client.get_collection('voyacart_products')
        assert collection.points_count == 5700
        assert collection.config.params.vectors.size == 1024
        return {'collection': 'voyacart_products', 'points': collection.points_count, 'vector_size': 1024}
    finally:
        index_client.close()


def original_preserved() -> dict:
    fixture = load_fixture()
    # 已停写原卷保持只读。WAL 库的 mode=ro 仍可能创建 -shm/-wal，
    # 因此连同已有 sidecar 复制到可写临时目录，只在副本查询，不忽略已提交 WAL。
    with tempfile.TemporaryDirectory(prefix='voyacart-original-check-') as directory:
        original = Path(directory)
        for name in ('voyacart.db', 'voyacart.db-wal', 'voyacart.db-shm'):
            source = ORIGINAL / name
            if name == 'voyacart.db' or source.exists():
                shutil.copyfile(source, original / name)
        with sqlite3.connect(f'file:{original / "voyacart.db"}?mode=ro', uri=True) as db:
            row = db.execute('SELECT status FROM orders WHERE order_id=?', (fixture['order']['order_id'],)).fetchone()
            assert row == ('CANCELLED',), row
        assert inventory(original) == fixture['original_inventory']
    assert inventory(DATA) == fixture['original_inventory'] - 1
    return {'source_volume_unchanged_after_restore': True, 'post_snapshot_mutation_absent_in_restored_volume': True}


def main() -> None:
    phases = {'seed': seed, 'verify': verify, 'snapshot': snapshot, 'restore': restore_snapshot,
              'mutate': mutate, 'index': index, 'original-preserved': original_preserved}
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=phases)
    args = parser.parse_args()
    result = phases[args.phase]()
    print(json.dumps({'phase': args.phase, 'status': 'passed', 'checks': result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
