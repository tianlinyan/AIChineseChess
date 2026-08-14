import threading
from typing import Optional


class AIManager:
    """AI 请求管理器 — 线程管理、取消"""

    def __init__(self) -> None:
        self.ai_move_in_progress: bool = False
        self.active_worker = None
        self._active_thread: Optional[threading.Thread] = None
        self._shutting_down: bool = False
        self._cancel_version: int = 0

    @property
    def cancel_version(self) -> int:
        """当前取消版本号。"""
        return self._cancel_version

    @property
    def is_shutting_down(self) -> bool:
        """是否正在关闭。"""
        return self._shutting_down

    def clear_queue(self) -> None:
        self._cancel_version += 1
        if self.active_worker:
            self.active_worker.cancel()
            self.active_worker = None
        # 复位 busy 与线程引用：避免调用方忘记手动 set_busy(False) 导致
        # is_busy() 恒 True、后续走子被拒的死锁（脆弱隐式契约的显式化）
        self.ai_move_in_progress = False
        self._active_thread = None

    def is_busy(self) -> bool:
        return self.ai_move_in_progress or self.active_worker is not None

    def set_busy(self, busy: bool) -> None:
        self.ai_move_in_progress = busy

    def set_active_worker(self, worker) -> None:
        self.active_worker = worker

    def set_active_thread(self, thread: threading.Thread) -> None:
        self._active_thread = thread

    def clear_active_worker(self) -> None:
        self.active_worker = None
        self._active_thread = None

    def shutdown(self) -> None:
        self._shutting_down = True
        if self.active_worker:
            self.active_worker.cancel()
        thread = self._active_thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self.active_worker = None
        self._active_thread = None
        self.ai_move_in_progress = False
