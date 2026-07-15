"""左侧面板 UI 构建"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QPushButton, QCheckBox,
    QLabel, QGroupBox, QSpinBox, QFrame,
)


def _section_label(text: str) -> QLabel:
    """统一的小节标题样式。"""
    label = QLabel(text)
    label.setStyleSheet(
        "font-size: 13px; font-weight: bold; color: #7eb8da; "
        "padding: 4px 0 2px 4px;"
    )
    return label


def _h_separator() -> QFrame:
    """水平分隔线。"""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    line.setStyleSheet("color: #444;")
    line.setFixedHeight(1)
    return line


def setup_left_expanded(parent) -> None:
    """在 parent (MainWindow) 上构建左侧展开面板的所有控件"""
    layout = QVBoxLayout(parent.left_expanded)
    layout.setContentsMargins(6, 0, 6, 8)
    layout.setSpacing(8)

    # ── 标题行 ──
    title_layout = QHBoxLayout()
    title_layout.setContentsMargins(0, 4, 0, 0)
    title = QLabel("🔴⚫ AI 中国象棋")
    title.setStyleSheet(
        "font-size: 20px; font-weight: bold; color: #4a6fa5; "
        "padding: 12px 0 4px 4px;"
    )
    title_layout.addWidget(title)
    parent.expand_collapse_btn = QPushButton("◀")
    parent.expand_collapse_btn.setFixedSize(22, 22)
    parent.expand_collapse_btn.setStyleSheet(
        "QPushButton { background: transparent; color: #666; border: none; font-size: 14px; }"
        "QPushButton:hover { color: #aaa; }"
    )
    parent.expand_collapse_btn.clicked.connect(parent.toggle_left_panel)
    title_layout.addWidget(parent.expand_collapse_btn, 0, Qt.AlignmentFlag.AlignTop)
    layout.addLayout(title_layout)

    layout.addWidget(_h_separator())
    layout.addSpacing(4)

    # ── 对弈模型 ──
    layout.addWidget(_section_label("🔴 红方（先手）"))
    parent.model1_combo = QComboBox()
    parent.model1_combo.setMinimumHeight(28)
    parent.model1_combo.currentIndexChanged.connect(parent.on_model1_changed)
    layout.addWidget(parent.model1_combo)

    layout.addSpacing(8)

    layout.addWidget(_section_label("⚫ 黑方（后手）"))
    parent.model2_combo = QComboBox()
    parent.model2_combo.setMinimumHeight(28)
    parent.model2_combo.currentIndexChanged.connect(parent.on_model2_changed)
    layout.addWidget(parent.model2_combo)

    layout.addSpacing(6)
    layout.addWidget(_h_separator())
    layout.addSpacing(4)

    # ── AI 引擎 ──
    engine_group = QGroupBox("AI 引擎")
    engine_group.setStyleSheet(
        "QGroupBox { font-weight: bold; color: #b0b0b0; border: 1px solid #444; "
        "margin-top: 10px; padding: 12px 8px 8px 8px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #7eb8da; }"
    )
    engine_layout = QGridLayout(engine_group)
    engine_layout.setVerticalSpacing(12)
    engine_layout.setHorizontalSpacing(8)

    engine_layout.addWidget(QLabel("AI 模式:"), 0, 0)
    parent.ai_mode_combo = QComboBox()
    parent.ai_mode_combo.addItem("LLM + 搜索（推荐）", "hybrid")
    parent.ai_mode_combo.addItem("仅搜索", "search_only")
    parent.ai_mode_combo.addItem("仅 LLM", "llm_only")
    parent.ai_mode_combo.setCurrentIndex(0)
    parent.ai_mode_combo.currentIndexChanged.connect(parent.on_ai_mode_changed)
    engine_layout.addWidget(parent.ai_mode_combo, 0, 1, 1, 2)

    engine_layout.addWidget(QLabel("搜索深度:"), 1, 0)
    parent.search_depth_spin = QSpinBox()
    parent.search_depth_spin.setRange(1, 6)
    parent.search_depth_spin.setValue(5)
    parent.search_depth_spin.setToolTip("Alpha-Beta 搜索深度（1=最快/最浅，6=最慢/最深）")
    parent.search_depth_spin.valueChanged.connect(parent.on_search_depth_changed)
    engine_layout.addWidget(parent.search_depth_spin, 1, 1)

    parent.opening_book_check = QCheckBox("使用开局库")
    parent.opening_book_check.setChecked(True)
    parent.opening_book_check.setToolTip("启用开局库可在前 12 步快速出子，节省 token")
    parent.opening_book_check.stateChanged.connect(parent.on_opening_book_changed)
    engine_layout.addWidget(parent.opening_book_check, 1, 2)

    layout.addWidget(engine_group)

    layout.addSpacing(2)

    # ── AI 控制 ──
    board_group = QGroupBox("AI 控制")
    board_group.setStyleSheet(
        "QGroupBox { font-weight: bold; color: #b0b0b0; border: 1px solid #444; "
        "margin-top: 10px; padding: 12px 8px 8px 8px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #7eb8da; }"
    )
    board_layout = QVBoxLayout(board_group)
    board_layout.setSpacing(8)

    parent.vision_check = QCheckBox("使用图像输入（视觉模式）")
    parent.vision_check.setChecked(False)
    board_layout.addWidget(parent.vision_check)

    parent.think_check = QCheckBox("think / no_think")
    parent.think_check.setChecked(True)
    board_layout.addWidget(parent.think_check)

    parent.disable_think_check = QCheckBox("禁用 think 参数（兼容模式）")
    parent.disable_think_check.setChecked(False)
    parent.disable_think_check.stateChanged.connect(parent.on_disable_think_changed)
    board_layout.addWidget(parent.disable_think_check)

    layout.addWidget(board_group)

    layout.addSpacing(6)

    # ── 控制按钮 ──
    btn_layout = QVBoxLayout()
    btn_layout.setSpacing(6)

    parent.start_btn = QPushButton("▶  开始对弈")
    parent.start_btn.setMinimumHeight(34)
    parent.start_btn.setStyleSheet(
        "QPushButton { background-color: #4a9e5a; color: white; border: none; "
        "border-radius: 4px; padding: 6px; font-size: 13px; font-weight: bold; }"
        "QPushButton:hover { background-color: #3a8a4a; }"
        "QPushButton:disabled { background-color: #444; color: #777; }"
    )
    parent.start_btn.clicked.connect(parent.game_controller.start_game)
    btn_layout.addWidget(parent.start_btn)

    parent.pause_btn = QPushButton("⏸  暂停")
    parent.pause_btn.setMinimumHeight(30)
    parent.pause_btn.setEnabled(False)
    parent.pause_btn.clicked.connect(parent._on_pause_clicked)
    btn_layout.addWidget(parent.pause_btn)

    parent.reset_btn = QPushButton("⏹  停止游戏")
    parent.reset_btn.setMinimumHeight(30)
    parent.reset_btn.setEnabled(False)
    parent.reset_btn.setStyleSheet(
        "QPushButton { background-color: #a54a4a; color: white; border: none; "
        "border-radius: 4px; padding: 6px; font-size: 12px; }"
        "QPushButton:hover { background-color: #8a3a3a; }"
        "QPushButton:disabled { background-color: #444; color: #777; }"
    )
    parent.reset_btn.clicked.connect(parent._on_reset_clicked)
    btn_layout.addWidget(parent.reset_btn)

    layout.addLayout(btn_layout)

    layout.addSpacing(4)
    layout.addWidget(_h_separator())
    layout.addSpacing(4)

    # ── 游戏状态 ──
    status_group = QGroupBox("游戏状态")
    status_group.setStyleSheet(
        "QGroupBox { font-weight: bold; color: #b0b0b0; border: 1px solid #444; "
        "margin-top: 10px; padding: 12px 8px 8px 8px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #7eb8da; }"
    )
    status_layout = QVBoxLayout(status_group)
    status_layout.setSpacing(3)

    parent.game_status_label = QLabel("等待开始")
    parent.game_status_label.setStyleSheet("color: #e0c070; font-weight: bold;")
    status_layout.addWidget(parent.game_status_label)
    parent.turn_label = QLabel("当前回合: —")
    status_layout.addWidget(parent.turn_label)
    parent.think_timer_label = QLabel("思考用时: —")
    status_layout.addWidget(parent.think_timer_label)

    layout.addWidget(status_group)

    layout.addSpacing(4)

    # ── 统计 ──
    stats_group = QGroupBox("统计")
    stats_group.setStyleSheet(
        "QGroupBox { font-weight: bold; color: #b0b0b0; border: 1px solid #444; "
        "margin-top: 10px; padding: 12px 8px 8px 8px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #7eb8da; }"
    )
    stats_layout = QVBoxLayout(stats_group)
    stats_layout.setSpacing(2)

    parent.total_moves_label = QLabel("总步数: 0")
    stats_layout.addWidget(parent.total_moves_label)
    parent.game_duration_label = QLabel("持续时间: 0 秒")
    stats_layout.addWidget(parent.game_duration_label)
    parent.red_tokens_label = QLabel("红方用时: 0 秒")
    stats_layout.addWidget(parent.red_tokens_label)
    parent.black_tokens_label = QLabel("黑方用时: 0 秒")
    stats_layout.addWidget(parent.black_tokens_label)
    parent.search_nodes_label = QLabel("搜索节点: 0")
    stats_layout.addWidget(parent.search_nodes_label)

    layout.addWidget(stats_group)

    layout.addStretch()
