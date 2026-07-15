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

import subprocess
import threading
import time
import os
import atexit
import shutil
from typing import Optional, Dict, List, Tuple


# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

# Pikafish 二进制路径（相对于项目根目录）
PIKAFISH_BINARY = 'engines/pikafish.exe'

# 引擎启动超时（秒）
ENGINE_STARTUP_TIMEOUT = 10.0

# 默认搜索时间（毫秒）
DEFAULT_MOVE_TIME_MS = 5000

# MultiPV 设置（用于 get_top_moves）
DEFAULT_MULTIPV = 3


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
                 move_time_ms: int = DEFAULT_MOVE_TIME_MS,
                 multi_pv: int = DEFAULT_MULTIPV):
        """初始化 Pikafish 引擎。

        Args:
            binary_path: Pikafish 可执行文件路径。None 则自动查找。
            move_time_ms: 每步搜索时间（毫秒）。
            multi_pv: MultiPV 行数（用于 get_top_moves）。
        """
        self.move_time_ms = move_time_ms
        self.multi_pv = multi_pv
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._available = False
        self._error_msg: str = ''  # 启动失败时的诊断信息
        self._last_multi_pv: List[Tuple[tuple, int, float]] = []

        if binary_path is None:
            binary_path = _find_pikafish()

        if binary_path and os.path.isfile(binary_path):
            self._start_engine(binary_path)
            if self._available:
                # 注册退出清理：即使应用崩溃也尽力杀子进程
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
                fen = _game_to_fen(game, player)
                self._send(f'position fen {fen}')
                self._send(f'go movetime {time_ms}')

                best_move_uci = self._read_bestmove(time_ms)
                if best_move_uci:
                    move = _uci_to_tuple(best_move_uci)
                    # 验证走法合法性 — 最终防线
                    if move and move in game.get_all_legal_moves(player):
                        return move
            except (OSError, ValueError, BrokenPipeError):
                self._available = False
                self._kill_proc()
            return None

    def get_top_moves(self, n: int = 3) -> List[Tuple[tuple, int, float]]:
        """返回前 N 个最优走法（使用 MultiPV）。

        注意：此方法会执行一次搜索以获取 MultiPV 信息。
        若最近一次 search() 已启用 MultiPV，则返回缓存结果。

        Returns:
            [(move, visits_approx, score), ...] 按评分降序排列
        """
        if not self._available:
            return []

        # 返回最近一次 MultiPV 搜索的缓存结果
        if self._last_multi_pv:
            return self._last_multi_pv[:n]

        return []

    def close(self):
        """关闭引擎进程。"""
        if self._proc:
            try:
                self._send('quit')
                self._proc.wait(timeout=3.0)
            except Exception:
                self._proc.kill()
            finally:
                self._proc = None
                self._available = False

    def _kill_proc(self):
        """强制终止引擎进程（异常恢复用）。"""
        if self._proc:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            except Exception:
                pass
            finally:
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
            self._send('uci')
            # 等待 uciok
            start = time.time()
            while time.time() - start < ENGINE_STARTUP_TIMEOUT:
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

                line = self._proc.stdout.readline()
                if not line:
                    time.sleep(0.05)  # 避免忙等待
                    continue
                if 'uciok' in line:
                    self._available = True
                    return
            # 超时未收到 uciok → 清理僵尸进程
            self._error_msg = (
                f'Pikafish 启动超时（{ENGINE_STARTUP_TIMEOUT}s 未收到 uciok）'
            )
            self._kill_proc()
        except (OSError, FileNotFoundError) as e:
            self._error_msg = f'无法启动 Pikafish: {e}'

    def _send(self, command: str):
        """发送命令到引擎。"""
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(command + '\n')
            self._proc.stdin.flush()

    def _read_bestmove(self, time_ms: int = 5000) -> Optional[str]:
        """读取引擎输出直到 bestmove 行。

        安全机制：
        - 每次读取前检查进程是否存活（poll）
        - 总时间上限 = 实际搜索时间 + 30s 兜底
        - 超时或进程死亡时返回 None（调用方回退 MCTS）
        """
        if not self._proc or not self._proc.stdout:
            return None

        # 总时间上限：实际搜索时间 + 30s 兜底
        deadline = time.time() + (time_ms / 1000.0) + 30.0
        multi_pv_results: Dict[int, Tuple[str, int, float]] = {}

        while time.time() < deadline:
            # 进程已死 → 立即退出，不再阻塞 readline
            if self._proc.poll() is not None:
                break

            line = self._proc.stdout.readline()
            if not line:
                break  # EOF

            line = line.strip()

            # 解析 MultiPV 信息行
            if 'multipv' in line and ' pv ' in line:
                try:
                    mpv = _parse_multipv_line(line)
                    if mpv:
                        pv_num, uci_move, cp_score = mpv
                        multi_pv_results[pv_num] = (uci_move, cp_score)
                except (ValueError, IndexError):
                    pass

            if line.startswith('bestmove'):
                parts = line.split()
                if len(parts) >= 2:
                    best = parts[1]

                    # 构建 MultiPV 结果列表
                    if multi_pv_results:
                        results = []
                        for pv_num in sorted(multi_pv_results.keys()):
                            uci_move, cp_score = multi_pv_results[pv_num]
                            move_tuple = _uci_to_tuple(uci_move)
                            if move_tuple:
                                approx_visits = max(1, 100 + cp_score)
                                normalized_score = cp_score / 100.0
                                results.append((move_tuple, approx_visits, normalized_score))
                        self._last_multi_pv = results

                    return best

        # 超时或进程死亡 → 发送 stop 并排空残留数据，防止污染下次搜索
        try:
            self._send('stop')
            # 继续读取直到收到（过期）bestmove 或 EOF
            drain_deadline = time.time() + 5.0
            while time.time() < drain_deadline:
                if self._proc and self._proc.poll() is not None:
                    break
                if self._proc and self._proc.stdout:
                    line = self._proc.stdout.readline()
                    if not line:
                        break
                    if line.strip().startswith('bestmove'):
                        break
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════════════════════

