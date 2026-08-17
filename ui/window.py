"""主窗口"""

from PyQt6.QtCore import Qt, QSettings, QByteArray, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTextEdit, QPushButton,
    QLabel, QStackedWidget, QFrame,
)

from domain.game import ChineseChessGame
from domain.evaluation import compute_material
from domain.prompts import HUMAN_MODEL
from services.logging import LogManager
from services.models import ModelManager
from ai.manager import AIManager
from app.controller import GameController
from ui.board import BoardWidget
from ui.theme import (
    WINDOW_WIDTH, WINDOW_HEIGHT, MIDDLE_PANEL_MIN_WIDTH,
    SPLITTER_SIZES, DARK_THEME_QSS, PANEL_BG_STYLE,
)
from ui.panel import setup_left_expanded


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("中国象棋 AI 对弈")
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowMaximizeButtonHint)
        # QSettings 在 setup_ui 之前创建：toggle_left_panel 依赖它，
        # 避免"用户不可能在 __init__ 完成前点击"的脆弱时序契约
        self.settings = QSettings('ChineseChessAI', 'ChineseChess')

        self.game = ChineseChessGame()
        self.model_manager = ModelManager()
        self.log_manager = LogManager()
        self.ai_manager = AIManager()

        self.game_controller = GameController(self.game, self.ai_manager)
        self.game_controller.main = self

        self.setStyleSheet(DARK_THEME_QSS)

        self.setup_ui()
        self.load_models()
        self.game_controller.reset_game()

        # 推迟到事件循环执行，避免引擎子进程启动/UCI 握手卡住窗口显示；
        # 排在 setup_ui() 之后，事件循环启动时日志面板 widget 早已创建
        QTimer.singleShot(0, self.game_controller._engine.init_pikafish)

    # ── UI 构建 ──

    def _make_status_label(self, text: str, color: str, top_pad: int,
                           bottom_margin: int) -> QLabel:
        """构造上下状态标签（仅颜色与边距不同）。"""
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFrameShape(QFrame.Shape.Box)
        label.setStyleSheet(
            f"color: {color}; padding: 12px 5px; font-size: 16px; font-weight: bold;")
        label.setContentsMargins(0, top_pad, 0, bottom_margin)
        return label

    def setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)

        # ── 左侧面板（可折叠） ──
        self.left_panel = QWidget()
        self.left_panel.setMinimumWidth(40)
        self.left_stack = QStackedWidget()
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(5, 0, 0, 0)
        left_layout.addWidget(self.left_stack)

        self.left_expanded = QWidget()
        setup_left_expanded(self)
        self.left_collapsed = QWidget()
        collapse_layout = QVBoxLayout(self.left_collapsed)
        self.collapse_btn = QPushButton("▶")
        self.collapse_btn.setFixedSize(40, 40)
        self.collapse_btn.clicked.connect(self.toggle_left_panel)
        collapse_layout.addWidget(self.collapse_btn)
        collapse_layout.addStretch()

        self.left_stack.addWidget(self.left_expanded)
        self.left_stack.addWidget(self.left_collapsed)
        self.left_stack.setCurrentIndex(0)
        self.left_collapsed_flag = False

        # ── 中间面板（棋盘） ──
        self.middle_panel = QWidget()
        self.middle_panel.setStyleSheet(PANEL_BG_STYLE)
        self.middle_panel.setMinimumWidth(MIDDLE_PANEL_MIN_WIDTH)
        middle_layout = QVBoxLayout(self.middle_panel)
        middle_layout.setSpacing(0)

        # 颜色与思考日志黑方一致（#61afef）
        self.black_status = self._make_status_label(
            "⚫ 黑方 等待...", "#61afef", 50, 0)
        middle_layout.addWidget(self.black_status)

        self.board_widget = BoardWidget()
        self.board_widget.set_game(self.game)
        self.board_widget.move_made.connect(self.game_controller.on_human_move)
        middle_layout.addWidget(self.board_widget, 1, Qt.AlignmentFlag.AlignCenter)

        self.red_status = self._make_status_label(
            "🔴 红方 等待...", "#b74c3c", 0, 27)
        middle_layout.addWidget(self.red_status)

        middle_layout.addSpacing(10)

        # 历史
        self.history_mini = QWidget()
        self.history_mini_layout = QHBoxLayout(self.history_mini)
        self.history_mini_layout.setContentsMargins(5, 0, 5, 10)
        self.history_label = QLabel("📋 落子历史:")
        self.history_mini_layout.addWidget(self.history_label)
        # 走子记录用单个 QLabel 拼接纯文本，避免每次刷新都 deleteLater/新建控件
        self.history_moves_label = QLabel('')
        self.history_mini_layout.addWidget(self.history_moves_label)
        self.history_mini_layout.addStretch()
        middle_layout.addWidget(self.history_mini)

        # ── 右侧面板（日志） ──
        self.right_panel = QWidget()
        self.right_panel.setStyleSheet(PANEL_BG_STYLE)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(5, 0, 5, 15)

        log_label = QLabel("📋 思考日志 ")
        log_label.setFont(QFont('Arial', 12))
        log_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #4a6fa5; "
            "text-decoration: underline; padding: 8px 12px;")
        log_label.setContentsMargins(0, 15, 0, 0)
        right_layout.addWidget(log_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self.think_log = QTextEdit()
        self.think_log.setReadOnly(True)
        self.think_log.setFont(QFont('Monospace', 10))
        right_layout.addWidget(self.think_log)

        self.log_manager.set_widget(self.think_log)

        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.middle_panel)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setSizes(SPLITTER_SIZES)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)

    # ── 面板折叠 ──

    def toggle_left_panel(self) -> None:
        if self.left_collapsed_flag:
            self.left_stack.setCurrentIndex(0)
            self.left_collapsed_flag = False
            state = self.settings.value('splitter_expanded_state')
            if (state and isinstance(state, QByteArray)
                    and self.splitter.restoreState(state)):
                return
            # 状态缺失/损坏（如面板数变化）→ 按当前尺寸恢复左侧
            self.splitter.setSizes(
                [SPLITTER_SIZES[0], self.splitter.sizes()[1],
                 self.splitter.sizes()[2]])
        else:
            state = self.splitter.saveState()
            self.settings.setValue('splitter_expanded_state', state)
            self.left_stack.setCurrentIndex(1)
            self.left_collapsed_flag = True
            sizes = self.splitter.sizes()
            self.splitter.setSizes([40, sizes[1], sizes[2]])

    # ── 模型加载 ──

    def _populate_combo(self, combo, models) -> None:
        """填充模型下拉框：人类选项 + 模型列表。"""
        combo.clear()
        combo.addItem("人类", 'human')
        for m in models:
            combo.addItem(m.name, m.id)

    def load_models(self) -> None:
        def on_error(msg):
            self.log_manager.log(msg, 'WARNING')

        self.model_manager.load(on_error=on_error)

        self._populate_combo(self.model1_combo, self.model_manager.player1_models)
        self._populate_combo(self.model2_combo, self.model_manager.player2_models)

        if self.model1_combo.count() > 1:
            self.model1_combo.setCurrentIndex(1)
        if self.model2_combo.count() > 1:
            self.model2_combo.setCurrentIndex(1)

    def _on_model_changed(self, idx: int) -> None:
        self.update_start_button()

    def update_start_button(self) -> None:
        enabled = (self.model1_combo.currentData() is not None and
                   self.model2_combo.currentData() is not None)
        self.start_btn.setEnabled(enabled)

    # ── AI 引擎控制（红/黑共用槽，side 区分） ──

    def _on_ai_mode_changed(self, _idx: int, side: str) -> None:
        mode = getattr(self, f'{side}_ai_mode_combo').currentData()
        if mode:
            setattr(self.game_controller, f'{side}_ai_mode', mode)
            search_enabled = mode != 'llm_only'
            getattr(self, f'{side}_search_depth_spin').setEnabled(search_enabled)

    def _on_search_depth_changed(self, value: int, side: str) -> None:
        setattr(self.game_controller, f'{side}_search_depth', value)

    def _on_opening_book_changed(self, state: int, side: str) -> None:
        setattr(self.game_controller, f'{side}_use_opening_book',
                state == Qt.CheckState.Checked.value)

    # ── 按钮事件 ──

    def _on_pause_clicked(self) -> None:
        self.game_controller.toggle_pause()

    def _on_reset_clicked(self) -> None:
        self.game_controller.reset_game()

    # ── UI 更新方法 ──

    def update_game_status(self) -> None:
        g = self.game
        if g.game_over:
            if g.winner == 1:
                self.game_status_label.setText("游戏状态: 红方获胜")
                self.turn_label.setText("游戏结束: 红方获胜")
            elif g.winner == 2:
                self.game_status_label.setText("游戏状态: 黑方获胜")
                self.turn_label.setText("游戏结束: 黑方获胜")
            else:
                self.game_status_label.setText("游戏状态: 平局")
                self.turn_label.setText("游戏结束: 平局")
        else:
            self.game_status_label.setText(
                "游戏状态: 进行中" if self.game_controller.is_active else "游戏状态: 等待开始")
            self.turn_label.setText(
                f"当前回合: {'红方' if g.current_player == 1 else '黑方'}")

        self.total_moves_label.setText(f"总步数: {g.total_moves_count}")

        # ── 棋力显示（共享子力统计：不含将/帥，单位=兵，残局自动切换估值表）──
        red_mat, black_mat, red_count, black_count = compute_material(g.board)
        self.red_material_label.setText(f"红: {red_mat:g}")
        self.black_material_label.setText(f"黑: {black_mat:g}")
        self.red_pieces_label.setText(f"红子: {red_count}")
        self.black_pieces_label.setText(f"黑子: {black_count}")

    def update_history_list(self) -> None:
        # 最近 8 手拼成纯文本一次 setText（无走子时清空），不再反复增删 QLabel
        parts = []
        for move in self.game.moves[-8:]:
            player = move[4]
            parts.append(
                f"{'🔴' if player == 1 else '⚫'} {self.game.move_notation(move)}")
        self.history_moves_label.setText('  '.join(parts))

    def update_ai_score(self) -> None:
        """更新左侧面板的 AI 仲裁计分显示。"""
        score = self.game_controller.ai_score
        if score > 0:
            color = '#4a9e5a'  # 绿色 - 正分
            prefix = '+'
        elif score < 0:
            color = '#c44a4a'  # 红色 - 负分
            prefix = ''
        else:
            color = '#f0c040'  # 金色 - 零分
            prefix = ''
        self.ai_score_label.setText(f"得分: {prefix}{score}")
        self.ai_score_label.setStyleSheet(
            f"color: {color}; font-size: 18px; font-weight: bold; padding: 4px;"
        )
        self.ai_arbitration_count_label.setText(
            f"仲裁次数: {self.game_controller.arbitration_count}")

    def _set_status_pair(self, red_text: str, black_text: str) -> None:
        """成对更新红/黑状态标签。"""
        self.red_status.setText(red_text)
        self.black_status.setText(black_text)

    def update_player_status(self) -> None:
        g = self.game
        gc = self.game_controller
        current = g.current_player
        current_model = gc.model1 if current == 1 else gc.model2
        is_human = (current_model == HUMAN_MODEL)
        busy = gc.ai_manager.is_busy()

        # AI点评进行中：被解析方显示"解析中"（对局阻塞等待解析完成）
        if gc.commentary_mover is not None:
            if gc.commentary_mover == 1:
                self._set_status_pair("🔴 红方 解析中...", "⚫ 黑方 等待...")
            else:
                self._set_status_pair("🔴 红方 等待...", "⚫ 黑方 解析中...")
            return

        if g.game_over:
            if g.winner == 1:
                self._set_status_pair("🔴 红方 🏆 获胜！", "⚫ 黑方")
            elif g.winner == 2:
                self._set_status_pair("🔴 红方", "⚫ 黑方 🏆 获胜！")
            else:
                self._set_status_pair("🔴 红方 平局", "⚫ 黑方 平局")
            return

        if busy and not is_human:
            if current == 1:
                self._set_status_pair("🔴 红方 思考中...", "⚫ 黑方 等待...")
            else:
                self._set_status_pair("🔴 红方 等待...", "⚫ 黑方 思考中...")
        elif is_human and not busy:
            if current == 1:
                self._set_status_pair("🔴 红方 请走子", "⚫ 黑方 等待...")
            else:
                self._set_status_pair("🔴 红方 等待...", "⚫ 黑方 请走子")
        else:
            self._set_status_pair(
                f"🔴 红方 {gc.last_red_raw if gc.last_red_raw else '等待...'}",
                f"⚫ 黑方 {gc.last_black_raw if gc.last_black_raw else '等待...'}")

    def closeEvent(self, event) -> None:
        # GameController.shutdown() 内部已调用 stop_thinking_timer() 与
        # ai_manager.shutdown()，此处不再重复
        self.game_controller.shutdown()
        event.accept()
