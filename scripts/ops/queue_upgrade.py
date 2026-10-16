"""停写期间的旧队列 TTL/期限迁移；默认只读，不能恢复已经遗失的任务身份。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import os
from pathlib import Path

from app.domain.queue.ports.task_queue import IntentTask
from app.infrastructure.queue.redis_stream_queue import (
    _DEADLINE_INDEX, _LARGE_STREAM, _STATUS_PREFIX, _STREAM,
)

_NONTERMINAL = {"queued", "running", "retrying"}
_STREAMS = (_STREAM, _LARGE_STREAM)
# 先检查所有类型/来源，再写入。Lua 运行时错误不会自动回滚。
_APPLY = """
local function stream_version(key)
  local kind = redis.call('TYPE', key).ok
  if kind == 'none' then return 'absent' end
  if kind ~= 'stream' then return 'wrongtype' end
  local values = redis.call('XINFO', 'STREAM', key)
  local info = {}
  for i = 1, #values, 2 do info[values[i]] = values[i + 1] end
  return tostring(info['length']) .. '|' .. info['last-generated-id'] .. '|' .. tostring(info['entries-added'])
end
if stream_version(KEYS[4]) ~= ARGV[8] or stream_version(KEYS[5]) ~= ARGV[9] then return -4 end
local kind = redis.call('TYPE', KEYS[3]).ok
if kind ~= 'none' and kind ~= 'zset' then return -3 end
local raw = redis.call('GET', KEYS[1])
if not raw or raw ~= ARGV[1] then return -1 end
local entries = redis.call('XRANGE', KEYS[2], ARGV[2], ARGV[2])
if #entries ~= 1 then return -2 end
local found = nil
for i = 1, #entries[1][2], 2 do
  if entries[1][2][i] == 'payload' then found = entries[1][2][i + 1] end
end
if found ~= ARGV[3] then return -2 end
redis.call('SET', KEYS[1], ARGV[4])
if ARGV[6] == 'running' then
  redis.call('ZREM', KEYS[3], ARGV[5])
else
  redis.call('ZADD', KEYS[3], ARGV[7], ARGV[5])
