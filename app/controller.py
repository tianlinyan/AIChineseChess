import random
import threading
from typing import Any, Optional

from PyQt6.QtCore import QTimer, QDateTime

from domain.constants import (
    AI_RETRY_LIMIT, AI_RETRY_DELAY_MS, AI_DELAY_MS,
    THINKING_TIMER_INTERVAL,
    DEFAULT_SEARCH_DEPTH,
    OPENING_BOOK_ENABLED, OPENING_BOOK_MAX_MOVES,
    OPENING_DELAY_MS,
    AI_DEFAULT_MODE, ARBITRATION_TIMEOUT_SECONDS,
    MCTS_TIME_LIMIT,
    PIECE_SYMBOLS, format_duration, format_coord,
    parse_coord, format_move,
    PROMPT_HISTORY_MAX_ITEMS,
)
from domain.prompts import (
    HUMAN_MODEL, get_system_prompt,
    build_move_prompt, format_legal_moves,
    get_arbitration_system_prompt, build_arbitration_prompt,
    TOOLS_BASIC, DEFAULT_TOOLS,
)
from domain.game import ChineseChessGame
from domain.evaluation import compute_material, evaluate
from domain.openings import get_opening_move, get_opening_candidates, get_opening_names
from ai.manager import AIManager
from ai.worker import AIWorker
from app.engine_bridge import EngineBridge

