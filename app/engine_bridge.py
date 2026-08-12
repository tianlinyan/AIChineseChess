"""引擎桥接模块 — Pikafish/MCTS 生命周期管理 + 异步回调。

从 GameController 中抽取，负责：
- Pikafish UCI 引擎的延迟初始化和生命周期
- MCTS 后台线程搜索
- 跨线程信号中继（daemon → Qt 主线程）
- 搜索深度到模拟数/时间的映射
"""

import threading
from typing import Optional, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from domain.constants import (
    MCTS_TIME_LIMIT, MCTS_FALLBACK_SIMULATIONS, MCTS_FALLBACK_TIME_LIMIT,
    PIECE_SYMBOLS, format_move,
)
from domain.game import ChineseChessGame
from domain.mcts import MCTSEngine

try:
    from domain.pikafish import PikafishEngine
except ImportError:
    PikafishEngine = None


class EngineBridge(QObject):
    """Pikafish / MCTS 引擎管理。

    所有异步搜索结果通过 search_done 信号投递到 Qt 主线程。
    版本门控检查委托给注入的 check_callback_valid 回调。
    """

    # 跨线程信号：daemon 线程 emit → Qt 自动排队到主线程
    # 参数: (move, player, on_done, captured_version, captured_cancel, error)
    search_done = pyqtSignal(tuple)
    # 人类提示搜索结果中继（独立信号，不触发 _finish_ai_move）
    hint_done = pyqtSignal(tuple)

    # 搜索深度 → MCTS 模拟次数映射
    _DEPTH_SIMS_MAP = {1: 500, 2: 800, 3: 1200, 4: 1600,
                       5: 2000, 6: 3000, 7: 4000, 8: 5000}

    def __init__(self,
                 log_cb: Callable,
                 check_version_cb: Callable,
                 finish_move_cb: Callable,
                 get_search_depth: Callable,
                 get_cancel_version: Callable,
                 get_game_version: Callable) -> None:
        super().__init__()
        self._log = log_cb
        self._check_version = check_version_cb
        self._finish_move = finish_move_cb
        self._get_depth = get_search_depth
        self._get_cancel = get_cancel_version
        self._get_game_ver = get_game_version

        self._pikafish: Optional['PikafishEngine'] = None
        self._pikafish_ready: bool = False
        self._active_mcts: Optional['MCTSEngine'] = None
        self._mcts_thread: Optional[threading.Thread] = None

    # ── 公开接口 ──

    @property
    def pikafish_available(self) -> bool:
        return self._pikafish is not None and self._pikafish.available

    @property
    def pikafish(self) -> Optional['PikafishEngine']:
        return self._pikafish

    def init_pikafish(self) -> None:
        """延迟初始化 Pikafish + NNUE。需在 main 就绪后调用。"""
        if self._pikafish_ready:
            return
        self._pikafish_ready = True

        # ── Pikafish ──
        if PikafishEngine is None:
            return
        try:
            self._pikafish = PikafishEngine()
            if self._pikafish.available:
                self._log("🐟 Pikafish 引擎已就绪（NNUE 评估，大师级棋力）", 'INFO')
            else:
                err = self._pikafish.error_msg
                if err:
                    for line in err.split('\n'):
                        self._log(f"[Pikafish] {line.strip()}", 'WARNING')
                self._pikafish = None
        except Exception as e:
            self._log(f"[Pikafish] 初始化异常: {e}", 'WARNING')
            self._pikafish = None

        # ── 本地 NNUE 评估网络 ──
        try:
            from domain.nnue import get_nnue
            nnue = get_nnue()
            if nnue is not None:
                self._log("🧠 本地 NNUE 评估网络已加载 "
                          "(Alpha-Beta 搜索加速)", 'INFO')
            else:
                self._log("🧠 本地 NNUE 权重未找到，搜索使用手工评估", 'INFO')
        except Exception as e:
            self._log(f"[NNUE] 加载异常: {e}", 'WARNING')

    def start_search(self, game: ChineseChessGame, player: int,
                     on_done: Callable) -> None:
        """启动引擎搜索（Pikafish 优先，回退 MCTS 后台线程）。

        结果经 search_done 信号排队回主线程后调用 on_done(best_move_or_None, player)。
        """
        depth = self._get_depth()
        cancel_version = self._get_cancel()
        game_version = self._get_game_ver()

        self._log("🔍 启动引擎搜索...", 'INFO')

        # Pikafish 异步路径
        if self.pikafish_available and on_done is not None:
            time_ms = self._pikafish_time_s(depth) * 1000
            self._log(f"  🐟 Pikafish 搜索中（时限 {time_ms // 1000}s）...", 'INFO')
            self._pikafish.search_async(
                game, player, time_ms=time_ms,
                callback=lambda m, err: self.search_done.emit(
                    (m, player, on_done, game_version, cancel_version, err)))
            return

        # MCTS 后台线程路径
        if on_done is not None:
            sims = self._depth_to_sims(depth)
            self._start_mcts_async(game, player, sims, on_done,
                                   game_version, cancel_version)

    def start_mcts_fallback(self, game: ChineseChessGame, player: int,
                            on_done: Callable) -> None:
        """MCTS 快速回退搜索（后台线程，短时限）。"""
        game_version = self._get_game_ver()
        cancel_version = self._get_cancel()
        sims = MCTS_FALLBACK_SIMULATIONS
        self._log(f"  🌳 MCTS 回退 ({sims}次模拟)", 'INFO')
        result = {}
        g = game.snapshot()
        g.current_player = player
        engine = MCTSEngine(max_simulations=sims, time_limit=MCTS_FALLBACK_TIME_LIMIT)
        self._active_mcts = engine

        def _run():
            move = None
            try:
                move = engine.search(g, player)
                result['sims'] = engine.simulations
            except Exception as e:
                result['error'] = f'MCTS 回退异常: {e}'
            finally:
                self._active_mcts = None
                self.search_done.emit((move, player, on_done, game_version, cancel_version, ''))

        self._mcts_thread = threading.Thread(target=_run, daemon=True)
        self._mcts_thread.start()

    def pikafish_time_s(self) -> int:
        """当前搜索深度对应的 Pikafish 搜索秒数。"""
        return self._pikafish_time_s(self._get_depth())

    def stop_all(self) -> None:
        """停止在飞的后台搜索（reset/pause/shutdown 时调用）。"""
        if self._pikafish is not None:
            self._pikafish.stop()
        mcts = self._active_mcts
        if mcts is not None:
            try:
                mcts.stop()
            except Exception:
                pass

    def handle_search_result(self, args: tuple) -> None:
        """主线程处理 Pikafish/MCTS 异步搜索结果。

        由 search_done 信号连接。版本门控后调用 on_done。
        """
        try:
            move, player, on_done, cv, cc, error = args
        except Exception as e:
            self._log(f"[PF错误] 搜索回调参数解包失败: {e}", 'ERROR')
            return
        try:
            # PF 回调不 clean up（老回调不能清新对局 busy）
            if not self._check_version(cv, cc, 'PF', needs_cleanup=False):
                return
            if move is None and error:
                self._log(f"  ⚠️ Pikafish 搜索失败: {error}", 'WARNING')
            on_done(move, player)
        except Exception as e:
            self._log(f"[PF诊断] 搜索回调异常: {e}", 'ERROR')
            self._finish_move()

    def start_hint_search(self, game: ChineseChessGame, player: int,
                          on_done: Callable) -> None:
        """启动引擎提示搜索（Pikafish 优先，MCTS 兜底），结果通过 on_done 回调返回。

        用于人类玩家的参考提示，不干扰主搜索流程。
        on_done(move_or_None, game_version, cancel_version) 在 Qt 主线程被调用。
        """
        g = game.snapshot()
        g.current_player = player
        game_version = self._get_game_ver()
        cancel_version = self._get_cancel()

        # ── Pikafish 路径 ──
        if self.pikafish_available:
            pf = self._pikafish
            # 启动前健康检查：进程可能在上次搜索后意外死亡
            if pf._proc is not None and pf._proc.poll() is not None:
                self._log(
                    f"  ⚠️ Pikafish 进程已意外退出（code={pf._proc.returncode}），"
                    f"回退 MCTS", 'WARNING')
                pf._available = False
                # 继续往下走 MCTS
            else:
                self._log(f"  🐟 Pikafish 提示搜索中（{int(MCTS_TIME_LIMIT)}s）...", 'INFO')
                self._start_pf_hint(g, game_version, cancel_version, on_done)
                return

        # ── MCTS 兜底路径 ──
        self._log("  🌳 Pikafish 不可用，MCTS 提示搜索中（800次模拟）...", 'INFO')

        def _run_mcts() -> None:
            move = None
            try:
                engine = MCTSEngine(max_simulations=800, time_limit=5.0)
                move = engine.search(g, player)
            except Exception:
                pass
            finally:
                self.hint_done.emit((move, game_version, cancel_version, on_done))

        threading.Thread(target=_run_mcts, daemon=True).start()

    # ── 提示搜索内部实现 ──

    def _start_pf_hint(self, g: ChineseChessGame, game_version: int,
                       cancel_version: int, on_done: Callable) -> None:
        """启动 Pikafish 提示搜索，复用 search_async（与 AI 搜索同路径）。"""

        def _on_result(move, error: str) -> None:
            if error:
                self._log(f"  ⚠️ Pikafish 提示搜索失败: {error}", 'WARNING')
            # 用 pyqtSignal 中继到主线程
            self.hint_done.emit((move, game_version, cancel_version, on_done))

        self._pikafish.search_async(g, g.current_player,
                                    time_ms=int(MCTS_TIME_LIMIT * 1000),
                                    callback=_on_result)

    def shutdown(self) -> None:
        """清理引擎资源。"""
        self.stop_all()
        mcts_thread = self._mcts_thread
        if mcts_thread and mcts_thread.is_alive():
            mcts_thread.join(timeout=2.0)
        try:
            self.search_done.disconnect()
        except TypeError:
            pass
        if self._pikafish:
            try:
                self._pikafish.close()
            except Exception:
                pass
            self._pikafish = None

    # ── 内部 ──

    def _depth_to_sims(self, depth: int) -> int:
        return self._DEPTH_SIMS_MAP.get(depth, 2000)

    def _pikafish_time_s(self, depth: int) -> int:
        return min(depth * 3, int(MCTS_TIME_LIMIT))

    def _start_mcts_async(self, game: ChineseChessGame, player: int,
                          sims: int, on_done: Callable,
                          game_version: int, cancel_version: int) -> None:
        """后台线程跑 MCTS，结果经 search_done 信号回主线程。"""
        depth = self._get_depth()
        g = game.snapshot()
        g.current_player = player

        desc = "均匀先验"
        self._log(f"  🌳 MCTS 启动 ({desc}, {sims}次模拟, 深度={depth})", 'INFO')
        result = {}
        engine = MCTSEngine(max_simulations=sims, time_limit=MCTS_TIME_LIMIT)
        self._active_mcts = engine

        def _run():
            move = None
            try:
                move = engine.search(g, player)
                result['sims'] = engine.simulations
                result['top'] = engine.get_top_moves(3)
            except Exception as e:
                result['error'] = f'MCTS 搜索异常: {e}'
            finally:
                self._active_mcts = None
                self.search_done.emit(
                    (move, player, _logged_on_done,
                     game_version, cancel_version, ''))

        def _logged_on_done(move, player):
            if move:
                fr, fc, tr, tc = move
                pn = PIECE_SYMBOLS.get(game.board[fr][fc], '?')
                self._log(f"  ✅ MCTS选择: {pn} "
                          f"{format_move(fr, fc, tr, tc)} "
                          f"({result.get('sims', 0)}次模拟)", 'INFO')
                for i, (m, visits, val) in enumerate(result.get('top', [])):
                    mfr, mfc, mtr, mtc = m
                    mpn = PIECE_SYMBOLS.get(game.board[mfr][mfc], '?')
                    self._log(f"    {i+1}. {mpn} "
                              f"{format_move(mfr, mfc, mtr, mtc)} "
                              f"[访问{visits}次, 价值{val:.3f}]", 'INFO')
            else:
                detail = (f"（{result['error']}）"
                          if result.get('error') else '')
                self._log(f"  ⚠️ MCTS未找到走法{detail}", 'WARNING')
            on_done(move, player)

        self._mcts_thread = threading.Thread(target=_run, daemon=True)
        self._mcts_thread.start()