end
return 1
"""


def _fingerprint(task: IntentTask) -> str:
    payload = {key: value for key, value in task.to_dict().items()
               if key not in {"enqueued_at", "priority", "traceparent", "tracestate", "request_id"}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _validated(raw: str | None, payload: str, message_id: str, max_age: float):
    """只以仍存在的旧身份/完整消息交叉核验；不接受客户端时间作为期限起点。"""
    if raw is None:
        raise ValueError("missing_identity")
    try:
        status = json.loads(raw)
        fields = json.loads(payload)
        if not isinstance(status, dict) or not isinstance(fields, dict):
            raise ValueError
        task = IntentTask.from_dict(fields)
    except (ValueError, TypeError, KeyError):
        raise ValueError("malformed_state_or_payload") from None
    if not isinstance(task.task_id, str) or not task.task_id or status.get("task_id") != task.task_id:
        raise ValueError("task_identity_mismatch")
    if status.get("state") in {"done", "failed"}:
        return task, status, None
    if status.get("state") not in _NONTERMINAL:
        raise ValueError("unsupported_state")
    if status.get("payload_fingerprint") != _fingerprint(task):
        raise ValueError("payload_fingerprint_mismatch")
    if status.get("task") is not None:
        try:
            if _fingerprint(IntentTask.from_dict(status["task"])) != _fingerprint(task):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("stored_task_mismatch") from None
    try:
        accepted = int(message_id.split("-", 1)[0]) / 1000
        deadline = status.get("queue_deadline_at", accepted + max_age)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("invalid_server_deadline") from None
    updated = dict(status)
    updated.setdefault("accepted_at", accepted)
    updated.setdefault("queue_deadline_at", deadline)
    # 完整消息来自 Stream，语义指纹已经验证。原执行状态/结果/归属保持原样。
    updated.setdefault("task", task.to_dict())
    return task, status, updated


async def _stream_version(client, stream):
    if not await client.exists(stream):
        return "absent"
    info = await client.xinfo_stream(stream)
    if "entries-added" not in info:
        raise ValueError("来源版本核验需要 Redis 7 的 entries-added")
    return f"{info['length']}|{info['last-generated-id']}|{info['entries-added']}"


async def upgrade_queue(client, *, max_age_seconds: float = 900, limit: int = 200,
                        cursors: dict | None = None, apply: bool = False,
                        quiesced: bool = False) -> dict:
    if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds) or max_age_seconds <= 0):
        raise ValueError("max_age_seconds 必须为有限正数")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit 必须为 1 到 1000 的整数")
    if apply and not quiesced:
        raise ValueError("apply 要求操作员确认所有 API/worker/automation/维护写入者已停止")
    cursors = dict(cursors or {})
    if set(cursors) - set(_STREAMS) or any(
            not isinstance(v, str) or (v != "END" and not re.fullmatch(r"[0-9]+-[0-9]+", v))
            for v in cursors.values()):
        raise ValueError("非法 stream cursor")
    source_versions = {stream: await _stream_version(client, stream) for stream in _STREAMS}
    source_entries = {stream: await client.xrange(stream, count=1001) for stream in _STREAMS}
    source_after = {stream: await _stream_version(client, stream) for stream in _STREAMS}
    source_counts = {stream: len(entries) for stream, entries in source_entries.items()}
    source_inventory = {}
    malformed_sources = False
    for stream, entries in source_entries.items():
        for message_id, fields in entries:
            try:
                source_id = json.loads(fields.get("payload", ""))["task_id"]
                if not isinstance(source_id, str) or not source_id:
                    raise ValueError
                source_inventory.setdefault(source_id, []).append((stream, message_id))
            except (ValueError, TypeError, KeyError):
                malformed_sources = True
    report = {"schema": "voyacart-queue-upgrade-v1", "mode": "apply" if apply else "dry_run",
              "quiescence_is_operator_assertion": bool(quiesced), "scanned": 0, "eligible": 0,
              "applied": 0, "terminal_unchanged": 0, "terminal_with_ttl_unchanged": 0, "already_current": 0,
              "blocked": [], "next_cursors": dict(cursors), "batch_complete": False,
              "max_age_seconds": max_age_seconds, "contains_payload": False,
              "source_inventory_reads": sum(source_counts.values()),
              "source_inventory_complete": all(count <= 1000 for count in source_counts.values()),
              "source_inventory_stable": source_versions == source_after,
              "limits": ["只扫描两条任务流；不证明孤立状态键完整", "不重建已遗失身份，不延长原有期限",
                         "停写声明不自动证明无写入者；必须先保留一致恢复快照",
                         "终态旧TTL不在本工具改写范围，不能据scan_complete宣称所有历史幂等键已永久保留",
                         "每次先读取每流最多1001项核对唯一来源，任一流超过1000项拒绝迁移"]}
    if not report["source_inventory_stable"]:
        report["blocked"].append({"reason": "source_inventory_changed"})
        report["status"] = "blocked"
        return report
    if not report["source_inventory_complete"] or malformed_sources:
        report["blocked"].append({"reason": "source_inventory_too_large" if not report["source_inventory_complete"] else "source_inventory_malformed"})
        report["status"] = "blocked"
        return report
    # 每个 stream 独立有界，最多 2*limit 项；cursor=END 不重新遍历已完成的流。
    for stream in _STREAMS:
        cursor = cursors.get(stream, "0-0")
        if cursor == "END":
            report["next_cursors"][stream] = "END"
            continue
        entries = await client.xrange(stream, min="-" if cursor == "0-0" else "(" + cursor, count=limit)
        report["next_cursors"][stream] = entries[-1][0] if len(entries) == limit else "END"
        for message_id, fields in entries:
            report["scanned"] += 1
            # 报告只放来源 ID 和原因，绝不放 task_id/买家/查询/凭据。
            location = {"stream": stream, "message_id": message_id}
            try:
                payload = fields.get("payload", "")
                parsed = json.loads(payload)
                task_id = parsed["task_id"]
                if not isinstance(task_id, str) or not task_id:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                report["blocked"].append({**location, "reason": "malformed_payload"})
                continue
            raw = await client.get(_STATUS_PREFIX + task_id)
            try:
                task, status, updated = _validated(raw, payload, message_id, max_age_seconds)
            except ValueError as error:
                report["blocked"].append({**location, "reason": str(error)})
                continue
            if updated is not None:
                bound_id, bound_stream = status.get("message_id"), status.get("stream")
                if bound_id:
                    if (bound_stream, bound_id) != (stream, message_id):
                        report["blocked"].append({**location, "reason": "status_source_mismatch"})
                        continue
                elif source_inventory.get(task_id) != [(stream, message_id)]:
                    report["blocked"].append({**location, "reason": "ambiguous_task_source"})
                    continue
            if updated is None:
                report["terminal_unchanged"] += 1
                if await client.pttl(_STATUS_PREFIX + task_id) >= 0:
                    report["terminal_with_ttl_unchanged"] += 1
                continue
            ttl = await client.pttl(_STATUS_PREFIX + task_id)
            # Lua 仍校验原始字符串，PTTL 不用于决定新截止时间。
            if ttl == -2:
                report["blocked"].append({**location, "reason": "identity_expired_during_scan"})
                continue
            expected_score = None if status["state"] == "running" else updated["queue_deadline_at"]
            score = await client.zscore(_DEADLINE_INDEX, task_id)
            if raw == json.dumps(updated, ensure_ascii=False) and ttl == -1 and score == expected_score:
                report["already_current"] += 1
                continue
            report["eligible"] += 1
            if apply:
                result = await client.eval(_APPLY, 5, _STATUS_PREFIX + task_id, stream, _DEADLINE_INDEX, _STREAM, _LARGE_STREAM,
                    raw, message_id, payload, json.dumps(updated, ensure_ascii=False), task_id,
                    status["state"], updated["queue_deadline_at"], source_versions[_STREAM], source_versions[_LARGE_STREAM])
                if result != 1:
                    report["blocked"].append({**location, "reason": {
                        -1: "state_changed_or_missing", -2: "stream_changed_or_missing",
                        -3: "deadline_index_wrongtype", -4: "source_inventory_changed"}.get(result, "cas_failed")})
                else:
                    report["applied"] += 1
    report["batch_complete"] = all(report["next_cursors"].get(s) == "END" for s in _STREAMS)
    report["status"] = "blocked" if report["blocked"] else ("scan_complete" if report["batch_complete"] else "more_batches")
    return report


async def _main(args):
    import redis.asyncio as redis
    # 不加载应用 Settings/.env，不需要模型密钥，不打印 URI（可能含密码）。
    url = os.environ.get("REDIS_URL", "")
    if not url:
        raise ValueError("请显式设置 REDIS_URL；本工具不会猜测生产地址")
    if args.output.exists():
        raise ValueError("报告路径已存在，拒绝覆盖历史结果")
    previous = json.loads(args.cursor_file.read_text()) if args.cursor_file else {}
    if previous and previous.get("status") == "blocked":
        raise ValueError("前批次仍有阻塞，必须解决后从头重扫，不能用后页掩盖")
    if previous and previous.get("mode") != ("apply" if args.apply else "dry_run"):
        raise ValueError("不能复用另一阶段的游标；apply 必须从两条流起点重新扫描")
    cursors = previous.get("next_cursors", {})
    client = redis.from_url(url, decode_responses=True)
    try:
        result = await upgrade_queue(client, max_age_seconds=args.max_age_seconds, limit=args.limit,
                                     cursors=cursors, apply=args.apply, quiesced=args.quiesced)
    finally:
        await client.aclose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["blocked"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cursor-file", type=Path)
    parser.add_argument("--limit", type=int, default=200, help="每流每批 1–1000 项")
    parser.add_argument("--max-age-seconds", type=float, default=900)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    try:
        code = asyncio.run(_main(args))
    except Exception as error:
        # 外部连接异常文本可能包含地址或凭据，仅公开异常类型。
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__,
                          "message": "迁移未完成，请核对输入和依赖；不能据此放行升级"}, ensure_ascii=False))
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
