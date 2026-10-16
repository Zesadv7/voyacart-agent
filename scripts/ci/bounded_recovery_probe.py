"""隔离的 M5 观察驱动：真实 Redis/SQLite/HTTP，合成 Agent；不调用外部模型。

同一驱动可通过 --source-root 对冻结基线和候选执行。不会修改被测源码。
负载仅度量运行/投递边界，不代表完整目录、真实模型或生产容量。
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve()


def dump(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def supported(cls, options):
    parameters = inspect.signature(cls).parameters
    return {key: value for key, value in options.items() if key in parameters}


def fingerprint(root):
    files = sorted(p for folder in ("app", "tests", "scripts") for p in (root / folder).rglob("*.py"))
    files += [root / p for p in ("uv.lock", "pyproject.toml", "data/catalog-demo.jsonl.gz")]
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    return {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), "files": hashes}


def body(run="r1", session=None):
    return {"threadId": session or run + "-session", "runId": run, "messages": [{"id": "u-" + run,
        "role": "user", "content": "查询合成旅行背包"}], "state": {}, "tools": [], "context": [],
        "forwardedProps": {"buyerId": "probe-buyer"}}


def frames(text):
    result = []
    for part in text.replace("\r\n", "\n").split("\n\n"):
        lines = part.splitlines()
        data = next((line[5:].strip() for line in lines if line.startswith("data:")), None)
        if data:
            result.append({"id": next((line[3:].strip() for line in lines if line.startswith("id:")), ""),
                           "event": json.loads(data)})
    return result


class SyntheticAgent:
    def __init__(self, *, block=False, delay=0, marker=None, metrics_path=None):
        self.block, self.delay, self.marker = block, delay, marker
        self.metrics_path = metrics_path
        self.release = asyncio.Event()
        self.calls = self.active = self.peak = 0
        self.timeline = []

    def persist_metrics(self):
        if self.metrics_path:
            temporary = Path(str(self.metrics_path) + ".tmp")
            dump(temporary, {"calls": self.calls, "active": self.active, "active_peak": self.peak, "timeline": self.timeline})
            temporary.replace(self.metrics_path)

    async def handle_intent(self, intent, event_observer, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        entered = time.monotonic()
        self.timeline.append({"session": intent.shopping_session_id, "entered": entered})
        self.persist_metrics()
        if self.marker:
            Path(self.marker).write_text(intent.shopping_session_id)
        try:
            event_observer(SimpleNamespace(type="REPLY_START"))
            event_observer(SimpleNamespace(type="TEXT_BLOCK_START", block_id="synthetic-text"))
            if self.block:
                await self.release.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            event_observer(SimpleNamespace(type="TEXT_BLOCK_DELTA", block_id="synthetic-text", delta="合成结果"))
            event_observer(SimpleNamespace(type="TEXT_BLOCK_END", block_id="synthetic-text"))
            return SimpleNamespace(final_text="合成结果", error=None)
        finally:
            self.active -= 1
            self.timeline.append({"session": intent.shopping_session_id, "exited": time.monotonic()})
            self.persist_metrics()


async def eventually(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("隔离探针等待状态变化超时")
        await asyncio.sleep(.01)


def imports(root):
    sys.path.insert(0, str(root))
    from ag_ui.core import RunAgentInput
    from app.application.agent_runtime import AGUIRuntime
    from app.infrastructure.ag_ui_journal import AGUIJournal
    from app.infrastructure.queue import redis_stream_queue as queue_module
    from app.presentation.ag_ui import parse_intent, register_ag_ui_routes
    from app.worker import execute_ag_ui_task
    from fastapi import FastAPI
    import httpx
    import redis.asyncio as redis
    import app.application.agent_runtime as runtime_module
    assert Path(runtime_module.__file__).resolve().is_relative_to(root), runtime_module.__file__
    return SimpleNamespace(**locals())


def make_runtime(m, path, agent, queue=None, **extras):
    options = {"max_active_runs": 2, "run_timeout_seconds": 20, "lease_seconds": .6,
               "heartbeat_seconds": .05, "task_queue": queue, "queue_max_pending": 2, **extras}
    return m.AGUIRuntime(m.AGUIJournal(path), agent, **supported(m.AGUIRuntime, options))


def make_queue(m, redis, **extras):
    return m.queue_module.RedisStreamTaskQueue(redis, **supported(m.queue_module.RedisStreamTaskQueue,
        {"max_pending": 2, "max_age_seconds": 20, **extras}))


def application(m, runtime):
    app = m.FastAPI()
    m.register_ag_ui_routes(app, lambda: runtime.orchestrator, get_runtime=lambda: runtime)
    return app


def consumer(m, queue, runtime, stopping):
    async def handle(task):
        return await m.execute_ag_ui_task(task, runtime)
    return asyncio.create_task(queue.consume("probe-worker", handle, stopping.is_set, block_ms=10,
        concurrency=2, reclaim_idle_ms=150, lease_ms=800, heartbeat_interval_ms=30))


async def drained(m, client):
    return sum([(await client.xpending(stream, m.queue_module._GROUP))["pending"]
               for stream in (m.queue_module._STREAM, m.queue_module._LARGE_STREAM)]) == 0


async def terminal(runtime, run):
    try:
        row = await runtime.run(run, "probe-buyer")
        return row if row["status"] not in {"running", "queued"} else None
    except Exception:
        return None


async def observe(args, m, redis_url, directory):
    client = m.redis.from_url(redis_url, decode_responses=True)
    rows = []
    async def fresh(name):
        await client.flushdb()  # 只清理当前驱动新建的专用 Redis。
        path = directory / (name + ".db")
        queue = make_queue(m, client)
        await queue.ensure_group()
        return path, queue
    async def save(name, value):
        rows.append({"scenario": name, **value})
        dump(args.output / (name + ".json"), rows[-1])
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    # A1：真实 SQLite，直接 runtime 准入观察；不把直接异常包装成 HTTP 结论。
    path, _ = await fresh("A1-active-bound")
    agent = SyntheticAgent(block=True)
    runtime = make_runtime(m, path, agent)
    await runtime.startup()
    results = []
    try:
        for index in range(3):
            request = m.RunAgentInput.model_validate(body("bound-" + str(index)))
            try:
                result = await runtime.submit(request, m.parse_intent(request))
                results.append({"accepted": True, "status": result["status"]})
            except Exception as error:
                results.append({"accepted": False, "exception": type(error).__name__, "code": getattr(error, "code", None)})
        await eventually(lambda: agent.calls == sum(r["accepted"] for r in results))
        peak = agent.peak
        rejected_journal_absent = False
        try:
            await runtime.journal.run("bound-2", "probe-buyer")
        except Exception as error:
            rejected_journal_absent = type(error).__name__ == "JournalNotFound"
        agent.release.set()
        for index, result in enumerate(results):
            if result["accepted"]:
                await eventually(lambda i=index: terminal(runtime, "bound-" + str(i)))
        await save("A1-active-bound", {"level": "runtime+SQLite", "configured_bound": 2,
            "supported": "max_active_runs" in inspect.signature(m.AGUIRuntime).parameters,
            "submissions": results, "active_peak": peak, "third_journal_absent": rejected_journal_absent,
            "contract_passed": peak <= 2 and not results[2]["accepted"] and rejected_journal_absent})
    finally:
        agent.release.set()
        await runtime.shutdown()

    # A3：AGUI 与旧入口共用两条真实流。调用旧 helper，不声称路由 HTTP。
    path, queue = await fresh("A3-legacy-capacity")
    from app.presentation import server
    from app.application.agents.orchestrator import SubmitIntentInput
    runtime = make_runtime(m, path, SyntheticAgent(), queue)
    await runtime.startup()
    c = SimpleNamespace(task_queue=queue, settings=SimpleNamespace(queue_priority_enabled=False,
        queue_max_pending=2, agui_queue_max_pending=2), bus=SimpleNamespace(publish=lambda *a, **k: None))
    outcomes = []
    try:
        request = m.RunAgentInput.model_validate(body("mixed-agui"))
        await runtime.submit(request, m.parse_intent(request))
        for index in range(2):
            intent = SubmitIntentInput(shopping_session_id=f"legacy-{index}", buyer_id="probe-buyer",
                locale="zh-CN", currency="CNY", raw_query="合成查询")
            try:
                task_id = await server._enqueue(c, intent, request_id=f"legacy-{index}")
                outcomes.append({"accepted": True, "task_id": task_id})
            except Exception as error:
                outcomes.append({"accepted": False, "exception": type(error).__name__})
        depth = await queue.depth()
        await save("A3-legacy-capacity", {"level": "legacy enqueue helper+real Redis", "depth": depth,
            "legacy": outcomes, "contract_passed": depth <= 2 and not outcomes[1]["accepted"]})
    finally:
        await runtime.shutdown()

    # A4/A5/A6：只代理本实例的 Redis eval，在真实 Lua 前/后注入响应错误。
    for name, fault in (("A4-wrongtype", "wrongtype"), ("A5-response-lost", "after"), ("A6-before-enqueue", "before")):
        path, _ = await fresh(name)
        class BoundaryProxy:
            injected = False
            def __getattr__(self, key):
                return getattr(client, key)
            async def eval(self, script, *values):
                should = script == m.queue_module._ENQUEUE_SCRIPT and not self.injected and fault in {"after", "before"}
                if should:
                    self.injected = True
                    if fault == "before":
                        raise ConnectionError("probe-before-enqueue")
                result = await client.eval(script, *values)
                if should:
                    raise ConnectionError("probe-enqueue-response-lost")
                return result
        proxy = BoundaryProxy()
        queue = make_queue(m, proxy)
        await queue.ensure_group()
        api_agent, worker_agent = SyntheticAgent(), SyntheticAgent()
        api, worker = make_runtime(m, path, api_agent, queue), make_runtime(m, path, worker_agent)
        await api.startup(); await worker.startup()
        if fault == "wrongtype":
            await client.delete(m.queue_module._STREAM)
            await client.set(m.queue_module._STREAM, "probe-invalid-stream")
        try:
            async with m.httpx.AsyncClient(transport=m.httpx.ASGITransport(app=application(m, api)), base_url="http://probe") as http:
                response = await http.post("/commerce/ag-ui/run", json=body(name))
                (args.output / (name + "-first-response.txt")).write_text(response.text)
                first_api_calls = api_agent.calls
                initial_length = None if fault == "wrongtype" else await client.xlen(m.queue_module._STREAM)
                if fault == "wrongtype":
                    await client.delete(m.queue_module._STREAM)
                    await queue.ensure_group()
                stopping = asyncio.Event(); runner = consumer(m, queue, worker, stopping)
                replay = await http.post("/commerce/ag-ui/run", json=body(name))
                (args.output / (name + "-retry-response.txt")).write_text(replay.text)
                result = await eventually(lambda: terminal(api, name))
                await eventually(lambda: drained(m, client))
                stopping.set(); await asyncio.wait_for(runner, 5)
                expected_initial_length = 1 if fault == "after" else 0
                await save(name, {"level": "ASGI HTTP+real Redis+SQLite", "first_http_status": response.status_code,
                    "retry_after": response.headers.get("retry-after"), "initial_stream_length": initial_length,
                    "first_api_calls": first_api_calls, "api_calls": api_agent.calls, "worker_calls": worker_agent.calls,
                    "retry_status": replay.status_code, "terminal": result["status"], "pending_final": 0,
                    "stream_length_final": await client.xlen(m.queue_module._STREAM),
                    "contract_passed": response.status_code == 503 and first_api_calls == 0 and worker_agent.calls == 1
                        and (fault == "wrongtype" or initial_length == expected_initial_length)})
        finally:
            await api.shutdown(); await worker.shutdown()

    # T1：仅旧版 Lua 的 TTL 参数缩至 1s；候选不注入人工过期。
    name = "T1-nonterminal-identity"
    path, _ = await fresh(name)
    class ShortOldTTL:
        def __getattr__(self, key): return getattr(client, key)
        async def eval(self, script, *values):
            values = list(values)
            if script == m.queue_module._ENQUEUE_SCRIPT and "'EX'" in script:
                values[1 + int(values[0]) + 2] = 1
            return await client.eval(script, *values)
    queue = make_queue(m, ShortOldTTL()); await queue.ensure_group()
    runtime = make_runtime(m, path, SyntheticAgent(), queue); await runtime.startup()
    try:
        request = m.RunAgentInput.model_validate(body(name)); await runtime.submit(request, m.parse_intent(request))
        initial_ttl = await client.ttl(m.queue_module._STATUS_PREFIX + "agui:" + name)
        await asyncio.sleep(1.15)
        readable = await queue.get_task("agui:" + name) is not None
        await runtime.submit(request, m.parse_intent(request))
        length = await client.xlen(m.queue_module._STREAM)
        changed = body(name); changed["messages"][0]["content"] = "另一合成需求"
        changed = m.RunAgentInput.model_validate(changed)
        conflict = None
        try: await runtime.submit(changed, m.parse_intent(changed))
        except Exception as error: conflict = type(error).__name__
        await save(name, {"level": "real Redis+SQLite with old TTL shortened only", "initial_ttl": initial_ttl,
            "readable_after_old_ttl": readable, "stream_length_after_retry": length, "changed_payload": conflict,
            "contract_passed": readable and length == 1 and conflict == "JournalConflict"})
    finally: await runtime.shutdown()

    # T2/T4：候选明确的排队/整轮期限，旧版记录没有该合同的行为。
    name = "T2-queued-deadline"
    path, _ = await fresh(name)
    queue = make_queue(m, client, max_age_seconds=.2); await queue.ensure_group()
    api = make_runtime(m, path, SyntheticAgent(), queue); worker_agent = SyntheticAgent()
    worker_runtime = make_runtime(m, path, worker_agent)
    await api.startup(); await worker_runtime.startup()
    try:
        request = m.RunAgentInput.model_validate(body(name)); await api.submit(request,m.parse_intent(request))
        await asyncio.sleep(.3)
        if hasattr(api,"recover"): await api.recover()
        stopping = asyncio.Event(); runner = consumer(m,queue,worker_runtime,stopping)
        result = await eventually(lambda: terminal(api,name)); await eventually(lambda: drained(m,client))
        stopping.set(); await asyncio.wait_for(runner,5)
        events,_,_ = await api.journal.events(name,"probe-buyer")
        code = events[-1]["event"].get("code")
        await save(name,{"level":"real Redis+SQLite, short queue deadline", "supported":"max_age_seconds" in inspect.signature(m.queue_module.RedisStreamTaskQueue).parameters,
            "terminal":result["status"],"terminal_code":code,"worker_calls":worker_agent.calls,"pending_final":0,
            "contract_passed":worker_agent.calls == 0 and code == "QUEUE_DEADLINE_EXCEEDED"})
    finally: await api.shutdown(); await worker_runtime.shutdown()
    name = "T4-run-deadline"
    path,_ = await fresh(name); agent = SyntheticAgent(delay=.3)
    runtime = make_runtime(m,path,agent,run_timeout_seconds=.1); await runtime.startup()
    try:
        request = m.RunAgentInput.model_validate(body(name)); started=time.monotonic()
        await runtime.submit(request,m.parse_intent(request)); result=await eventually(lambda: terminal(runtime,name))
        events,_,_ = await runtime.journal.events(name,"probe-buyer"); code=events[-1]["event"].get("code")
        await save(name,{"level":"runtime+SQLite, synthetic cancellable Agent", "supported":"run_timeout_seconds" in inspect.signature(m.AGUIRuntime).parameters,
            "terminal":result["status"],"terminal_code":code,"elapsed_seconds":time.monotonic()-started,
            "agent_active_final":agent.active,"contract_passed":code == "RUN_DEADLINE_EXCEEDED" and agent.active == 0})
    finally: await runtime.shutdown()

    # F4：journal 终态已提交后分别丢状态提交和 ACK；同实例子类注入，不改全局。
    for phase in ("status", "ack"):
        name = "F4-terminal-before-" + phase
        path, _ = await fresh(name)
        class BoundaryQueue(m.queue_module.RedisStreamTaskQueue):
            injected = False
            async def set_status(self, status):
                if phase == "status" and status.state == "done" and not self.injected:
                    self.injected = True
                    raise ConnectionError("probe-journal-terminal-before-queue-status")
                return await super().set_status(status)
            async def _ack(self, delivery, lease=None):
                if phase == "ack" and not self.injected:
                    self.injected = True
                    raise ConnectionError("probe-queue-done-before-ack")
                return await super()._ack(delivery, lease)
        queue = BoundaryQueue(client, **supported(m.queue_module.RedisStreamTaskQueue, {"max_pending": 2, "max_age_seconds": 20}))
        await queue.ensure_group()
        agent = SyntheticAgent()
        api, worker = make_runtime(m, path, SyntheticAgent(), queue), make_runtime(m, path, agent)
        await api.startup(); await worker.startup()
        try:
            request = m.RunAgentInput.model_validate(body(name)); await api.submit(request, m.parse_intent(request))
            stopping = asyncio.Event(); runner = consumer(m, queue, worker, stopping)
            result = await eventually(lambda: terminal(api, name))
            await eventually(lambda: drained(m, client))
            stopping.set(); await asyncio.wait_for(runner, 5)
            events, _, _ = await api.journal.events(name, "probe-buyer")
            status = await queue.get_status("agui:" + name)
            dump(args.output / (name + "-events.json"), events)
            await save(name, {"level": "real Redis+SQLite, method-boundary fault", "injected": queue.injected,
                "terminal": result["status"], "queue_state": status.state, "agent_calls": agent.calls,
                "run_started_count": sum(e["event"]["type"] == "RUN_STARTED" for e in events), "pending_final": 0,
                "contract_passed": queue.injected and agent.calls == 1 and status.state == "done" and result["status"] == "completed"})
        finally:
            await api.shutdown(); await worker.shutdown()

    # F1/F7：SIGKILL 的是专用真实 worker 子进程，恢复不重新运行 Agent。
    name = "F1-worker-sigkill-replay"
    path, queue = await fresh(name)
    api = make_runtime(m, path, SyntheticAgent(), queue)
    await api.startup()
    marker = directory / "worker-entered"
    log = (args.output / (name + "-worker.log")).open("w")
    process = None
    try:
        request = m.RunAgentInput.model_validate(body(name)); await api.submit(request, m.parse_intent(request))
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--mode", "worker", "--child", "--source-root", str(args.source_root),
            "--database", str(path), "--redis-url", redis_url, "--marker", str(marker)], env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
        await eventually(marker.exists)
        events_before, _, _ = await api.journal.events(name, "probe-buyer")
        killed_at = time.monotonic(); process.kill(); await asyncio.to_thread(process.wait)
        await asyncio.sleep(.85)
        await api.journal.recover_expired()
        replacement_agent = SyntheticAgent(); replacement = make_runtime(m, path, replacement_agent)
        await replacement.startup()
        stopping = asyncio.Event(); runner = consumer(m, queue, replacement, stopping)
        await eventually(lambda: drained(m, client))
        result = await eventually(lambda: terminal(api, name))
        stopping.set(); await asyncio.wait_for(runner, 5)
        async with m.httpx.AsyncClient(transport=m.httpx.ASGITransport(app=application(m, api)), base_url="http://probe") as http:
            response = await http.get(f"/commerce/ag-ui/runs/{name}/events", params={"buyer_id": "probe-buyer"})
            all_events = frames(response.text)
            cursor = all_events[0]["id"]
            replay = await http.get(f"/commerce/ag-ui/runs/{name}/events", params={"buyer_id": "probe-buyer"}, headers={"Last-Event-ID": cursor})
            invalid = await http.get(f"/commerce/ag-ui/runs/{name}/events", params={"buyer_id": "probe-buyer"}, headers={"Last-Event-ID": name + ":99999"})
            (args.output / (name + "-full.sse")).write_text(response.text)
            (args.output / (name + "-replay.sse")).write_text(replay.text)
        await save(name, {"level": "SIGKILL real worker process+ASGI SSE replay", "worker_exit": process.returncode,
            "prekill_event_count": len(events_before), "terminal": result["status"], "replacement_agent_calls": replacement_agent.calls,
            "pending_final": 0, "recovery_seconds": time.monotonic() - killed_at,
            "run_started_count": sum(e["event"]["type"] == "RUN_STARTED" for e in all_events),
            "replay_exact": frames(replay.text) == all_events[1:], "future_cursor_http": invalid.status_code,
            "contract_passed": result["status"] == "interrupted" and replacement_agent.calls == 0
                and frames(replay.text) == all_events[1:] and invalid.status_code == 409})
        await replacement.shutdown()
    finally:
        if process: stop(process)
        log.close(); await api.shutdown()
    await client.aclose()
    return rows


async def worker(args, m):
    client = m.redis.from_url(args.redis_url, decode_responses=True)
    queue = make_queue(m, client)
    runtime = make_runtime(m, args.database, SyntheticAgent(block=args.mode == "worker", delay=.15 if args.mode == "load-worker" else 0, marker=args.marker, metrics_path=args.worker_metrics))
    await runtime.startup()
    await queue.consume("probe-killed-worker", lambda task: m.execute_ag_ui_task(task, runtime), lambda: False,
        block_ms=10, concurrency=2, reclaim_idle_ms=150, lease_ms=800, heartbeat_interval_ms=30)


async def serve(args, m):
    import uvicorn
    agent = SyntheticAgent(delay=.15)
    client = m.redis.from_url(args.redis_url, decode_responses=True) if args.redis_url else None
    queue = make_queue(m, client, max_age_seconds=20) if client else None
    runtime = make_runtime(m, args.database, agent, queue if args.queue_mode else None)
    worker_runtime = make_runtime(m, args.database, agent) if args.queue_mode and not args.worker_metrics else None
    await runtime.startup()
    app = application(m, runtime)
    @app.get("/_probe/metrics")
    async def metrics():
        values = {"calls": agent.calls, "active": agent.active, "active_peak": agent.peak, "timeline": agent.timeline}
        if args.worker_metrics and args.worker_metrics.exists():
            values = json.loads(args.worker_metrics.read_text())
        return {**values, "queue_depth": await queue.depth() if queue else 0}
    @app.post("/_probe/reset")
    async def reset():
        assert agent.active == 0
        agent.calls = agent.peak = 0; agent.timeline = []
        return {"ok": True}
    stopping = asyncio.Event(); runner = None
    if worker_runtime:
        await worker_runtime.startup(); runner = consumer(m, queue, worker_runtime, stopping)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"))
    try:
        await server.serve()
    finally:
        stopping.set()
        if runner: await asyncio.wait_for(runner, 8)
        if worker_runtime: await worker_runtime.shutdown()
        await runtime.shutdown()
        if client: await client.aclose()


def percentile(values, q):
    values = sorted(values)
    if not values: return None
    return values[min(len(values)-1, int((len(values)-1)*q))]


def process_sample(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        return {"at": time.monotonic(), "rss_bytes": int(stat[23]) * os.sysconf("SC_PAGE_SIZE"),
            "cpu_seconds": (int(stat[13]) + int(stat[14])) / os.sysconf("SC_CLK_TCK")}
    except (OSError, IndexError, ValueError):
        return {"at": time.monotonic(), "unavailable": True}


async def load(args, m, redis_url, directory):
    rows = []
    for mode in ("inline", "queue"):
        for users in args.users:
            for repeat in range(args.repetitions):
                name = f"load-{mode}-u{users}-r{repeat+1}"
                client_redis = m.redis.from_url(redis_url, decode_responses=True)
                await client_redis.flushdb(); await client_redis.aclose()
                port = free_port(); database = directory / (name + ".db")
                command = [sys.executable, str(SCRIPT), "--mode", "serve", "--child", "--source-root", str(args.source_root),
                    "--database", str(database), "--port", str(port)]
                worker_metrics = directory / (name + "-worker-metrics.json")
                if mode == "queue": command += ["--queue-mode", "--redis-url", redis_url, "--worker-metrics", str(worker_metrics)]
                with (args.output / (name + ".log")).open("w") as log:
                    process = subprocess.Popen(command, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
                    records, samples = [], []
                    worker_process = None
                    worker_log = None
                    if mode == "queue":
                        worker_log = (args.output / (name + "-worker.log")).open("w")
                        worker_process = subprocess.Popen([sys.executable, str(SCRIPT), "--mode", "load-worker", "--child", "--source-root", str(args.source_root), "--database", str(database), "--redis-url", redis_url, "--worker-metrics", str(worker_metrics)], env=os.environ.copy(), stdout=worker_log, stderr=subprocess.STDOUT)
                    try:
                        async with m.httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False) as client:
                            async def ready():
                                try: return (await client.get("/_probe/metrics")).status_code == 200
                                except m.httpx.HTTPError: return False
                            await eventually(ready, timeout=30)
                            for index in range(20):
                                response = await client.post("/commerce/ag-ui/run", json=body(name + "-warm-" + str(index)))
                                assert response.status_code == 200 and frames(response.text)[-1]["event"]["type"] == "RUN_FINISHED"
                            await client.post("/_probe/reset")
                            warm_metrics = (await client.get("/_probe/metrics")).json()
                            warm_calls = warm_metrics["calls"]
                            warm_timeline_count = len(warm_metrics["timeline"])
                            started = time.monotonic(); end = started + args.seconds
                            async def user(worker_index):
                                index = 0
                                while time.monotonic() < end:
                                    run = f"{name}-w{worker_index}-{index}"; index += 1
                                    before = time.monotonic(); item = {"run_id": run, "submitted": before}
                                    try:
                                        async with client.stream("POST", "/commerce/ag-ui/run", json=body(run)) as response:
                                            item.update(http_status=response.status_code, response_headers_seconds=time.monotonic()-before)
                                            raw = (await response.aread()).decode()
                                        item["elapsed_seconds"] = time.monotonic()-before
                                        if response.status_code == 200:
                                            events = frames(raw); last = events[-1]["event"] if events else {}
                                            item.update(accepted=True, terminal=last.get("type", "missing"), event_count=len(events))
                                        else:
                                            detail = json.loads(raw).get("detail", {})
                                            item.update(accepted=False, code=detail.get("code") if isinstance(detail, dict) else str(detail))
                                    except Exception as error:
                                        item.update(accepted=None, exception=type(error).__name__, elapsed_seconds=time.monotonic()-before)
                                    records.append(item)
                                    # 受拒客户端固定退避，保留拒绝分母，不制造无界紧循环。
                                    if item.get("accepted") is not True: await asyncio.sleep(.02)
                            async def sample():
                                while time.monotonic() < end:
                                    metric = await client.get("/_probe/metrics")
                                    samples.append({**process_sample(process.pid), "worker": process_sample(worker_process.pid) if worker_process else None, **{k:v for k,v in metric.json().items() if k != "timeline"}})
                                    await asyncio.sleep(.1)
                            await asyncio.gather(*(user(i) for i in range(users)), sample())
                            final = (await client.get("/_probe/metrics")).json()
                            final["calls"] -= warm_calls
                            final["timeline"] = final["timeline"][warm_timeline_count:]
                            elapsed = time.monotonic() - started
                            for item in records:
                                if item.get("accepted") is True:
                                    saved = await client.get("/commerce/ag-ui/runs/" + item["run_id"], params={"buyer_id":"probe-buyer"})
                                    item["durable_status"] = saved.json().get("status") if saved.status_code == 200 else "query-failed"
                            accepted = [r for r in records if r.get("accepted") is True]
                            rejected = [r for r in records if r.get("accepted") is False]
                            unknown = [r for r in records if r.get("accepted") is None]
                            completed = [r for r in accepted if r.get("durable_status") == "completed"]
                            pending = [r for r in accepted if r.get("durable_status") in {"running", "queued", "query-failed"}]
                            times = [r["elapsed_seconds"] for r in accepted]
                            starts = {r["session"]: r["entered"] for r in final["timeline"] if "entered" in r}
                            ends = {r["session"]: r["exited"] for r in final["timeline"] if "exited" in r}
                            waits = [starts[r["run_id"] + "-session"] - r["submitted"] for r in accepted if r["run_id"] + "-session" in starts]
                            runtimes = [ends[key] - value for key, value in starts.items() if key in ends]
                            headers = [r["response_headers_seconds"] for r in records if "response_headers_seconds" in r]
                            summary = {"mode": mode, "users": users, "repeat": repeat+1, "window_seconds": args.seconds,
                                "measurement_and_drain_seconds": elapsed, "offered": len(records), "accepted": len(accepted),
                                "rejected": len(rejected), "acceptance_unknown": len(unknown), "accepted_completed": len(completed),
                                "accepted_other_terminal": len(accepted)-len(completed)-len(pending), "censored": len(pending),
                                "completed_per_second": len(completed)/elapsed, "overload_rejection_rate": len(rejected)/len(records),
                                "end_to_end_success_rate": len(completed)/len(records), "accepted_error_rate": (len(accepted)-len(completed))/max(1,len(accepted)),
                                "accepted_latency_p50": percentile(times,.5), "accepted_latency_p95": percentile(times,.95),
                                "accepted_latency_p99": percentile(times,.99),
                                "submission_headers_p50": percentile(headers,.5), "submission_headers_p95": percentile(headers,.95), "submission_headers_p99": percentile(headers,.99),
                                "submission_to_agent_p50": percentile(waits,.5), "submission_to_agent_p95": percentile(waits,.95),
                                "agent_runtime_p50": percentile(runtimes,.5), "agent_runtime_p95": percentile(runtimes,.95),
                                "active_peak": final["active_peak"],
                                "queue_depth_sampled_peak": max(s["queue_depth"] for s in samples),
                                "api_rss_peak_bytes": max(s.get("rss_bytes",0) for s in samples),
                                "worker_rss_peak_bytes": max((s["worker"] or {}).get("rss_bytes",0) for s in samples), "sqlite_internal_wait": "unknown",
                                "paid_calls": 0, "fixture_has_no_trade_store": True, "status_reconciliation": len(records) == len(accepted) + len(rejected) + len(unknown),
                                "configured_active_bound": 2, "active_bound_supported": "max_active_runs" in inspect.signature(m.AGUIRuntime).parameters,
                                "queue_bound": 2, "worker_concurrency": 2}
                            (args.output / (name + "-requests.jsonl")).write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in records))
                            dump(args.output / (name + "-samples.json"), samples)
                            dump(args.output / (name + "-timeline.json"), final["timeline"])
                            dump(args.output / (name + "-summary.json"), summary)
                            print(json.dumps(summary, ensure_ascii=False), flush=True); rows.append(summary)
                    finally:
                        await asyncio.to_thread(stop, process)
                        if worker_process: await asyncio.to_thread(stop, worker_process)
                        if worker_log: worker_log.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=["observe", "load", "serve", "worker", "load-worker"], default="observe")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--redis-binary", default=os.environ.get("VOYACART_REDIS_SERVER_BIN", "/tmp/voyacart-redis/src/redis-server"))
    parser.add_argument("--redis-url", default="")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--worker-metrics", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--queue-mode", action="store_true")
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--users", type=lambda s: [int(v) for v in s.split(",")], default=[1,2,4,8])
    args = parser.parse_args(); args.source_root = args.source_root.resolve()
    if args.mode in {"serve", "worker", "load-worker"}:
        if not args.child: parser.error("服务子进程必须由隔离驱动启动")
        return asyncio.run(serve(args, imports(args.source_root)) if args.mode == "serve" else worker(args, imports(args.source_root)))
    if args.output is None: parser.error("--output is required")
    args.output = args.output.resolve()
    if not args.child:
        args.output.mkdir(parents=True, exist_ok=False)
        sys.path.insert(0, str(args.source_root))
        from scripts.ci.offline_env import isolated_env
        before = fingerprint(args.source_root)
        dump(args.output / "source-before.json", before)
        dump(args.output / "driver.json", {"path": str(SCRIPT), "sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            "argv": sys.argv, "fixture": "synthetic Agent fixed 150ms; actual AGUI+SQLite+Redis; no catalog, trade store or external model", "python": sys.version})
        with tempfile.TemporaryDirectory(prefix="voyacart-m5-probe-") as temp:
            env = isolated_env(Path(temp)); env["PYTHONPATH"] = str(args.source_root)
            result = subprocess.run([sys.executable, str(SCRIPT), *sys.argv[1:], "--output", str(args.output), "--source-root", str(args.source_root), "--child"], cwd=args.source_root, env=env)
        after = fingerprint(args.source_root)
        dump(args.output / "source-after.json", after)
        dump(args.output / "source-stable.json", {"stable": before == after, "returncode": result.returncode})
        raise SystemExit(result.returncode or (0 if before == after else 3))
    with tempfile.TemporaryDirectory(prefix="voyacart-m5-redis-") as temp, ExitStack() as stack:
        directory = Path(temp); port = free_port(); redis_url = f"redis://127.0.0.1:{port}/0"
        log = stack.enter_context((args.output / "redis.log").open("w"))
        process = subprocess.Popen([args.redis_binary, "--bind", "127.0.0.1", "--port", str(port), "--save", "",
            "--appendonly", "yes", "--appendfsync", "always", "--dir", temp], stdout=log, stderr=subprocess.STDOUT)
        stack.callback(stop, process)
        m = imports(args.source_root)
        async def execute():
            client = m.redis.from_url(redis_url, decode_responses=True)
            async def ready():
                try: return await client.ping()
                except Exception: return False
            await eventually(ready); await client.aclose()
            return await (observe(args,m,redis_url,directory) if args.mode == "observe" else load(args,m,redis_url,directory))
        results = asyncio.run(execute())
        dump(args.output / "report.json", {"mode": args.mode, "results": results,
            "limitations": ["合成 Agent 不代表完整目录/工具/模型容量", "queue load 使用专用API进程和独立worker进程；SIGKILL另用阻塞worker", "Redis AOF配置开启；本驱动不强杀Redis", "没有交易库；不能以本驱动声称库存不变量", "外部真实模型调用0"]})


if __name__ == "__main__":
    main()
