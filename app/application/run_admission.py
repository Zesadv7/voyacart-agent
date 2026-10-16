"""进程内运行准入：无等待队列，名额覆盖准备、运行与取消收尾。"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from app.domain.queue.ports.task_queue import QueueCapacityExceeded


@dataclass
class RunPermit:
    gate: "RunAdmission"
    released: bool = False

    def release(self) -> None:
        with self.gate._lock:
            if not self.released:
                self.released = True
                self.gate.active -= 1


class RunAdmission:
    """只在当前进程计数；多进程总上限为各进程上限之和。"""
    def __init__(self, limit: int = 8):
        if type(limit) is not int or limit < 1:
            raise ValueError("RUN_MAX_ACTIVE 必须为正整数")
        self.limit, self.active, self.peak = limit, 0, 0
        self.accepted, self.rejected = 0, 0
        self._lock = Lock()

    def acquire(self) -> RunPermit:
        with self._lock:
            if self.active >= self.limit:
                self.rejected += 1
                raise QueueCapacityExceeded("当前运行已达上限，请使用原请求标识稍后重试")
            self.active += 1
            self.accepted += 1
            self.peak = max(self.peak, self.active)
            return RunPermit(self)

    def snapshot(self) -> dict:
        with self._lock:
            return {"scope": "process_local_runs", "limit": self.limit, "active": self.active,
                    "peak": self.peak, "accepted": self.accepted, "rejected": self.rejected}
