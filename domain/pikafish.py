"""Pikafish UCI 引擎封装 — 中国象棋顶级开源引擎

Pikafish（皮卡鱼）基于 Stockfish，使用 NNUE 评估网络，棋力远超
本项目的 MCTS/Alpha-Beta 搜索引擎。通过 UCI 协议与引擎进程通信。

UCI 协议要点：
- 引擎作为子进程运行，通过 stdin/stdout 文本通信
- 所有命令以换行符结束
- 引擎回复以 "bestmove" 行结束搜索
- FEN 字符串描述局面

集成方式：
- PikafishEngine 提供与 MCTSEngine 兼容的接口
- 当引擎二进制不可用时静默回退，不影响正常使用
"""

import queue
import subprocess
import threading
import time
import os
import atexit
import shutil
from typing import Optional, Dict, List, Tuple

from domain.fen import board_to_fen


# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

# Pikafish 二进制路径（相对于项目根目录）
PIKAFISH_BINARY = 'engines/pikafish.exe'

# 引擎启动超时（秒）
ENGINE_STARTUP_TIMEOUT = 10.0

# 默认搜索时间（毫秒）
DEFAULT_MOVE_TIME_MS = 5000

def _find_pikafish() -> Optional[str]:
    """查找 Pikafish 二进制文件。

    按以下优先级搜索：
    1. 环境变量 PIKAFISH_PATH
    2. 项目根目录下的 engines/pikafish.exe
    3. 系统 PATH 中的 pikafish

    Returns:
        可执行文件路径，或 None
    """
    # 1. 环境变量
    env_path = os.environ.get('PIKAFISH_PATH', '')
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. 项目目录下的 engines/
    project_root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    local_path = os.path.join(project_root, PIKAFISH_BINARY)
    if os.path.isfile(local_path):
        return local_path

    # 3. 系统 PATH
    for ext in ('', '.exe'):
        full = f'pikafish{ext}'
        found = shutil.which(full)
        if found:
            return found

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Pikafish 引擎
# ══════════════════════════════════════════════════════════════════════════════

