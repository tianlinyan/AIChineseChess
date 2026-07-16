import random
import threading
from typing import Optional, TYPE_CHECKING

from PyQt6.QtCore import QTimer, QDateTime, QObject, pyqtSignal

from domain.constants import (
    AI_RETRY_LIMIT, AI_RETRY_DELAY_MS, AI_DELAY_MS,
    THINKING_TIMER_INTERVAL,
    SEARCH_MAX_DEPTH,
    OPENING_BOOK_ENABLED, OPENING_BOOK_MAX_MOVES,
    OPENING_DELAY_MS,
    MCTS_SIMULATIONS, MCTS_TIME_LIMIT,
    AI_DEFAULT_MODE, PIECE_SYMBOLS, format_duration,
)
from domain.prompts import (
    HUMAN_MODEL, get_system_prompt, build_move_prompt, format_legal_moves,
)
from domain.game import ChineseChessGame
from domain.mcts import MCTSEngine
from domain.openings import get_opening_move, is_in_opening_book
try:
    from domain.pikafish import PikafishEngine, _game_to_fen
except ImportError:
    PikafishEngine = None  # pikafish 模块不可用
from ai.manager import AIManager
from ai.worker import AIWorker

if TYPE_CHECKING:
    from app.protocols import MainWindowProtocol


class _PikafishRelay(QObject):
    """跨线程信号中继：daemon 线程 emit → Qt 自动排队到主线程。"""
    done = pyqtSignal(tuple)       # 验证结果
    search_done = pyqtSignal(tuple)  # 搜索/回退结果


