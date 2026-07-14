import threading
from collections import deque
from typing import Optional


class AIManager:
    """AI 请求管理器 — 队列、线程管理、取消"""

    def __init__(self) -> None:
        self.ai_move_queue: deque = deque()
        self.ai_move_in_progress: bool = False
        self.active_worker = None
        self._active_thread: Optional[threading.Thread] = None
        self._shutting_down: bool = False
        self._cancel_version: int = 0

    @property
    def cancel_version(self) -> int:
        """当前取消版本号 — GameController 用于构建 AIWorker 和过期检查。"""
        return self._cancel_version

    def clear_queue(self) -> None:
        self.ai_move_queue.clear()
        self._cancel_version += 1
        if self.active_worker:
            self.active_worker.cancel()
            self.active_worker = None

    def is_busy(self) -> bool:
        return self.ai_move_in_progress or self.active_worker is not None

    def set_busy(self, busy: bool) -> None:
        self.ai_move_in_progress = busy

    def add_to_queue(self, player: int, version: int) -> None:
        if (player, version) not in self.ai_move_queue:
            self.ai_move_queue.append((player, version))

    def pop_next(self) -> Optional[tuple]:
        return self.ai_move_queue.popleft() if self.ai_move_queue else None

    def set_active_worker(self, worker) -> None:
        self.active_worker = worker

    def set_active_thread(self, thread: threading.Thread) -> None:
        self._active_thread = thread

    def clear_active_worker(self) -> None:
        self.active_worker = None
        self._active_thread = None

    def shutdown(self) -> None:
        self._shutting_down = True
        self.ai_move_queue.clear()
        if self.active_worker:
            self.active_worker.cancel()
        thread = self._active_thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self.active_worker = None
        self._active_thread = None
        self.ai_move_in_progress = False
