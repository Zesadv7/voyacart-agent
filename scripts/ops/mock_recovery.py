"""M6 合成停写恢复：真实 HTTP/多 SQLite/Redis AOF，派生本地 Qdrant 重建。

只接触新临时目录与回环服务；快照、密钥、数据库和 AOF 不进入证据目录。
队列复用正式 runtime/worker helper，但 Agent 是可控合成实现，不是容器部署验收。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

from scripts.ci.mock_smoke import ROOT, frames, free_port, start, stop
from scripts.ci.offline_env import isolated_env

BUYER = "m6-synthetic-buyer"
SESSION = "m6-synthetic-session"
HMAC_FIXTURE = "m6-public-synthetic-HMAC-never-production"
# 固定公开夹具，不可用于实际部署。只在临时目录写入，不进入报告。
MASTER_FIXTURE = base64.urlsafe_b64encode(bytes(range(32)))
IDENTITY = {"buyer_id": BUYER, "session_id": SESSION}


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


@contextmanager
def clean_environment(data: Path, mock_url: str):
    """拒绝有 .env 的工作树，避免现有 settings 的显式 Langfuse 读取旁路。"""
    if (ROOT / ".env").exists() or (ROOT / ".env").is_symlink():
        raise RuntimeError("隔离演练需要没有 .env 的干净工作树；不会读取或修改现有文件")
    previous = dict(os.environ)
    env = isolated_env(data, mock_url=mock_url)
    env.update(IDENTITY_MODE="hmac", IDENTITY_HMAC_SECRET=HMAC_FIXTURE)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield env
    finally:
        os.environ.clear()
        os.environ.update(previous)


def request_body(run: str, *, session: str | None = None) -> dict:
    return {"threadId": session or SESSION, "runId": run, "state": {},
            "messages": [{"id": "message-" + run, "role": "user", "content": "查找寄到中国的旅行背包"}],
            "tools": [], "context": [], "forwardedProps": {"buyerId": BUYER}}


def inventory(data: Path) -> int:
    with sqlite3.connect(f"file:{data / 'voyacart.db'}?mode=ro", uri=True) as db:
        return db.execute("SELECT stock FROM trade_sku_inventory WHERE sku_id='P1001-S1'").fetchone()[0]


def table_counts(data: Path) -> dict:
    """只报告聚合；不把订单、买家原文或模型密文写入证据。"""
    result = {}
    for path in sorted(data.rglob("*")):
        if path.suffix not in {".db", ".sqlite3"} or any(p in {"qdrant", "qdrant_kb", "home"} for p in path.relative_to(data).parts):
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            result[str(path.relative_to(data))] = {name: db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}
    return result


def tree_digest(directory: Path) -> dict:
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file() and p.name != ".lock"}


def source_fingerprint() -> dict:
    paths = [p for name in ("app", "scripts", "tests", "configs", "docker", "knowledge", ".github", "frontend/src", "frontend/tests", "frontend/public")
             for p in (ROOT / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    paths += [ROOT / name for name in ("uv.lock", "pyproject.toml", "Dockerfile", ".dockerignore", ".env.example", "Makefile", ".python-version",
        "frontend/package.json", "frontend/package-lock.json", "frontend/Dockerfile", "frontend/.dockerignore",
        "frontend/vite.config.ts", "frontend/tsconfig.json", "frontend/nginx.conf", "frontend/index.html", "data/catalog-demo.jsonl.gz")]
    values = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return {"git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "sha256": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(), "files": values}


def start_redis(stack: ExitStack, binary: Path, directory: Path, port: int, log_path: Path):
    import redis
    directory.mkdir(parents=True, exist_ok=True)
    log = stack.enter_context(log_path.open("w"))
    process = subprocess.Popen([str(binary), "--bind", "127.0.0.1", "--port", str(port),
        "--dir", str(directory), "--appendonly", "yes", "--appendfsync", "always", "--save", "",
        "--maxmemory-policy", "noeviction", "--protected-mode", "yes"], stdout=log, stderr=subprocess.STDOUT)
    stack.callback(stop, process)
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("专用 Redis 启动失败")
        try:
            if client.ping():
                client.close()
                return process
        except redis.ConnectionError:
            pass
        time.sleep(.02)
    client.close()
    raise TimeoutError("专用 Redis 启动超时")


def seed_http(client: httpx.Client, data: Path) -> dict:
    response = client.post("/commerce/ag-ui/run", json=request_body("m6-search"))
    response.raise_for_status()
    events = frames(response.text)
    assert events[0]["data"]["type"] == "RUN_STARTED" and events[-1]["data"]["type"] == "RUN_FINISHED"
    assert [int(e["id"].rsplit(":", 1)[1]) for e in events] == list(range(1, len(events) + 1))
    cards = next(e["data"]["snapshot"]["products"] for e in reversed(events) if e["data"].get("snapshot", {}).get("products"))
    selections = [(p, s) for p in cards for s in p["skus"]][:2]
    assert len(selections) == 2
    url = f"/commerce/ag-ui/sessions/{SESSION}/candidates"
    query = {"buyer_id": BUYER}
    for n, (product, sku) in enumerate(selections):
        response = client.post(url, params=query, json={"request_id": f"m6-candidate-{n}", "expected_revision": n,
            "source": {"kind": "run", "run_id": "m6-search", "product_id": product["product_id"], "sku_id": sku["sku_id"]}})
        response.raise_for_status()
    comparison = {"request_id": "m6-comparison", "expected_revision": 2,
                  "candidate_ids": [i["candidate_id"] for i in response.json()["items"]]}
    response = client.put(url + "/comparison", params=query, json=comparison)
    response.raise_for_status()
    candidates = response.json()
    original = inventory(data)
    prepare = {**IDENTITY, "request_id": "m6-prepare",
               "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
               "shipping_address": {"recipient_name": "M6合成买家", "country": "CN", "city": "测试市", "address_line": "合成地址"}}
    response = client.post("/commerce/confirmations/orders", json=prepare)
    response.raise_for_status()
    card = response.json()["confirmation"]
    assert inventory(data) == original
    resolve = {**IDENTITY, "snapshot_hash": card["snapshot_hash"], "approved": True}
    response = client.post(f"/commerce/confirmations/{card['confirmation_id']}/resolve", json=resolve)
    response.raise_for_status()
    order = response.json()["order"]
    assert order["status"] == "CONFIRMED" and order["order_kind"] == "purchase_intent"
    assert inventory(data) == original - 1
    return dict(events=events, candidates=candidates, comparison=comparison, prepare=prepare, card=card,
                resolve=resolve, order=order, original_inventory=original)


async def seed_auxiliary(data: Path) -> dict:
    from app.infrastructure.buyer_favorites import BuyerFavoriteStore
    from app.infrastructure.buyer_skills import BuyerSkillStore
    from app.infrastructure.capability_registry import CapabilityRegistry
    from app.infrastructure.shopping_forms import ShoppingFormStore
    from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
    from app.infrastructure.semantic_memory import SemanticPreferenceStore
    from app.domain.buyer.preference import BuyerPreference
    from app.infrastructure.persistence.sql.repositories import create_engine
    from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.application.usecases.confirmation_service import ConfirmationService
    from app.application.usecases.order_usecases import OrderItemInput
    from app.domain.order.address import Address
    from app.infrastructure.settings import load_settings
    from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient

    await BuyerFavoriteStore(data / "buyer_favorites.db").save(BUYER, {"product_id": "P1001", "title": "合成收藏"})
    BuyerSkillStore(data / "buyer_skills.db").save(BUYER, "合成技能", "恢复检查", "只读比较合成背包")
    CapabilityRegistry(data / "capabilities.db").import_draft({"kind": "skill", "id": "m6-fixture", "version": "1",
        "title": "合成技能", "description": "恢复检查", "body": "只读比较合成背包", "allowed_tools": [],
        "evidence": [{"reference": "synthetic:m6", "summary": "合成夹具，不是正式审核"}]}, author="m6-fixture")
    store = ShoppingFormStore(data / "shopping_forms.db")
    form = await store.create_clarification(BUYER, SESSION, "合成需求", [{"id": "purpose", "type": "text", "label": "用途", "required": True}])
    await store.submit(BUYER, SESSION, form["form_id"], form["revision"], "m6-form-request", {"purpose": "合成旅行"})
    await ContextEvidenceStore(data / "context_evidence.db").save(BUYER, SESSION, "m6-marker", {"marker": "snapshot-A"})
    class Legacy:
        async def list_by_buyer(self, buyer):
            return []
    class Extract:
        async def extract(self, preference, existing=()):
            return [{"kind": preference.kind, "statement": preference.statement}]
    settings = load_settings()
    memory = SemanticPreferenceStore(data / "buyer_memory.db", Legacy(), Extract(), OpenAIEmbeddingClient(settings), "mock-embedding:1024")
    await memory.append(BuyerPreference(BUYER, "like", "喜欢合成轻便背包"))
    engine = create_engine(f"sqlite+aiosqlite:///{data / 'voyacart.db'}")
    try:
        confirmation = ConfirmationService(InMemoryProductRepository(), SqlTradeStore(engine), ttl_seconds=1)
        expiring = await confirmation.prepare_order(BUYER, SESSION, [OrderItemInput("P1001", "P1001-S1", 1)],
            Address(recipient_name="M6合成买家", country="CN", city="测试市", state="", postal_code="", phone="", address_line="合成地址"), request_id="m6-expiring")
    finally:
        await engine.dispose()
    return expiring["confirmation"]


async def redis_inventory(client) -> dict:
    from app.infrastructure.queue.redis_stream_queue import _STREAM, _LARGE_STREAM, _GROUP, _STATUS_PREFIX
    result = {"streams": {}, "statuses": {}}
    for stream in (_STREAM, _LARGE_STREAM):
        groups = await client.xinfo_groups(stream)
        pending = await client.xpending_range(stream, _GROUP, "-", "+", 20)
        result["streams"][stream] = {"length": await client.xlen(stream),
            "groups": [{key: g[key] for key in ("name", "consumers", "pending", "last-delivered-id")} for g in groups],
            "pending": [{key: p[key] for key in ("message_id", "consumer", "times_delivered")} for p in pending]}
    for run in ("m6-queue-completed", "m6-queue-pending"):
        raw = await client.get(_STATUS_PREFIX + "agui:" + run)
        status = json.loads(raw) if raw else {}
        result["statuses"][run] = {key: status.get(key) for key in ("state", "archived", "message_id", "deliveries")}
        result["statuses"][run]["permanent_identity"] = await client.ttl(_STATUS_PREFIX + "agui:" + run) == -1
    return result


async def queue_phase(data: Path, redis_url: str, *, restored: bool = False) -> dict:
    import redis.asyncio as redis
    from ag_ui.core import RunAgentInput
    from app.application.agent_runtime import AGUIRuntime
    from app.infrastructure.ag_ui_journal import AGUIJournal, JournalLeaseLost
    from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _STREAM, _GROUP
    from app.infrastructure.queue.archive import QueueArchive
    from app.infrastructure.queue.maintenance import maintain_queue
    from app.presentation.ag_ui import parse_intent
    from app.worker import execute_ag_ui_task
    from scripts.ci.bounded_recovery_probe import SyntheticAgent
    client = redis.from_url(redis_url, decode_responses=True)
    archive = QueueArchive(data / "queue_archive.db")
    queue = RedisStreamTaskQueue(client, archive, max_pending=5, max_age_seconds=3600)
    await queue.ensure_group()
    agent = SyntheticAgent()
    journal = AGUIJournal(data / "ag_ui_runs.db")
    runtime = AGUIRuntime(journal, agent, task_queue=queue)
    await runtime.startup()
    async def handle(task):
        return await execute_ag_ui_task(task, runtime)
    try:
        if not restored:
            completed = RunAgentInput.model_validate(request_body("m6-queue-completed", session="m6-queue-completed-session"))
            assert (await runtime.submit(completed, parse_intent(completed)))["status"] == "queued"
            message = (await client.xreadgroup(_GROUP, "m6-original-worker", {_STREAM: ">"}, count=1))[0][1][0]
            await queue._handle_one(_STREAM, message[0], message[1], handle, 5, consumer_name="m6-original-worker")
            assert (await runtime.run("m6-queue-completed", BUYER))["status"] == "completed"
            archived = await maintain_queue(client, archive, older_than_seconds=0, apply=True)
            assert archived["deleted"] == 1
            dump(data / "queue_maintenance_cursors.json", archived["next_cursors"])
            pending = RunAgentInput.model_validate(request_body("m6-queue-pending", session="m6-queue-pending-session"))
            assert (await runtime.submit(pending, parse_intent(pending)))["status"] == "queued"
            # 领取但尚未进入 handler：真实 PEL + queued，不制造伪造 running 成功。
            assert await client.xreadgroup(_GROUP, "m6-retired-consumer", {_STREAM: ">"}, count=1)
            assert (await runtime.run("m6-queue-pending", BUYER))["status"] == "queued"
            await journal.reserve(request_body("m6-stale-owner", session="m6-stale-owner-session"), BUYER, "m6-retired-owner", .01)
            result = {"agent_calls": agent.calls, "inventory": await redis_inventory(client), "archive_messages": await archive.message_count()}
            assert result["agent_calls"] == 1
        else:
            old_write_rejected = False
            try:
                await journal.append("m6-stale-owner", "m6-retired-owner", [{"type": "RUN_FINISHED", "threadId": "m6-stale-owner-session", "runId": "m6-stale-owner"}])
            except JournalLeaseLost:
                old_write_rejected = True
            assert old_write_rejected
            assert (await runtime.run("m6-stale-owner", BUYER))["status"] == "interrupted"
            assert (await runtime.run("m6-queue-completed", BUYER))["status"] == "completed"
            assert (await queue.get_status("agui:m6-queue-completed")).state == "done"
            completed = RunAgentInput.model_validate(request_body("m6-queue-completed", session="m6-queue-completed-session"))
            await runtime.submit(completed, parse_intent(completed))
            assert agent.calls == 0
            claimed = await queue.claim_stale("m6-restored-worker", idle_ms=0, count=10)
            assert len(claimed) == 1
            delivery = claimed[0]
            await queue._handle_one(delivery.stream, delivery.message_id, {"payload": delivery.payload}, handle, 5,
                                    consumer_name="m6-restored-worker", delivery=delivery)
            assert (await runtime.run("m6-queue-pending", BUYER))["status"] == "completed"
            assert (await queue.get_status("agui:m6-queue-pending")).state == "done"
            # 同一已确认投递再次到达不重复调用 Agent。
            await queue._handle_one(delivery.stream, delivery.message_id, {"payload": delivery.payload}, handle, 5,
                                    consumer_name="m6-restored-worker", delivery=delivery)
            assert agent.calls == 1 and (await client.xpending(_STREAM, _GROUP))["pending"] == 0
            result = {"agent_calls": agent.calls, "inventory": await redis_inventory(client),
                      "archive_messages": await archive.message_count(), "old_owner_rejected": old_write_rejected,
                      "queued_completed": True, "duplicate_delivery_no_reexecution": True}
        return result
    finally:
        await runtime.shutdown()
        await client.aclose()


async def inspect_redis(redis_url: str) -> dict:
    import redis.asyncio as redis
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        return await redis_inventory(client)
    finally:
        await client.aclose()


async def model_check(data: Path, settings) -> dict:
    from app.infrastructure.model_registry import ModelRegistry
    registry = ModelRegistry(replace(settings, data_dir=data))
    await registry.initialize()
    snapshot = await registry.resolve_default(buyer_id=BUYER)
    runtime = registry.runtime(snapshot)
    assert runtime["api_key"] == "ci-local-placeholder" and runtime["model_id"] == "mock-chat"
    return {"profile_id": runtime["profile_id"], "model_id": runtime["model_id"], "original_synthetic_credential_decrypted": True}


async def rebuild_index(data: Path, catalog: Path, settings) -> dict:
    from app.infrastructure.persistence.seed_products import build_seed_products
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
    from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
    from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
    assert not (data / "qdrant").exists()
    products = build_seed_products(catalog)
    assert len(products) == 5700
    index = QdrantProductIndex(replace(settings, data_dir=data))
    result = {"mode": "new_embedded_local_qdrant", "frozen_catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest()}
    try:
        assert await bootstrap_product_index(InMemoryProductRepository(products), OpenAIEmbeddingClient(settings), index, batch_size=100, report=result)
        assert result["indexed_products"] == 5700 and result["missing_products"] == result["stale_points"] == result["invalid_points"] == 0
        assert result["reused_products"] == 0 and result["written_products"] == 5700
        from app.infrastructure.vector.embedding_identity import embedding_identity
        assert result["model_key"] == embedding_identity(settings)
        result["frozen_model_binding_verified"] = True
        result["embedding_model"] = settings.embedding_model
        result["embedding_version"] = settings.embedding_version
        return result
    finally:
        await index.close()


def verify_http(client: httpx.Client, data: Path, fixture: dict, expired: dict, policy) -> dict:
    original = fixture["original_inventory"]
    assert inventory(data) == original - 1
    orders = client.get("/commerce/orders", params={"buyer_id": BUYER}).json()
    assert orders["total"] == 1
    replay = client.post("/commerce/confirmations/orders", json=fixture["prepare"])
    replay.raise_for_status()
    assert replay.json()["order"]["order_id"] == fixture["order"]["order_id"]
    replay = client.post(f"/commerce/confirmations/{fixture['card']['confirmation_id']}/resolve", json=fixture["resolve"])
    replay.raise_for_status()
    assert replay.json()["order"]["order_id"] == fixture["order"]["order_id"] and inventory(data) == original - 1
    events = client.get("/commerce/ag-ui/runs/m6-search/events", params={"buyer_id": BUYER})
    events.raise_for_status()
    assert frames(events.text) == fixture["events"]
    midpoint = len(fixture["events"]) // 2
    replay = client.get("/commerce/ag-ui/runs/m6-search/events", params={"buyer_id": BUYER},
        headers={"Last-Event-ID": fixture["events"][midpoint - 1]["id"]})
    assert frames(replay.text) == fixture["events"][midpoint:]
    url = f"/commerce/ag-ui/sessions/{SESSION}/candidates"
    assert client.get(url, params={"buyer_id": BUYER}).json() == fixture["candidates"]
    assert client.put(url + "/comparison", params={"buyer_id": BUYER}, json=fixture["comparison"]).json() == fixture["candidates"]
    candidate = fixture["candidates"]["items"][0]["candidate_id"]
    assert client.get(url + f"/{candidate}/verification", params={"buyer_id": BUYER, "ship_to": "CN"}).json()["status"] == "available"
    other = {"Authorization": "Bearer " + policy.issue("m6-other-buyer")}
    for endpoint in (url, "/commerce/ag-ui/runs/m6-search/events", f"/commerce/orders/{fixture['order']['order_id']}"):
        assert client.get(endpoint, params={"buyer_id": "m6-other-buyer"}, headers=other).status_code in {403, 404}
    # TTL=1 使用真实时钟创建；恢复和重建没有续期。
    response = client.post(f"/commerce/confirmations/{expired['confirmation_id']}/resolve", json={**IDENTITY, "approved": True, "snapshot_hash": expired["snapshot_hash"]})
    assert response.status_code == 409 and inventory(data) == original - 1
    cancel_payload = {**IDENTITY, "request_id": "m6-cancel", "reason": "合成恢复验证"}
    cancel_url = f"/commerce/orders/{fixture['order']['order_id']}/cancel"
    cancel = client.post(cancel_url, json=cancel_payload)
    cancel.raise_for_status()
    card = cancel.json()["confirmation"]
    response = client.post(f"/commerce/confirmations/{card['confirmation_id']}/resolve", json={**IDENTITY, "approved": True, "snapshot_hash": card["snapshot_hash"]})
    response.raise_for_status()
    assert response.json()["order"]["status"] == "CANCELLED" and inventory(data) == original
    assert client.post(cancel_url, json=cancel_payload).json() == response.json() and inventory(data) == original
    return {"prepare_and_resolve_replay": True, "orders_before_cancel": 1, "inventory_after_replay": original - 1,
            "sse_exact_and_cursor_replay": True, "event_count": len(fixture["events"]),
            "candidate_comparison_revision": fixture["candidates"]["revision"], "candidate_count": 2,
            "candidate_and_comparison_exact": True, "other_buyer_denied": True,
            "expired_confirmation_rejected": True, "cancel_replenished_once": True, "inventory_after_cancel": original}


def run(output: Path, *, redis_server: Path, require_source_stable: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {"status": "running", "mode": "synthetic_quiesced_recovery_group", "paid_calls": 0,
        "limits": ["same-version rollback-to-snapshot only", "synthetic queue Agent; production runtime/worker helper",
                   "embedded local Qdrant only; no server/container deployment", "production RPO/RTO unknown"], "checks": {}}
    try:
        assert redis_server.is_file(), "需要现有 redis-server；本工具不会安装软件"
        initial_source = source_fingerprint()
        dump(output / "source-before.json", initial_source)
        with tempfile.TemporaryDirectory(prefix="voyacart-m6-recovery-") as temporary, ExitStack() as stack:
            root = Path(temporary)
            data, redis_data, snapshot = root / "source", root / "redis-source", root / "snapshot"
            restored, redis_restored, catalog_restored = root / "restored", root / "redis-restored", root / "catalog-restored"
            data.mkdir()
            secrets = data / ".secrets"
            secrets.mkdir(mode=0o700)
            (secrets / "model_registry.key").write_bytes(MASTER_FIXTURE)
            (secrets / "model_registry.key").chmod(0o600)
            ports = set()
            while len(ports) < 4:
                ports.add(free_port())
            mock_port, api_port, redis_port, new_redis_port = sorted(ports)
            mock_url = f"http://127.0.0.1:{mock_port}/v1"
            with clean_environment(data, mock_url) as env:
                from scripts.ops.state_snapshot import backup, restore, fingerprint_files, summary
                from app.infrastructure.identity import IdentityPolicy
                from app.infrastructure.settings import load_settings
                policy = IdentityPolicy(mode="hmac", secret=HMAC_FIXTURE)
                settings = load_settings()
                mock_process = start(stack, "mock_llm:app", mock_port, env, output / "mock.log")
                api = start(stack, "app.presentation.server:app", api_port, env, output / "api-source.log")
                client = stack.enter_context(httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=180, trust_env=False,
                    headers={"Authorization": "Bearer " + policy.issue(BUYER)}))
                fixture = seed_http(client, data)
                stop(api)
                assert api.poll() in (0, -signal.SIGTERM)
                expired = asyncio.run(seed_auxiliary(data))
                redis_process = start_redis(stack, redis_server, redis_data, redis_port, output / "redis-source.log")
                redis_url = f"redis://127.0.0.1:{redis_port}/0"
                report["queue_before"] = asyncio.run(queue_phase(data, redis_url))
                stop(redis_process)
                assert redis_process.poll() == 0
                # 此时 API/所有合成 runtime/Redis 均已关闭，mock 服务不写应用状态。
                report["checks"]["all_state_writers_stopped_before_snapshot"] = True
                counts = table_counts(data)
                report["snapshot_table_counts"] = counts
                assert len(counts) == 11 and all(sum(tables.values()) > 0 for tables in counts.values())
                source_before = tree_digest(data)
                frozen_paths = {"application": ROOT / "app", "config": ROOT / "configs", "knowledge": ROOT / "knowledge",
                    "dockerfile": ROOT / "Dockerfile", "dockerignore": ROOT / ".dockerignore", "compose": ROOT / "docker",
                    "uv_lock": ROOT / "uv.lock", "project": ROOT / "pyproject.toml"}
                expected = fingerprint_files(frozen_paths)
                stamp = time.monotonic()
                manifest = backup(data, snapshot, quiesced=True, redis_dir=redis_data, redis_stopped=True,
                    catalog_paths={"catalog-demo.jsonl.gz": ROOT / "data/catalog-demo.jsonl.gz", "runtime-demo-v1.json": ROOT / "configs/catalogs/runtime-demo-v1.json"},
                    fingerprint_paths=frozen_paths, include_model_key=True,
                    database_url=f"sqlite+aiosqlite:///{data / 'voyacart.db'}")
                report["backup_seconds"] = round(time.monotonic() - stamp, 3)
                report["snapshot_summary"] = summary(manifest)
                report["redis_persistence"] = {"appendfsync": "always", "aof_files": sorted(str(p.relative_to(redis_data)) for p in redis_data.rglob("*") if p.is_file())}
                assert tree_digest(data) == source_before
                snapshot_digest = tree_digest(snapshot)
                # B 是快照之后、原始合成源目录中的一次真正持久写入，不进入备份。
                from app.infrastructure.buyer_favorites import BuyerFavoriteStore
                asyncio.run(BuyerFavoriteStore(data / "buyer_favorites.db").save(BUYER, {"product_id": "M6-POST-SNAPSHOT-B", "title": "合成后写入"}))
                assert len(asyncio.run(BuyerFavoriteStore(data / "buyer_favorites.db").list(BUYER))) == 2
                source_after_b = tree_digest(data)
                recovery_started = stamp = time.monotonic()
                restore(snapshot, restored, redis_target=redis_restored, catalog_target=catalog_restored, expected_fingerprints=expected)
                report["restore_copy_seconds"] = round(time.monotonic() - stamp, 3)
                assert table_counts(restored) == counts
                assert tree_digest(data) == source_after_b and tree_digest(snapshot) == snapshot_digest
                report["checks"].update(snapshot_sqlite_counts_equal=True, source_and_snapshot_not_modified_by_restore=True,
                    post_snapshot_operation_B_absent=True, snapshot_committed_operations_recovered=True,
                    derived_indexes_excluded=not (restored / "qdrant").exists())
                favorites = asyncio.run(BuyerFavoriteStore(restored / "buyer_favorites.db").list(BUYER))
                assert [item["product_id"] for item in favorites] == ["P1001"]
                report["model_registry"] = asyncio.run(model_check(restored, settings))
                from app.infrastructure.model_connection import ModelConfigurationError
                negative = {}
                for condition in ("missing", "wrong"):
                    bad = root / ("model-" + condition)
                    bad.mkdir()
                    shutil.copy2(restored / "models.sqlite3", bad / "models.sqlite3")
                    if condition == "wrong":
                        (bad / ".secrets").mkdir()
                        (bad / ".secrets/model_registry.key").write_bytes(base64.urlsafe_b64encode(bytes(reversed(range(32)))))
                    try:
                        asyncio.run(model_check(bad, settings))
                    except ModelConfigurationError:
                        negative[condition + "_key_rejected"] = True
                    else:
                        raise AssertionError("缺失或错误密钥被错误接受")
                report["model_registry"].update(negative)
                # 缺 key 的应用本身会生成新 key 再因历史密文解密失败；工具预检另行 fail-closed。
                report["model_registry"]["missing_key_application_behavior"] = "new temporary key generated, historical decrypt fails; never accepted as restored"
                redis_process = start_redis(stack, redis_server, redis_restored, new_redis_port, output / "redis-restored.log")
                restored_url = f"redis://127.0.0.1:{new_redis_port}/0"
                report["queue_restored_before_consume"] = asyncio.run(inspect_redis(restored_url))
                assert report["queue_restored_before_consume"] == report["queue_before"]["inventory"]
                report["checks"]["redis_stream_group_pel_status_identical"] = True
                report["queue_after"] = asyncio.run(queue_phase(restored, restored_url, restored=True))
                assert inventory(restored) == fixture["original_inventory"] - 1
                stop(redis_process)
                stamp = time.monotonic()
                report["index"] = asyncio.run(rebuild_index(restored, catalog_restored / "catalog-demo.jsonl.gz", settings))
                report["index_rebuild_seconds"] = round(time.monotonic() - stamp, 3)
                new_env = isolated_env(restored, mock_url=mock_url)
                new_env.update(IDENTITY_MODE="hmac", IDENTITY_HMAC_SECRET=HMAC_FIXTURE)
                requests_before = httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json()
                stamp = time.monotonic()
                api = start(stack, "app.presentation.server:app", api_port, new_env, output / "api-restored.log")
                report["api_ready_seconds"] = round(time.monotonic() - stamp, 3)
                report["ready_seconds"] = round(time.monotonic() - recovery_started, 3)
                report["http_after_restore"] = verify_http(client, restored, fixture, expired, policy)
                requests_after = httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json()
                assert requests_before == requests_after
                report["checks"]["replay_does_not_rerun_chat_model"] = True
                report["mock_chat_requests"] = requests_after["requests"]
                stop(api)
                assert api.poll() in (0, -signal.SIGTERM) and redis_process.poll() == 0
                stop(mock_process)
                assert mock_process.poll() in (0, -signal.SIGTERM)
                report["checks"]["all_owned_processes_stopped"] = True
                report["recovery_group"] = {"sqlite_databases": len(counts), "redis": "stopped multipart AOF",
                    "source_config": "same version and fingerprints", "catalog_products": 5700,
                    "model_master_key": "original public fixture preserved only in temporary snapshot",
                    "source_additional_operation_B": "present", "restored_operation_B": "absent", "production_RPO": "unknown", "production_RTO": "unknown"}
        final_source = source_fingerprint()
        dump(output / "source-after.json", final_source)
        report["source_stable"] = initial_source == final_source
        report["git_head"] = initial_source["git_head"]
        report["source_sha256"] = initial_source["sha256"]
        if require_source_stable:
            assert report["source_stable"], "冻结源码在演练期间发生变化"
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        report["scenario_total_seconds"] = round(time.monotonic() - started, 3)
        dump(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--redis-server", type=Path, default=Path(shutil.which("redis-server") or "/tmp/voyacart-redis/src/redis-server"))
    parser.add_argument("--require-source-stable", action="store_true")
    args = parser.parse_args()
    result = run(args.output.resolve(), redis_server=args.redis_server.resolve(), require_source_stable=args.require_source_stable)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
