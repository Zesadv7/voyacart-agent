"""仅在临时真实 Redis 验证旧身份迁移；无生产连接。"""
import json

import pytest

from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _STREAM, _LARGE_STREAM, _STATUS_PREFIX, _DEADLINE_INDEX
from scripts.ops.queue_upgrade import upgrade_queue
from tests.test_queue_reliability import isolated_redis_url, real_redis, task


async def legacy(client, name="legacy", state="queued", priority=0):
    queue = RedisStreamTaskQueue(client)
    await queue.ensure_group()
    original = task(name, priority=priority)
    await queue.enqueue(original)
    key = _STATUS_PREFIX + name
    status = json.loads(await client.get(key))
    for field in ("task", "accepted_at", "queue_deadline_at"):
        status.pop(field)
    status["state"] = state
    await client.set(key, json.dumps(status), ex=3600)
    await client.zrem(_DEADLINE_INDEX, name)
    return queue, original, key


@pytest.mark.parametrize("state", ["queued", "running", "retrying"])
async def test_dry_run_and_cas_apply_preserve_identity_and_stream(real_redis, state):
    queue, original, key = await legacy(real_redis, state=state)
    before = await real_redis.get(key)
    dry = await upgrade_queue(real_redis)
    assert dry["eligible"] == 1 and dry["applied"] == 0
    assert await real_redis.get(key) == before and await real_redis.ttl(key) > 0
    result = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert result["applied"] == 1 and not result["blocked"] and result["batch_complete"]
    status = json.loads(await real_redis.get(key))
    entry = (await real_redis.xrange(_STREAM))[0][0]
    accepted = int(entry.split("-")[0]) / 1000
    assert status["accepted_at"] == accepted and status["queue_deadline_at"] == accepted + 900
    assert status["task"] == original.to_dict() and status["state"] == state
    assert status["payload_fingerprint"] == json.loads(before)["payload_fingerprint"]
    assert await real_redis.pttl(key) == -1
    assert await real_redis.zscore(_DEADLINE_INDEX, original.task_id) == (None if state == "running" else accepted + 900)
    await queue.enqueue(original)
    assert await real_redis.xlen(_STREAM) == 1
    replay = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert replay["already_current"] == 1 and replay["applied"] == 0
    assert original.buyer_id not in json.dumps(result) and original.raw_query not in json.dumps(result)


async def test_apply_requires_operator_quiescence(real_redis):
    _, _, key = await legacy(real_redis)
    with pytest.raises(ValueError, match="停止"):
        await upgrade_queue(real_redis, apply=True)
    assert await real_redis.ttl(key) > 0


@pytest.mark.parametrize("damage,reason", [("delete", "missing_identity"), ("fingerprint", "payload_fingerprint_mismatch"),
                                           ("task", "stored_task_mismatch"), ("state", "unsupported_state"),
                                           ("deadline", "invalid_server_deadline")])
async def test_invalid_legacy_state_is_never_rebuilt(real_redis, damage, reason):
    _, original, key = await legacy(real_redis)
    status = json.loads(await real_redis.get(key))
    if damage == "delete":
        await real_redis.delete(key)
    else:
        if damage == "fingerprint": status["payload_fingerprint"] = "wrong"
        if damage == "task": status["task"] = {**original.to_dict(), "buyer_id": "different"}
        if damage == "state": status["state"] = "unknown"
        if damage == "deadline": status["queue_deadline_at"] = float("nan")
        await real_redis.set(key, json.dumps(status), ex=3600)
    before = await real_redis.get(key)
    result = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert result["blocked"][0]["reason"] == reason and result["applied"] == 0
    assert await real_redis.get(key) == before


async def test_existing_deadline_never_extended_and_terminal_unchanged(real_redis):
    _, _, key = await legacy(real_redis)
    status = json.loads(await real_redis.get(key)); status["queue_deadline_at"] = 1
    await real_redis.set(key, json.dumps(status), ex=10)
    _, _, terminal_key = await legacy(real_redis, "terminal", state="done")
    terminal_raw = await real_redis.get(terminal_key)
    result = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert result["applied"] == 1 and result["terminal_unchanged"] == 1
    assert result["terminal_with_ttl_unchanged"] == 1
    assert json.loads(await real_redis.get(key))["queue_deadline_at"] == 1
    assert await real_redis.get(terminal_key) == terminal_raw