class GameController:
    """游戏控制器 — 编排游戏逻辑、AI调用、搜索、开局库、UI更新"""

    def __init__(self, game: ChineseChessGame,
                 ai_manager: AIManager) -> None:
        self.game = game
        self.ai_manager = ai_manager

        self.main: Optional['MainWindowProtocol'] = None

        self.model1 = None       # 红方模型
        self.model2 = None       # 黑方模型

        self.is_active: bool = False
        self.is_paused: bool = False
        self.retry_count: int = 0
        self.last_move_error: str = ''
        self.game_version: int = 0

        # ── AI 配置（可被 UI 修改） ──
        self.ai_mode: str = AI_DEFAULT_MODE  # "hybrid" | "search_only" | "llm_only"
        self.search_depth: int = SEARCH_MAX_DEPTH
        self.use_opening_book: bool = OPENING_BOOK_ENABLED

        self.stats: dict = {
            'start_time': None,
            'move_count': 0,
            'estimated_tokens': 0,
            'red_tokens': 0,
            'black_tokens': 0,
            'search_nodes': 0,
        }
        self.red_total_time: int = 0
        self.black_total_time: int = 0

        self.thinking_start_time: Optional[QDateTime] = None
        self.thinking_timer: Optional[QTimer] = None

        self.last_red_raw: str = ''
        self.last_black_raw: str = ''

        self._random_action_count: int = 0

        # ── Pikafish 引擎（延迟初始化——需等 main 就绪后才能写日志） ──
        self._pikafish: Optional['PikafishEngine'] = None
        self._pikafish_initialized: bool = False

        # 跨线程信号中继：Pikafish 异步回调 → 主线程
        self._pikafish_relay = _PikafishRelay()
        self._pikafish_relay.search_done.connect(self._on_pikafish_search_done)

        # ── Hybrid 模式：引擎搜索结果暂存（LLM 失败时兜底用）──
        self._hybrid_engine_move: Optional[tuple] = None

    def _init_pikafish(self) -> None:
        """尝试初始化 Pikafish 外部引擎。

        必须在 self.main 赋值后调用（日志需要 main.log_manager）。
        若引擎二进制不可用，静默跳过——后续自动回退到 MCTS。
        """
        if self._pikafish_initialized:
            return
        self._pikafish_initialized = True

        if PikafishEngine is not None:
            try:
                self._pikafish = PikafishEngine(move_time_ms=int(MCTS_TIME_LIMIT * 1000))
                if self._pikafish.available:
                    self.log("Pikafish 引擎已就绪（NNUE 评估，大师级棋力）", 'INFO')
                else:
                    # 输出详细诊断信息帮助用户排查
                    err = self._pikafish.error_msg
                    if err:
                        for line in err.split('\n'):
                            self.log(f"[Pikafish] {line.strip()}", 'WARNING')
                    self._pikafish = None
            except Exception as e:
                self.log(f"[Pikafish] 初始化异常: {e}", 'WARNING')
                self._pikafish = None

    # ── 游戏控制 ──

    def start_game(self) -> None:
        self.reset_game()

        if not self.main:
            return

        model1_id = self.main.model1_combo.currentData()
        model2_id = self.main.model2_combo.currentData()
        if not model1_id or not model2_id:
            return

        # 解析模型
        if model1_id == 'human':
            self.model1 = HUMAN_MODEL
        else:
            self.model1 = next(
                (m for m in self.main.model_manager.models if m.id == model1_id), None)
        if model2_id == 'human':
            self.model2 = HUMAN_MODEL
        else:
            self.model2 = next(
                (m for m in self.main.model_manager.models if m.id == model2_id), None)

        if not self.model1 or not self.model2:
            return

        self.main.board_widget.update()

        self.is_active = True
        self.is_paused = False
        self.retry_count = 0
        self.last_move_error = ''
        self._random_action_count = 0
        self.stats = {
            'start_time': QDateTime.currentDateTime(),
            'move_count': 0,
            'estimated_tokens': 0,
            'red_tokens': 0,
            'black_tokens': 0,
            'search_nodes': 0,
        }
        self.red_total_time = 0
        self.black_total_time = 0

        self.main.start_btn.setEnabled(False)
        self.main.pause_btn.setEnabled(True)
        self.main.reset_btn.setEnabled(True)

        self.main.update_game_status()
        self.main.update_player_status()

        # 先手方走子
        first_player = self.game.current_player
        first_model = self.model1 if first_player == 1 else self.model2
        self.main.start_thinking_timer(first_player)
        if first_model != HUMAN_MODEL:
            QTimer.singleShot(1000, lambda v=self.game_version:
                              self.make_ai_move(expected_version=v))

    def reset_game(self) -> None:
        self.game_version += 1
        self.stop_thinking_timer()

        # 取消运行中的 AI 任务，防止残留 Worker 阻塞新游戏
        self.ai_manager.clear_queue()
        self.ai_manager.set_busy(False)

        self.is_active = False
        self.is_paused = False
        self.retry_count = 0
        self.last_move_error = ''
        self._random_action_count = 0

        self.game.reset()
        if self.main:
            self.main.board_widget.selected_row = -1
            self.main.board_widget.selected_col = -1
            self.main.board_widget.update()

        self.last_red_raw = ''
        self.last_black_raw = ''
        self.red_total_time = 0
        self.black_total_time = 0

        self.stats = {
            'start_time': None, 'move_count': 0,
            'estimated_tokens': 0, 'red_tokens': 0, 'black_tokens': 0,
            'search_nodes': 0,
        }

        if self.main:
            self.main.update_player_status()
            self.main.update_game_status()
            self.main.update_stats_display()
            self.main.update_history_list()
            self.main.start_btn.setEnabled(True)
            self.main.pause_btn.setEnabled(False)
            self.main.pause_btn.setText("暂停")
            self.main.reset_btn.setEnabled(False)

    def toggle_pause(self) -> None:
        if not self.is_active:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.main.pause_btn.setText("恢复")
            self.main.pause_thinking_timer()
            self.ai_manager.clear_queue()
            self.ai_manager.set_busy(False)
            self.retry_count = 0
        else:
            self.main.pause_btn.setText("暂停")
            self.main.resume_thinking_timer()
            if not self.game.game_over and not self.ai_manager.is_busy():
                current_player = self.game.current_player
                current_model = self.model1 if current_player == 1 else self.model2
                if current_model != HUMAN_MODEL:
                    QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                        self.make_ai_move(expected_version=v))
                else:
                    self.main.start_thinking_timer(current_player)

    # ── 人类落子 ──

    def on_human_move(self, from_row: int, from_col: int,
                      to_row: int, to_col: int) -> None:
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        current_player = self.game.current_player
        current_model = self.model1 if current_player == 1 else self.model2
        if current_model != HUMAN_MODEL:
            return

        result = self.game.move_piece(from_row, from_col, to_row, to_col)
        if result['success']:
            if self.thinking_start_time:
                elapsed = self.thinking_start_time.secsTo(QDateTime.currentDateTime())
                if elapsed < 0:
                    elapsed = 0
                if current_player == 1:
                    self.red_total_time += elapsed
                else:
                    self.black_total_time += elapsed
                self.thinking_start_time = None

            from_coord = f"{chr(65 + from_col)}{from_row + 1}"
            to_coord = f"{chr(65 + to_col)}{to_row + 1}"
            if current_player == 1:
                self.last_red_raw = f"人类走子: {from_coord}→{to_coord}"
            else:
                self.last_black_raw = f"人类走子: {from_coord}→{to_coord}"
            self.log(f"{'红方' if current_player == 1 else '黑方'} 人类走子: {from_coord}→{to_coord}",
                     'red' if current_player == 1 else 'black')

            self.main.board_widget.update()
            self.main.update_game_status()
            self.main.update_history_list()
            self.main.update_player_status()

            if result['game_over']:
                self.handle_game_over(result)
            else:
                next_player = self.game.current_player
                next_model = self.model1 if next_player == 1 else self.model2
                if next_model != HUMAN_MODEL:
                    self.main.start_thinking_timer(next_player)
                    QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                        self.make_ai_move(expected_version=v))
                else:
                    self.main.start_thinking_timer(next_player)
        else:
            self.log(f"移动非法: {result['message']}", 'ERROR')

    # ── AI 走子（核心引擎） ──

    def make_ai_move(self, expected_version: Optional[int] = None) -> None:
        if expected_version is not None and expected_version != self.game_version:
            return
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        if self.ai_manager.is_busy():
            return

        current_player = self.game.current_player
        model = self.model1 if current_player == 1 else self.model2
        if model == HUMAN_MODEL:
            return

        # ── 1. 开局库查询 ──
        if self.use_opening_book and len(self.game.moves) < OPENING_BOOK_MAX_MOVES:
            if is_in_opening_book(self.game.moves):
                book_move = get_opening_move(self.game.moves)
                if book_move:
                    fr, fc, tr, tc = book_move
                    result = self.game.move_piece(fr, fc, tr, tc)
                    if result['success']:
                        self._on_move_success(fr, fc, tr, tc, current_player,
                                              source='开局库')
                        self.main.board_widget.update()
                        self.main.update_game_status()
                        self.main.update_history_list()
                        self.main.update_stats_display()
                        self.main.update_player_status()
                        if result.get('game_over'):
                            self.handle_game_over(result)
                            return
                        next_player = self.game.current_player
                        next_model = self.model1 if next_player == 1 else self.model2
                        if next_model != HUMAN_MODEL and self.is_active and not self.is_paused:
                            QTimer.singleShot(OPENING_DELAY_MS, lambda v=self.game_version:
                                self.make_ai_move(expected_version=v))
                        return

        # ── 2. 纯搜索模式（Pikafish/MCTS 异步，不阻塞UI）──
        if self.ai_mode == 'search_only':
            self.ai_manager.set_busy(True)

            def _on_search(move, p):
                # Pikafish 异步失败 → MCTS 同步回退（不重试Pikafish，避免UI再阻塞15s）
                if not move:
                    mcts = MCTSEngine(max_simulations=MCTS_SIMULATIONS, time_limit=MCTS_TIME_LIMIT)
                    move = mcts.search(self.game, p)
                    self.stats['search_nodes'] = mcts.simulations
                if not move:
                    self._finish_ai_move()
                    self._random_move(p)
                    return
                fr, fc, tr, tc = move
                result = self.game.move_piece(fr, fc, tr, tc)
                if result['success']:
                    self._on_move_success(fr, fc, tr, tc, p, source='搜索')
                    self.main.board_widget.update()
                    self.main.update_game_status()
                    self.main.update_history_list()
                    self.main.update_stats_display()
                    self.main.update_player_status()
                    if result.get('game_over'):
                        self._finish_ai_move()
                        self.handle_game_over(result)
                        return
                    np = self.game.current_player
                    nm = self.model1 if np == 1 else self.model2
                    if nm != HUMAN_MODEL and self.is_active and not self.is_paused:
                        QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                            self.make_ai_move(expected_version=v))
                    self._finish_ai_move()
                else:
                    self._finish_ai_move()
                    self._random_move(p)

            self._mcts_search(current_player, on_done=_on_search)
            return

        # ── 3. LLM 模式（llm_only / hybrid） ──
        if self.ai_mode == 'hybrid':
            # 引擎先行：Pikafish 同步搜索 → MCTS 同步兜底 → LLM
            self.ai_manager.set_busy(True)
            engine_move = None
            engine_name = 'MCTS'
            if self._pikafish and self._pikafish.available:
                try:
                    engine_move = self._pikafish.search(
                        self.game, current_player,
                        time_ms=int(MCTS_TIME_LIMIT * 1000))
                    if engine_move:
                        engine_name = 'Pikafish'
                except Exception as e:
                    self.log(f"Pikafish 同步搜索异常: {e}", 'WARNING')
            if not engine_move:
                mcts = MCTSEngine(max_simulations=MCTS_SIMULATIONS,
                                  time_limit=MCTS_TIME_LIMIT)
                engine_move = mcts.search(self.game, current_player)
                self.stats['search_nodes'] = mcts.simulations
            self._on_hybrid_engine_done(engine_move, current_player, engine_name)
            return

        # llm_only: 直接启动 LLM（无引擎参考）
        self._start_llm_request(current_player, model)

    def _start_llm_request(self, player: int, model,
                           engine_hint: str = '') -> None:
        """构建提示词并启动 LLM worker 线程。

        Args:
            player: 当前走子方 (1=红, 2=黑)
            model: AI 模型
            engine_hint: 引擎参考走法文本（hybrid 模式传入，llm_only 为空）
        """
        # 视觉模式判断
        use_vision = self.main and self.main.vision_check.isChecked()

        # 构建提示词
        board_str = '' if use_vision else self.game.get_board_state_string()
        history = self.game.format_move_history()

        # 将军状态
        in_check = self.game._is_in_check(player)
        opponent = 2 if player == 1 else 1
        opponent_in_check = self.game._is_in_check(opponent)
        move_count = len(self.game.moves) + 1

        # 合法走法
        legal_moves = self.game.get_all_legal_moves(player)
        legal_move_count = len(legal_moves)

        # 零合法走法 = 将杀或困毙 → 直接判定游戏结束，跳过 LLM 调用
        if legal_move_count == 0:
            self.game.game_over = True
            self.game.winner = opponent
            self.handle_game_over({
                'game_over': True, 'winner': opponent,
                'message': f"{'红方' if opponent == 1 else '黑方'}获胜（{'将杀' if self.game._is_in_check(player) else '困毙'}）"
            })
            self._finish_ai_move()
            return

        # 格式化合法走法列表
        legal_moves_str = format_legal_moves(legal_moves, self.game.board)

        # 上一步走子描述
        last_move_str = ''
        if self.game.last_move:
            fr, fc, tr, tc, lp = self.game.last_move
            piece = self.game.board[tr][tc]
            piece_name = PIECE_SYMBOLS.get(piece, piece)
            captured = self.game.moves[-1][5] if self.game.moves else '.'
            action = f"{'红方' if lp == 1 else '黑方'} {piece_name} {chr(65+fc)}{fr+1}→{chr(65+tc)}{tr+1}"
            tags = []
            if captured != '.':
                target_name = PIECE_SYMBOLS.get(captured, captured)
                tags.append(f"吃{target_name}")
            if in_check:
                tags.append("将军！")
            if tags:
                action += "（" + "，".join(tags) + "）"
            last_move_str = action

        prompt = build_move_prompt(
            player, board_str, history,
            in_check=in_check,
            opponent_in_check=opponent_in_check,
            move_count=move_count,
            last_move_str=last_move_str,
            legal_move_count=legal_move_count,
            legal_moves_str=legal_moves_str,
            last_move_error=self.last_move_error,
            retry_count=self.retry_count,
            vision_mode=use_vision,
            mcts_suggestions=engine_hint,
        )

        # 确认日志：引擎参考是否已注入提示词
        if engine_hint:
            self.log("📤 引擎推荐已注入 LLM 提示词", 'INFO')

        image = None
        if use_vision:
            try:
                image = self.main.board_widget.capture_board_image()
            except Exception as e:
                self.log(f"视觉模式截图失败: {e}", 'WARNING')

        player_name = '红方' if player == 1 else '黑方'
        current_version = self.game_version

        # think参数
        if self.main and self.main.disable_think_check.isChecked():
            think_enabled = None
        else:
            think_enabled = (self.main.think_check.isChecked()
                             if self.main else True)

        # 系统提示词 — 强模型用精简版，本地模型用完整版
        if model.system_prompt:
            system_prompt = model.system_prompt
        elif model.type and model.type.startswith('deepseek'):
            from domain.prompts import get_system_prompt_lite
            system_prompt = get_system_prompt_lite()
        else:
            system_prompt = get_system_prompt()

        # 工具选择：仅 LLM 模式只用 move_piece；其他模式用全部工具
        from domain.prompts import TOOLS_BASIC, DEFAULT_TOOLS
        worker_tools = TOOLS_BASIC if self.ai_mode == 'llm_only' else DEFAULT_TOOLS

        # 标记忙碌
        self.ai_manager.set_busy(True)

        worker = AIWorker(
            model, prompt, image, player_name,
            version=current_version,
            cancel_version=self.ai_manager.cancel_version,
            think=think_enabled,
            system_prompt=system_prompt,
            tools=worker_tools,
            game=self.game,
            current_player=player,
        )
        worker.signals.finished.connect(self.on_ai_finished)
        self.ai_manager.set_active_worker(worker)
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        self.ai_manager.set_active_thread(t)

    def _on_hybrid_engine_done(self, engine_move: Optional[tuple],
                                player: int, engine_name: str = '引擎') -> None:
        """Hybrid 模式：引擎搜索完成 → 日志 + 格式化提示 → 启动 LLM。"""
        if not self.is_active or self.is_paused or self.game.game_over:
            self._finish_ai_move()
            return

        player_name = '红方' if player == 1 else '黑方'

        # 格式化引擎参考走法 + 日志输出
        engine_hint = ''
        if engine_move:
            efr, efc, etr, etc = engine_move
            if self.game.board[efr][efc] != '.':
                epn = PIECE_SYMBOLS.get(self.game.board[efr][efc], '?')
                emove_str = f"{epn} {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1}"
                self.log(f"🔍 {engine_name} 推荐: {emove_str}", 'INFO')
                engine_hint = (
                    f"## 🔍 引擎参考走法\n\n"
                    f"{engine_name} 经 {MCTS_TIME_LIMIT:.0f} 秒搜索，推荐：\n"
                    f"**{emove_str}**\n\n"
                    f"引擎在战术计算（捉双、抽将、杀棋等）方面有优势，"
                    f"但缺乏战略视野。请认真参考，结合你的判断做最终决定。"
                    f"在分析中简要说明你采纳与否及理由即可。"
                )
            else:
                self.log(f"引擎走法起始空位，丢弃", 'WARNING')
                engine_move = None
        else:
            self.log(f"⚠️ {player_name} 引擎搜索无结果，LLM 独立决策", 'WARNING')

        # 暂存引擎结果（LLM 失败时兜底）
        self._hybrid_engine_move = engine_move

        model = self.model1 if player == 1 else self.model2
        self._start_llm_request(player, model, engine_hint=engine_hint)

    def on_ai_finished(self, from_coord: str, to_coord: str,
                       full_text: str, error: str,
                       tokens: int, version: int,
                       cancel_version: int = 0) -> None:
        if self.ai_manager._shutting_down:
            return
        if version != self.game_version:
            self._finish_ai_move()
            return
        if cancel_version != self.ai_manager.cancel_version:
            self._finish_ai_move()
            return
        if self.is_paused:
            self._finish_ai_move()
            return
        if not self.is_active or self.game.game_over:
            self._finish_ai_move()
            return

        current_player = self.game.current_player
        player_name = '红方' if current_player == 1 else '黑方'

        if full_text:
            self.log(f"  {player_name} AI 思考:\n{full_text}\n",
                     'red' if current_player == 1 else 'black')
        if error:
            self.log(f"{player_name} AI 错误: {error}", 'ERROR')

        # ── LLM 失败 → 优先用引擎兜底，否则搜索回退 ──
        if error or not from_coord or not to_coord:
            if self.ai_mode == 'hybrid':
                self.log(f"{player_name} LLM 调用失败，尝试引擎兜底", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._fallback_to_search(current_player)
                return
            else:
                # llm_only 模式：原有重试逻辑
                self._retry_move(error)
                return

        # ── 解析坐标 ──
        try:
            from_col = ord(from_coord[0].upper()) - 65
            from_row = int(from_coord[1:]) - 1
            to_col = ord(to_coord[0].upper()) - 65
            to_row = int(to_coord[1:]) - 1
            if not (self.game.in_board(from_row, from_col) and
                    self.game.in_board(to_row, to_col)):
                raise ValueError
            if from_row < 0 or from_row > 9 or to_row < 0 or to_row > 9:
                raise ValueError
        except (ValueError, IndexError):
            self.last_move_error = (
                f"坐标 '{from_coord}'→'{to_coord}' 无法解析。"
                f"请确保坐标格式正确：列字母 A~I，行数字 1~10。"
            )
            if self.ai_mode == 'hybrid':
                self.log(f"{player_name} 坐标解析失败，尝试引擎兜底", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._fallback_to_search(current_player)
                return
            else:
                self.retry_count += 1
                if self.retry_count <= AI_RETRY_LIMIT:
                    self.ai_manager.set_busy(False)
                    self.ai_manager.clear_active_worker()
                    QTimer.singleShot(self.retry_count * 2000, lambda v=self.game_version:
                        self.make_ai_move(expected_version=v))
                    return
                else:
                    self._finish_ai_move()
                    self._fallback_to_search(current_player)
                    return

        # ── LLM 走法即为最终决定（hybrid 模式下引擎仅作参考）──
        final_move = (from_row, from_col, to_row, to_col)

        # ── 执行最终走法 ──
        result = self.game.move_piece(from_row, from_col, to_row, to_col)
        if not result['success']:
            reason = result.get('message', '未知原因')
            self.last_move_error = f"{from_coord}→{to_coord}（原因：{reason}）"

            if self.ai_mode == 'hybrid':
                self.log(f"{player_name} 走子非法 ({reason})，尝试引擎兜底", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move and self._hybrid_engine_move != final_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._fallback_to_search(current_player)
                return
            else:
                # llm_only：重试
                self.retry_count += 1
                if self.retry_count <= AI_RETRY_LIMIT:
                    self.ai_manager.set_busy(False)
                    self.ai_manager.clear_active_worker()
                    QTimer.singleShot(self.retry_count * 2000, lambda v=self.game_version:
                        self.make_ai_move(expected_version=v))
                    return
                else:
                    self._finish_ai_move()
                    self._fallback_to_search(current_player)
                    return

        # ── 移动成功 ──
        # Hybrid 模式：LLM 与引擎推荐不一致时警告
        if self.ai_mode == 'hybrid' and self._hybrid_engine_move \
                and final_move != self._hybrid_engine_move:
            efr, efc, etr, etc = self._hybrid_engine_move
            epn = PIECE_SYMBOLS.get(self.game.board[efr][efc], '?')
            erec = f"{epn} {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1}"
            lpn = PIECE_SYMBOLS.get(self.game.board[to_row][to_col], '?')
            lrec = f"{lpn} {chr(65+from_col)}{from_row+1}→{chr(65+to_col)}{to_row+1}"
            self.log(
                f"⚠️ LLM 未采纳引擎推荐 | 引擎推荐: {erec} | LLM选择: {lrec}",
                'WARNING')

        self._on_move_success(from_row, from_col, to_row, to_col,
                              current_player, tokens=tokens, source='LLM')

        self.main.board_widget.update()
        self.main.update_game_status()
        self.main.update_history_list()
        self.main.update_stats_display()
        self.main.update_player_status()

        self._finish_ai_move()

        if result.get('game_over'):
            self.handle_game_over(result)
            return

        # ── 下一步 ──
        self.main.start_thinking_timer(self.game.current_player)
        next_player = self.game.current_player
        next_model = self.model1 if next_player == 1 else self.model2
        if next_model != HUMAN_MODEL and self.is_active and not self.is_paused:
            QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                self.make_ai_move(expected_version=v))

    # ── 搜索引擎集成 ──

    def _mcts_search(self, player: int,
                     priors: dict = None,
                     on_done = None) -> Optional[tuple]:
        """搜索最佳走法。优先 Pikafish（若可用），回退 MCTS。

        on_done 回调提供时：Pikafish 异步执行(不阻塞UI)，结果通过信号回主线程。
        on_done=None：同步返回结果(向后兼容)。

        on_done 签名为 on_done(best_move_or_None, player)
        """
        # ── 异步路径：Pikafish（MCTS 回退在主线程回调中处理）──
        if on_done is not None and self._pikafish and self._pikafish.available:
            time_ms = int(MCTS_TIME_LIMIT * 1000)
            captured_version = self.game_version
            self._pikafish.search_async(
                self.game, player, time_ms=time_ms,
                callback=lambda m: self._pikafish_relay.search_done.emit(
                    (m, player, on_done, captured_version)))
            return None

        # ── 同步路径：Pikafish → MCTS 回退 ──
        best_move = None
        if self._pikafish and self._pikafish.available:
            try:
                time_ms = int(MCTS_TIME_LIMIT * 1000)
                best_move = self._pikafish.search(self.game, player, priors=priors, time_ms=time_ms)
                # Pikafish 不暴露节点数，保留上次统计值（不设 0）
                if best_move:
                    fr, fc, tr, tc = best_move
                    piece = self.game.board[fr][fc]
                    if piece == '.':
                        # 走法非法：起始位置无棋子 → 诊断并回退 MCTS
                        fen = _game_to_fen(self.game, player)
                        self.log(
                            f"  ⚠️ Pikafish 返回非法走法 {chr(65+fc)}{fr+1}→{chr(65+tc)}{tr+1}"
                            f"（起始位置无棋子），回退 MCTS", 'WARNING')
                        self.log(
                            f"    诊断: FEN={fen[:60]}... 棋盘[{fr}][{fc}]='{piece}'",
                            'INFO')
                        best_move = None  # 触发 MCTS 回退
                    else:
                        pn = PIECE_SYMBOLS.get(piece, '?')
                        self.log(f"  ✅ Pikafish选择: {pn} "
                                 f"{chr(65+fc)}{fr+1}→{chr(65+tc)}{tr+1} "
                                 f"({time_ms}ms)", 'INFO')
                        if on_done:
                            on_done(best_move, player)
                            return None
                        return best_move
            except Exception as e:
                self.log(f"  ⚠️ Pikafish 搜索失败 ({e})，回退到 MCTS", 'WARNING')

        # ── 回退：内置 MCTS 引擎 ──
        engine = MCTSEngine(
            max_simulations=MCTS_SIMULATIONS,
            time_limit=MCTS_TIME_LIMIT,
        )
        desc = "LLM引导" if priors else "均匀先验"
        self.log(f"  🌳 MCTS 启动 ({desc}, {MCTS_SIMULATIONS}次模拟)", 'INFO')

        best_move = engine.search(self.game, player, priors=priors)
        self.stats['search_nodes'] = engine.simulations

        if best_move:
            fr, fc, tr, tc = best_move
            pn = PIECE_SYMBOLS.get(self.game.board[fr][fc], '?')
            self.log(f"  ✅ MCTS选择: {pn} {chr(65+fc)}{fr+1}→{chr(65+tc)}{tr+1} "
                     f"({engine.simulations}次模拟)", 'INFO')

            # 显示 Top-3 走法（调试用）
            top = engine.get_top_moves(3)
            for i, (move, visits, val) in enumerate(top):
                mfr, mfc, mtr, mtc = move
                mpn = PIECE_SYMBOLS.get(
                    self.game.board[mfr][mfc], '?')
                self.log(f"    {i+1}. {mpn} {chr(65+mfc)}{mfr+1}→{chr(65+mtc)}{mtr+1} "
                         f"[访问{visits}次, 价值{val:.3f}]", 'INFO')
        else:
            self.log(f"  ⚠️ MCTS未找到走法", 'WARNING')

        if on_done:
            on_done(best_move, player)
            return None
        return best_move

    def _fallback_to_search(self, player: int) -> None:
        """LLM 失败时回退到搜索引擎（替代随机走子）"""
        # 计数统一由 _random_move 管理（阈值 >3），_fallback_to_search 不单独计数
        if self._random_action_count > 3:
            self.log("连续回退过多 — 停止游戏", 'ERROR')
            self.is_active = False
            if self.main:
                self.main.start_btn.setEnabled(True)
                self.main.pause_btn.setEnabled(False)
            return

        # Pikafish/MCTS 异步回退
        self.ai_manager.set_busy(True)

        def _on_fb(move, p):
            # Pikafish 异步失败 → MCTS 同步回退
            if not move:
                mcts = MCTSEngine(max_simulations=MCTS_SIMULATIONS, time_limit=MCTS_TIME_LIMIT)
                move = mcts.search(self.game, p)
                self.stats['search_nodes'] = mcts.simulations
            if move:
                fr, fc, tr, tc = move
                result = self.game.move_piece(fr, fc, tr, tc)
                if result['success']:
                    self._on_move_success(fr, fc, tr, tc, p, source='搜索回退')
                    self._random_action_count = 0  # 搜索成功，重置计数
                    if self.main:
                        self.main.board_widget.update()
                        self.main.update_game_status()
                        self.main.update_history_list()
                        self.main.update_stats_display()
                        self.main.update_player_status()
                    if result.get('game_over'):
                        self._finish_ai_move()
                        self.handle_game_over(result)
                        return
                    self.main.start_thinking_timer(self.game.current_player)
                    np = self.game.current_player
                    nm = self.model1 if np == 1 else self.model2
                    if nm != HUMAN_MODEL and self.is_active and not self.is_paused:
                        QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                            self.make_ai_move(expected_version=v))
                    self._finish_ai_move()
                    return
                else:
                    self.log(f"搜索回退走子失败: {result.get('message', '未知')}", 'ERROR')
            self._finish_ai_move()
            self._random_move(p)

        self._mcts_search(player, on_done=_on_fb)
        return

    def _random_move(self, current_player: int) -> None:
        """随机走子（最后的fallback）"""
        self._random_action_count += 1
        if self._random_action_count > 3:
            self.log("连续随机走子过多 — 停止游戏", 'ERROR')
            self.is_active = False
            if self.main:
                self.main.start_btn.setEnabled(True)
                self.main.pause_btn.setEnabled(False)
            return

        moves = self.game.get_all_legal_moves(current_player)
        if moves:
            fr, fc, tr, tc = random.choice(moves)
            result = self.game.move_piece(fr, fc, tr, tc)
            if result['success']:
                self._on_move_success(fr, fc, tr, tc, current_player,
                                      source='随机')
                # 注意：随机走子成功不重置计数——防止无限[随机]循环

                if self.main:
                    self.main.board_widget.update()
                    self.main.update_game_status()
                    self.main.update_history_list()
                    self.main.update_stats_display()
                    self.main.update_player_status()

                if result['game_over']:
                    self.handle_game_over(result)
                    return

                self.main.start_thinking_timer(self.game.current_player)
                next_player = self.game.current_player
                next_model = self.model1 if next_player == 1 else self.model2
                if next_model != HUMAN_MODEL and self.is_active and not self.is_paused:
                    QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                        self.make_ai_move(expected_version=v))
                return
            else:
                self.log(f"随机走子失败: {result.get('message', '未知')}", 'ERROR')
                self._finish_ai_move()
                return

        # 确实无合法走法 — 困毙
        self.game.game_over = True
        self.handle_game_over({'game_over': True, 'winner': None, 'message': '无合法移动，平局'})

    def _retry_move(self, error: str = '') -> None:
        """LLM 错误时的重试逻辑（仅 llm_only 模式使用）"""
        if error:
            non_retryable = (
                error.startswith('客户端错误:')
                or 'cancelled' in error.lower()
                or 'endpoint' in error.lower()
            )
            if non_retryable:
                self.log(f"不可重试的错误，回退搜索", 'ERROR')
                self._finish_ai_move()
                self._fallback_to_search(self.game.current_player)
                return

        self.retry_count += 1
        if self.retry_count <= AI_RETRY_LIMIT:
            delay = self.retry_count * AI_RETRY_DELAY_MS
            self.log(f"重试 ({self.retry_count}/{AI_RETRY_LIMIT}) 延迟 {delay}ms", 'ERROR')
            self.ai_manager.set_busy(False)
            self.ai_manager.clear_active_worker()
            QTimer.singleShot(delay, lambda v=self.game_version:
                self.make_ai_move(expected_version=v))
            return

        self.log("超过重试次数，回退搜索", 'ERROR')
        self._finish_ai_move()
        self._fallback_to_search(self.game.current_player)

    # ── 走子成功处理 ──

    def _on_move_success(self, from_row: int, from_col: int,
                          to_row: int, to_col: int,
                          current_player: int,
                          tokens: int = 0,
                          source: str = '') -> None:
        """统一处理走子成功后的状态更新"""
        # 计时
        if self.thinking_start_time:
            elapsed = self.thinking_start_time.secsTo(QDateTime.currentDateTime())
            if elapsed < 0:
                elapsed = 0
            if current_player == 1:
                self.red_total_time += elapsed
            else:
                self.black_total_time += elapsed
            self.thinking_start_time = None

        # 更新统计
        self.last_move_error = ''
        self.stats['move_count'] += 1
        self.stats['estimated_tokens'] += tokens
        if current_player == 1:
            self.stats['red_tokens'] += tokens
        else:
            self.stats['black_tokens'] += tokens

        # 记录走子
        from_coord = f"{chr(65+from_col)}{from_row+1}"
        to_coord = f"{chr(65+to_col)}{to_row+1}"
        move_desc = f"{source}:{from_coord}→{to_coord}" if source else f"{from_coord}→{to_coord}"

        if current_player == 1:
            self.last_red_raw = move_desc
        else:
            self.last_black_raw = move_desc

        self.retry_count = 0

        piece_name = PIECE_SYMBOLS.get(
            self.game.board[to_row][to_col], '?')
        self.log(f"{'红方' if current_player == 1 else '黑方'} [{source}] "
                 f"{piece_name} {from_coord}→{to_coord}",
                 'red' if current_player == 1 else 'black')

    def _on_pikafish_search_done(self, args: tuple) -> None:
        """主线程：处理 Pikafish 异步搜索/回退结果。"""
        try:
            move, player, on_done, captured_version = args
        except Exception as e:
            self.log(f"[PF错误] 搜索回调参数解包失败: {e}", 'ERROR')
            self._finish_ai_move()
            return
        try:
            if captured_version != self.game_version:
                self.log(f"[PF诊断] 搜索回调版本不匹配({captured_version}!={self.game_version})，丢弃", 'INFO')
                self._finish_ai_move()
                return
            if not self.is_active or self.game.game_over:
                self.log(f"[PF诊断] 搜索回调状态异常(active={self.is_active} over={self.game.game_over})，丢弃", 'INFO')
                self._finish_ai_move()
                return
            on_done(move, player)
        except Exception as e:
            self.log(f"[PF诊断] 搜索回调异常: {e}", 'ERROR')
            self._finish_ai_move()

    def _execute_fallback_move(self, move: tuple, player: int) -> None:
        """Hybrid 模式兜底：直接执行引擎走法（跳过 LLM）。

        LLM 失败/非法走法时调用。引擎走法经过搜索验证，直接执行。
        走法非法时回退到 _fallback_to_search。
        """
        fr, fc, tr, tc = move
        result = self.game.move_piece(fr, fc, tr, tc)
        if result['success']:
            self._on_move_success(fr, fc, tr, tc, player, source='引擎兜底')
            self._random_action_count = 0
            if self.main:
                self.main.board_widget.update()
                self.main.update_game_status()
                self.main.update_history_list()
                self.main.update_stats_display()
                self.main.update_player_status()
            if result.get('game_over'):
                self._finish_ai_move()
                self.handle_game_over(result)
                return
            self.main.start_thinking_timer(self.game.current_player)
            np = self.game.current_player
            nm = self.model1 if np == 1 else self.model2
            if nm != HUMAN_MODEL and self.is_active and not self.is_paused:
                QTimer.singleShot(AI_DELAY_MS, lambda v=self.game_version:
                    self.make_ai_move(expected_version=v))
            self._finish_ai_move()
        else:
            self.log(f"引擎兜底走子失败: {result.get('message', '未知')}", 'ERROR')
            self._finish_ai_move()
            self._fallback_to_search(player)

    def _finish_ai_move(self) -> None:
        self.ai_manager.set_busy(False)
        self.ai_manager.clear_active_worker()

    # ── 游戏结束 ──

    def handle_game_over(self, result: dict) -> None:
        self.stop_thinking_timer()
        self.is_active = False
        self.retry_count = 0
        if self.main:
            self.main.update_game_status()
            self.main.update_player_status()
            self.main.start_btn.setEnabled(True)
            self.main.pause_btn.setEnabled(False)

    # ── 计时器 ──

    def start_thinking_timer(self, player: int) -> None:
        self.stop_thinking_timer()
        model = self.model1 if player == 1 else self.model2
        self.thinking_start_time = QDateTime.currentDateTime()
        if model == HUMAN_MODEL:
            self.main.think_timer_label.setText("等待人类走子...")
            return
        self.main.think_timer_label.setText(f"思考用时: {format_duration(0)}")
        if self.is_active and not self.is_paused and not self.game.game_over:
            if self.thinking_timer is None:
                self.thinking_timer = QTimer()
                self.thinking_timer.timeout.connect(self._update_thinking_time)
            self.thinking_timer.start(THINKING_TIMER_INTERVAL)

    def stop_thinking_timer(self) -> None:
        if self.thinking_timer:
            self.thinking_timer.stop()
        self.thinking_start_time = None
        if self.main:
            self.main.think_timer_label.setText("思考用时: -")

    def pause_thinking_timer(self) -> None:
        if self.thinking_timer:
            self.thinking_timer.stop()
        if self.thinking_start_time:
            elapsed = self.thinking_start_time.secsTo(QDateTime.currentDateTime())
            if elapsed > 0:
                current_player = self.game.current_player
                if current_player == 1:
                    self.red_total_time += elapsed
                else:
                    self.black_total_time += elapsed
        self.thinking_start_time = None
        if self.main:
            self.main.think_timer_label.setText("思考用时: -")

    def resume_thinking_timer(self) -> None:
        if self.is_active and not self.is_paused and not self.game.game_over:
            current_player = self.game.current_player
            model = self.model1 if current_player == 1 else self.model2
            if model != HUMAN_MODEL:
                self.start_thinking_timer(current_player)

    def _update_thinking_time(self) -> None:
        if self.thinking_start_time:
            elapsed = self.thinking_start_time.secsTo(QDateTime.currentDateTime())
            formatted = format_duration(elapsed)
            self.main.think_timer_label.setText(f"思考用时: {formatted}")

    # ── 日志 ──

    def log(self, text: str, msg_type: str = 'INFO') -> None:
        """委托给 LogManager，确保 HTML 正确渲染"""
        if self.main and hasattr(self.main, 'log_manager'):
            self.main.log_manager.log(text, msg_type)

    def shutdown(self) -> None:
        """清理资源 — 关闭 Pikafish 引擎进程等。"""
        if self._pikafish:
            try:
                self._pikafish.close()
            except Exception:
                pass
            self._pikafish = None
