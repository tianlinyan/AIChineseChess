"""主窗口"""

from PyQt6.QtCore import Qt, QSettings, QDateTime, QByteArray, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTextEdit, QPushButton,
    QLabel, QStackedWidget, QFrame,
)

from domain.game import ChineseChessGame
from domain.prompts import HUMAN_MODEL
from domain.constants import format_duration
from services.logging import LogManager
from services.models import ModelManager
from ai.manager import AIManager
from app.controller import GameController
from ui.board import BoardWidget
from ui.theme import (
    WINDOW_WIDTH, WINDOW_HEIGHT, MIDDLE_PANEL_MIN_WIDTH,
    SPLITTER_SIZES, DARK_THEME_QSS,
)
from ui.panel import setup_left_expanded


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("中国象棋 AI 对弈")
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowMaximizeButtonHint)

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

        # 必须在 setup_ui() 之后调用——日志面板的 widget 在那时才创建
        self.game_controller._init_pikafish()

        self.settings = QSettings('ChineseChessAI', 'ChineseChess')

    # ── UI 构建 ──

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
        self.middle_panel.setMinimumWidth(MIDDLE_PANEL_MIN_WIDTH)
        middle_layout = QVBoxLayout(self.middle_panel)
        middle_layout.setSpacing(0)

        self.black_status = QLabel("⚫ 黑方 等待...")
        self.black_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.black_status.setFrameShape(QFrame.Shape.Box)
        self.black_status.setStyleSheet(
            "color: #808080; padding: 12px 5px; font-size: 16px; font-weight: bold;")
        self.black_status.setContentsMargins(0, 50, 0, 0)
        middle_layout.addWidget(self.black_status)

        self.board_widget = BoardWidget()
        self.board_widget.set_game(self.game)
        self.board_widget.move_made.connect(self.game_controller.on_human_move)
        middle_layout.addWidget(self.board_widget, 1, Qt.AlignmentFlag.AlignCenter)

        self.red_status = QLabel("🔴 红方 等待...")
        self.red_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.red_status.setFrameShape(QFrame.Shape.Box)
        self.red_status.setStyleSheet(
            "color: #b74c3c; padding: 12px 5px; font-size: 16px; font-weight: bold;")
        self.red_status.setContentsMargins(0, 0, 0, 27)
        middle_layout.addWidget(self.red_status)

        middle_layout.addSpacing(10)

        # 历史
        self.history_mini = QWidget()
        self.history_mini_layout = QHBoxLayout(self.history_mini)
        self.history_mini_layout.setContentsMargins(5, 0, 5, 10)
        self.history_label = QLabel("📋 落子历史:")
        self.history_mini_layout.addWidget(self.history_label)
        self.history_mini_layout.addStretch()
        middle_layout.addWidget(self.history_mini)

        # ── 右侧面板（日志） ──
        self.right_panel = QWidget()
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
            if state and isinstance(state, QByteArray):
                self.splitter.restoreState(state)
            else:
                self.splitter.setSizes([280, self.splitter.sizes()[1], self.splitter.sizes()[2]])
        else:
            state = self.splitter.saveState()
            self.settings.setValue('splitter_expanded_state', state)
            self.left_stack.setCurrentIndex(1)
            self.left_collapsed_flag = True
            sizes = self.splitter.sizes()
            self.splitter.setSizes([40, sizes[1], sizes[2]])

    # ── 模型加载 ──

    def load_models(self) -> None:
        def on_error(msg):
            self.log_manager.log(msg, 'WARNING')

        self.model_manager.load(on_error=on_error)

        human_label = "人类"
        self.model1_combo.clear()
        self.model1_combo.addItem(human_label, 'human')
        for m in self.model_manager.player1_models:
            self.model1_combo.addItem(m.name, m.id)

        self.model2_combo.clear()
        self.model2_combo.addItem(human_label, 'human')
        for m in self.model_manager.player2_models:
            self.model2_combo.addItem(m.name, m.id)

        if self.model1_combo.count() > 1:
            self.model1_combo.setCurrentIndex(1)
        if self.model2_combo.count() > 1:
            self.model2_combo.setCurrentIndex(1)

    def on_model1_changed(self, idx: int) -> None:
        self.update_start_button()

    def on_model2_changed(self, idx: int) -> None:
        self.update_start_button()

    def update_start_button(self) -> None:
        enabled = (self.model1_combo.currentData() is not None and
                   self.model2_combo.currentData() is not None)
        self.start_btn.setEnabled(enabled)

    def on_disable_think_changed(self, state: int) -> None:
        if state == Qt.CheckState.Checked.value:
            self.think_check.setEnabled(False)
            self.think_check.setChecked(False)
        else:
            self.think_check.setEnabled(True)

    # ── AI 引擎控制事件（新增） ──

    def on_ai_mode_changed(self, idx: int) -> None:
        mode = self.ai_mode_combo.currentData()
        if mode:
            self.game_controller.ai_mode = mode
            # 搜索相关控件仅在非 llm_only 模式下启用
            search_enabled = mode != 'llm_only'
            self.search_depth_spin.setEnabled(search_enabled)

    def on_search_depth_changed(self, value: int) -> None:
        self.game_controller.search_depth = value

    def on_opening_book_changed(self, state: int) -> None:
        self.game_controller.use_opening_book = (
            state == Qt.CheckState.Checked.value)

    # ── 按钮事件 ──

    def _on_pause_clicked(self) -> None:
        self.game_controller.toggle_pause()

    def _on_reset_clicked(self) -> None:
        self.game_controller.reset_game()

    # ── 计时器方法（委托给 Controller） ──

    def start_thinking_timer(self, player: int) -> None:
        self.game_controller.start_thinking_timer(player)

    def stop_thinking_timer(self) -> None:
        self.game_controller.stop_thinking_timer()

    def pause_thinking_timer(self) -> None:
        self.game_controller.pause_thinking_timer()

    def resume_thinking_timer(self) -> None:
        self.game_controller.resume_thinking_timer()

    # ── UI 更新方法 ──

    def update_ui(self) -> None:
        self.board_widget.update()
        self.update_game_status()
        self.update_stats_display()
        self.update_history_list()

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
        self.total_moves_label.setText(f"总步数: {len(g.moves)}")

    def update_stats_display(self) -> None:
        stats = self.game_controller.stats
        if not stats['start_time']:
            return
        duration = stats['start_time'].secsTo(QDateTime.currentDateTime())
        self.game_duration_label.setText(
            f"持续时间: {format_duration(duration)}")
        self.red_tokens_label.setText(
            f"红方用时: {format_duration(self.game_controller.red_total_time)}")
        self.black_tokens_label.setText(
            f"黑方用时: {format_duration(self.game_controller.black_total_time)}")
        search_nodes = stats.get('search_nodes', 0)
        if search_nodes > 0:
            if search_nodes >= 1000:
                self.search_nodes_label.setText(f"搜索节点: {search_nodes/1000:.1f}K")
            else:
                self.search_nodes_label.setText(f"搜索节点: {search_nodes}")

    def update_history_list(self) -> None:
        # 删除除 history_label 之外的所有旧走子标签和 stretch
        for i in range(self.history_mini_layout.count() - 1, 0, -1):
            item = self.history_mini_layout.takeAt(i)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        # 添加最近的走子记录
        for move in self.game.moves[-8:]:
            fr, fc, tr, tc, player = move[0:5]
            from_coord = f"{chr(65 + fc)}{fr + 1}"
            to_coord = f"{chr(65 + tc)}{tr + 1}"
            label = QLabel(f"{'🔴' if player == 1 else '⚫'} {from_coord}→{to_coord}")
            self.history_mini_layout.addWidget(label)

        self.history_mini_layout.addStretch()

    def update_player_status(self) -> None:
        g = self.game
        gc = self.game_controller
        current = g.current_player
        current_model = gc.model1 if current == 1 else gc.model2
        is_human = (current_model == HUMAN_MODEL)
        busy = gc.ai_manager.is_busy()

        if g.game_over:
            if g.winner == 1:
                self.red_status.setText("🔴 红方 🏆 获胜！")
                self.black_status.setText("⚫ 黑方")
            elif g.winner == 2:
                self.black_status.setText("⚫ 黑方 🏆 获胜！")
                self.red_status.setText("🔴 红方")
            else:
                self.red_status.setText("🔴 红方 平局")
                self.black_status.setText("⚫ 黑方 平局")
            return

        if busy and not is_human:
            if current == 1:
                self.red_status.setText("🔴 红方 思考中...")
                self.black_status.setText("⚫ 黑方 等待...")
            else:
                self.black_status.setText("⚫ 黑方 思考中...")
                self.red_status.setText("🔴 红方 等待...")
        elif is_human and not busy:
            if current == 1:
                self.red_status.setText("🔴 红方 请走子")
                self.black_status.setText("⚫ 黑方 等待...")
            else:
                self.black_status.setText("⚫ 黑方 请走子")
                self.red_status.setText("🔴 红方 等待...")
        else:
            self.red_status.setText(
                f"🔴 红方 {gc.last_red_raw if gc.last_red_raw else '等待...'}")
            self.black_status.setText(
                f"⚫ 黑方 {gc.last_black_raw if gc.last_black_raw else '等待...'}")

    def closeEvent(self, event) -> None:
        self.game_controller.stop_thinking_timer()
        self.game_controller.shutdown()
        self.ai_manager.shutdown()
        event.accept()