async def test_scan_cursor_is_bounded_and_never_restarts_finished_stream(real_redis):
    for i in range(3): await legacy(real_redis, f"normal-{i}")
    await legacy(real_redis, "large", priority=1)
    first = await upgrade_queue(real_redis, limit=2)
    assert first["scanned"] == 3 and not first["batch_complete"]
    second = await upgrade_queue(real_redis, limit=2, cursors=first["next_cursors"])
    assert second["scanned"] == 1 and second["batch_complete"]
    assert (await upgrade_queue(real_redis, cursors=second["next_cursors"]))["scanned"] == 0


async def test_changed_state_between_read_and_apply_does_not_mutate(real_redis):
    _, _, key = await legacy(real_redis)
    before = await real_redis.get(key)
    class RacingClient:
        def __getattr__(self, name): return getattr(real_redis, name)
        async def eval(self, *args):
            await real_redis.delete(key)
            return await real_redis.eval(*args)
    result = await upgrade_queue(RacingClient(), apply=True, quiesced=True)
    assert before and result["blocked"][0]["reason"] == "state_changed_or_missing"
    assert await real_redis.get(key) is None


async def test_index_wrongtype_leaves_legacy_value_and_ttl_unchanged(real_redis):
    _, _, key = await legacy(real_redis)
    before = await real_redis.get(key)
    await real_redis.delete(_DEADLINE_INDEX)
    await real_redis.set(_DEADLINE_INDEX, "wrong-type")
    with pytest.raises(Exception):
        await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert await real_redis.get(key) == before and await real_redis.ttl(key) > 0


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 1001}, {"limit": True},
                                      {"max_age_seconds": 0}, {"max_age_seconds": float("inf")},
                                      {"cursors": {"foreign": "0-0"}}, {"cursors": {_STREAM: "-"}}])
async def test_invalid_options_rejected_without_io(real_redis, options):
    with pytest.raises(ValueError): await upgrade_queue(real_redis, **options)


async def test_stream_mutation_between_scan_and_apply_is_rejected(real_redis):
    _, _, key = await legacy(real_redis)
    before = await real_redis.get(key)
    class RacingClient:
        def __getattr__(self, name): return getattr(real_redis, name)
        async def eval(self, *args):
            await real_redis.delete(_STREAM)
            return await real_redis.eval(*args)
    result = await upgrade_queue(RacingClient(), apply=True, quiesced=True)
    assert result["applied"] == 0 and result["blocked"][0]["reason"] == "source_inventory_changed"
    assert await real_redis.get(key) == before and await real_redis.pttl(key) > 0


async def test_index_type_changes_after_preflight_never_half_writes(real_redis):
    _, _, key = await legacy(real_redis)
    before = await real_redis.get(key)
    class RacingClient:
        def __getattr__(self, name): return getattr(real_redis, name)
        async def eval(self, *args):
            await real_redis.delete(_DEADLINE_INDEX)
            await real_redis.set(_DEADLINE_INDEX, "wrong-type")
            return await real_redis.eval(*args)
    result = await upgrade_queue(RacingClient(), apply=True, quiesced=True)
    assert result["blocked"][0]["reason"] == "deadline_index_wrongtype"
    assert await real_redis.get(key) == before and await real_redis.pttl(key) > 0


