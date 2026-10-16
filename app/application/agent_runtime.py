# -*- coding: utf-8 -*-
"""运行独立于 HTTP 订阅存在；日志写入成功后事件才对 SSE 可见。"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import math
import time
import uuid

from ag_ui.core import RunAgentInput

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.agents.orchestrator import SubmitIntentInput
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalLeaseLost, JournalNotFound, JournalForbidden, JournalConflict
from app.domain.queue.ports.task_queue import IntentTask, QueueCapacityExceeded, QueueUnavailable
from app.application.run_admission import RunAdmission
from app.infrastructure.ag_ui_signals import AGUISignals
from app.infrastructure.eventbus import observe_run_events
from app.infrastructure.learning_context import learning_scope
from app.infrastructure.security.public_output import public_value, public_messages

logger = logging.getLogger(__name__)
RECOVERY_INTERVAL_SECONDS = 5


@dataclass
class Running:
    owner: str
    deadline: float
    valid: bool = True
    reason: str = ""
    task: asyncio.Task | None = None
    hard_deadline: float = float("inf")

    def is_valid(self):
        return self.valid and time.monotonic() < min(self.deadline, self.hard_deadline)


class AGUIRuntime:
    def __init__(self, journal: AGUIJournal, orchestrator, confirmations=None, *, lease_seconds=30, heartbeat_seconds=5,
                 delta_coalesce_ms=0, backplane=None, task_queue=None, queue_max_pending=200, queue_wait_seconds=300, queue_enabled=False,
                 admission=None, max_active_runs=8, run_timeout_seconds=300, recovery_queue=None):
        self.journal, self.orchestrator, self.confirmations = journal, orchestrator, confirmations
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self.running: dict[str, Running] = {}
        self.delta_coalesce_ms = delta_coalesce_ms
        self.signals = AGUISignals(backplane)
        self._recovery_task = None
        self.task_queue = task_queue
        self.recovery_queue = recovery_queue if recovery_queue is not None else task_queue
        self.queue_enabled = queue_enabled or task_queue is not None
        self.queue_max_pending, self.queue_wait_seconds = queue_max_pending, queue_wait_seconds
        if type(run_timeout_seconds) not in (int, float) or not math.isfinite(run_timeout_seconds) or run_timeout_seconds <= 0:
            raise ValueError("RUN_TIMEOUT_SECONDS 必须大于0")
        self.admission = admission or RunAdmission(max_active_runs)
        self.run_timeout_seconds = run_timeout_seconds

    async def startup(self):
        await self.journal.initialize()
        await self.journal.recover_expired()
        self._recovery_task = asyncio.create_task(self._recover_periodically(), name="agui-recovery")

    async def _recover_periodically(self):
        while True:
            await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
            try:
                await self.recover()
            except Exception as error:
                # 单次依赖异常不终止恢复循环；原有持久记录保留供恢复后继续对账。
                logger.warning("运行恢复暂时不可用（%s）", type(error).__name__)

    async def recover(self):
        await self.journal.recover_expired()
        if self.recovery_queue is not None and hasattr(self.recovery_queue, "expire_due"):
            for task in await self.recovery_queue.expire_due(limit=100):
                await self.journal.fail_unstarted(task.ag_ui_input, task.buyer_id)
                await self.recovery_queue.acknowledge_expired(task.task_id)

    async def shutdown(self):
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        active = list(self.running.items())
        for _, entry in active:
            entry.reason = "restart"
            entry.valid = False
            if entry.task:
                entry.task.cancel()
        await asyncio.gather(*(entry.task for _, entry in active if entry.task), return_exceptions=True)
        for run_id, entry in active:
            await self.journal.end_owned(run_id, entry.owner)
            self.running.pop(run_id, None)

    async def submit(self, body: RunAgentInput, intent: SubmitIntentInput):
        existing = await self.journal.existing(body.model_dump(mode="json", by_alias=True), intent.buyer_id)
        if existing is not None:
            return existing
        if self.task_queue is None:
            if self.queue_enabled:
                raise QueueUnavailable()
            return await self.start(body, intent)
        # 历史与客户端 state 不参与执行，重连请求只绑定本轮有效输入。
        props = {"buyerId": intent.buyer_id, "locale": intent.locale, "currency": intent.currency}
        if intent.selected_skill is not None:
            props["selectedSkill"] = body.forwarded_props["selectedSkill"]
        payload = body.model_copy(update={"messages": [body.messages[-1]], "state": {},
            "context": [], "forwarded_props": props}).model_dump(mode="json", by_alias=True)
        task = IntentTask("agui:" + body.run_id, body.thread_id, intent.buyer_id,
            intent.locale, intent.currency, intent.raw_query, ag_ui_input=payload)
        try:
            await self.task_queue.enqueue(task, max_pending=self.queue_max_pending)
        except QueueCapacityExceeded:
            raise
        except ValueError as error:
            raise JournalConflict("运行标识已绑定不同请求，请核对已有运行后重试") from error
        except Exception as error:
            logger.warning("AG-UI 入队结果未知，不转进程内执行（%s）", type(error).__name__)
            raise QueueUnavailable(acceptance_unknown=True) from error
        # 同键重放可能对应过期/死信，先返回其可查询的持久终态。
        status = await self._queue_status(task.task_id)
        if status is not None and status.state == "failed":
            return await self._failed_queued(task, status)
        return {"runId": body.run_id, "threadId": body.thread_id, "status": "queued", "cursor": 0}

    async def _queue_status(self, task_id):
        try:
            return await (self.task_queue or self.recovery_queue).get_status(task_id)
        except Exception as error:
            raise QueueUnavailable() from error

    async def _failed_queued(self, task, status):
        code = "QUEUE_DEADLINE_EXCEEDED" if status.error == "QUEUE_DEADLINE_EXCEEDED" else "QUEUE_FAILED"
        run = await self.journal.fail_unstarted(task.ag_ui_input, task.buyer_id, code=code)
        queue = self.task_queue or self.recovery_queue
        if hasattr(queue, "acknowledge_expired"):
            try:
                await queue.acknowledge_expired(task.task_id)
            except Exception as error:
                logger.warning("排队终态已写日志，索引确认稍后重试（%s）", type(error).__name__)
        return run

    async def queued_task(self, run_id, buyer_id):
        queue = self.task_queue or self.recovery_queue
        if self.queue_enabled and queue is None:
            raise QueueUnavailable()
        try:
            task = await queue.get_task("agui:" + run_id) if queue is not None else None
        except Exception as error:
            raise QueueUnavailable() from error
        if task is None:
            raise JournalNotFound("运行不存在")
        if task.buyer_id != buyer_id:
            raise JournalForbidden("无权访问该买家的运行")
        return task

    async def run(self, run_id, buyer_id):
        try:
            return await self.journal.run(run_id, buyer_id)
        except JournalNotFound:
            task = await self.queued_task(run_id, buyer_id)
            status = await self._queue_status(task.task_id)
            if status is None:
                raise QueueUnavailable()
            if status.state == "failed":
                return await self._failed_queued(task, status)
            if status.state == "done":
                # 交付完成却找不到journal是恢复异常，绝不推断业务成功或重新运行。
                raise QueueUnavailable()
            return {"runId": run_id, "threadId": task.shopping_session_id,
                "status": "queued", "cursor": 0,
                "messages": public_messages(task.ag_ui_input["messages"]),
                "input": {**public_value(task.ag_ui_input), "messages": public_messages(task.ag_ui_input["messages"])},
                "state": {}, "stopRequested": False}

    async def start(self, body: RunAgentInput, intent: SubmitIntentInput, *, automation_ticket=None):
        payload = body.model_dump(mode="json", by_alias=True)
        existing = await self.journal.existing(payload, intent.buyer_id)
        if existing is not None:
            return existing
        permit = self.admission.acquire()
        owner = uuid.uuid4().hex
        started = time.monotonic()
        created = False
        try:
            run, created = await self.journal.reserve(payload, intent.buyer_id, owner, self.lease_seconds, automation_ticket=automation_ticket)
            if created:
                entry = Running(owner, started + self.lease_seconds, hard_deadline=started + self.run_timeout_seconds)
                self.running[body.run_id] = entry
                effective = RunAgentInput.model_validate(run["input"])
                producer = self._produce(effective, intent, entry)
                try:
                    entry.task = asyncio.create_task(producer, name=f"agui-run:{body.run_id}")
                except BaseException:
                    producer.close()
                    raise
                # done callback覆盖首次调度前取消；permit幂等，不依赖producer是否进入finally。
                entry.task.add_done_callback(lambda _: permit.release())
            else:
                permit.release()
            return run
        except BaseException:
            permit.release()
            if created:
                self.running.pop(body.run_id, None)
                await self.journal.end_owned(body.run_id, owner)
            raise

    async def cancel(self, run_id, buyer_id):
        try:
            run = await self.journal.request_stop(run_id, buyer_id)
        except JournalNotFound:
            task = await self.queued_task(run_id, buyer_id)
            run = await self.journal.fail_unstarted(task.ag_ui_input, buyer_id, code="CANCELLED")
            if run["status"] == "running":
                run = await self.journal.request_stop(run_id, buyer_id)
        entry = self.running.get(run_id)
        if entry and entry.task:
            entry.reason = "stop"
            entry.valid = False
            entry.task.cancel()
            await asyncio.shield(asyncio.gather(entry.task, return_exceptions=True))
            await self.journal.end_owned(run_id, entry.owner, stopped=True)
            self.running.pop(run_id, None)
            run = await self.journal.run(run_id, buyer_id)
        return run

    async def _produce(self, body, intent, entry):
        queue = asyncio.Queue()
        adapter = AGUIRunAdapter(body, queue.put_nowait, authoritative_state=body.state)

        async def write_events():
            while True:
                event = await queue.get()
                if event is None:
                    return
                events = [event]
                ending = False
                deadline = time.monotonic() + self.delta_coalesce_ms / 1000
                while len(events) < 64:
                    if not queue.empty():
                        following = queue.get_nowait()
                    elif self.delta_coalesce_ms and events[-1].type in {"TEXT_MESSAGE_CONTENT", "TOOL_CALL_ARGS"}:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            following = await asyncio.wait_for(queue.get(), remaining)
                        except asyncio.TimeoutError:
                            break
                    else:
                        break
                    if following is None:
                        ending = True
                        break
                    events.append(following)
                payloads = [value.model_dump(mode="json", by_alias=True, exclude_none=True) for value in events]
                if self.delta_coalesce_ms:
                    merged = []
                    for value in payloads:
                        identity = "messageId" if value["type"] == "TEXT_MESSAGE_CONTENT" else "toolCallId" if value["type"] == "TOOL_CALL_ARGS" else None
                        if (identity and merged and merged[-1]["type"] == value["type"]
                                and merged[-1][identity] == value[identity]):
                            merged[-1]["delta"] += value["delta"]
                        else:
                            merged.append(value)
                    payloads = merged
                if entry.reason == "restart":
                    for value in payloads:
                        if value["type"] == "RUN_ERROR":
                            value["code"] = "SERVER_RESTART"
                try:
                    await self.journal.append(body.run_id, entry.owner, payloads)
                except Exception:
                    entry.valid = False
                    entry.reason = "restart"
                    if entry.task:
                        entry.task.cancel()
                    raise
                await self.signals.publish(body.run_id, body.thread_id)
                if ending:
                    return

        async def heartbeat():
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                started = time.monotonic()
                try:
                    renewed = await asyncio.wait_for(self.journal.renew(body.run_id, entry.owner, self.lease_seconds),
                                                    max(0.001, entry.deadline - started))
                    if not renewed:
                        run = await self.journal.run(body.run_id, intent.buyer_id)
                        entry.reason = "stop" if run["stopRequested"] else "restart"
                        raise JournalLeaseLost("运行日志租约失效或收到跨实例停止请求")
                    entry.deadline = started + self.lease_seconds
                except Exception:
                    entry.valid = False
                    if entry.task:
                        entry.task.cancel()
                    return

        async def enforce_deadline():
            await asyncio.sleep(max(0, entry.hard_deadline - time.monotonic()))
            entry.reason, entry.valid = "deadline", False
            if entry.task:
                entry.task.cancel()
            # 即使下游吞掉取消，也独立提交终态；名额仍由真实producer结束时释放。
            await self.journal.end_owned(body.run_id, entry.owner, deadline=True)

        writer = asyncio.create_task(write_events())
        lease_task = asyncio.create_task(heartbeat())
        deadline_task = asyncio.create_task(enforce_deadline())
        adapter.start()
        def on_trade_event(event):
            if event.type == "usage.summary":
                from ag_ui.core import CustomEvent
                queue.put_nowait(CustomEvent(name="usage.summary", value=event.payload))
            adapter.on_trade_event(event)
        try:
            if self.confirmations is not None:
                saved = await self.confirmations.list(intent.buyer_id, intent.shopping_session_id)
                adapter.state["confirmations"] = saved["confirmations"]
                adapter.snapshot()
            with learning_scope(self.journal, body.run_id, intent.buyer_id, intent.shopping_session_id), observe_run_events(on_trade_event):
                result = await self.orchestrator.handle_intent(intent, event_observer=adapter.on_agent_event,
                    use_semantic_cache=False, persistence_guard=entry.is_valid)
            if not entry.is_valid():
                raise asyncio.CancelledError()
            if getattr(result,"error_code",None)=="SESSION_VERSION_CHANGED":
                adapter.state["resumeDestination"]=await self.journal.latest_destination(intent.shopping_session_id,intent.buyer_id)
            if result.error or adapter.error:
                adapter.fail(result.error if getattr(result,"error_code",None) in {"SESSION_VERSION_CHANGED","CONTEXT_CAPACITY_EXCEEDED","MODEL_RESPONSE_INCOMPLETE"} else adapter.error or "本轮服务暂时未能完成请求，请重试。", code=getattr(result,"error_code",None))
            else:
                adapter.finish(result.final_text)
        except asyncio.CancelledError:
            adapter.fail("本轮已明确停止" if entry.reason == "stop" else "本轮已超过执行期限，已保存的记录仍可查看。" if entry.reason == "deadline" else "服务重启或执行中断，已保存的内容仍可恢复。",
                         cancelled=entry.reason == "stop", code=None if entry.reason == "stop" else "RUN_DEADLINE_EXCEEDED" if entry.reason == "deadline" else "SERVER_RESTART")
        except Exception as error:
            logger.warning("AG-UI 运行失败（%s）", type(error).__name__)
            adapter.fail("本轮服务暂时未能完成请求，请重试。")
        finally:
            lease_task.cancel()
            deadline_task.cancel()
            await asyncio.gather(lease_task, deadline_task, return_exceptions=True)
            queue.put_nowait(None)
            try:
                await writer
            except Exception as error:
                logger.warning("AG-UI 日志写入失败，等待持久租约过期后标记中断（%s）", type(error).__name__)
            entry.valid = False
            self.running.pop(body.run_id, None)
