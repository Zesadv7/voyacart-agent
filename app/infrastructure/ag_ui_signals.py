"""进程内共享运行信号；Redis 背板沿用 API 已有的唯一订阅。"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import asyncio

from app.infrastructure.eventbus import TradeEvent


class AGUISignals:
    def __init__(self, backplane=None):
        self.backplane = backplane
        self._subscribers = defaultdict(set)

    @contextmanager
    def subscribe(self, run_id):
        signal = asyncio.Event()
        self._subscribers[run_id].add(signal)
        try:
            yield signal
        finally:
            self._subscribers[run_id].remove(signal)
            if not self._subscribers[run_id]:
                del self._subscribers[run_id]

    def notify(self, run_id):
        for signal in self._subscribers.get(run_id, ()):
            signal.set()

    async def publish(self, run_id, session_id):
        self.notify(run_id)
        if self.backplane is not None:
            await self.backplane.publish(TradeEvent(
                shopping_session_id=session_id, type="agui.events", payload={"run_id": run_id},
                occurred_at=datetime.now(timezone.utc).isoformat(),
            ))
