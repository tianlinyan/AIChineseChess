"""Pikafish UCI 引擎封装 — 中国象棋顶级开源引擎

Pikafish（皮卡鱼）基于 Stockfish，使用 NNUE 评估网络，棋力远超
本项目的 MCTS 搜索引擎。通过 UCI 协议与引擎进程通信。

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
import re
import subprocess
import threading
import time
import os
import atexit
import shutil
import sys
from typing import Optional, Tuple

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
    提供与 MCTSEngine 兼容的 search() 接口。

    Usage:
        engine = PikafishEngine()
        if engine.available:
            move = engine.search(game, player=1, time_ms=5000)
        engine.close()
    """

    def __init__(self,
                 binary_path: Optional[str] = None,
                 move_time_ms: int = DEFAULT_MOVE_TIME_MS,
                 threads: Optional[int] = None,
                 hash_mb: int = 512):
        """初始化 Pikafish 引擎。

        Args:
            binary_path: Pikafish 可执行文件路径。None 则自动查找。
            move_time_ms: 每步搜索时间（毫秒）。
            threads: UCI Threads（搜索线程数）。None → 自动
                min(16, CPU 逻辑核数)——单线程（UCI 默认）棋力明显偏弱；
                现代 Pikafish 在 8→16 线程仍有可观提升（尤其长思考/
                深残局），故封顶放宽到 16。手动传值可覆盖（如留核给
                其他任务）。
            hash_mb: UCI Hash 大小（MB）。默认 512（UCI 默认 16 过小，
                中残局换位表命中率低）。
        """
        self.move_time_ms = move_time_ms
        self._threads = (min(16, os.cpu_count() or 1)
                         if threads is None else max(1, threads))
        self._hash_mb = max(16, hash_mb)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()  # 可重入：_kill_proc 可能在持锁时被调用
        # stdin 写锁（独立于 _lock）：stop() 故意不取 _lock（搜索线程整个
        # 搜索期间持锁，取锁会永远等不到），但多线程同时写 stdin 有命令
        # 撕裂风险（如 stop_all 与搜索线程的 _send 并发）。此锁只保护
        # 写入，持有时间极短，不阻塞 stop 的"尽快返回"语义。
        self._stdin_lock = threading.Lock()
        self._available = False
        self._error_msg: str = ''  # 启动失败时的诊断信息
        # 引擎 stdout 由独立 reader 线程读入行队列 —— 主逻辑按截止时间
        # queue.get(timeout=...) 取行，避免 readline() 在引擎静默挂起时
        # 永久阻塞（旧实现会连关窗退出都一起卡死）
        self._out_q: 'queue.Queue' = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        # reader 线程死亡标记：__init__ 即置位（_start_engine 前若失败，
        # _read_line 的守卫逻辑也能安全读取该属性）
        self._reader_dead = False
        # MultiPV 结果缓存（每次搜索前清零，_read_bestmove 中收集）
        self._top_moves: list = []  # [(move_tuple, score_cp), ...]（按序号升序）
        self._top_moves_dict: dict = {}  # multipv 序号 -> (move, score)，只存最新迭代
        # 当前引擎 MultiPV 配置值（正式走子恒 1；search_atomic 内部
        # 临时切 MultiPV 并在 finally 恢复 1）
        self._multipv: int = 1

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
        """引擎是否可用。False 时应回退到 MCTS。"""
        return self._available

    @property
    def error_msg(self) -> str:
        """引擎不可用时的诊断信息。"""
        return self._error_msg

    # ── 公开接口（兼容 MCTSEngine） ──

    def evaluate_fen(self, board: list, player: int,
                     timeout_ms: int = 2000,
                     lock_timeout_ms: int = 3000) -> Optional[float]:
        """通过 UCI `eval` 命令获取 Pikafish 的 NNUE 静态评估。

        Args:
            board: 10×9 棋盘
            player: 走子方（1=红 2=黑，用于生成 FEN 的 w/b）
            timeout_ms: 获取锁后读取 Final evaluation 的超时（毫秒）
            lock_timeout_ms: 获取引擎锁的超时（毫秒）。引擎正忙
                （如主线程正式搜索持锁）时放弃返回 None，避免调用方
                （LLM 工具线程）被搜索时长阻塞卡死。

        Returns:
            float: **红方视角**厘兵（正值=红优，负值=黑优，±100≈1兵，
            与 domain.evaluation.evaluate / search 的 MATE_TT_BOUND 量纲
            一致）；引擎不可用 / 锁超时 / 解析失败 / 读取超时返回 None。

        实现说明（已实证）：
        - 引擎 `eval` 输出末尾行为
          "Final evaluation       +0.30 (white side) [with scaled NNUE, ...]"
          white side = 红方视角，单位 = 兵（+0.30 → +30 厘兵）。
        - 走完整 position fen + eval 命令路径，解析 Final evaluation 行。

        适用：LLM evaluate_position 工具增强、训练标签源（蒸馏）、
        根节点/复盘分析。⚠️ 不可用于搜索热路径（_fast_eval）：
        UCI 进程往返为毫秒级，会拖垮叶节点评估（与残局库本地查询同理，
        搜索循环内禁止同步外部进程调用）。
        """
        if not self._available or not self._proc:
            return None

        # 等锁带超时：引擎正忙（主线程搜索持锁数秒）时放弃，避免
        # LLM 工具线程被阻塞卡死（timeout_ms 只约束锁内读取）
        if not self._lock.acquire(timeout=max(0.5, lock_timeout_ms / 1000.0)):
            return None
        try:
            self._purge_lines()
            fen = board_to_fen(board, player)
            self._send(f'position fen {fen}')
            self._send('eval')
            deadline = time.time() + timeout_ms / 1000.0
            while time.time() < deadline:
                if self._proc.poll() is not None:
                    break
                line = self._read_line(0.5)
                if line is None:
                    if self._reader_dead:
                        break
                    continue
                line_str = line.strip()
                if line_str.startswith('Final evaluation'):
                    # 严格匹配红方视角标记：若未来引擎改为按走子方
                    # (black side) 输出，必须取反而不是静默用错符号
                    m = re.search(
                        r'Final evaluation\s+([+-]?\d+(?:\.\d+)?)\s+'
                        r'\((white|black) side\)', line_str)
                    if m:
                        val = float(m.group(1)) * 100.0
                        return val if m.group(2) == 'white' else -val
            return None
        except (OSError, ValueError, BrokenPipeError):
            self._available = False
            self._kill_proc()
            return None
        finally:
            self._lock.release()

    def _search_locked(self, game, player: int,
                       time_ms: int) -> Optional[tuple]:
        """锁内搜索核心（调用方必须已持 self._lock）。

        执行 purge → position → go movetime → read_bestmove →
        合法性校验，返回合法 bestmove。搜索期间 _top_moves 被
        _finalize_top_moves 导出（bestmove 收到时）。
        """
        self._top_moves = []  # 清除缓存
        self._top_moves_dict = {}
        fen = board_to_fen(game.board, player)
        self._purge_lines()
        self._send(f'position fen {fen}')
        self._send(f'go movetime {time_ms}')

        best_move_uci = self._read_bestmove(time_ms)
        if not best_move_uci:
            print(f"[Pikafish.search] _read_bestmove 返回空 "
                  f"(time_ms={time_ms}, fen={fen[:40]}...)",
                  file=sys.stderr, flush=True)
            return None
        move = _uci_to_tuple(best_move_uci)
        if not move:
            print(f"[Pikafish.search] _uci_to_tuple 失败 "
                  f"(uci={best_move_uci!r})",
                  file=sys.stderr, flush=True)
            return None
        # 验证走法合法性 — 最终防线
        if move not in game.get_all_legal_moves(player):
            print(f"[Pikafish.search] 走法非法 "
                  f"(uci={best_move_uci!r} move={move})",
                  file=sys.stderr, flush=True)
            return None
        return move

    def search(self,
               game,
               player: int,
               time_ms: Optional[int] = None) -> Optional[tuple]:
        """搜索最佳走法（同步，无锁超时——适合单线程调用方如数据生成）。

        Args:
            game: ChineseChessGame 实例
            player: 当前走子方 (1=红, 2=黑)
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
                return self._search_locked(game, player, time_ms)
            except (OSError, ValueError, BrokenPipeError):
                self._available = False
                self._kill_proc()
                return None

    def search_atomic(self, game, player: int, time_ms: int,
                      multipv: int = 1,
                      lock_timeout_ms: int = 5000
                      ) -> Optional[Tuple[tuple, list]]:
        """持锁原子搜索：设置 MultiPV → 搜索 → 恢复 MultiPV 1 → 返回快照。

        供并发调用方（LLM 工具线程）使用，一次持锁内完成全部步骤：

        - **消除 MultiPV 撕裂**：set/search/restore 合并为原子段，其他
          线程无法在中间插入（旧的"先切 MultiPV 再搜索、事后恢复"写法
          存在"错误 MultiPV 值启动搜索"的窗口）。
        - **消除 _top_moves 跨搜索 TOCTOU**：候选快照在持锁期间导出
          （_search_locked 已 finalize），返回后不再另起锁读取，不会被
          并发 search_async 的清空/覆盖污染。
        - **锁获取超时**：引擎正忙（如主线程正式搜索持锁）时
          lock_timeout_ms 后放弃返回 None，不阻塞调用方。

        Returns:
            (best_move, top_snapshot) 或 None（锁超时/引擎不可用/搜索失败）
            top_snapshot: [(move, score_cp), ...] 走子方视角厘兵，
            按 multipv 序号升序（与 _top_moves 同源数据）。
        """
        if not self._available or not self._proc:
            return None
        if not self._lock.acquire(timeout=max(1.0, lock_timeout_ms / 1000.0)):
            return None
        try:
            n = max(1, int(multipv))
            if n != self._multipv:
                self._send(f'setoption name MultiPV value {n}')
                self._multipv = n
            try:
                move = self._search_locked(game, player, time_ms)
            finally:
                # 无论搜索成功/失败/被 stop 中断，都恢复正式配置 MultiPV 1
                if self._multipv != 1:
                    self._send('setoption name MultiPV value 1')
                    self._multipv = 1
            if not move:
                return None
            # 持锁内快照：finalize 已按序号升序导出，直接拷贝
            snapshot = list(self._top_moves)
            return (move, snapshot)
        except (OSError, ValueError, BrokenPipeError):
            self._available = False
            self._kill_proc()
            return None
        finally:
            self._lock.release()

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

        # 原子快照：深拷贝棋盘，从副本推导 FEN + 合法走法（避免 TOCTOU）
        # 注：_top_moves 缓存的重置在 daemon 持锁段内进行（见 _run），
        # 锁外重置会让并发的 search_atomic 在"重置→搜索"窗口内 finalize
        # 进本搜索的空缓存，污染 _top_moves
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
            callback(None, f'棋盘快照失败: {e}')
            return

        def _run():
            move = None
            error = ''
            try:
                with self._lock:
                    # 重置 MultiPV 收集缓存（持锁内，与 _search_locked 同序）
                    self._top_moves = []
                    self._top_moves_dict = {}
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
                        # 区分进程死亡 vs 真超时：进程静默退出时 _read_bestmove
                        # 因 poll() 非 None 直接 break 返回 None，不抛异常，
                        # 若不在此标记不可用，后续每次搜索都会白跑一次死引擎
                        # 再 MCTS 兜底，且日志反复误报"超时"。
                        proc = self._proc
                        if proc is None or proc.poll() is not None:
                            if proc is not None:
                                error = (f'引擎进程已意外退出'
                                         f'（退出码 0x{proc.returncode & 0xFFFFFFFF:08X}）')
                            else:
                                error = '引擎进程已关闭'
                            self._available = False
                            if proc is not None:
                                self._kill_proc()
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

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _parse_multipv_line(self, line: str) -> None:
        """解析 MultiPV info 行，按 multipv 序号存最终迭代结果。

        格式：info multipv 1 score cp 120 pv e0e1 ...
        分数可能为 mate N（将杀距离），此时用 ±99999 替代。

        引擎每次迭代加深都会重复输出 multipv 1..N 行；只保留每个
        序号**最新**（最深迭代）的一条，否则 _top_moves 会累积
        所有迭代的行（前 3 条是浅迭代结果，语义错误）。
        """
        parts = line.split()
        try:
            mp_idx = None
            pv_idx = None
            score_val = 0
            for i, token in enumerate(parts):
                if token == 'multipv' and i + 1 < len(parts):
                    mp_idx = int(parts[i + 1])
                elif token == 'score':
                    # score cp X 或 score mate Y
                    if i + 2 < len(parts):
                        if parts[i + 1] == 'cp':
                            score_val = int(parts[i + 2])
                        elif parts[i + 1] == 'mate':
                            mate_in = int(parts[i + 2])
                            score_val = 99999 if mate_in > 0 else -99999
                elif token == 'pv' and i + 1 < len(parts):
                    # pv 后面的第一个走法是该主变的 UCI 走法
                    pv_idx = i
                    break
            if mp_idx is not None and pv_idx is not None and pv_idx + 1 < len(parts):
                uci = parts[pv_idx + 1]
                move = _uci_to_tuple(uci)
                if move:
                    self._top_moves_dict[mp_idx] = (move, score_val)
        except (ValueError, IndexError):
            pass  # 格式异常，静默跳过

    def _finalize_top_moves(self) -> None:
        """搜索结束（收到 bestmove）后：按 multipv 序号升序导出最终结果。

        _top_moves_dict 只含每个序号的最新（最深迭代）条目；
        转成列表供 search_atomic 快照读取。
        """
        self._top_moves = [self._top_moves_dict[i]
                           for i in sorted(self._top_moves_dict)]

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

    def restart(self) -> bool:
        """引擎死亡后自动重启（重新 Popen + UCI 握手 + 棋力配置）。

        返回是否可用（重启成功，或进程本就真实存活）。调用方
        （engine_bridge 健康检查）检测到进程死亡后调用，成功则继续
        用 Pikafish，失败才回退 MCTS，避免"引擎中途死亡后整局棋力
        骤降为 MCTS 兜底"。

        注意：进程被 kill 后 _available 仍可能为 True（假可用），
        守卫必须检查进程真实存活（poll），而非仅看标志。
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return True  # 进程真实存活
        with self._lock:
            try:
                self._kill_proc()      # 清理残留进程（若有）
                self._reader_dead = False
                self._top_moves = []
                self._top_moves_dict = {}
                self._error_msg = ''
                self._available = False  # 明确复位；_start_engine 成功会重新置位
                binary_path = _find_pikafish()
                if binary_path and os.path.isfile(binary_path):
                    self._start_engine(binary_path)
            except Exception:
                self._available = False
        if not self._available and not self._error_msg:
            self._error_msg = 'Pikafish 重启失败'
        return self._available

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
                    # ── 棋力配置（正式走子用单主变 + 多线程 + 大哈希）──
                    # MultiPV 1：MultiPV>1 会把算力摊给多条主变、降低单线
                    # 深度，正式走子不必要；LLM search_best_move 工具需要
                    # 候选时经 search_atomic(multipv=3) 临时切换、用后恢复。
                    # Threads：单线程（UCI 默认）棋力明显偏弱，按核数启用。
                    # Hash：默认 16MB 换位表过小，中残局重复局面重算。
                    # Move Overhead 0：本地进程无网络/界面延迟，UCI 默认
                    # 10ms 纯属浪费（每步省 10ms，长对局累计可观）。
                    try:
                        self._send(f'setoption name Threads value {self._threads}')
                        self._send(f'setoption name Hash value {self._hash_mb}')
                        self._send('setoption name MultiPV value 1')
                        self._send('setoption name Move Overhead value 0')
                    except Exception:
                        pass  # 旧版引擎个别 option 不支持，静默回退
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
            print(f"[Pikafish] reader 线程异常退出: {e}",
                  file=sys.stderr, flush=True)
        finally:
            # 仅当自己仍是**当前** reader 线程时才标记死亡/放哨兵：
            # restart() 场景下，旧 reader 的 finally 可能在新 reader 启动
            # 之后才执行，无条件置位会覆盖新 reader 的存活状态，导致
            # 重启后的搜索全部被误判为"reader 已死"而读不到数据。
            if threading.current_thread() is self._reader_thread:
                self._reader_dead = True
                try:
                    self._out_q.put(None)  # EOF 哨兵，唤醒等待中的读取方
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
        """发送命令到引擎。返回 True 表示发送成功。

        写操作受 _stdin_lock 保护（防并发 stop() 与搜索线程的命令撕裂）；
        不取 self._lock——stop() 依赖本方法在搜索线程持锁期间仍可调用。
        """
        try:
            if self._proc and self._proc.stdin:
                with self._stdin_lock:
                    self._proc.stdin.write(command + '\n')
                    self._proc.stdin.flush()
                return True
        except (OSError, BrokenPipeError):
            pass
        return False

    def _drain_out_q(self) -> None:
        """非阻塞排空行队列（丢弃残留输出）。"""
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass

    def _purge_lines(self, timeout: float = 1.0) -> None:
        """排空行队列 + isready/readyok 握手，确保上一局已收尾。

        上次搜索超时残留的 bestmove 若留在队列里，会被下一次搜索的
        _read_bestmove 第一行读到——把上一局的走法当成本局结果返回
        （合法性校验拦不住"合法但错误"的走法）。每次发 position 前
        调用（持锁状态下，残留只可能来自上次超时）。

        固定 sleep 无法可靠覆盖"stop→bestmove"的异步延迟窗口（引擎卡顿
        时可能远超 50ms），改用 UCI isready 握手：引擎在完成上一局收尾
        （吐出 bestmove）后才会响应 readyok，天然形成同步屏障，且不引入
        固定等待。代价是一次往返（毫秒级），搜索以秒计可忽略。
        """
        self._drain_out_q()
        if not self._proc or not self._proc.stdout:
            return
        if not self._send('isready'):
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                return  # 进程已死，_read_bestmove 会因 poll() 直接失败
            line = self._read_line(min(0.2, deadline - time.time()))
            if line and 'readyok' in line:
                break
            if self._reader_dead and self._out_q.empty():
                return
        # 握手后再排空一次，消费 readyok 之前的任何残留行
        self._drain_out_q()

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
        - 沿途收集 multipv info 行到 self._top_moves（供 search_atomic 快照读取）
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
                    # bestmove 已收到：导出最终迭代的 MultiPV 结果
                    # （_top_moves_dict 只含每个序号的最新条目）
                    self._finalize_top_moves()
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
                    # 排空阶段收到（过期）bestmove 也导出 MultiPV，
                    # 与正常路径保持一致（超时并不等于无候选）
                    self._finalize_top_moves()
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
