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
    format_move, format_chinese_notation,
)
from domain.game import ChineseChessGame
from domain.mcts import MCTSEngine, DEFAULT_SIMULATIONS

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
    # Pikafish/NNUE 后台初始化完成：参数 (pf_or_None, diag_lines)
    init_done = pyqtSignal(tuple)

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
        # 后台初始化结果排队回主线程处理（EngineBridge 在主线程创建）
        self.init_done.connect(self._on_init_done)

        self._pikafish: Optional['PikafishEngine'] = None
        self._pikafish_ready: bool = False
        # shutdown 后置位：init 线程的迟到结果不再覆盖已清理的状态
        self._shutdown: bool = False
        # 在飞的 MCTS 搜索：按线程对象关联引擎与线程，支持并发兜底搜索。
        # 单槽位会让旧线程的 finally 清空新搜索的引用，导致 stop_all 失效。
        self._active_mcts: dict = {}     # thread -> MCTSEngine

    # ── 公开接口 ──

    @property
    def pikafish_available(self) -> bool:
        # 不仅要 available 标志，还要进程真实存活：PikafishEngine.restart()
        # 的文档明确"进程被 kill 后 _available 仍可能为 True（假可用）"，
        # 控制器据此决定"工具由 Pikafish 提供"的文案，假可用会误导
        pf = self._pikafish
        return (pf is not None and pf.available
                and pf._proc is not None and pf._proc.poll() is None)

    @property
    def pikafish(self) -> Optional['PikafishEngine']:
        return self._pikafish

    def init_pikafish(self) -> None:
        """延迟初始化 Pikafish + NNUE。需在 main 就绪后调用。

        构造 PikafishEngine 含 UCI 握手，最多阻塞 10s——放在后台线程执行，
        避免主窗口启动期假死；结果经 init_done 信号排队回主线程写日志、
        更新状态（不跨线程触碰 QTextEdit）。
        """
        if self._shutdown:
            # 竞态守卫：QTimer.singleShot(0) 可能在 closeEvent 的
            # shutdown() 之后才触发，此时不得再拉起引擎子进程
            return
        if self._pikafish_ready:
            return
        self._pikafish_ready = True

        def _init() -> None:
            diag_lines: list = []  # [(text, level), ...]
            pf = None

            # ── Pikafish ──
            if PikafishEngine is not None:
                try:
                    pf = PikafishEngine()
                    if pf.available:
                        diag_lines.append(("🐟 Pikafish 引擎已就绪"
                                          "（NNUE 评估，大师级棋力）", 'INFO'))
                    else:
                        err = pf.error_msg
                        if err:
                            diag_lines.extend(
                                (f"[Pikafish] {line.strip()}", 'WARNING')
                                for line in err.split('\n'))
                        pf = None
                except Exception as e:
                    diag_lines.append((f"[Pikafish] 初始化异常: {e}", 'WARNING'))
                    pf = None

            # ── 本地 NNUE 评估网络 ──
            try:
                from domain.nnue import get_nnue
                nnue = get_nnue()
                if nnue is not None:
                    diag_lines.append(("🧠 本地 NNUE 评估网络已加载 "
                                      "(Alpha-Beta 搜索加速)", 'INFO'))
                else:
                    diag_lines.append(("🧠 本地 NNUE 权重未找到，"
                                      "搜索使用手工评估", 'INFO'))
            except Exception as e:
                diag_lines.append((f"[NNUE] 加载异常: {e}", 'WARNING'))

            # 回主线程（QObject 信号自动排队，不直接跨线程调用 _log）
            self.init_done.emit((pf, diag_lines))

        threading.Thread(target=_init, daemon=True).start()

    def _on_init_done(self, result: tuple) -> None:
        """主线程：应用后台初始化结果（更新状态 + 写日志）。"""
        try:
            pf, diag_lines = result
        except (TypeError, ValueError):
            return
        if self._shutdown:
            # 迟到结果：shutdown 已清理引擎，直接关闭并丢弃
            if pf is not None:
                try:
                    pf.close()
                except Exception:
                    pass
            return
        self._pikafish = pf
        for entry in diag_lines:
            try:
                text, level = entry
            except (TypeError, ValueError):
                text, level = str(entry), 'INFO'
            self._log(text, level)

    def start_search(self, game: ChineseChessGame, player: int,
                     on_done: Callable) -> None:
        """启动引擎搜索（Pikafish 优先，回退 MCTS 后台线程）。

        结果经 search_done 信号排队回主线程后调用 on_done(best_move_or_None, player)。
        """
        depth = self._get_depth()
        cancel_version = self._get_cancel()
        game_version = self._get_game_ver()

        self._log("🔍 启动引擎搜索...", 'INFO')

        # Pikafish 异步路径（健康检查 + 自动重启：进程可能在上次搜索后
        # 静默死亡——假可用或已标记不可用；尝试重启恢复，失败才回退 MCTS，
        # 避免"引擎中途死亡后整局棋力骤降为 MCTS 兜底"）
        if self._pikafish is not None:
            if self._ensure_pikafish_alive('搜索'):
                time_ms = self._pikafish_time_s(depth) * 1000
                self._log(f"  🐟 Pikafish 搜索中（时限 {time_ms // 1000}s）...", 'INFO')
                self._pikafish.search_async(
                    game, player, time_ms=time_ms,
                    callback=lambda m, err: self.search_done.emit(
                        (m, player, on_done, game_version, cancel_version, err)))
                return

        # MCTS 后台线程路径
        sims = self._DEPTH_SIMS_MAP.get(depth, DEFAULT_SIMULATIONS)
        self._start_mcts_async(game, player, sims, on_done,
                               game_version, cancel_version)

    def start_mcts_fallback(self, game: ChineseChessGame, player: int,
                            on_done: Callable) -> None:
        """MCTS 快速回退搜索（后台线程，短时限）。"""
        game_version = self._get_game_ver()
        cancel_version = self._get_cancel()
        g = game.snapshot()
        g.current_player = player

        def _emit(move) -> None:
            self.search_done.emit(
                (move, player, on_done, game_version, cancel_version, ''))

        self._start_fallback_mcts(
            g, player, _emit,
            f"  🌳 MCTS 回退 ({MCTS_FALLBACK_SIMULATIONS}次模拟)")

    def stop_all(self) -> None:
        """停止在飞的后台搜索（reset/pause/shutdown 时调用）。"""
        if self._pikafish is not None:
            self._pikafish.stop()
        for engine in list(self._active_mcts.values()):
            try:
                engine.stop()
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

        # ── Pikafish 路径（健康检查 + 自动重启，与 start_search 一致）──
        if self._pikafish is not None:
            if self._ensure_pikafish_alive('提示搜索'):
                self._log(f"  🐟 Pikafish 提示搜索中（{int(MCTS_TIME_LIMIT)}s）...", 'INFO')

                def _on_result(move, error: str) -> None:
                    if error:
                        self._log(f"  ⚠️ Pikafish 提示搜索失败: {error}", 'WARNING')
                    # 用 pyqtSignal 中继到主线程
                    self.hint_done.emit(
                        (move, game_version, cancel_version, on_done))

                # 提示搜索与 AI 走子路径一致：时限 = 深度×3s（上限 MCTS_TIME_LIMIT）；
                # 日志显示的（30s）为上限提示
                self._pikafish.search_async(
                    g, g.current_player,
                    time_ms=self._pikafish_time_s(self._get_depth()) * 1000,
                    callback=_on_result)
                return

        # ── MCTS 兜底路径（引擎在线程外创建并注册，stop_all 可中断）──
        def _emit(move) -> None:
            self.hint_done.emit((move, game_version, cancel_version, on_done))

        self._start_fallback_mcts(
            g, player, _emit,
            f"  🌳 Pikafish 不可用，MCTS 提示搜索中"
            f"（{MCTS_FALLBACK_SIMULATIONS}次模拟）...")

    def shutdown(self) -> None:
        """清理引擎资源。"""
        self._shutdown = True
        self.stop_all()
        for thread in list(self._active_mcts.keys()):
            if thread.is_alive():
                thread.join(timeout=2.0)
        # 断开全部信号：shutdown 后迟到的 emit 不再投递
        # （hint_done/init_done 的槽仍连着，之前只断开 search_done）
        for sig in (self.search_done, self.hint_done, self.init_done):
            try:
                sig.disconnect()
            except TypeError:
                pass
        if self._pikafish:
            try:
                self._pikafish.close()
            except Exception:
                pass
            self._pikafish = None

    # ── 内部 ──

    def _ensure_pikafish_alive(self, action: str) -> bool:
        """Pikafish 健康检查 + 自动重启（start_search / start_hint_search 共用）。

        进程可能在上次搜索后静默死亡（假可用或已标记不可用）；
        尝试重启恢复，返回是否可继续使用（重启成功或进程本就存活）。
        """
        pf = self._pikafish
        if pf is None:
            return False
        if pf._proc is None or pf._proc.poll() is not None:
            if pf._proc is not None:
                self._log(
                    f"  ⚠️ Pikafish 进程已意外退出（code={pf._proc.returncode}），"
                    f"尝试重启...", 'WARNING')
            if pf.restart():
                self._log(f"  ✅ Pikafish 重启成功，继续{action}", 'INFO')
            else:
                self._log(
                    f"  ⚠️ Pikafish 重启失败（{pf.error_msg}），回退 MCTS",
                    'WARNING')
        return (pf.available and pf._proc is not None
                and pf._proc.poll() is None)

    def _spawn_mcts(self, engine: MCTSEngine, g: ChineseChessGame,
                    player: int, emit_fn: Callable,
                    on_result: Optional[Callable] = None,
                    on_error: Optional[Callable] = None) -> None:
        """后台线程跑 MCTS 并回传结果（MCTS 线程样板共用）。

        emit_fn(move) 负责信号中继（search_done/hint_done）；on_result(move)/
        on_error(e) 可选，在 try 内搜索结果后/异常时调用（_start_mcts_async
        的诊断收集用）；finally 中只移除自己的条目，避免清空并发启动的
        新搜索引用（D1 修复语义）。
        """
        def _run() -> None:
            move = None
            try:
                move = engine.search(g, player)
                if on_result:
                    on_result(move)
            except Exception as e:
                if on_error:
                    on_error(e)
            finally:
                self._active_mcts.pop(threading.current_thread(), None)
                # shutdown 后信号已 disconnect / QObject 可能销毁，
                # emit 会抛 RuntimeError——daemon 线程静默死亡即可，
                # 不能让它冒泡（避免干扰退出时序）
                try:
                    emit_fn(move)
                except Exception:
                    pass

        thread = threading.Thread(target=_run, daemon=True)
        self._active_mcts[thread] = engine
        thread.start()

    def _start_fallback_mcts(self, g: ChineseChessGame, player: int,
                             emit_fn: Callable, log_msg: str) -> None:
        """MCTS 短时限回退搜索（回退/提示路径共用样板）。"""
        sims = MCTS_FALLBACK_SIMULATIONS
        self._log(log_msg, 'INFO')
        engine = MCTSEngine(max_simulations=sims,
                            time_limit=MCTS_FALLBACK_TIME_LIMIT)
        self._spawn_mcts(engine, g, player, emit_fn)

    def _pikafish_time_s(self, depth: int) -> int:
        return min(depth * 3, int(MCTS_TIME_LIMIT))

    def _start_mcts_async(self, game: ChineseChessGame, player: int,
                          sims: int, on_done: Callable,
                          game_version: int, cancel_version: int) -> None:
        """后台线程跑 MCTS，结果经 search_done 信号回主线程。"""
        depth = self._get_depth()
        g = game.snapshot()
        g.current_player = player

        self._log(f"  🌳 MCTS 启动 ({sims}次模拟, 深度={depth})", 'INFO')
        result = {}
        engine = MCTSEngine(max_simulations=sims, time_limit=MCTS_TIME_LIMIT)

        def _collect(move) -> None:
            result['sims'] = engine.simulations
            result['top'] = engine.get_top_moves(3)

        def _collect_error(e) -> None:
            result['error'] = f'MCTS 搜索异常: {e}'

        def _emit(move) -> None:
            self.search_done.emit(
                (move, player, _logged_on_done,
                 game_version, cancel_version, ''))

        def _logged_on_done(move, player):
            if move:
                fr, fc, tr, tc = move
                try:
                    notation = format_chinese_notation(
                        game.board, fr, fc, tr, tc)
                except (ValueError, IndexError):
                    notation = format_move(fr, fc, tr, tc)
                self._log(f"  ✅ MCTS选择: {notation} "
                          f"({result.get('sims', 0)}次模拟)", 'INFO')
                for i, (m, visits, val) in enumerate(result.get('top', [])):
                    mfr, mfc, mtr, mtc = m
                    try:
                        mn = format_chinese_notation(
                            game.board, mfr, mfc, mtr, mtc)
                    except (ValueError, IndexError):
                        mn = format_move(mfr, mfc, mtr, mtc)
                    self._log(f"    {i+1}. {mn} "
                              f"[访问{visits}次, 价值{val:.3f}]", 'INFO')
            else:
                detail = (f"（{result['error']}）"
                          if result.get('error') else '')
                self._log(f"  ⚠️ MCTS未找到走法{detail}", 'WARNING')
            on_done(move, player)

        self._spawn_mcts(engine, g, player, _emit,
                         on_result=_collect, on_error=_collect_error)