class GameController:
    """游戏控制器 — 编排游戏逻辑、AI调用、搜索、开局库、UI更新"""

    def __init__(self, game: ChineseChessGame,
                 ai_manager: AIManager) -> None:
        self.game = game
        self.ai_manager = ai_manager

        self.main: Optional[Any] = None  # MainWindow（duck typing）

        self.model1 = None       # 红方模型
        self.model2 = None       # 黑方模型

        self.is_active: bool = False
        self.is_paused: bool = False
        self.retry_count: int = 0
        self.last_move_error: str = ''
        self.game_version: int = 0

        # ── AI 配置（红黑独立，可被 UI 修改） ──
        self.red_ai_mode: str = AI_DEFAULT_MODE
        self.black_ai_mode: str = AI_DEFAULT_MODE
        self.red_search_depth: int = DEFAULT_SEARCH_DEPTH
        self.black_search_depth: int = DEFAULT_SEARCH_DEPTH
        self.red_use_opening_book: bool = OPENING_BOOK_ENABLED
        self.black_use_opening_book: bool = OPENING_BOOK_ENABLED

        self.thinking_start_time: Optional[QDateTime] = None
        self.thinking_timer: Optional[QTimer] = None

        self.last_red_raw: str = ''
        self.last_black_raw: str = ''

        self._random_action_count: int = 0

        # ── 引擎桥接（Pikafish/MCTS 生命周期 + 异步回调）──
        self._engine = EngineBridge(
            log_cb=self.log,
            check_version_cb=self._check_callback_valid,
            finish_move_cb=self._finish_ai_move,
            get_search_depth=self._get_current_search_depth,
            get_cancel_version=lambda: self.ai_manager.cancel_version,
            get_game_version=lambda: self.game_version,
        )
        self._engine.search_done.connect(self._engine.handle_search_result)
        self._engine.hint_done.connect(self._on_hint_result)

        # ── Hybrid 模式：引擎搜索结果暂存（LLM 失败时兜底用）──
        self._hybrid_engine_move: Optional[tuple] = None

        # ── AI 计分（仲裁模式）──
        self.ai_score: int = 0  # LLM 与仲裁一致 +1，不一致 +0（不倒扣）
        self.arbitration_count: int = 0  # 仲裁触发次数

        # ── 仲裁暂存（LLM 与引擎分歧时等待 DeepSeek 裁决）──
        self._arbitration_llm_move: Optional[tuple] = None
        self._arbitration_llm_text: str = ''
        self._arbitration_engine_move: Optional[tuple] = None

        # ── 人类玩家参考提示 ──
        self._hint_active: bool = False
        self._current_ai_player: int = 0  # 当前正在决策的 AI 方

    # ── per-side 便捷访问 ──

    def _get_current_search_depth(self) -> int:
        """引擎桥接回调：返回当前 AI 方的搜索深度。

        所有搜索路径（make_ai_move / _fallback_to_search / 提示搜索）都会
        先设置 _current_ai_player；0 仅在尚未设置时出现，防御性回退到红方。
        """
        if self._current_ai_player == 1:
            return self.red_search_depth
        if self._current_ai_player == 2:
            return self.black_search_depth
        # 未初始化（0）：防御性回退到红方深度
        return self.red_search_depth

    def _ai_mode_for(self, player: int) -> str:
        return self.red_ai_mode if player == 1 else self.black_ai_mode

    def _use_opening_book_for(self, player: int) -> bool:
        return self.red_use_opening_book if player == 1 else self.black_use_opening_book

    # ── 游戏控制 ──

    def start_game(self) -> None:
        self.reset_game()

        if not self.main:
            return

        self.main.log_manager.clear()

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

        self.main.start_btn.setEnabled(False)
        self.main.pause_btn.setEnabled(True)
        self.main.reset_btn.setEnabled(True)
        # 对局进行中锁定模型选择：换模型不即时生效（易误解），
        # 且改动下拉框会误启用"开始对弈"（无确认重开会丢整局）
        self.main.model1_combo.setEnabled(False)
        self.main.model2_combo.setEnabled(False)

        self.main.update_game_status()
        self.main.update_player_status()

        # 先手方走子
        first_player = self.game.current_player
        first_model = self.model1 if first_player == 1 else self.model2
        self.main.start_thinking_timer(first_player)
        if first_model != HUMAN_MODEL:
            self._defer_ai_move(1000)

    def reset_game(self) -> None:
        self.game_version += 1
        self.stop_thinking_timer()

        # 取消运行中的 AI 任务，防止残留 Worker 阻塞新游戏
        self.ai_manager.clear_queue()
        self.ai_manager.set_busy(False)
        self._engine.stop_all()

        self.is_active = False
        self.is_paused = False
        self.retry_count = 0
        self.last_move_error = ''
        self._random_action_count = 0
        self._hint_active = False

        self.game.reset()
        if self.main:
            self.main.board_widget.selected_row = -1
            self.main.board_widget.selected_col = -1
            self.main.board_widget.update()

        self.last_red_raw = ''
        self.last_black_raw = ''

        # 重置 AI 计分
        self.ai_score = 0
        self.arbitration_count = 0
        self._arbitration_llm_move = None
        self._arbitration_llm_text = ''
        self._arbitration_engine_move = None
        if self.main:
            self.main.update_ai_score()

        if self.main:
            self.main.update_player_status()
            self.main.update_game_status()

            self.main.update_history_list()
            self.main.start_btn.setEnabled(True)
            self.main.pause_btn.setEnabled(False)
            self.main.pause_btn.setText("暂停")
            self.main.reset_btn.setEnabled(False)
            self.main.model1_combo.setEnabled(True)
            self.main.model2_combo.setEnabled(True)

    def toggle_pause(self) -> None:
        if not self.is_active:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.main.pause_btn.setText("恢复")
            self.main.pause_thinking_timer()
            self.ai_manager.clear_queue()
            self.ai_manager.set_busy(False)
            self._engine.stop_all()
            self.retry_count = 0
        else:
            self.main.pause_btn.setText("暂停")
            self.main.resume_thinking_timer()
            if not self.game.game_over and not self.ai_manager.is_busy():
                current_player = self.game.current_player
                current_model = self.model1 if current_player == 1 else self.model2
                if current_model != HUMAN_MODEL:
                    self._defer_ai_move(AI_DELAY_MS)
                else:
                    self.main.start_thinking_timer(current_player)

    # ── 人类落子 ──

    def on_human_move(self, from_row: int, from_col: int,
                      to_row: int, to_col: int) -> None:
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        if self.ai_manager.is_busy():
            # 防御：AI 回合残留 busy 时禁止人类走子，避免与在飞 AI 请求竞争
            self.log("AI 正在思考中，暂无法走子", 'WARNING')
            return
        current_player = self.game.current_player
        current_model = self.model1 if current_player == 1 else self.model2
        if current_model != HUMAN_MODEL:
            return

        result = self.game.move_piece(from_row, from_col, to_row, to_col)
        if result['success']:
            self._hint_active = False  # 取消进行中的提示搜索
            # 结算人类思考计时（红/黑总用时数据无消费者，仅复位计时段）
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
                    self._defer_ai_move(AI_DELAY_MS)
                else:
                    self.main.start_thinking_timer(next_player)
        else:
            self.log(f"移动非法: {result['message']}", 'ERROR')

    # ── 人类参考提示 ──

    def _trigger_hint(self) -> None:
        """人类回合开始时触发参考提示（输出到思考日志）。

        两阶段：
        1. 开局阶段（≤OPENING_BOOK_MAX_MOVES 手）：显示开局库候选走法
        2. 中残局阶段：启动 Pikafish 搜索（时间=搜索深度×3s，上限 MCTS_TIME_LIMIT），结果显示在日志中
        """
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        # 重入保护：人类快速连走时 start_thinking_timer 会投递多个
        # singleShot(300)，不检查会导致并发启动多个 30s 提示搜索，
        # 且先完成的回调把 _hint_active 置 False 使后一个结果被丢弃
        if self._hint_active:
            return
        current_player = self.game.current_player
        current_model = self.model1 if current_player == 1 else self.model2
        if current_model != HUMAN_MODEL:
            return

        player_name = '红方' if current_player == 1 else '黑方'

        # ── 阶段 1：开局库提示 ──
        if self._use_opening_book_for(current_player) and len(self.game.moves) < OPENING_BOOK_MAX_MOVES:
            candidates = get_opening_candidates(self.game.moves)
            if candidates:
                # 显示匹配的开局名称
                names = get_opening_names(self.game.moves)
                if names:
                    self.log(f"📚 当前开局: {', '.join(names)}", 'INFO')
                lines = []
                for (fr, fc, tr, tc), _ in candidates[:5]:
                    pn = PIECE_SYMBOLS.get(self.game.board[fr][fc], '?')
                    lines.append(f"{pn} {format_move(fr, fc, tr, tc)}")
                self.log(
                    f"📖 {player_name} 开局库参考: {' | '.join(lines)}",
                    'INFO')
                return

        # ── 阶段 2：Pikafish 引擎提示 ──
        self.log(f"🔍 正在为{player_name}生成 Pikafish 参考（{int(MCTS_TIME_LIMIT)}s）...", 'INFO')
        self._hint_active = True

        def _on_hint_done(move, version, cancel_version):
            if not self._check_callback_valid(version, cancel_version, 'hint'):
                return
            if not self._hint_active:
                return
            self._hint_active = False
            if self.game.current_player != current_player:
                return  # 人类已经落子了
            if move:
                fr, fc, tr, tc = move
                pn = PIECE_SYMBOLS.get(self.game.board[fr][fc], '?')
                self.log(
                    f"🔍 {player_name} Pikafish 参考: "
                    f"{pn} {format_move(fr, fc, tr, tc)}",
                    'INFO')
            else:
                self.log(f"💡 {player_name} 引擎暂无建议，请自行走子", 'INFO')

        self._current_ai_player = current_player  # 引擎桥接回调用（提示搜索也可能先于任何 AI 走子）

        self._engine.start_hint_search(
            self.game, current_player, _on_hint_done)

    def _on_hint_result(self, args: tuple) -> None:
        """中继 hint 结果到主线程（由 hint_done 信号触发）。"""
        move, version, cancel_version, on_done = args
        on_done(move, version, cancel_version)

    # ── AI 走子（核心引擎） ──

    def make_ai_move(self, expected_version: Optional[int] = None) -> None:
        if expected_version is not None and expected_version != self.game_version:
            return
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        if self.ai_manager.is_busy():
            return

        current_player = self.game.current_player
        self._current_ai_player = current_player  # 引擎桥接回调用
        # 重置上轮引擎走法——防止开局库路径跳过引擎搜索后
        # 旧走法被本轮仲裁/兜底误用
        self._hybrid_engine_move = None
        model = self.model1 if current_player == 1 else self.model2
        if model == HUMAN_MODEL:
            return

        ai_mode = self._ai_mode_for(current_player)

        # 回合分隔标记（重试/兜底链再入时不重复打印）
        if self.retry_count == 0:
            ply = len(self.game.moves) + 1
            player_name = '红方' if current_player == 1 else '黑方'
            self.log(f"━━ 第 {ply} 手 · {player_name}（{model.name}）━━", 'INFO')

        # ── 1. 开局库查询 ──
        if self._use_opening_book_for(current_player) and len(self.game.moves) < OPENING_BOOK_MAX_MOVES:
            # get_opening_move 不在库中时返回 None，无需 is_in_opening_book 预检
            # （旧写法预检本身做一次加权随机再丢弃，纯属浪费）
            book_move = get_opening_move(self.game.moves)
            if book_move:
                fr, fc, tr, tc = book_move
                result = self.game.move_piece(fr, fc, tr, tc)
                if result['success']:
                    self._on_move_success(fr, fc, tr, tc, current_player,
                                          source='开局库')
                    # 显示匹配的开局名称
                    names = get_opening_names(self.game.moves)
                    if names:
                        self.log(f"📚 当前开局: {', '.join(names)}", 'INFO')
                    self._refresh_ui()
                    if result.get('game_over'):
                        self.handle_game_over(result)
                        return
                    self._schedule_next_ai_move(delay=OPENING_DELAY_MS)
                    return

        # ── 2. 纯搜索模式（Pikafish/MCTS 异步，不阻塞UI）──
        if ai_mode == 'search_only':
            self.ai_manager.set_busy(True)

            def _on_search(move, p):
                # 检查暂停/关闭状态，防止异步回调在暂停后执行走子
                if self.is_paused or not self.is_active or self.game.game_over:
                    self._finish_ai_move()
                    return
                # Pikafish 失败 → MCTS 快速回退（同样后台线程）
                if not move:
                    self._engine.start_mcts_fallback(
                        self.game, p,
                        lambda m2, p2: self._execute_engine_move_or_random(
                            m2, p2, '搜索'))
                    return
                # search_only 模式下 Pikafish 结果即最终走法，在此记日志
                # （hybrid 模式由 _on_hybrid_engine_done 另行记录）
                fr, fc, tr, tc = move
                pn = PIECE_SYMBOLS.get(self.game.board[fr][fc], '?')
                self.log(f"🔍 Pikafish 推荐: {pn} "
                         f"{format_move(fr, fc, tr, tc)}", 'INFO')
                self._execute_engine_move_or_random(move, p, '搜索')

            self._engine.start_search(self.game, current_player, _on_search)
            return

        # ── 3. Hybrid 模式：引擎优先（Pikafish→MCTS）→ LLM 参考引擎 ──
        if ai_mode == 'hybrid':
            self.ai_manager.set_busy(True)

            def _on_engine_ready(move, p):
                if not self.is_active or self.is_paused or self.game.game_over:
                    self._finish_ai_move()
                    return
                if move and self._engine.pikafish and self._engine.pikafish_available:
                    engine_name = 'Pikafish'
                    engine_desc = f'深度搜索完成'
                elif move:
                    engine_name = 'MCTS'
                    engine_desc = f'Pikafish 不可用，MCTS 搜索完成'
                else:
                    # Pikafish 无结果 → MCTS 兜底
                    def _on_fb(m2, p2):
                        if not self.is_active or self.is_paused or self.game.game_over:
                            self._finish_ai_move()
                            return
                        self._on_hybrid_engine_done(m2, p2, 'MCTS(回退)',
                                                    'Pikafish 失败，MCTS 兜底')
                    self._engine.start_mcts_fallback(self.game, p, _on_fb)
                    return
                self._on_hybrid_engine_done(move, p, engine_name, engine_desc)

            self._engine.start_search(self.game, current_player, _on_engine_ready)
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
        # 视觉模式判断（截图失败时回退文字棋盘，否则 LLM 将完全
        # 拿不到棋盘信息——提示词声称有图，实际既无图也无文字棋盘）
        use_vision = self.main and self.main.vision_check.isChecked()
        # DeepSeek API 不支持 image_url 类型，启用视觉会直接 400
        if use_vision and model.type == 'deepseek':
            use_vision = False
            self.log("视觉模式对 DeepSeek 不可用（API 仅支持 text），已自动切换文字棋盘", 'INFO')
        image = None
        if use_vision:
            try:
                image = self.main.board_widget.capture_board_image()
            except Exception as e:
                self.log(f"视觉模式截图失败: {e}，回退到文字棋盘", 'WARNING')
                image = None
            if not image:  # 异常或空结果（如 pixmap 为空返回 ''）
                use_vision = False
                self.log("视觉模式无有效截图，回退到文字棋盘", 'WARNING')

        # 构建提示词
        board_str = '' if use_vision else self.game.get_board_state_string()
        history = self.game.format_move_history(max_items=PROMPT_HISTORY_MAX_ITEMS)

        # 将军状态
        in_check = self.game.is_in_check(player)
        opponent = 2 if player == 1 else 1
        opponent_in_check = self.game.is_in_check(opponent)
        move_count = len(self.game.moves) // 2 + 1  # 回合数（双方各走一次=一回合）

        # 合法走法
        legal_moves = self.game.get_all_legal_moves(player)
        legal_move_count = len(legal_moves)

        # 零合法走法 = 将杀或困毙 → 直接判定游戏结束，跳过 LLM 调用
        if legal_move_count == 0:
            self.game.game_over = True
            self.game.winner = opponent
            self.handle_game_over({
                'game_over': True, 'winner': opponent,
                'message': f"{'红方' if opponent == 1 else '黑方'}获胜（{'将杀' if self.game.is_in_check(player) else '困毙'}）"
            })
            self._finish_ai_move()
            return

        # 格式化合法走法列表（×吃子 / +将军 / ⚠️重复标注）
        repetition_moves = self.game.find_repetition_moves(legal_moves)
        legal_moves_str = format_legal_moves(
            legal_moves, self.game.board, player,
            repetition_moves=repetition_moves)

        # 子力对比（视角相对：帮助 LLM 落实"优势简化、劣势复杂"策略）
        red_mat, black_mat, _, _ = compute_material(self.game.board)
        mine, theirs = (red_mat, black_mat) if player == 1 else (black_mat, red_mat)
        mat_diff = mine - theirs
        if mat_diff > 0:
            mat_trend = f"你领先 +{mat_diff:g}，可主动兑子简化"
        elif mat_diff < 0:
            mat_trend = f"你落后 {-mat_diff:g}，避免无补偿兑子"
        else:
            mat_trend = "子力均势"
        material_str = f"子力对比（单位=兵）：你 {mine:g} : {theirs:g} 对手 —— {mat_trend}"

        # 上一步走子描述
        last_move_str = ''
        if self.game.last_move:
            fr, fc, tr, tc, lp = self.game.last_move
            piece = self.game.board[tr][tc]
            piece_name = PIECE_SYMBOLS.get(piece, piece)
            captured = self.game.moves[-1][5] if self.game.moves else '.'
            action = f"{'红方' if lp == 1 else '黑方'} {piece_name} {format_move(fr, fc, tr, tc)}"
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
            legal_moves_str=legal_moves_str,
            last_move_error=self.last_move_error,
            retry_count=self.retry_count,
            vision_mode=use_vision,
            engine_hint=engine_hint,
            material_str=material_str,
        )

        # 确认日志：引擎参考是否已注入提示词
        if engine_hint:
            self.log("📤 引擎推荐已注入 LLM 提示词", 'INFO')

        player_name = '红方' if player == 1 else '黑方'
        current_version = self.game_version

        # 系统提示词 — 统一使用完整版（含完整棋规：棋子走法表/九宫/约束），
        # 不依赖"模型已掌握棋规"的假设；DeepSeek 等强模型同样接收完整规则。
        # models.json 中配置了 system_prompt 的模型优先使用自定义提示词。
        # llm_only 模式下不描述不可用的分析工具。
        include_tools = self._ai_mode_for(player) != 'llm_only'
        if model.system_prompt:
            system_prompt = model.system_prompt
        else:
            system_prompt = get_system_prompt(include_analysis_tools=include_tools)

        # 工具选择：仅 LLM 模式只用 move_piece；其他模式用全部工具
        worker_tools = TOOLS_BASIC if self._ai_mode_for(player) == 'llm_only' else DEFAULT_TOOLS

        # LLM 启动日志：显示模型名与关键上下文（视觉/引擎参考）
        extras = []
        if use_vision:
            extras.append('视觉模式')
        if engine_hint:
            extras.append('含引擎参考')
        extra_str = f" · {'，'.join(extras)}" if extras else ''
        self.log(f"🤖 {player_name} {model.name} 思考中{extra_str}...", 'INFO')

        # 标记忙碌
        self.ai_manager.set_busy(True)

        worker = AIWorker(
            model, prompt, image, player_name,
            version=current_version,
            cancel_version=self.ai_manager.cancel_version,
            system_prompt=system_prompt,
            tools=worker_tools,
            game=self.game,
            current_player=player,
            # evaluate_position 工具用 Pikafish 大师级评估（None 时回退手工）
            pikafish=self._engine.pikafish,
        )
        worker.signals.finished.connect(self.on_ai_finished)
        self.ai_manager.set_active_worker(worker)
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        self.ai_manager.set_active_thread(t)

    def _on_hybrid_engine_done(self, engine_move: Optional[tuple],
                                player: int, engine_name: str = '引擎',
                                engine_desc: str = '') -> None:
        """Hybrid 模式：引擎搜索完成 → 日志 + 格式化提示 → 启动 LLM。"""
        if not self.is_active or self.is_paused or self.game.game_over:
            self._finish_ai_move()
            return

        player_name = '红方' if player == 1 else '黑方'

        # 格式化引擎参考走法 + 日志输出
        engine_hint = ''
        if engine_move:
            efr, efc, etr, etc = engine_move
            # 验证走法在合法列表中（防止引擎因坐标系问题返回非法走法）
            legal = self.game.get_all_legal_moves(player)
            if (efr, efc, etr, etc) not in legal:
                self.log(f"引擎走法 {format_move(efr, efc, etr, etc)} 不在合法列表中，丢弃", 'WARNING')
                engine_move = None
            elif self.game.board[efr][efc] != '.':
                epn = PIECE_SYMBOLS.get(self.game.board[efr][efc], '?')
                emove_str = f"{epn} {format_move(efr, efc, etr, etc)}"
                self.log(f"🔍 {engine_name} 推荐: {emove_str}", 'INFO')
                # 信任分级：Pikafish 推荐是重要参考——不强制采信，也不轻慢；
                # MCTS 兜底为本地轻量引擎，仅作参考。
                # （search_best_move 现也由 Pikafish 提供，与引擎推荐同源，
                # 不重复强调"低于引擎"以免错误校准 LLM 信任。）
                if engine_name == 'Pikafish':
                    trust_note = (
                        f"{engine_name} 是顶级战术引擎，战术计算远超语言模型，"
                        f"其推荐经过深度分析，应作为**重要参考优先考虑**。"
                        f"你拥有最终决定权：没有反对理由时采纳推荐；"
                        f"若基于具体战略理由（长线弃子/残局过渡/阵型缺陷/局面直觉）"
                        f"认为推荐并非最优，可坚持己见并说明理由——"
                        f"理由充分时，不采纳推荐是允许的。"
                        f"对推荐有疑虑时，可调用 evaluate_position / search_best_move "
                        f"二次核对（两者同样由 {engine_name} 提供，与推荐同源，"
                        f"仅在核对时有意义）。"
                    )
                else:
                    # MCTS 兜底分支：此时 Pikafish 不可用（否则不会走兜底），
                    # 工具实际会降级为本地搜索/手工评估——文案不得声称
                    # "由 Pikafish 提供"，否则误导 LLM 信任降级工具
                    if self._engine.pikafish_available:
                        tool_note = ("建议用 search_best_move / evaluate_position"
                                     "（由 Pikafish 提供）独立分析后自主决策。")
                    else:
                        tool_note = ("分析工具当前降级为本地轻量评估"
                                     "（Pikafish 不可用），以你的判断为主。")
                    trust_note = (
                        f"{engine_name} 是本地轻量引擎，强度有限，其推荐仅供参考。"
                        f"{tool_note}"
                    )
                engine_hint = (
                    f"## 🔍 {engine_name} 参考走法\n\n"
                    f"{engine_name} {engine_desc}，推荐：\n"
                    f"**{emove_str}**\n\n"
                    f"{trust_note}"
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
        if not self._check_callback_valid(version, cancel_version, 'AI',
                                          needs_cleanup=True):
            return

        current_player = self.game.current_player
        player_name = '红方' if current_player == 1 else '黑方'

        if full_text:
            # 标题行保留红/黑颜色区分（红方日志红、黑方日志黑）；
            # 思考正文改用 INFO 灰（与 "🔍 Pikafish 推荐" 等引擎日志同色），
            # 避免整块红/黑着色导致长思考文本难以阅读
            self.log(f"  {player_name} AI 思考:",
                     'red' if current_player == 1 else 'black')
            self.log(full_text.strip(), 'INFO')
        if error:
            self.log(f"{player_name} AI 错误: {error}", 'ERROR')

        # ── LLM 失败 → 直接采用引擎结果，不仲裁 ──
        if error or not from_coord or not to_coord:
            if self._ai_mode_for(current_player) == 'hybrid':
                self.log(f"{player_name} LLM 调用失败 ({error or '无有效走法'})，直接采用引擎走法", 'WARNING')
                self._fallback_hybrid_engine(current_player)
                return
            else:
                # llm_only 模式：原有重试逻辑
                self._retry_move(error)
                return

        # ── 解析坐标 ──
        try:
            from_row, from_col = parse_coord(from_coord)
            to_row, to_col = parse_coord(to_coord)
        except (ValueError, IndexError, TypeError, AttributeError):
            self.last_move_error = (
                f"坐标 '{from_coord}'→'{to_coord}' 无法解析。"
                f"请确保坐标格式正确：列字母 A~I，行数字 1~10。"
            )
            if self._ai_mode_for(current_player) == 'hybrid':
                self.log(f"{player_name} 坐标解析失败，直接采用引擎走法", 'WARNING')
                self._fallback_hybrid_engine(current_player)
                return
            else:
                self.retry_count += 1
                if self.retry_count <= AI_RETRY_LIMIT:
                    self.ai_manager.set_busy(False)
                    self.ai_manager.clear_active_worker()
                    self._defer_ai_move(self.retry_count * AI_RETRY_DELAY_MS)
                    return
                else:
                    self._finish_ai_move()
                    self._fallback_to_search(current_player)
                    return

        # ── LLM 走法即为最终决定（hybrid 模式下引擎仅作参考）──
        final_move = (from_row, from_col, to_row, to_col)

        # ── Hybrid 模式合法性预检：非法走法直接用引擎兜底，
        #    不进入仲裁（否则仲裁一个非法候选，最终可能随机落子，
        #    而验证过的引擎走法明明可用）──
        if self._ai_mode_for(current_player) == 'hybrid':
            legal_moves = self.game.get_all_legal_moves(current_player)
            if final_move not in legal_moves:
                self.last_move_error = f"{from_coord}→{to_coord}（原因：走法不合法）"
                self.log(f"{player_name} 走法 {from_coord}→{to_coord} 非法，直接采用引擎走法", 'WARNING')
                self._fallback_hybrid_engine(current_player, final_move)
                return

        # ── Hybrid 模式分歧检测：LLM ≠ 引擎 → 启动第三方仲裁 ──
        if (self._ai_mode_for(current_player) == 'hybrid' and self._hybrid_engine_move
                and final_move != self._hybrid_engine_move):
            # 暂存双方走法，启动 DeepSeek 仲裁
            self._arbitration_llm_move = final_move
            self._arbitration_llm_text = full_text
            self._arbitration_engine_move = self._hybrid_engine_move
            efr, efc, etr, etc = self._hybrid_engine_move

            # 格式化日志
            epiece = self.game.board[efr][efc] if self.game.board[efr][efc] != '.' else '?'
            epn = PIECE_SYMBOLS.get(epiece, epiece)
            erec = f"{epn} {format_move(efr, efc, etr, etc)}"
            lpiece = self.game.board[from_row][from_col] if self.game.board[from_row][from_col] != '.' else '?'
            lpn = PIECE_SYMBOLS.get(lpiece, lpiece)
            lrec = f"{lpn} {format_move(from_row, from_col, to_row, to_col)}"
            self.log(
                f"⚖️ 分歧检测 | LLM选择: {lrec} | 引擎推荐: {erec} | 启动 DeepSeek 仲裁...",
                'WARNING')

            # 启动仲裁（异步），仲裁完成后由 on_arbitration_finished 接管
            self._start_arbitration(current_player)
            return  # 暂不执行走子，等待仲裁结果

        # ── Hybrid 模式且 LLM 采纳了引擎走法 → 记一致性日志（最常见路径不再静默）──
        if (self._ai_mode_for(current_player) == 'hybrid' and self._hybrid_engine_move
                and final_move == self._hybrid_engine_move):
            self.log("🤝 LLM 与引擎意见一致", 'INFO')

        # ── 执行最终走法 ──
        result = self.game.move_piece(from_row, from_col, to_row, to_col)
        if not result['success']:
            reason = result.get('message', '未知原因')
            self.last_move_error = f"{from_coord}→{to_coord}（原因：{reason}）"

            if self._ai_mode_for(current_player) == 'hybrid':
                self.log(f"{player_name} 走子非法 ({reason})，直接采用引擎走法", 'WARNING')
                self._fallback_hybrid_engine(current_player, final_move)
                return
            else:
                # llm_only：重试
                self.retry_count += 1
                if self.retry_count <= AI_RETRY_LIMIT:
                    self.ai_manager.set_busy(False)
                    self.ai_manager.clear_active_worker()
                    self._defer_ai_move(self.retry_count * AI_RETRY_DELAY_MS)
                    return
                else:
                    self._finish_ai_move()
                    self._fallback_to_search(current_player)
                    return

        # ── 移动成功 ──
        self._on_move_success(from_row, from_col, to_row, to_col,
                              current_player, source='LLM')
        self._refresh_ui()
        self._finish_ai_move()

        if result.get('game_over'):
            self.handle_game_over(result)
            return

        # ── 下一步 ──
        self._schedule_next_ai_move()

    def _execute_engine_move_or_random(self, move: Optional[tuple],
                                       player: int, source: str,
                                       reset_random_count: bool = False) -> None:
        """主线程：执行引擎走法；无走法/走子失败时回退随机走子。"""
        if self.is_paused or not self.is_active or self.game.game_over:
            self._finish_ai_move()
            return
        if move:
            self._execute_guarded_move(move, player, source,
                                       reset_random=reset_random_count)
        else:
            self._finish_ai_move()
            self._random_move(player)

    def _fallback_to_search(self, player: int) -> None:
        """LLM 失败时回退到搜索引擎（替代随机走子）"""
        self._current_ai_player = player  # 确保引擎桥接读到正确深度
        if self._random_action_count > 3:
            self.log("连续回退过多 — 停止游戏", 'ERROR')
            self.handle_game_over(
                {'game_over': True, 'winner': 0, 'message': '连续异常，游戏终止'})
            return

        # Pikafish/MCTS 异步回退
        self.ai_manager.set_busy(True)

        def _on_fb(move, p):
            # 检查暂停/关闭状态，防止异步回调在暂停后执行走子
            if self.is_paused or not self.is_active or self.game.game_over:
                self._finish_ai_move()
                return
            # Pikafish 失败 → MCTS 快速回退（后台线程）
            if not move:
                self._engine.start_mcts_fallback(
                    self.game, p,
                    lambda m2, p2: self._execute_engine_move_or_random(
                        m2, p2, '搜索回退', reset_random_count=True))
                return
            self._execute_engine_move_or_random(
                move, p, '搜索回退', reset_random_count=True)

        self._engine.start_search(self.game, player, _on_fb)
        return

    def _random_move(self, current_player: int) -> None:
        """随机走子（最后的fallback）"""
        self._random_action_count += 1
        if self._random_action_count > 3:
            self.log("连续随机走子过多 — 停止游戏", 'ERROR')
            self.handle_game_over(
                {'game_over': True, 'winner': 0, 'message': '连续异常，游戏终止'})
            return

        moves = self.game.get_all_legal_moves(current_player)
        if moves:
            fr, fc, tr, tc = random.choice(moves)
            result = self.game.move_piece(fr, fc, tr, tc)
            if result['success']:
                self._on_move_success(fr, fc, tr, tc, current_player,
                                      source='随机')
                # 注意：随机走子成功不重置计数——防止无限[随机]循环
                self._refresh_ui()
                if result['game_over']:
                    self.handle_game_over(result)
                    return
                self._schedule_next_ai_move()
                return
            else:
                self.log(f"随机走子失败: {result.get('message', '未知')}", 'ERROR')
                self._finish_ai_move()
                return

        # 确实无合法走法 — 困毙/将杀判负（中国象棋规则：无棋可走者负）
        self.game.game_over = True
        opponent = 3 - current_player
        self.game.winner = opponent
        kind = '将杀' if self.game.is_in_check(current_player) else '困毙'
        self.handle_game_over({
            'game_over': True, 'winner': opponent,
            'message': f"{'红方' if opponent == 1 else '黑方'}获胜（{kind}）"})

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
            self._defer_ai_move(delay)
            return

        self.log("超过重试次数，回退搜索", 'ERROR')
        self._finish_ai_move()
        self._fallback_to_search(self.game.current_player)

    # ── 走子成功处理 ──

    def _on_move_success(self, from_row: int, from_col: int,
                          to_row: int, to_col: int,
                          current_player: int,
                          source: str = '') -> None:
        """统一处理走子成功后的状态更新"""
        # 结束当前思考计时段（红/黑总用时数据无消费者，仅复位）
        self.thinking_start_time = None

        # 更新统计
        self.last_move_error = ''

        # 记录走子
        from_coord = format_coord(from_row, from_col)
        to_coord = format_coord(to_row, to_col)
        move_desc = f"{source}:{from_coord}→{to_coord}" if source else f"{from_coord}→{to_coord}"

        if current_player == 1:
            self.last_red_raw = move_desc
        else:
            self.last_black_raw = move_desc

        self.retry_count = 0
        # 非随机走子成功 → 重置随机回退计数（"连续随机上限 3 次"指连续，
        # 中间有正常走子应重新累计；随机走子自身不重置，防无限循环）
        if source != '随机':
            self._random_action_count = 0

        piece_name = PIECE_SYMBOLS.get(
            self.game.board[to_row][to_col], '?')
        self.log(f"{'红方' if current_player == 1 else '黑方'} [{source}] "
                 f"{piece_name} {from_coord}→{to_coord}",
                 'red' if current_player == 1 else 'black')

    # ── 走子后 UI 刷新辅助方法 ──

    def _refresh_ui(self) -> None:
        """统一刷新棋盘和状态 UI（替代多处重复的 4 行调用）。"""
        if self.main:
            self.main.board_widget.update()
            self.main.update_game_status()
            self.main.update_history_list()
            self.main.update_player_status()

    def _defer_ai_move(self, delay: int = AI_DELAY_MS) -> None:
        """延迟调度 AI 走子（自动捕获 game_version 防陈旧回调）。"""
        QTimer.singleShot(delay,
            lambda v=self.game_version: self.make_ai_move(expected_version=v))

    def _schedule_next_ai_move(self, delay: int = AI_DELAY_MS) -> None:
        """若游戏仍在进行且下一方为 AI，则调度其走子。"""
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        if self.main:
            self.main.start_thinking_timer(self.game.current_player)
        next_player = self.game.current_player
        next_model = self.model1 if next_player == 1 else self.model2
        if next_model != HUMAN_MODEL:
            self._defer_ai_move(delay)

    def _check_callback_valid(self, version: int, cancel_version: int,
                              label: str, needs_cleanup: bool = False) -> bool:
        """检查异步回调是否仍有效（版本/取消门控 + 关闭状态）。

        Args:
            version: 回调携带的 game_version
            cancel_version: 回调携带的 cancel_version
            label: 日志标签（如 "AI"、"PF"、"仲裁"）
            needs_cleanup: True 时在状态异常后调用 _finish_ai_move()

        Returns:
            True 表示回调有效可继续处理，False 表示已过期需丢弃。
        """
        if self.ai_manager.is_shutting_down:
            if needs_cleanup:
                self._finish_ai_move()
            return False
        if version != self.game_version:
            self.log(f"[诊断] {label}回调版本不匹配({version}!={self.game_version})，丢弃", 'INFO')
            return False
        if cancel_version != self.ai_manager.cancel_version:
            self.log(f"[诊断] {label}回调取消版本不匹配({cancel_version}!={self.ai_manager.cancel_version})，丢弃", 'INFO')
            return False
        if self.is_paused or not self.is_active or self.game.game_over:
            if needs_cleanup:
                self._finish_ai_move()
            return False
        return True

    def _fallback_hybrid_engine(self, player: int, final_move=None) -> None:
        """Hybrid 模式引擎兜底：LLM 失败时采用引擎走法或随机。"""
        self._finish_ai_move()
        engine_move = self._hybrid_engine_move
        if engine_move and (final_move is None or engine_move != final_move):
            self._execute_guarded_move(engine_move, player, '引擎兜底')
        else:
            self._random_move(player)

    def _execute_guarded_move(self, move: tuple, player: int, source: str,
                              reset_random: bool = True) -> None:
        """统一兜底走法：执行走法并处理后走子流程。

        失败时回退 _random_move（原 on_failure 参数无任何调用方传值，已移除）。
        """
        fr, fc, tr, tc = move
        result = self.game.move_piece(fr, fc, tr, tc)
        if result['success']:
            self._on_move_success(fr, fc, tr, tc, player, source=source)
            if reset_random:
                self._random_action_count = 0
            self._refresh_ui()
            if result.get('game_over'):
                self._finish_ai_move()
                self.handle_game_over(result)
                return
            self._schedule_next_ai_move()
            self._finish_ai_move()
        else:
            self.log(f"{source}走子失败: {result.get('message', '未知')}", 'ERROR')
            self._finish_ai_move()
            self._random_move(player)

    # ── 第三方仲裁（DeepSeek） ──

    def _build_candidate_facts(self, move: tuple, repetition_moves: set) -> str:
        """为仲裁候选生成客观事实包（吃子/将军/将杀/评估/重复），不标识来源。

        两个候选由同一代码生成同等密度的事实，供仲裁只按客观优劣裁决，
        消除"LLM 候选带完整推理、引擎候选依据为空"的信息不对称。
        评分沿用 evaluate_position 口径：红方视角，正值=红优。
        """
        fr, fc, tr, tc = move
        facts = []
        captured = self.game.board[tr][tc]
        if captured != '.':
            facts.append(f"直接吃子：得{PIECE_SYMBOLS.get(captured, captured)}。")
        if move in repetition_moves:
            facts.append("走后局面第三次重复 — 将判和棋/长将判负。")
            return ''.join(facts)
        mover = self.game.current_player
        tmp = self.game.snapshot()
        tmp.current_player = mover
        result = tmp.move_piece(fr, fc, tr, tc)
        if result.get('success'):
            msg = result.get('message', '')
            if '将死' in msg:
                facts.append("走后将死对方，直接获胜。")
            elif tmp.is_in_check(3 - mover):
                facts.append("走后将军对方。")
            facts.append(f"走后局面评估：{evaluate(tmp.board):+.0f}（红方视角，正值=红优）。")
        return ''.join(facts) if facts else "（未提供分析）"

    def _start_arbitration(self, player: int) -> None:
        """启动 DeepSeek 第三方仲裁。

        当 LLM 与引擎走法不一致时调用。
        异步请求 DeepSeek 裁决，结果由 on_arbitration_finished 处理。
        DeepSeek 不可用时直接采用 LLM 走法。
        """
        # 查找仲裁模型：优先 id='arbitration'，其次 type='deepseek'
        arbitrator_model = None
        if self.main and hasattr(self.main, 'model_manager'):
            for m in self.main.model_manager.models:
                if m.id == 'arbitration':
                    arbitrator_model = m
                    break
            if not arbitrator_model:
                for m in self.main.model_manager.models:
                    if m.type and m.type == 'deepseek':
                        arbitrator_model = m
                        break

        if not arbitrator_model:
            self.log("⚠️ 未找到 DeepSeek 模型配置，仲裁跳过 → 采用 LLM 走法", 'WARNING')
            self._finish_ai_move()
            if self._arbitration_llm_move:
                self._execute_guarded_move(self._arbitration_llm_move,
                                           player, 'LLM(仲裁回退)')
            else:
                self._random_move(player)
            return

        player_name = '红方' if player == 1 else '黑方'

        # 构建仲裁提示词
        board_str = self.game.get_board_state_string()
        history = self.game.format_move_history(max_items=PROMPT_HISTORY_MAX_ITEMS)
        legal_moves = self.game.get_all_legal_moves(player)

        opponent = 2 if player == 1 else 1
        move_count = len(self.game.moves) // 2 + 1  # 回合数

        # 零合法走法 = 不应到达这里（LLM 和引擎都已返回走法），但防御性检查
        if len(legal_moves) == 0:
            self.game.game_over = True
            self.game.winner = opponent
            self.log("仲裁时检测到零合法走法，游戏结束", 'ERROR')
            self._finish_ai_move()
            self.handle_game_over({
                'game_over': True, 'winner': opponent,
                'message': f"{'红方' if opponent == 1 else '黑方'}获胜"
            })
            return

        repetition_moves = self.game.find_repetition_moves(legal_moves)
        legal_moves_str = format_legal_moves(
            legal_moves, self.game.board, player,
            repetition_moves=repetition_moves)

        # 子力对比（红黑对称的客观事实，供"形势匹配"维度裁决）
        red_mat, black_mat, _, _ = compute_material(self.game.board)
        material_str = f"子力对比（单位=兵）：红 {red_mat:g} : {black_mat:g}"

        # LLM 走法描述
        lfr, lfc, ltr, ltc = self._arbitration_llm_move
        lpiece = self.game.board[lfr][lfc]
        lpn = PIECE_SYMBOLS.get(lpiece, '?')
        llm_move_str = f"{lpn} {format_move(lfr, lfc, ltr, ltc)}"

        # 引擎走法描述
        efr, efc, etr, etc = self._arbitration_engine_move
        epiece = self.game.board[efr][efc]
        epn = PIECE_SYMBOLS.get(epiece, '?')
        engine_move_str = f"{epn} {format_move(efr, efc, etr, etc)}"

        # 候选依据：为两个候选对称生成客观事实（吃子/将军/将杀/评估/重复），
        # 不出现"引擎/搜索"等来源标识——与仲裁提示词"不要揣测候选来源"一致，
        # 同时消除"LLM 候选带完整推理、引擎候选依据为空"的信息不对称
        engine_basis = self._build_candidate_facts(
            (efr, efc, etr, etc), repetition_moves)
        llm_basis = self._build_candidate_facts(
            (lfr, lfc, ltr, ltc), repetition_moves)

        # 将军状态
        in_check = self.game.is_in_check(player)
        opponent_in_check = self.game.is_in_check(opponent)

        # 候选 A/B 顺序由本处决定并同步构建 label_to_move 映射：
        # 实证 qwen3.8 思考模式会吞掉工具调用（0 tool_calls + 空 content），
        # 仲裁模型只需在文本里表达"选 A/B"，worker 据此解析候选。
        order = [0, 1]  # 0=LLM 候选, 1=引擎候选
        random.shuffle(order)
        llm_move = self._arbitration_llm_move
        engine_move = self._arbitration_engine_move
        label_to_move = {
            'A': llm_move if order[0] == 0 else engine_move,
            'B': llm_move if order[1] == 0 else engine_move,
        }

        prompt = build_arbitration_prompt(
            player=player,
            board_str=board_str,
            history=history,
            legal_moves_str=legal_moves_str,
            llm_move_str=llm_move_str,
            # 客观事实 + 模型原文推理：事实与引擎候选同源对称，
            # 推理保留战略判断（长线弃子/阵型缺陷等事实无法覆盖的理由）
            llm_reasoning=(llm_basis + "\n" + self._arbitration_llm_text
                           if self._arbitration_llm_text.strip() else llm_basis),
            engine_move_str=engine_move_str,
            engine_basis=engine_basis,
            in_check=in_check,
            opponent_in_check=opponent_in_check,
            move_count=move_count,
            material_str=material_str,
            candidate_order=order,
        )

        system_prompt = get_arbitration_system_prompt()
        current_version = self.game_version

        self.arbitration_count += 1
        self.log(f"🔨 已启动仲裁 ({arbitrator_model.name}) 第 {self.arbitration_count} 次", 'INFO')

        # 更新 UI 仲裁次数
        if self.main:
            self.main.update_ai_score()

        worker = AIWorker(
            arbitrator_model, prompt, None, f"{player_name}仲裁",
            version=current_version,
            cancel_version=self.ai_manager.cancel_version,
            system_prompt=system_prompt,
            tools=TOOLS_BASIC,
            timeout=ARBITRATION_TIMEOUT_SECONDS,
            game=self.game,
            current_player=player,
            # 仲裁必须二选一：白名单限死候选，标签映射供文本兜底解析
            allowed_moves={llm_move, engine_move},
            label_to_move=label_to_move,
        )
        worker.signals.finished.connect(self.on_arbitration_finished)
        self.ai_manager.set_active_worker(worker)
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        self.ai_manager.set_active_thread(t)

    def on_arbitration_finished(self, from_coord: str, to_coord: str,
                                 full_text: str, error: str,
                                 tokens: int, version: int,
                                 cancel_version: int = 0) -> None:
        """仲裁完成回调：评分 + 执行仲裁结果。"""
        if not self._check_callback_valid(version, cancel_version, '仲裁',
                                          needs_cleanup=True):
            return

        current_player = self.game.current_player

        # ── 仲裁失败 → 采用 LLM 走法兜底（不计分）──
        if error or not from_coord or not to_coord:
            self.log(f"⚠️ DeepSeek 仲裁失败 ({error or '无有效走法'})，采用 LLM 走法（不计分）", 'WARNING')
            self._finish_ai_move()
            if self._arbitration_llm_move:
                self._execute_guarded_move(self._arbitration_llm_move,
                                           current_player, 'LLM(仲裁回退)')
            else:
                self._random_move(current_player)
            return

        # ── 解析仲裁坐标 ──
        try:
            arb_row, arb_col = parse_coord(from_coord)
            arb_to_row, arb_to_col = parse_coord(to_coord)
        except (ValueError, IndexError, TypeError, AttributeError):
            self.log(f"仲裁坐标解析失败 '{from_coord}'→'{to_coord}'，采用 LLM 走法（不计分）", 'WARNING')
            self._finish_ai_move()
            if self._arbitration_llm_move:
                self._execute_guarded_move(self._arbitration_llm_move,
                                           current_player, 'LLM(仲裁回退)')
            else:
                self._random_move(current_player)
            return

        arb_move = (arb_row, arb_col, arb_to_row, arb_to_col)
        llm_move = self._arbitration_llm_move
        engine_move = self._arbitration_engine_move

        # ── 仲裁结果必须是候选走法之一（提示词要求二选一，但不强制时
        #    模型可能给出第三走法；此时按约定采纳引擎并记 WARNING）──
        if arb_move != llm_move and arb_move != engine_move:
            efr0, efc0, etr0, etc0 = engine_move
            ep0 = PIECE_SYMBOLS.get(self.game.board[efr0][efc0], '?')
            self.log(
                f"⚠️ 仲裁结果 {chr(65+arb_col)}{arb_row+1}→{chr(65+arb_to_col)}{arb_to_row+1} "
                f"不是候选走法之一，按约定采纳引擎走法 "
                f"{ep0} {chr(65+efc0)}{efr0+1}→{chr(65+etc0)}{etr0+1}", 'WARNING')
            arb_move = engine_move
            arb_row, arb_col, arb_to_row, arb_to_col = engine_move

        # ── 计分 ──
        if arb_move == llm_move:
            self.ai_score += 1
            score_change = '+1'
            score_reason = 'LLM 与仲裁一致'
        else:
            score_change = '0'
            score_reason = 'LLM 与仲裁不一致'

        self.log(
            f"📊 仲裁计分: {score_change} (总分: {self.ai_score}) — {score_reason}",
            'INFO')

        # 更新 UI 计分
        if self.main:
            self.main.update_ai_score()

        # ── 日志仲裁分析 ──
        if full_text:
            self.log(f"  🔨 仲裁分析:\n{full_text}\n", 'INFO')

        # 格式化双方走法用于日志
        arb_piece = self.game.board[arb_row][arb_col]
        arb_pn = PIECE_SYMBOLS.get(arb_piece, '?')
        arb_rec = f"{arb_pn} {chr(65+arb_col)}{arb_row+1}→{chr(65+arb_to_col)}{arb_to_row+1}"

        lfr, lfc, ltr, ltc = llm_move
        lpiece = self.game.board[lfr][lfc]
        lpn = PIECE_SYMBOLS.get(lpiece, '?')
        llm_rec = f"{lpn} {chr(65+lfc)}{lfr+1}→{chr(65+ltc)}{ltr+1}"

        efr, efc, etr, etc = self._arbitration_engine_move
        epiece = self.game.board[efr][efc]
        epn = PIECE_SYMBOLS.get(epiece, '?')
        eng_rec = f"{epn} {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1}"

        self.log(
            f"⚖️ 仲裁结果: {arb_rec} | LLM: {llm_rec} | 引擎: {eng_rec} | "
            f"{'✅ 采纳LLM' if arb_move == llm_move else '🔄 采纳引擎'}",
            'WARNING')

        # ── 执行仲裁走法 ──
        result = self.game.move_piece(arb_row, arb_col, arb_to_row, arb_to_col)
        if not result['success']:
            reason = result.get('message', '未知原因')
            self.log(f"仲裁走法非法 ({reason})，回退 LLM 走法（不计分）", 'ERROR')
            self._finish_ai_move()
            if self._arbitration_llm_move:
                self._execute_guarded_move(self._arbitration_llm_move,
                                           current_player, 'LLM(仲裁回退)')
            else:
                self._random_move(current_player)
            return

        self._on_move_success(arb_row, arb_col, arb_to_row, arb_to_col,
                              current_player,
                              source=f'仲裁({"LLM" if arb_move == llm_move else "引擎"})')
        self._refresh_ui()
        self._finish_ai_move()

        if result.get('game_over'):
            self.handle_game_over(result)
            return

        # ── 下一步 ──
        self._schedule_next_ai_move()

    def _finish_ai_move(self) -> None:
        self.ai_manager.set_busy(False)
        self.ai_manager.clear_active_worker()

    # ── 游戏结束 ──

    def handle_game_over(self, result: dict) -> None:
        self.stop_thinking_timer()
        self.is_active = False
        self.retry_count = 0
        self._hint_active = False
        # 停止在飞的引擎搜索（将杀后 MCTS/Pikafish 继续烧 CPU 至自然结束，
        # 结果虽被版本门控丢弃，纯属浪费）
        self._engine.stop_all()
        if self.main:
            # 清除棋盘上残留的选中高亮（人类将杀 AI 时选中圈会遗留）
            self.main.board_widget.selected_row = -1
            self.main.board_widget.selected_col = -1
            self.main.board_widget.update()
            self.main.update_game_status()
            self.main.update_player_status()
            self.main.start_btn.setEnabled(True)
            self.main.pause_btn.setEnabled(False)
            self.main.model1_combo.setEnabled(True)
            self.main.model2_combo.setEnabled(True)

    # ── 计时器 ──

    def start_thinking_timer(self, player: int) -> None:
        self.stop_thinking_timer()
        model = self.model1 if player == 1 else self.model2
        self.thinking_start_time = QDateTime.currentDateTime()
        if model == HUMAN_MODEL:
            self.main.think_timer_label.setText("等待人类走子...")
            QTimer.singleShot(300, self._trigger_hint)
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
        # 暂停即结束当前思考计时段（总用时无消费者）
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
        """清理资源 — 关闭引擎、取消任务、停止计时器。"""
        self.is_active = False
        self.stop_thinking_timer()
        self._engine.shutdown()
        self.ai_manager.shutdown()