class PikafishEngine:
    """Pikafish UCI 引擎封装。

    通过子进程 + UCI 文本协议与 Pikafish 通信。
    提供与 MCTSEngine 兼容的 search() / get_top_moves() 接口。

    Usage:
        engine = PikafishEngine()
        if engine.available:
            move = engine.search(game, player=1, time_ms=5000)
            top3 = engine.get_top_moves(3)
        engine.close()
    """

    def __init__(self,
                 binary_path: Optional[str] = None,
                 move_time_ms: int = DEFAULT_MOVE_TIME_MS):
        """初始化 Pikafish 引擎。

        Args:
            binary_path: Pikafish 可执行文件路径。None 则自动查找。
            move_time_ms: 每步搜索时间（毫秒）。
        """
        self.move_time_ms = move_time_ms
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()  # 可重入：_kill_proc 可能在持锁时被调用
        self._available = False
        self._error_msg: str = ''  # 启动失败时的诊断信息
        self._pending_async: int = 0   # 进行中的异步搜索计数
        # 引擎 stdout 由独立 reader 线程读入行队列 —— 主逻辑按截止时间
        # queue.get(timeout=...) 取行，避免 readline() 在引擎静默挂起时
        # 永久阻塞（旧实现会连关窗退出都一起卡死）
        self._out_q: 'queue.Queue' = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None

        if binary_path is None:
            binary_path = _find_pikafish()

        if binary_path and os.path.isfile(binary_path):
            self._start_engine(binary_path)
            if self._available:
                atexit.register(self._kill_proc)
        elif binary_path:
            self._error_msg = f'Pikafish 路径不存在: {binary_path}'
        else:
            self._error_msg = (
                '未找到 Pikafish 引擎。请将 pikafish.exe 放入 engines/ 目录，'
                '或设置环境变量 PIKAFISH_PATH。'
            )

    @property
    def available(self) -> bool:
        """引擎是否可用。False 时应回退到 MCTS/Alpha-Beta。"""
        return self._available

    @property
    def error_msg(self) -> str:
        """引擎不可用时的诊断信息。"""
        return self._error_msg

    # ── 公开接口（兼容 MCTSEngine） ──

    def search(self,
               game,
               player: int,
               priors: Optional[Dict[tuple, float]] = None,
               time_ms: Optional[int] = None) -> Optional[tuple]:
        """搜索最佳走法。

        Args:
            game: ChineseChessGame 实例
            player: 当前走子方 (1=红, 2=黑)
            priors: 忽略（Pikafish 不需要先验，仅为接口兼容）
            time_ms: 搜索时间（毫秒），None 则使用默认值

        Returns:
            最佳走法 (fr, fc, tr, tc) 或 None
        """
        if not self._available:
            return None

        if time_ms is None:
            time_ms = self.move_time_ms

        with self._lock:
            try:
                fen = board_to_fen(game.board, player, reverse_rows=True)
                self._send(f'position fen {fen}')
                self._send(f'go movetime {time_ms}')

                best_move_uci = self._read_bestmove(time_ms)
                if not best_move_uci:
                    import sys
                    print(f"[Pikafish.search] _read_bestmove 返回空 "
                          f"(time_ms={time_ms}, fen={fen[:40]}...)",
                          file=sys.stderr, flush=True)
                    return None
                move = _uci_to_tuple(best_move_uci)
                if not move:
                    import sys
                    print(f"[Pikafish.search] _uci_to_tuple 失败 "
                          f"(uci={best_move_uci!r})",
                          file=sys.stderr, flush=True)
                    return None
                # 验证走法合法性 — 最终防线
                if move not in game.get_all_legal_moves(player):
                    import sys
                    print(f"[Pikafish.search] 走法非法 "
                          f"(uci={best_move_uci!r} move={move})",
                          file=sys.stderr, flush=True)
                    return None
                return move
            except (OSError, ValueError, BrokenPipeError):
                self._available = False
                self._kill_proc()
            return None

    def search_async(self,
                     game,
                     player: int,
                     time_ms: int,
                     callback) -> None:
        """异步搜索 — 在后台线程执行，不阻塞调用线程（UI）。

        在调用线程上快照 FEN + 合法走法（避免竞态），
        UCI 通信在 daemon 线程中执行，完成时回调 callback(move)。

        callback 在**工作线程**中被调用，调用方负责用
        QTimer.singleShot / pyqtSignal 将结果调度回主线程。

        Args:
            game: ChineseChessGame 实例（仅用于快照，不在工作线程中访问）
            player: 当前走子方
            time_ms: 搜索时间（毫秒）
            callback: callable(move_or_None)，在工作线程中调用
        """
        if not self._available:
            callback(None)
            return

        self._pending_async += 1
        # 原子快照：深拷贝棋盘，从副本推导 FEN + 合法走法（避免 TOCTOU）
        try:
            board_copy = [row[:] for row in game.board]
            fen = board_to_fen(board_copy, player, reverse_rows=True)
            # 用临时 Game 对象计算合法走法（隔离共享状态）
            from domain.game import ChineseChessGame
            tmp_game = ChineseChessGame()
            tmp_game.board = board_copy
            tmp_game.current_player = player
            legal_moves = set(tmp_game.get_all_legal_moves(player))
        except Exception as e:
            self._pending_async -= 1
            callback(None)
            return

        def _run():
            move = None
            try:
                with self._lock:
                    self._send(f'position fen {fen}')
                    self._send(f'go movetime {time_ms}')
                    uci = self._read_bestmove(time_ms)
                    if uci:
                        move = _uci_to_tuple(uci)
                        if not (move and move in legal_moves):
                            move = None
            except (OSError, ValueError, BrokenPipeError):
                self._available = False
                self._kill_proc()
            except Exception:
                pass
            try:
                callback(move)
            except Exception:
                pass
            finally:
                self._pending_async -= 1

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def get_top_moves(self, n: int = 3) -> List[Tuple[tuple, int, float]]:
        """返回前 N 个最优走法（接口兼容 MCTSEngine）。

        当前仅返回空列表——MultiPV 模式未启用。
        Pikafish 作为单走法推荐引擎使用。
        """
        return []

    def close(self):
        """关闭引擎进程。

        先杀进程再收锁：进程死后 reader 线程 EOF → 队列哨兵唤醒
        等待中的搜索（持锁方）尽快返回，close 不会被挂起的搜索拖住。
        """
        proc = self._proc
        if proc:
            try:
                self._send('quit')
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        with self._lock:
            self._proc = None
            self._available = False

    def _kill_proc(self):
        """强制终止引擎进程（异常恢复用）。同 close：先杀后收锁。"""
        proc = self._proc
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass
        with self._lock:
            self._proc = None

    # ── UCI 协议通信 ──

    def _start_engine(self, binary_path: str):
        """启动 UCI 引擎进程并完成握手。"""
        try:
            self._proc = subprocess.Popen(
                [binary_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # 避免管道溢出死锁；错误诊断靠退出码
                text=True,
                bufsize=1,
            )
            # 独立 reader 线程：引擎 stdout → 行队列
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True)
            self._reader_thread.start()

            self._send('uci')
            # 等待 uciok
            deadline = time.time() + ENGINE_STARTUP_TIMEOUT
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                # 检查进程是否意外退出
                if self._proc.poll() is not None:
                    rc = self._proc.returncode
                    self._error_msg = (
                        f'Pikafish 进程意外退出（退出码 0x{rc & 0xFFFFFFFF:08X}）'
                    )
                    # STATUS_DLL_NOT_FOUND
                    if rc == 0xC0000135 or rc == -1073741515:
                        self._error_msg += (
                            '\n→ 缺少 Visual C++ 运行时库。'
                            '\n→ 请安装 VC++ Redistributable: '
                            'https://aka.ms/vs/17/release/vc_redist.x64.exe'
                        )
                    # STATUS_ILLEGAL_INSTRUCTION
                    elif rc == 0xC000001D or rc == -1073741795:
                        self._error_msg += (
                            '\n→ CPU 不支持该二进制文件的指令集。'
                            '\n→ 请从 pikafish.org 下载适合您 CPU 的版本：'
                            '\n   - 较新 Intel/AMD → bmi2 或 avx2'
                            '\n   - 旧 CPU → modern 或 无后缀版本'
                            '\n   - 不确定时，先试 modern 版本'
                        )
                    return  # self._available stays False

                line = self._read_line(remaining)
                if line and 'uciok' in line:
                    self._available = True
                    return
            # 超时未收到 uciok → 清理僵尸进程
            self._error_msg = (
                f'Pikafish 启动超时（{ENGINE_STARTUP_TIMEOUT}s 未收到 uciok）'
            )
            self._kill_proc()
        except (OSError, FileNotFoundError) as e:
            self._error_msg = f'无法启动 Pikafish: {e}'

    def _reader_loop(self):
        """daemon 线程：持续读取引擎 stdout 到行队列（EOF 时放哨兵）。"""
        proc = self._proc
        try:
            while proc and proc.stdout:
                line = proc.stdout.readline()
                if not line:
                    break
                self._out_q.put(line)
        except Exception:
            pass
        finally:
            self._out_q.put(None)  # EOF 哨兵，唤醒所有等待中的读取方

    def _read_line(self, timeout: float) -> Optional[str]:
        """从行队列取一行。超时/EOF 返回 None（绝不永久阻塞）。"""
        try:
            return self._out_q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def _send(self, command: str):
        """发送命令到引擎。"""
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(command + '\n')
            self._proc.stdin.flush()

    def _read_bestmove(self, time_ms: int = 5000) -> Optional[str]:
        """读取引擎输出直到 bestmove 行。

        安全机制：
        - 从 reader 线程的行队列按剩余时间 queue.get(timeout=...) 取行，
          引擎静默挂起也只会等到截止时间，绝不永久阻塞
        - 总时间上限 = 实际搜索时间 + 30s 兜底
        - 超时或进程死亡时返回 None（调用方回退 MCTS）
        """
        if not self._proc or not self._proc.stdout:
            return None

        # 总时间上限：实际搜索时间 + 30s 兜底
        deadline = time.time() + (time_ms / 1000.0) + 30.0
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # 进程已死 → 立即退出
            if self._proc.poll() is not None:
                break

            line = self._read_line(remaining)
            if line is None:
                break  # 超时或 EOF

            line = line.strip()

            if line.startswith('bestmove'):
                parts = line.split()
                if len(parts) >= 2:
                    best = parts[1]

                    return best

        # 超时或进程死亡 → 发送 stop 并排空残留数据，防止污染下次搜索
        try:
            self._send('stop')
            # 继续读取直到收到（过期）bestmove 或 EOF
            drain_deadline = time.time() + 5.0
            while True:
                remaining = drain_deadline - time.time()
                if remaining <= 0:
                    break
                if self._proc and self._proc.poll() is not None:
                    break
                line = self._read_line(remaining)
                if line is None:
                    break
                if line.strip().startswith('bestmove'):
                    break
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════════════════════

def _uci_to_tuple(uci_move: str) -> Optional[tuple]:
    """将 Pikafish UCI 走法字符串转为内部元组格式。

    Pikafish UCI 坐标系与内部一致（经验证：d 显示行号 = UCI 行号 = 内部行号）。
    无需反转。

    UCI 格式: <from_col><from_row><to_col><to_row>
    例如 Pikafish 'e0e1'（红将E10→E9）→ 内部 (9, 4, 8, 4)
    col: a-i → 0-8
    row: 0-9 → 0-9
    """
    if len(uci_move) < 4:
        return None
    try:
        fc = ord(uci_move[0].lower()) - ord('a')
        fr = int(uci_move[1])
        tc = ord(uci_move[2].lower()) - ord('a')
        tr = int(uci_move[3])
        if 0 <= fc < 9 and 0 <= fr < 10 and 0 <= tc < 9 and 0 <= tr < 10:
            return (fr, fc, tr, tc)
    except (ValueError, IndexError):
        pass
    return None