def _game_to_fen(game, current_player: int) -> str:
    """将 10×9 棋盘转为中国象棋 FEN 字符串。

    坐标系约定（经验证）：
    - 内部 row 0 = 黑方底线（棋盘顶部）
    - Pikafish FEN：row 0 = 黑方底线 — 与内部一致，**不反转**
    - Pikafish UCI 坐标：与 FEN 一致，**不反转**
    - w/b：Pikafish 方向相反 — w = 黑方走, b = 红方走

    格式：rows/.../rows w/b
    - 大写 = 红方，小写 = 黑方
    """
    rows = []
    for r in range(10):
        row_str = ""
        empty = 0
        for c in range(9):
            p = game.board[r][c]
            if p == '.':
                empty += 1
            else:
                if empty > 0:
                    row_str += str(empty)
                    empty = 0
                row_str += p
        if empty > 0:
            row_str += str(empty)
        rows.append(row_str)
    # Pikafish w/b 方向相反：w = 黑方走, b = 红方走
    side = 'b' if current_player == 1 else 'w'
    return '/'.join(rows) + ' ' + side


def _uci_to_tuple(uci_move: str) -> Optional[tuple]:
    """将 Pikafish UCI 走法字符串转为内部元组格式。

    Pikafish 坐标系与内部一致：row 0 = 黑方底线。
    UCI 格式: <from_col><from_row><to_col><to_row>
    例如: 'g4g5'（黑卒 G5→G6）→ (4, 6, 5, 6)
          'b2e2'（红炮二平五）→ (7, 1, 7, 4)

    col: a-i → 0-8（不变）
    row: 0-9 → 0-9（不变）
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


def _parse_multipv_line(line: str) -> Optional[Tuple[int, str, int]]:
    """解析 MultiPV 信息行。

    格式示例:
      info depth 20 multipv 1 score cp 35 pv b0c2 ...

    Returns:
        (multipv_number, first_move_uci, cp_score) 或 None
    """
    parts = line.split()
    mpv_num = None
    cp_score = None
    pv_move = None

    for i, p in enumerate(parts):
        if p == 'multipv' and i + 1 < len(parts):
            mpv_num = int(parts[i + 1])
        elif p == 'cp' and i + 1 < len(parts):
            cp_score = int(parts[i + 1])
        elif p == 'pv' and i + 1 < len(parts):
            pv_move = parts[i + 1]

    if mpv_num is not None and pv_move is not None:
        return (mpv_num, pv_move, cp_score if cp_score is not None else 0)
    return None
