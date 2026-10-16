"""恢复驱动的只读聚合、环境隔离与合成身份合同；全链路由独立演练执行。"""
from pathlib import Path
import os
import sqlite3

import pytest

from scripts.ops import mock_recovery as recovery


def test_table_counts_checks_integrity_and_excludes_derived_index(tmp_path):
    with sqlite3.connect(tmp_path / "voyacart.db") as db:
        db.execute("CREATE TABLE marker(id INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO marker VALUES(?)", [(1,), (2,)])
    (tmp_path / "qdrant").mkdir()
    (tmp_path / "qdrant/storage.sqlite3").write_text("派生文件不属于应用 SQLite 清单")
    assert recovery.table_counts(tmp_path) == {"voyacart.db": {"marker": 2}}


def test_table_counts_rejects_corrupt_database(tmp_path):
    (tmp_path / "models.sqlite3").write_bytes(b"not-a-sqlite-database")
    with pytest.raises(sqlite3.DatabaseError):
        recovery.table_counts(tmp_path)


def test_clean_environment_drops_service_credentials_and_restores_parent(monkeypatch, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(recovery, "ROOT", root)
    monkeypatch.setenv("LLM_API_KEY", "outside-synthetic-sentinel")
    monkeypatch.setenv("HTTP_PROXY", "http://outside.invalid")
    monkeypatch.setenv("MODEL_MASTER_KEY", "outside-synthetic-sentinel")
    before = dict(os.environ)
    with recovery.clean_environment(tmp_path / "state", "http://127.0.0.1:1234/v1") as env:
        assert "HTTP_PROXY" not in os.environ and "MODEL_MASTER_KEY" not in os.environ
        assert os.environ["LLM_API_KEY"] == "ci-local-placeholder"
        assert os.environ["IDENTITY_HMAC_SECRET"] == recovery.HMAC_FIXTURE
        assert env["REDIS_URL"] == "" and env["AUTOMATION_WORKER_MODE"] == "off"
    assert dict(os.environ) == before


@pytest.mark.parametrize("symlink", [False, True])
def test_clean_environment_refuses_project_dotenv_without_reading(monkeypatch, tmp_path, symlink):
    monkeypatch.setattr(recovery, "ROOT", tmp_path)
    path = tmp_path / ".env"
    if symlink:
        path.symlink_to(tmp_path / "does-not-exist")
    else:
        path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RuntimeError, match="干净工作树"):
        with recovery.clean_environment(tmp_path / "state", "http://127.0.0.1:1234/v1"):
            pytest.fail("不应进入环境")


def test_request_ids_and_identity_are_public_synthetic_constants():
    body = recovery.request_body("m6-test", session="m6-session")
    assert body["runId"] == "m6-test" and body["threadId"] == "m6-session"
    assert body["forwardedProps"] == {"buyerId": "m6-synthetic-buyer"}
    assert body["state"] == {} and body["tools"] == []


async def test_auxiliary_seed_uses_real_stores_and_short_lived_confirmation(monkeypatch, tmp_path):
    from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.infrastructure.persistence.sql.repositories import create_engine
    from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
    async def embed(_self, texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(OpenAIEmbeddingClient, "embed_batch", embed)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'voyacart.db'}")
    try:
        await SqlTradeStore(engine).initialize_inventory(await InMemoryProductRepository().list_all())
    finally:
        await engine.dispose()
    with recovery.clean_environment(tmp_path, "http://127.0.0.1:1/v1"):
        confirmation = await recovery.seed_auxiliary(tmp_path)
    assert confirmation["status"] == "pending"
    counts = recovery.table_counts(tmp_path)
    for name in ("buyer_favorites.db", "buyer_skills.db", "capabilities.db", "shopping_forms.db", "context_evidence.db", "buyer_memory.db"):
        assert sum(counts[name].values()) > 0


async def test_queue_fixture_preserves_accepted_pending_and_no_duplicate_execution(tmp_path):
    from contextlib import ExitStack
    import asyncio
    import shutil
    binary = Path(os.environ.get("VOYACART_REDIS_SERVER_BIN") or shutil.which("redis-server") or "/tmp/voyacart-redis/src/redis-server")
    if not binary.is_file():
        pytest.skip("需要已有 redis-server；不安装，不使用用户 Redis")
    with ExitStack() as stack:
        port = recovery.free_port()
        recovery.start_redis(stack, binary, tmp_path / "redis", port, tmp_path / "redis.log")
        url = f"redis://127.0.0.1:{port}/0"
        first = await recovery.queue_phase(tmp_path / "state", url)
        assert first["agent_calls"] == 1 and first["archive_messages"] == 1
        assert first["inventory"]["statuses"]["m6-queue-pending"]["state"] == "queued"
        await asyncio.sleep(.02)
        second = await recovery.queue_phase(tmp_path / "state", url, restored=True)
        assert second["agent_calls"] == 1 and second["old_owner_rejected"]
        assert second["queued_completed"] and second["duplicate_delivery_no_reexecution"]


def test_source_fingerprint_binds_deploy_knowledge_and_frontend_inputs():
    files = recovery.source_fingerprint()["files"]
    for name in ("Dockerfile", ".dockerignore", ".env.example", "Makefile", "knowledge/manifest.jsonl",
                 "frontend/package-lock.json", "frontend/vite.config.ts", "frontend/tsconfig.json",
                 "frontend/Dockerfile", "frontend/nginx.conf", "frontend/index.html"):
        assert name in files
    assert not any("__pycache__" in name or name.endswith(".tsbuildinfo") for name in files)
