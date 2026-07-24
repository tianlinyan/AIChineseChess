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
    MCTS_TIME_LIMIT,
    MCTS_FALLBACK_SIMULATIONS, MCTS_FALLBACK_TIME_LIMIT,
    AI_DEFAULT_MODE, ARBITRATION_TIMEOUT_SECONDS,
    PIECE_SYMBOLS, format_duration, format_coord,
    parse_coord, format_move,
    PROMPT_HISTORY_MAX_ITEMS,
)
from domain.prompts import (
    HUMAN_MODEL, get_system_prompt, get_system_prompt_lite,
    build_move_prompt, format_legal_moves,
    get_arbitration_system_prompt, build_arbitration_prompt,
    TOOLS_BASIC, DEFAULT_TOOLS,
)
from domain.game import ChineseChessGame
from domain.evaluation import compute_material
from domain.mcts import MCTSEngine
from domain.openings import get_opening_move
try:
    from domain.pikafish import PikafishEngine
except ImportError:
    PikafishEngine = None  # pikafish 模块不可用
from ai.manager import AIManager
from ai.worker import AIWorker

if TYPE_CHECKING:
    from app.protocols import MainWindowProtocol


class _PikafishRelay(QObject):
    """跨线程信号中继：daemon 线程 emit → Qt 自动排队到主线程。"""
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
            'search_nodes': 0,
        }
        self.red_total_time: int = 0
        self.black_total_time: int = 0

        self.thinking_start_time: Optional[QDateTime] = None
        self.thinking_timer: Optional[QTimer] = None

        self.last_red_raw: str = ''
        self.last_black_raw: str = ''

        self._random_action_count: int = 0

        # ── 在飞的后台搜索（reset/pause/shutdown 时主动停止，释放 CPU） ──
        self._active_mcts: Optional['MCTSEngine'] = None
        self._mcts_thread: Optional[threading.Thread] = None

        # ── Pikafish 引擎（延迟初始化——需等 main 就绪后才能写日志） ──
        self._pikafish: Optional['PikafishEngine'] = None
        self._pikafish_initialized: bool = False

        # 跨线程信号中继：Pikafish 异步回调 → 主线程
        self._pikafish_relay = _PikafishRelay()
        self._pikafish_relay.search_done.connect(self._on_pikafish_search_done)

        # ── Hybrid 模式：引擎搜索结果暂存（LLM 失败时兜底用）──
        self._hybrid_engine_move: Optional[tuple] = None
        self._last_engine_name: str = '引擎'

        # ── AI 计分（仲裁模式）──
        self.ai_score: int = 0  # LLM 与仲裁一致 +1，不一致 +0（不倒扣）
        self.arbitration_count: int = 0  # 仲裁触发次数

        # ── 仲裁暂存（LLM 与引擎分歧时等待 DeepSeek 裁决）──
        self._arbitration_llm_move: Optional[tuple] = None
        self._arbitration_llm_text: str = ''
        self._arbitration_engine_move: Optional[tuple] = None
        self._arbitration_engine_name: str = ''

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
                self._pikafish = PikafishEngine()
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
        self.stats = {
            'start_time': QDateTime.currentDateTime(),
            'move_count': 0,
            'search_nodes': 0,
        }
        self.red_total_time = 0
        self.black_total_time = 0

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
            QTimer.singleShot(1000, lambda v=self.game_version:
                              self.make_ai_move(expected_version=v))

    def _stop_background_engines(self) -> None:
        """请求停止在飞的后台搜索（Pikafish/MCTS）。

        过期结果本就由版本门控丢弃，这里只为尽快释放 CPU 与搜索锁
        （否则新对局首次 Pikafish 搜索最坏被旧搜索拖约 45s）。

        注：_active_mcts 由后台 MCTS 线程写入、主线程读取，
        CPython GIL 保证引用读写原子性；本地捕获引用 + try/except
        防止 isnull 检查与 .stop() 之间被置 None 的 TOCTOU 窗口。
        """
        if self._pikafish is not None:
            self._pikafish.stop()
        mcts = self._active_mcts
        if mcts is not None:
            try:
                mcts.stop()
            except Exception:
                pass

    def reset_game(self) -> None:
        self.game_version += 1
        self.stop_thinking_timer()

        # 取消运行中的 AI 任务，防止残留 Worker 阻塞新游戏
        self.ai_manager.clear_queue()
        self.ai_manager.set_busy(False)
        self._stop_background_engines()

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

        # 重置 AI 计分
        self.ai_score = 0
        self.arbitration_count = 0
        self._arbitration_llm_move = None
        self._arbitration_llm_text = ''
        self._arbitration_engine_move = None
        self._arbitration_engine_name = ''
        if self.main:
            self.main.update_ai_score()

        self.stats = {
            'start_time': None, 'move_count': 0,
            'search_nodes': 0,
        }

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
            self._stop_background_engines()
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

        # 回合分隔标记（重试/兜底链再入时不重复打印）
        if self.retry_count == 0:
            ply = len(self.game.moves) + 1
            player_name = '红方' if current_player == 1 else '黑方'
            self.log(f"━━ 第 {ply} 手 · {player_name}（{model.name}）━━", 'INFO')

        # ── 1. 开局库查询 ──
        if self.use_opening_book and len(self.game.moves) < OPENING_BOOK_MAX_MOVES:
            # get_opening_move 不在库中时返回 None，无需 is_in_opening_book 预检
            # （旧写法预检本身做一次加权随机再丢弃，纯属浪费）
            book_move = get_opening_move(self.game.moves)
            if book_move:
                fr, fc, tr, tc = book_move
                result = self.game.move_piece(fr, fc, tr, tc)
                if result['success']:
                    self._on_move_success(fr, fc, tr, tc, current_player,
                                          source='开局库')
                    self._refresh_ui()
                    if result.get('game_over'):
                        self.handle_game_over(result)
                        return
                    self._schedule_next_ai_move(delay=OPENING_DELAY_MS)
                    return

        # ── 2. 纯搜索模式（Pikafish/MCTS 异步，不阻塞UI）──
        if self.ai_mode == 'search_only':
            self.ai_manager.set_busy(True)

            def _on_search(move, p):
                # 检查暂停/关闭状态，防止异步回调在暂停后执行走子
                if self.is_paused or not self.is_active or self.game.game_over:
                    self._finish_ai_move()
                    return
                # Pikafish 失败 → MCTS 快速回退（同样后台线程）
                if not move:
                    self._start_mcts_async(
                        p, None, MCTS_FALLBACK_SIMULATIONS,
                        MCTS_FALLBACK_TIME_LIMIT,
                        lambda m2, p2: self._execute_engine_move_or_random(
                            m2, p2, '搜索'))
                    return
                # search_only 模式下 Pikafish 结果即最终走法，在此记日志
                # （hybrid 模式由 _on_hybrid_engine_done 另行记录）
                fr, fc, tr, tc = move
                pn = PIECE_SYMBOLS.get(self.game.board[fr][fc], '?')
                self.log(f"🔍 Pikafish 推荐: {pn} "
                         f"{chr(65+fc)}{fr+1}→{chr(65+tc)}{tr+1}", 'INFO')
                self._execute_engine_move_or_random(move, p, '搜索')

            self._mcts_search(current_player, on_done=_on_search)
            return

        # ── 3. LLM 模式（llm_only / hybrid） ──
        if self.ai_mode == 'hybrid':
            # 引擎先行：Pikafish 异步搜索（不阻塞UI）→ MCTS 同步兜底 → LLM
            self.ai_manager.set_busy(True)

            def _on_engine_done(move, p):
                if move and self._pikafish and self._pikafish.available:
                    engine_name = 'Pikafish'
                    engine_desc = f"经深度搜索（时限 {self._pikafish_time_s()}s）"
                else:
                    engine_name = 'MCTS'
                    engine_desc = f"经蒙特卡洛搜索（{self._depth_to_sims()} 次模拟）"
                if not move:
                    # Pikafish 失败 → MCTS 快速回退（后台线程）
                    def _on_fallback_done(m2, p2):
                        if not self.is_active or self.is_paused or self.game.game_over:
                            self._finish_ai_move()
                            return
                        self._on_hybrid_engine_done(
                            m2, p2, 'MCTS',
                            f"经快速搜索（{MCTS_FALLBACK_SIMULATIONS} 次模拟）")
                    self._start_mcts_async(
                        p, None, MCTS_FALLBACK_SIMULATIONS,
                        MCTS_FALLBACK_TIME_LIMIT, _on_fallback_done)
                    return
                if not self.is_active or self.is_paused or self.game.game_over:
                    self._finish_ai_move()
                    return
                self._on_hybrid_engine_done(move, p, engine_name, engine_desc)

            self._mcts_search(current_player, on_done=_on_engine_done)
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
        in_check = self.game._is_in_check(player)
        opponent = 2 if player == 1 else 1
        opponent_in_check = self.game._is_in_check(opponent)
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
                'message': f"{'红方' if opponent == 1 else '黑方'}获胜（{'将杀' if self.game._is_in_check(player) else '困毙'}）"
            })
            self._finish_ai_move()
            return

        # 格式化合法走法列表（带 ×吃子 / +将军 战术标注）
        legal_moves_str = format_legal_moves(legal_moves, self.game.board, player)

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
        material_str = f"子力对比：你 {mine:g} : {theirs:g} 对手 —— {mat_trend}"

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
            engine_hint=engine_hint,
            material_str=material_str,
        )

        # 确认日志：引擎参考是否已注入提示词
        if engine_hint:
            self.log("📤 引擎推荐已注入 LLM 提示词", 'INFO')

        player_name = '红方' if player == 1 else '黑方'
        current_version = self.game_version

        # think参数
        if self.main and self.main.disable_think_check.isChecked():
            think_enabled = None
        else:
            think_enabled = (self.main.think_check.isChecked()
                             if self.main else True)

        # 系统提示词 — 强模型用精简版，本地模型用完整版
        # llm_only 模式下不描述不可用的分析工具
        include_tools = self.ai_mode != 'llm_only'
        if model.system_prompt:
            system_prompt = model.system_prompt
        elif model.type and model.type.startswith('deepseek'):
            system_prompt = get_system_prompt_lite(include_analysis_tools=include_tools)
        else:
            system_prompt = get_system_prompt(include_analysis_tools=include_tools)

        # 工具选择：仅 LLM 模式只用 move_piece；其他模式用全部工具
        worker_tools = TOOLS_BASIC if self.ai_mode == 'llm_only' else DEFAULT_TOOLS

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
                                player: int, engine_name: str = '引擎',
                                engine_desc: str = '') -> None:
        """Hybrid 模式：引擎搜索完成 → 日志 + 格式化提示 → 启动 LLM。"""
        if not self.is_active or self.is_paused or self.game.game_over:
            self._finish_ai_move()
            return

        player_name = '红方' if player == 1 else '黑方'

        # 保存引擎名称与描述（仲裁时需要）
        self._last_engine_name = engine_name
        self._last_engine_desc = engine_desc

        # 格式化引擎参考走法 + 日志输出
        engine_hint = ''
        if engine_move:
            efr, efc, etr, etc = engine_move
            # 验证走法在合法列表中（防止引擎因坐标系问题返回非法走法）
            legal = self.game.get_all_legal_moves(player)
            if (efr, efc, etr, etc) not in legal:
                self.log(f"引擎走法 {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1} 不在合法列表中，丢弃", 'WARNING')
                engine_move = None
            elif self.game.board[efr][efc] != '.':
                epn = PIECE_SYMBOLS.get(self.game.board[efr][efc], '?')
                emove_str = f"{epn} {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1}"
                self.log(f"🔍 {engine_name} 推荐: {emove_str}", 'INFO')
                # 信任分级：Pikafish 战术强度远超 LLM，默认采信；
                # MCTS 兜底强度有限（与 search_best_move 相当或更弱），仅作参考。
                # 旧文案固定声称"search_best_move 强度低于引擎"，
                # 在 MCTS 兜底时为假，会错误校准 LLM 的信任。
                if engine_name == 'Pikafish':
                    trust_note = (
                        f"{engine_name} 是顶级战术引擎，战术计算远超语言模型。"
                        f"**默认采信其推荐**；仅当你有具体的战略理由时才坚持己见，"
                        f"并解释为何优于推荐。"
                        f"（search_best_move 是本地浅层搜索，强度远低于 {engine_name}，"
                        f"只用于验证具体战术点。）"
                    )
                else:
                    trust_note = (
                        f"{engine_name} 是本地引擎，强度有限"
                        f"（与 search_best_move 相当或更弱），其推荐仅供参考。"
                        f"请用 evaluate_position / search_best_move 独立分析后自主决策。"
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
        if self.ai_manager._shutting_down:
            return
        # 过期回调只记日志，绝不能 _finish_ai_move() ——
        # 会清掉新对局的 busy/active_worker（与 relay 层同一并发纪律，
        # 见 _on_pikafish_search_done）
        if version != self.game_version:
            self.log(f"[诊断] AI 回调版本不匹配({version}!={self.game_version})，丢弃", 'INFO')
            return
        if cancel_version != self.ai_manager.cancel_version:
            self.log(f"[诊断] AI 回调取消版本不匹配({cancel_version}!={self.ai_manager.cancel_version})，丢弃", 'INFO')
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

        # ── LLM 失败 → 直接采用引擎结果，不仲裁 ──
        if error or not from_coord or not to_coord:
            if self.ai_mode == 'hybrid':
                self.log(f"{player_name} LLM 调用失败 ({error or '无有效走法'})，直接采用引擎走法", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._random_move(current_player)
                return
            else:
                # llm_only 模式：原有重试逻辑
                self._retry_move(error)
                return

        # ── 解析坐标 ──
        try:
            from_row, from_col = parse_coord(from_coord)
            to_row, to_col = parse_coord(to_coord)
        except (ValueError, IndexError):
            self.last_move_error = (
                f"坐标 '{from_coord}'→'{to_coord}' 无法解析。"
                f"请确保坐标格式正确：列字母 A~I，行数字 1~10。"
            )
            if self.ai_mode == 'hybrid':
                self.log(f"{player_name} 坐标解析失败，直接采用引擎走法", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._random_move(current_player)
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

        # ── Hybrid 模式合法性预检：非法走法直接用引擎兜底，
        #    不进入仲裁（否则仲裁一个非法候选，最终可能随机落子，
        #    而验证过的引擎走法明明可用）──
        if self.ai_mode == 'hybrid':
            legal_moves = self.game.get_all_legal_moves(current_player)
            if final_move not in legal_moves:
                self.last_move_error = f"{from_coord}→{to_coord}（原因：走法不合法）"
                self.log(f"{player_name} 走法 {from_coord}→{to_coord} 非法，直接采用引擎走法", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move and self._hybrid_engine_move != final_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._random_move(current_player)
                return

        # ── Hybrid 模式分歧检测：LLM ≠ 引擎 → 启动第三方仲裁 ──
        if (self.ai_mode == 'hybrid' and self._hybrid_engine_move
                and final_move != self._hybrid_engine_move):
            # 暂存双方走法，启动 DeepSeek 仲裁
            self._arbitration_llm_move = final_move
            self._arbitration_llm_text = full_text
            self._arbitration_engine_move = self._hybrid_engine_move
            # 保存引擎名称（从 _hybrid_engine_move 的来源推断）
            efr, efc, etr, etc = self._hybrid_engine_move
            self._arbitration_engine_name = getattr(
                self, '_last_engine_name', '引擎')

            # 格式化日志
            epiece = self.game.board[efr][efc] if self.game.board[efr][efc] != '.' else '?'
            epn = PIECE_SYMBOLS.get(epiece, epiece)
            erec = f"{epn} {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1}"
            lpiece = self.game.board[from_row][from_col] if self.game.board[from_row][from_col] != '.' else '?'
            lpn = PIECE_SYMBOLS.get(lpiece, lpiece)
            lrec = f"{lpn} {chr(65+from_col)}{from_row+1}→{chr(65+to_col)}{to_row+1}"
            self.log(
                f"⚖️ 分歧检测 | LLM选择: {lrec} | 引擎推荐: {erec} | 启动 DeepSeek 仲裁...",
                'WARNING')

            # 启动仲裁（异步），仲裁完成后由 on_arbitration_finished 接管
            self._start_arbitration(current_player)
            return  # 暂不执行走子，等待仲裁结果

        # ── Hybrid 模式且 LLM 采纳了引擎走法 → 记一致性日志（最常见路径不再静默）──
        if (self.ai_mode == 'hybrid' and self._hybrid_engine_move
                and final_move == self._hybrid_engine_move):
            self.log("🤝 LLM 与引擎意见一致", 'INFO')

        # ── 执行最终走法 ──
        result = self.game.move_piece(from_row, from_col, to_row, to_col)
        if not result['success']:
            reason = result.get('message', '未知原因')
            self.last_move_error = f"{from_coord}→{to_coord}（原因：{reason}）"

            if self.ai_mode == 'hybrid':
                self.log(f"{player_name} 走子非法 ({reason})，直接采用引擎走法", 'WARNING')
                self._finish_ai_move()
                if self._hybrid_engine_move and self._hybrid_engine_move != final_move:
                    self._execute_fallback_move(self._hybrid_engine_move,
                                                current_player)
                else:
                    self._random_move(current_player)
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
        self._on_move_success(from_row, from_col, to_row, to_col,
                              current_player, source='LLM')
        self._refresh_ui()
        self._finish_ai_move()

        if result.get('game_over'):
            self.handle_game_over(result)
            return

        # ── 下一步 ──
        self._schedule_next_ai_move()

    # ── 搜索引擎集成 ──

    # 搜索深度 → MCTS 模拟次数映射
    _DEPTH_SIMS_MAP = {1: 500, 2: 800, 3: 1200, 4: 1600, 5: 2000, 6: 3000}

    def _depth_to_sims(self) -> int:
        """将 UI 搜索深度(1~6)映射为 MCTS 模拟次数。"""
        return self._DEPTH_SIMS_MAP.get(self.search_depth, 2000)

    def _pikafish_time_s(self) -> int:
        """Pikafish 每步搜索秒数：深度×3s，封顶 MCTS_TIME_LIMIT(15s)。"""
        return min(self.search_depth * 3, int(MCTS_TIME_LIMIT))

    def _mcts_search(self, player: int,
                     priors: dict = None,
                     on_done = None) -> Optional[tuple]:
        """搜索最佳走法（全异步）。优先 Pikafish，回退后台线程 MCTS。

        结果经 _pikafish_relay 信号排队回主线程后调用
        on_done(best_move_or_None, player)。
        """
        self.log("🔍 启动引擎搜索...", 'INFO')

        # ── Pikafish 异步（daemon 线程搜索，信号回主线程）──
        if on_done is not None and self._pikafish and self._pikafish.available:
            time_ms = self._pikafish_time_s() * 1000  # 深度×3秒，封顶15s
            self.log(f"  🐟 Pikafish 搜索中（时限 {time_ms // 1000}s）...", 'INFO')
            captured_version = self.game_version
            # 同时捕获 cancel_version：暂停/恢复只自增 cancel_version，
            # 缺少这层门控时旧搜索回调会与新搜索叠加 → 同一回合走两步
            captured_cancel = self.ai_manager.cancel_version

            def _logged_pf_done(move, p, _od=on_done, _ms=time_ms):
                # 主线程：不在此记日志——各 on_done 回调各自负责
                # （hybrid 由 _on_hybrid_engine_done 记，search_only
                #  由 _on_search 记，避免同一条走法打两行）
                _od(move, p)

            self._pikafish.search_async(
                self.game, player, time_ms=time_ms,
                callback=lambda m, err: self._pikafish_relay.search_done.emit(
                    (m, player, _logged_pf_done,
                     captured_version, captured_cancel, err)))
            return None

        # ── MCTS 后台线程异步（旧实现主线程同步跑最长 15s，UI 冻结）──
        if on_done is not None:
            self._start_mcts_async(player, priors, self._depth_to_sims(),
                                   MCTS_TIME_LIMIT, on_done)
            return None

        return None

    def _start_mcts_async(self, player: int, priors: Optional[dict],
                          sims: int, time_limit: float, on_done) -> None:
        """后台线程跑 MCTS，结果经 relay 信号回主线程执行 on_done。

        先快照棋盘（搜索在快照上进行，不碰 live board），
        版本门控由 _on_pikafish_search_done 统一处理。
        """
        board_snapshot = self.game.get_board_copy()
        king_pos = dict(self.game._king_pos)
        captured_version = self.game_version
        captured_cancel = self.ai_manager.cancel_version
        desc = "LLM引导" if priors else "均匀先验"
        self.log(f"  🌳 MCTS 启动 ({desc}, {sims}次模拟, 深度={self.search_depth})", 'INFO')
        result = {}

        def _run():
            move = None
            try:
                g = ChineseChessGame()
                g.board = board_snapshot
                g.current_player = player
                g._king_pos = king_pos
                g.recompute_hash()
                engine = MCTSEngine(max_simulations=sims, time_limit=time_limit)
                self._active_mcts = engine
                move = engine.search(g, player, priors=priors)
                result['sims'] = engine.simulations
                result['top'] = engine.get_top_moves(3)
            except Exception as e:
                # 异常详情交由 _logged_on_done 展示（relay 层的失败日志
                # 只挂 Pikafish 来源，避免误标）
                result['error'] = f'MCTS 搜索异常: {e}'
            finally:
                self._active_mcts = None
                # 无论是否异常都必须 emit —— 漏发会让 busy 永久卡死、
                # 对局僵死（只能手动重置）
                self._pikafish_relay.search_done.emit(
                    (move, player, _logged_on_done,
                     captured_version, captured_cancel, ''))

        def _logged_on_done(move, p):
            # 主线程：先补 MCTS 结果日志，再转交原始回调
            self.stats['search_nodes'] = result.get('sims', 0)
            if move:
                fr, fc, tr, tc = move
                pn = PIECE_SYMBOLS.get(self.game.board[fr][fc], '?')
                self.log(f"  ✅ MCTS选择: {pn} {chr(65+fc)}{fr+1}→{chr(65+tc)}{tr+1} "
                         f"({result.get('sims', 0)}次模拟)", 'INFO')
                for i, (m, visits, val) in enumerate(result.get('top', [])):
                    mfr, mfc, mtr, mtc = m
                    mpn = PIECE_SYMBOLS.get(self.game.board[mfr][mfc], '?')
                    self.log(f"    {i+1}. {mpn} {chr(65+mfc)}{mfr+1}→{chr(65+mtc)}{mtr+1} "
                             f"[访问{visits}次, 价值{val:.3f}]", 'INFO')
            else:
                detail = f"（{result['error']}）" if result.get('error') else ''
                self.log(f"  ⚠️ MCTS未找到走法{detail}", 'WARNING')
            on_done(move, p)

        self._mcts_thread = threading.Thread(target=_run, daemon=True)
        self._mcts_thread.start()

    def _execute_engine_move_or_random(self, move: Optional[tuple],
                                       player: int, source: str,
                                       reset_random_count: bool = False) -> None:
        """主线程：执行引擎走法；无走法/走子失败时回退随机走子。"""
        if self.is_paused or not self.is_active or self.game.game_over:
            self._finish_ai_move()
            return
        if move:
            fr, fc, tr, tc = move
            result = self.game.move_piece(fr, fc, tr, tc)
            if result['success']:
                self._on_move_success(fr, fc, tr, tc, player, source=source)
                if reset_random_count:
                    self._random_action_count = 0
                self._refresh_ui()
                if result.get('game_over'):
                    self._finish_ai_move()
                    self.handle_game_over(result)
                    return
                self._schedule_next_ai_move()
                self._finish_ai_move()
                return
            self.log(f"{source}走子失败: {result.get('message', '未知')}", 'ERROR')
        self._finish_ai_move()
        self._random_move(player)

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
            # 检查暂停/关闭状态，防止异步回调在暂停后执行走子
            if self.is_paused or not self.is_active or self.game.game_over:
                self._finish_ai_move()
                return
            # Pikafish 失败 → MCTS 快速回退（后台线程）
            if not move:
                self._start_mcts_async(
                    p, None, MCTS_FALLBACK_SIMULATIONS,
                    MCTS_FALLBACK_TIME_LIMIT,
                    lambda m2, p2: self._execute_engine_move_or_random(
                        m2, p2, '搜索回退', reset_random_count=True))
                return
            self._execute_engine_move_or_random(
                move, p, '搜索回退', reset_random_count=True)

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
        
                    self.main.update_player_status()

                if result['game_over']:
                    self.handle_game_over(result)
                    return

                if self.main:
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

        # 确实无合法走法 — 困毙/将杀判负（中国象棋规则：无棋可走者负）
        self.game.game_over = True
        opponent = 3 - current_player
        self.game.winner = opponent
        kind = '将杀' if self.game._is_in_check(current_player) else '困毙'
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

        # 记录走子
        from_coord = f"{chr(65+from_col)}{from_row+1}"
        to_coord = f"{chr(65+to_col)}{to_row+1}"
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

    def _schedule_next_ai_move(self, delay: int = AI_DELAY_MS) -> None:
        """若游戏仍在进行且下一方为 AI，则调度其走子。"""
        if not self.is_active or self.is_paused or self.game.game_over:
            return
        if self.main:
            self.main.start_thinking_timer(self.game.current_player)
        next_player = self.game.current_player
        next_model = self.model1 if next_player == 1 else self.model2
        if next_model != HUMAN_MODEL:
            QTimer.singleShot(delay, lambda v=self.game_version:
                self.make_ai_move(expected_version=v))

    def _on_pikafish_search_done(self, args: tuple) -> None:
        """主线程：处理 Pikafish/MCTS 异步搜索结果。"""
        try:
            move, player, on_done, captured_version, captured_cancel, error = args
        except Exception as e:
            self.log(f"[PF错误] 搜索回调参数解包失败: {e}", 'ERROR')
            return
        try:
            if captured_version != self.game_version:
                # 过期回调：新对局可能已有自己的 worker/busy 状态，
                # 绝不能 _finish_ai_move()（会清掉新对局状态 → 并发双走）
                self.log(f"[PF诊断] 搜索回调版本不匹配({captured_version}!={self.game_version})，丢弃", 'INFO')
                return
            if captured_cancel != self.ai_manager.cancel_version:
                self.log(f"[PF诊断] 搜索回调取消版本不匹配({captured_cancel}!={self.ai_manager.cancel_version})，丢弃", 'INFO')
                return
            # shutdown 中文本控件可能已销毁，静默返回（其余状态异常记诊断日志）
            if self.ai_manager._shutting_down:
                return
            if self.is_paused or not self.is_active or self.game.game_over:
                self.log(f"[PF诊断] 搜索回调状态异常(paused={self.is_paused} active={self.is_active} over={self.game.game_over})，丢弃", 'INFO')
                return
            # Pikafish 失败：把原因写进思考日志（随后调用方会启动 MCTS 回退）
            if move is None and error:
                self.log(f"  ⚠️ Pikafish 搜索失败: {error}，回退到 MCTS", 'WARNING')
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
            self._refresh_ui()
            if result.get('game_over'):
                self._finish_ai_move()
                self.handle_game_over(result)
                return
            self._schedule_next_ai_move()
            self._finish_ai_move()
        else:
            self.log(f"引擎兜底走子失败: {result.get('message', '未知')}", 'ERROR')
            self._finish_ai_move()
            self._random_move(player)

    def _execute_llm_fallback(self, player: int) -> None:
        """仲裁失败兜底：直接执行 LLM 暂存走法。

        仲裁出错（连接问题、中断、第三方AI不在线等）时调用。
        LLM 走法已经过合法性验证（在 on_ai_finished 中解析通过），直接执行。
        """
        if not self._arbitration_llm_move:
            self._random_move(player)
            return
        fr, fc, tr, tc = self._arbitration_llm_move
        result = self.game.move_piece(fr, fc, tr, tc)
        if result['success']:
            self._on_move_success(fr, fc, tr, tc, player, source='LLM(仲裁失败回退)')
            self._random_action_count = 0
            self._refresh_ui()
            if result.get('game_over'):
                self._finish_ai_move()
                self.handle_game_over(result)
                return
            self._schedule_next_ai_move()
            self._finish_ai_move()
        else:
            self.log(f"LLM回退走子失败: {result.get('message', '未知')}", 'ERROR')
            self._finish_ai_move()
            self._random_move(player)

    # ── 第三方仲裁（DeepSeek） ──

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
            self._execute_llm_fallback(player)
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

        legal_moves_str = format_legal_moves(legal_moves, self.game.board, player)

        # LLM 走法描述
        lfr, lfc, ltr, ltc = self._arbitration_llm_move
        lpiece = self.game.board[lfr][lfc]
        lpn = PIECE_SYMBOLS.get(lpiece, '?')
        llm_move_str = f"{lpn} {chr(65+lfc)}{lfr+1}→{chr(65+ltc)}{ltr+1}"

        # 引擎走法描述
        efr, efc, etr, etc = self._arbitration_engine_move
        epiece = self.game.board[efr][efc]
        epn = PIECE_SYMBOLS.get(epiece, '?')
        engine_move_str = f"{epn} {chr(65+efc)}{efr+1}→{chr(65+etc)}{etr+1}"

        # 引擎候选依据（与 LLM 推理对等的信息密度，消除"修辞多的候选占优"偏差；
        # 不提引擎名字，避免仲裁方按来源锚定）
        basis_parts = [
            f"深度搜索引擎{getattr(self, '_last_engine_desc', '') or '经深度搜索'}后推荐此走法。"
        ]
        captured_piece = self.game.board[etr][etc]
        if captured_piece != '.':
            basis_parts.append(f"直接吃子：得{PIECE_SYMBOLS.get(captured_piece, captured_piece)}。")
        # 走后是否将军（在棋盘副本上模拟）
        _tmp = ChineseChessGame()
        _tmp.board = self.game.get_board_copy()
        _tmp.current_player = player
        _tmp._king_pos = dict(self.game._king_pos)
        _tmp.recompute_hash()
        if _tmp.move_piece(efr, efc, etr, etc).get('success'):
            if _tmp._is_in_check(3 - player):
                basis_parts.append("走后将军对方。")
        engine_basis = ''.join(basis_parts)

        # 将军状态
        in_check = self.game._is_in_check(player)
        opponent_in_check = self.game._is_in_check(opponent)

        prompt = build_arbitration_prompt(
            player=player,
            board_str=board_str,
            history=history,
            legal_moves_str=legal_moves_str,
            llm_move_str=llm_move_str,
            llm_reasoning=self._arbitration_llm_text,
            engine_move_str=engine_move_str,
            engine_name=self._arbitration_engine_name,
            engine_basis=engine_basis,
            in_check=in_check,
            opponent_in_check=opponent_in_check,
            move_count=move_count,
        )

        system_prompt = get_arbitration_system_prompt()
        current_version = self.game_version

        # think 参数与正常 LLM 请求一致：尊重 UI 的禁用/启用勾选
        # （旧代码硬编码 think=True，DeepSeek reasoner 又慢又贵）
        if self.main and self.main.disable_think_check.isChecked():
            arb_think = None
        else:
            arb_think = (self.main.think_check.isChecked()
                         if self.main else True)

        self.arbitration_count += 1
        self.log(f"🔨 已启动仲裁 ({arbitrator_model.name}) 第 {self.arbitration_count} 次", 'INFO')

        # 更新 UI 仲裁次数
        if self.main:
            self.main.update_ai_score()

        worker = AIWorker(
            arbitrator_model, prompt, None, f"{player_name}仲裁",
            version=current_version,
            cancel_version=self.ai_manager.cancel_version,
            think=arb_think,
            system_prompt=system_prompt,
            tools=TOOLS_BASIC,
            timeout=ARBITRATION_TIMEOUT_SECONDS,
            game=self.game,
            current_player=player,
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
        if self.ai_manager._shutting_down:
            return
        # 过期回调只记日志，绝不能 _finish_ai_move() ——
        # 会清掉新对局的 busy/active_worker（与 relay 层同一并发纪律，
        # 见 _on_pikafish_search_done）
        if version != self.game_version:
            self.log(f"[诊断] 仲裁回调版本不匹配({version}!={self.game_version})，丢弃", 'INFO')
            return
        if cancel_version != self.ai_manager.cancel_version:
            self.log(f"[诊断] 仲裁回调取消版本不匹配({cancel_version}!={self.ai_manager.cancel_version})，丢弃", 'INFO')
            return
        if self.is_paused or not self.is_active or self.game.game_over:
            self._finish_ai_move()
            return

        current_player = self.game.current_player

        # ── 仲裁失败 → 采用 LLM 走法兜底（不计分）──
        if error or not from_coord or not to_coord:
            self.log(f"⚠️ DeepSeek 仲裁失败 ({error or '无有效走法'})，采用 LLM 走法（不计分）", 'WARNING')
            self._finish_ai_move()
            self._execute_llm_fallback(current_player)
            return

        # ── 解析仲裁坐标 ──
        try:
            arb_row, arb_col = parse_coord(from_coord)
            arb_to_row, arb_to_col = parse_coord(to_coord)
        except (ValueError, IndexError):
            self.log(f"仲裁坐标解析失败 '{from_coord}'→'{to_coord}'，采用 LLM 走法（不计分）", 'WARNING')
            self._finish_ai_move()
            self._execute_llm_fallback(current_player)
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
            self._execute_llm_fallback(current_player)
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
        if self.main:
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
        """清理资源 — 关闭引擎、取消任务、停止计时器。"""
        self.is_active = False
        self.stop_thinking_timer()
        # 主动停止在飞的后台搜索并短暂等待 MCTS 线程退出：
        # 关窗后 daemon 线程若仍经 relay 向已销毁的控件 emit，
        # 有进程级崩溃风险；断开 relay 后迟到 emit 变为安全无操作
        self._stop_background_engines()
        mcts_thread = self._mcts_thread
        if mcts_thread and mcts_thread.is_alive():
            mcts_thread.join(timeout=2.0)
        try:
            self._pikafish_relay.search_done.disconnect(
                self._on_pikafish_search_done)
        except TypeError:
            pass  # 未连接过时 disconnect 抛 TypeError
        self.ai_manager.shutdown()
        if self._pikafish:
            try:
                self._pikafish.close()
            except Exception:
                pass
            self._pikafish = None
