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
        # MultiPV 结果缓存（每次搜索前清零，_read_bestmove 中收集）
        self._top_moves: list = []  # [(move_tuple, score_cp), ...]

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

    def evaluate_position(self, game, player: int,
                          depth: int = 1) -> Optional[int]:
        """快速评估局面 — go depth N，返回走子方视角 centipawn 评分。"""
        if not self._available:
            return None

        with self._lock:
            try:
                self._top_moves = []  # 清除缓存，确保读到本次搜索结果
                fen = board_to_fen(game.board, player)
                self._purge_lines()
                self._send(f'position fen {fen}')
                self._send(f'go depth {depth}')

                best_move_uci = self._read_bestmove(5000)
                if not best_move_uci:
                    return None
                if self._top_moves:
                    return self._top_moves[0][1]
            except (OSError, ValueError, BrokenPipeError):
                pass
            return None

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
                self._top_moves = []  # 清除缓存
                fen = board_to_fen(game.board, player)
                self._purge_lines()
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
        UCI 通信在 daemon 线程中执行，完成时回调 callback(move, error)。

        callback 在**工作线程**中被调用，调用方负责用
        QTimer.singleShot / pyqtSignal 将结果调度回主线程。

        Args:
            game: ChineseChessGame 实例（仅用于快照，不在工作线程中访问）
            player: 当前走子方
            time_ms: 搜索时间（毫秒）
            callback: callable(move_or_None, error_str)，在工作线程中调用；
                error_str 为空表示成功，否则为失败原因（用于日志展示）
        """
        if not self._available:
            callback(None, '引擎不可用')
            return

        self._pending_async += 1
        # 重置 MultiPV 收集缓存
        self._top_moves = []
        # 原子快照：深拷贝棋盘，从副本推导 FEN + 合法走法（避免 TOCTOU）
        try:
            board_copy = [row[:] for row in game.board]
            fen = board_to_fen(board_copy, player)
            # 用临时 Game 对象计算合法走法（隔离共享状态）
            from domain.game import ChineseChessGame
            tmp_game = ChineseChessGame()
            tmp_game.board = board_copy
            tmp_game.current_player = player
            legal_moves = set(tmp_game.get_all_legal_moves(player))
        except Exception as e:
            self._pending_async -= 1
            callback(None, f'棋盘快照失败: {e}')
            return

        def _run():
            move = None
            error = ''
            try:
                with self._lock:
                    self._purge_lines()
                    self._send(f'position fen {fen}')
                    self._send(f'go movetime {time_ms}')
                    uci = self._read_bestmove(time_ms)
                    if uci:
                        move = _uci_to_tuple(uci)
                        if not (move and move in legal_moves):
                            error = f'返回非法或无法解析的走法: {uci!r}'
                            move = None
                    else:
                        error = f'引擎无响应/超时（{time_ms}ms + 10s 兜底）'
            except (OSError, ValueError, BrokenPipeError) as e:
                error = f'引擎通信异常（进程已终止）: {e}'
                self._available = False
                self._kill_proc()
            except Exception as e:
                error = f'搜索异常: {e}'
            try:
                callback(move, error)
            except Exception:
                pass
            finally:
                self._pending_async -= 1

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def get_top_moves(self, n: int = 3) -> List[Tuple[tuple, int, float]]:
        """返回前 N 个最优走法及评分（接口兼容 MCTSEngine）。

        需 MultiPV 已启用。返回 [(move, visits=score_cp, avg_value=score_cp), ...]，
        其中 visits 实际存储 centipawn 评分。
        """
        result = []
        for move, score_cp in self._top_moves[:n]:
            # visits 存评分为整数（兼容 MCTSEngine 接口），avg_value 存归一化值
            result.append((move, int(score_cp), score_cp / 100.0))
        return result

    def get_top_moves_scores(self) -> list:
        """返回原始 MultiPV 评分列表 [(move, score_cp), ...]。

        供 controller 做高置信度判断。
        """
        return list(self._top_moves)

    def _parse_multipv_line(self, line: str) -> None:
        """解析 MultiPV info 行，将 (move, score_cp) 追加到 self._top_moves。

        格式：info multipv 1 score cp 120 pv e0e1 ...
        分数可能为 mate N（将杀距离），此时用 ±99999 替代。
        """
        parts = line.split()
        try:
            # 定位 multipv 和 score
            pv_idx = None
            score_val = 0
            for i, token in enumerate(parts):
                if token == 'score':
                    # score cp X 或 score mate Y
                    if i + 2 < len(parts):
                        if parts[i + 1] == 'cp':
                            score_val = int(parts[i + 2])
                        elif parts[i + 1] == 'mate':
                            mate_in = int(parts[i + 2])
                            score_val = 99999 if mate_in > 0 else -99999
                if token == 'pv' and i + 1 < len(parts):
                    # pv 后面的第一个走法是该主变的 UCI 走法
                    pv_idx = i
                    break
            if pv_idx is not None and pv_idx + 1 < len(parts):
                uci = parts[pv_idx + 1]
                move = _uci_to_tuple(uci)
                if move:
                    self._top_moves.append((move, score_val))
        except (ValueError, IndexError):
            pass  # 格式异常，静默跳过

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
            self._reader_dead = False
            self._proc = subprocess.Popen(
                [binary_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # 避免管道溢出死锁；错误诊断靠退出码
                text=True,
                # 显式 UTF-8 + 容错解码：中文 Windows 默认 cp936，
                # 引擎输出任一不可解码字节都会让 reader 线程静默死亡
                encoding='utf-8',
                errors='replace',
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
                    # 启用 MultiPV 以支持高置信度短路
                    try:
                        self._send('setoption name MultiPV value 3')
                    except Exception:
                        pass  # 旧版 Pikafish 不支持 MultiPV，静默回退
                    return
            # 超时未收到 uciok → 清理僵尸进程
            self._error_msg = (
                f'Pikafish 启动超时（{ENGINE_STARTUP_TIMEOUT}s 未收到 uciok）'
            )
            self._kill_proc()
        except (OSError, FileNotFoundError) as e:
            self._error_msg = f'无法启动 Pikafish: {e}'

    def _reader_loop(self):
        """daemon 线程：持续读取引擎 stdout 到行队列（EOF/异常时标记死亡）。"""
        proc = self._proc
        try:
            while proc and proc.stdout:
                line = proc.stdout.readline()
                if not line:
                    break
                self._out_q.put(line)
        except Exception as e:
            import sys
            print(f"[Pikafish] reader 线程异常退出: {e}",
                  file=sys.stderr, flush=True)
        finally:
            # 标记死亡 + 哨兵唤醒：_read_line 在队列空时立即失败，
            # 避免此后每次搜索都白等到截止时间
            self._reader_dead = True
            try:
                self._out_q.put(None)  # EOF 哨兵，唤醒所有等待中的读取方
            except Exception:
                pass

    def _read_line(self, timeout: float) -> Optional[str]:
        """从行队列取一行。超时/EOF/reader 死亡返回 None（绝不永久阻塞）。"""
        if self._reader_dead and self._out_q.empty():
            return None
        try:
            return self._out_q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def _send(self, command: str) -> bool:
        """发送命令到引擎。返回 True 表示发送成功。"""
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(command + '\n')
                self._proc.stdin.flush()
                return True
        except (OSError, BrokenPipeError):
            pass
        return False

    def _purge_lines(self) -> None:
        """排空行队列中的残留输出。

        上次搜索超时残留的 bestmove 若留在队列里，会被下一次搜索的
        _read_bestmove 第一行读到——把上一局的走法当成本局结果返回
        （合法性校验拦不住"合法但错误"的走法）。每次发 position 前
        调用（持锁状态下，残留只可能来自上次超时）。

        引擎收到 stop 后吐出 bestmove 存在几十 ms 延迟：单次排空可能
        刚好错过，导致残留 bestmove 被下一次 _read_bestmove 消费。
        50ms 后二次排空覆盖这一窗口（搜索耗时以秒计，代价可忽略）。
        """
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass
        # 二次排空：覆盖 stop→bestmove 的异步延迟窗口
        time.sleep(0.05)
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass

    def stop(self) -> None:
        """请求引擎中断当前搜索（UCI stop）。

        不取 _lock——搜索线程在整个搜索期间持锁，取锁会永远等不到。
        直接向 stdin 写入即可（与 reader 线程读 stdout 互不干扰）。
        引擎响应 stop 后吐出的 bestmove 由版本门控正常丢弃，
        本方法只为尽快释放 CPU 与搜索锁。
        """
        try:
            self._send('stop')
        except Exception:
            pass

    def _read_bestmove(self, time_ms: int = 5000) -> Optional[str]:
        """读取引擎输出直到 bestmove 行。

        安全机制：
        - 每 0.5s 轮询一次 queue，同时检查进程存活，引擎死亡后最多 0.5s 即可检测
        - 总时间上限 = 实际搜索时间 + 10s 兜底
        - 超时或进程死亡时返回 None（调用方回退 MCTS）
        - 沿途收集 multipv info 行到 self._top_moves（供高置信度短路使用）
        """
        if not self._proc or not self._proc.stdout:
            return None

        # 总时间上限：实际搜索时间 + 10s 兜底
        deadline = time.time() + (time_ms / 1000.0) + 10.0
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if self._proc.poll() is not None:
                break

            # 短超时轮询：每次最多等 0.5s，确保引擎死亡后快速检测
            line = self._read_line(min(0.5, remaining))
            if line is None:
                # 0.5s 内无数据 → 可能是引擎空闲或死亡
                if self._reader_dead:
                    break
                if self._proc.poll() is not None:
                    break
                continue  # 未到 deadline，继续等待

            line_str = line.strip()

            # ── 收集 MultiPV 评分行 ──
            if line_str.startswith('info') and 'multipv' in line_str:
                self._parse_multipv_line(line_str)

            if line_str.startswith('bestmove'):
                parts = line_str.split()
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
                line = self._read_line(min(0.5, remaining))
                if line is None:
                    if self._reader_dead or (self._proc and self._proc.poll() is not None):
                        break
                    continue
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

    Pikafish UCI 坐标系：rank 0 = 红方底线 = 内部行 9，
    rank 9 = 黑方底线 = 内部行 0，即 内部行 = 9 - rank
    （经验证：position startpos + d 命令，红方位于 rank 0 侧）。

    UCI 格式: <from_col><from_row><to_col><to_row>
    例如 Pikafish 'e0e1'（红帅E10→E9）→ 内部 (9, 4, 8, 4)
    col: a-i → 0-8
    row: 0-9 → 内部 9-0（行翻转）
    """
    if len(uci_move) < 4:
        return None
    try:
        fc = ord(uci_move[0].lower()) - ord('a')
        fr = 9 - int(uci_move[1])
        tc = ord(uci_move[2].lower()) - ord('a')
        tr = 9 - int(uci_move[3])
        if 0 <= fc < 9 and 0 <= fr < 10 and 0 <= tc < 9 and 0 <= tr < 10:
            return (fr, fc, tr, tc)
    except (ValueError, IndexError):
        pass
    return None