async def test_cli_rejects_cross_phase_cursor_before_connecting(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.ops.queue_upgrade import _main
    cursor = tmp_path / "previous.json"
    cursor.write_text(json.dumps({"mode": "dry_run", "next_cursors": {_STREAM: "END", _LARGE_STREAM: "END"}}))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    args = SimpleNamespace(output=tmp_path / "report.json", cursor_file=cursor, apply=True, quiesced=True, max_age_seconds=900, limit=200)
    with pytest.raises(ValueError, match="另一阶段"):
        await _main(args)
    assert not args.output.exists()


async def test_duplicate_unbound_legacy_identity_is_ambiguous_not_earliest(real_redis):
    _, original, key = await legacy(real_redis)
    await real_redis.xadd(_STREAM, {"payload": json.dumps(original.to_dict(), ensure_ascii=False)})
    before = await real_redis.get(key)
    result = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert result["applied"] == 0 and {x["reason"] for x in result["blocked"]} == {"ambiguous_task_source"}
    assert await real_redis.get(key) == before and await real_redis.pttl(key) > 0


async def test_bound_new_delivery_never_takes_older_stream_deadline(real_redis):
    _, original, key = await legacy(real_redis)
    old_id = (await real_redis.xrange(_STREAM))[0][0]
    new_id = await real_redis.xadd(_STREAM, {"payload": json.dumps(original.to_dict(), ensure_ascii=False)}, id=str(int(old_id.split("-")[0]) + 1) + "-0")
    status = json.loads(await real_redis.get(key)); status.update(state="running", stream=_STREAM, message_id=new_id)
    await real_redis.set(key, json.dumps(status), ex=3600)
    result = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert result["applied"] == 1 and result["blocked"][0]["reason"] == "status_source_mismatch"
    assert json.loads(await real_redis.get(key))["queue_deadline_at"] == int(new_id.split("-")[0]) / 1000 + 900


async def test_new_stream_message_after_inventory_rejects_apply(real_redis):
    _, original, key = await legacy(real_redis)
    before = await real_redis.get(key)
    class RacingClient:
        def __getattr__(self, name): return getattr(real_redis, name)
        async def eval(self, *args):
            await real_redis.xadd(_STREAM, {"payload": json.dumps(original.to_dict())})
            return await real_redis.eval(*args)
    result = await upgrade_queue(RacingClient(), apply=True, quiesced=True)
    assert result["blocked"][0]["reason"] == "source_inventory_changed"
    assert await real_redis.get(key) == before and await real_redis.pttl(key) > 0


async def test_source_inventory_larger_than_guard_is_fail_closed(real_redis):
    _, original, key = await legacy(real_redis)
    pipe = real_redis.pipeline()
    for i in range(1000): pipe.xadd(_STREAM, {"payload": json.dumps({**original.to_dict(), "task_id": f"large-{i}"})})
    await pipe.execute()
    before = await real_redis.get(key)
    result = await upgrade_queue(real_redis, apply=True, quiesced=True)
    assert result["blocked"][0]["reason"] == "source_inventory_too_large" and result["scanned"] == 0
    assert await real_redis.get(key) == before


async def test_cli_blocked_page_cannot_be_hidden_by_later_cursor(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.ops.queue_upgrade import _main
    cursor = tmp_path / "blocked.json"
    cursor.write_text(json.dumps({"mode": "apply", "status": "blocked", "next_cursors": {_STREAM: "1-0"}}))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    args = SimpleNamespace(output=tmp_path / "report.json", cursor_file=cursor, apply=True, quiesced=True, max_age_seconds=900, limit=200)
    with pytest.raises(ValueError, match="前批次"):
        await _main(args)


async def test_equal_length_stream_replacement_after_inventory_is_rejected(real_redis):
    _, original, key = await legacy(real_redis)
    before = await real_redis.get(key)
    old_id = (await real_redis.xrange(_STREAM))[0][0]
    class RacingClient:
        def __getattr__(self, name): return getattr(real_redis, name)
        async def eval(self, *args):
            await real_redis.xdel(_STREAM, old_id)
            await real_redis.xadd(_STREAM, {"payload": json.dumps(original.to_dict())})
            return await real_redis.eval(*args)
    result = await upgrade_queue(RacingClient(), apply=True, quiesced=True)
    assert result["blocked"][0]["reason"] == "source_inventory_changed"
    assert await real_redis.get(key) == before and await real_redis.pttl(key) > 0


@pytest.mark.parametrize("cursor", ["invalid", "١-٢", "1-2-3", "-1", "1-", "1 2-3"])
async def test_invalid_large_cursor_rejects_before_io_and_preserves_normal(real_redis, cursor):
    _, _, key = await legacy(real_redis)
    before = await real_redis.get(key)
    class NoIO:
        def __getattr__(self, name):
            pytest.fail("非法第二条流游标不得进行任何 Redis IO: " + name)
    with pytest.raises(ValueError, match="cursor"):
        await upgrade_queue(NoIO(), apply=True, quiesced=True, cursors={_STREAM: "0-0", _LARGE_STREAM: cursor})
    assert await real_redis.get(key) == before and await real_redis.ttl(key) > 0
