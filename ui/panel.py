"""左侧面板 UI 构建"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QPushButton, QCheckBox,
    QLabel, QGroupBox, QSpinBox,
)


def setup_left_expanded(parent) -> None:
    """在 parent (MainWindow) 上构建左侧展开面板的所有控件"""
    layout = QVBoxLayout(parent.left_expanded)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)

    # 标题
    title_layout = QHBoxLayout()
    title = QLabel("🔴⚫ AI中国象棋            ")
    title.setStyleSheet(
        "font-size: 24px; font-weight: bold; color: #4a6fa5; "
        "text-decoration: underline; padding: 8px 12px;"
    )
    title.setContentsMargins(0, 18, 0, 0)
    title_layout.addWidget(title)
    parent.expand_collapse_btn = QPushButton("◀")
    parent.expand_collapse_btn.setFixedSize(15, 15)
    parent.expand_collapse_btn.clicked.connect(parent.toggle_left_panel)
    title_layout.addWidget(parent.expand_collapse_btn)
    layout.addLayout(title_layout)

    layout.addSpacing(17)

    # 红方模型
    layout.addWidget(QLabel("🔴 红方 (先手)"))
    parent.model1_combo = QComboBox()
    parent.model1_combo.currentIndexChanged.connect(parent.on_model1_changed)
    layout.addWidget(parent.model1_combo)

    # 黑方模型
    layout.addSpacing(5)
    layout.addWidget(QLabel("⚫ 黑方 (后手)"))
    parent.model2_combo = QComboBox()
    parent.model2_combo.currentIndexChanged.connect(parent.on_model2_changed)
    layout.addWidget(parent.model2_combo)

    layout.addSpacing(5)

    # ── AI 引擎控制组（新增） ──
    engine_group = QGroupBox("AI 引擎")
    engine_layout = QGridLayout(engine_group)

    # AI 模式
    engine_layout.addWidget(QLabel("AI 模式:"), 0, 0)
    parent.ai_mode_combo = QComboBox()
    parent.ai_mode_combo.addItem("LLM + 搜索 (推荐)", "hybrid")
    parent.ai_mode_combo.addItem("仅搜索", "search_only")
    parent.ai_mode_combo.addItem("仅 LLM", "llm_only")
    parent.ai_mode_combo.setCurrentIndex(0)
    parent.ai_mode_combo.currentIndexChanged.connect(parent.on_ai_mode_changed)
    engine_layout.addWidget(parent.ai_mode_combo, 0, 1, 1, 2)

    # 搜索深度
    engine_layout.addWidget(QLabel("搜索深度:"), 1, 0)
    parent.search_depth_spin = QSpinBox()
    parent.search_depth_spin.setRange(1, 6)
    parent.search_depth_spin.setValue(5)
    parent.search_depth_spin.setToolTip("Alpha-Beta 搜索深度（1=最快/最浅，6=最慢/最深）")
    parent.search_depth_spin.valueChanged.connect(parent.on_search_depth_changed)
    engine_layout.addWidget(parent.search_depth_spin, 1, 1)

    # 开局库
    parent.opening_book_check = QCheckBox("使用开局库")
    parent.opening_book_check.setChecked(True)
    parent.opening_book_check.setToolTip("启用开局库可在前 12 步快速出子，节省 token")
    parent.opening_book_check.stateChanged.connect(parent.on_opening_book_changed)
    engine_layout.addWidget(parent.opening_book_check, 1, 2)

    layout.addWidget(engine_group)

    # ── AI 控制组（原有） ──
    board_group = QGroupBox("AI控制")
    board_layout = QGridLayout(board_group)

    parent.vision_check = QCheckBox("使用图像输入（视觉模式）")
    parent.vision_check.setChecked(False)
    board_layout.addWidget(parent.vision_check, 0, 0, 1, 3)

    parent.think_check = QCheckBox("think/no_think")
    parent.think_check.setChecked(True)
    board_layout.addWidget(parent.think_check, 1, 0, 1, 3)

    parent.disable_think_check = QCheckBox("禁用 think 参数（兼容模式）")
    parent.disable_think_check.setChecked(False)
    parent.disable_think_check.stateChanged.connect(parent.on_disable_think_changed)
    board_layout.addWidget(parent.disable_think_check, 2, 0, 1, 3)

    layout.addWidget(board_group)

    # 按钮
    btn_layout = QVBoxLayout()
    parent.start_btn = QPushButton("开始对弈")
    parent.start_btn.clicked.connect(parent.game_controller.start_game)
    btn_layout.addWidget(parent.start_btn)
    parent.pause_btn = QPushButton("暂停")
    parent.pause_btn.setEnabled(False)
    parent.pause_btn.clicked.connect(parent._on_pause_clicked)
    btn_layout.addWidget(parent.pause_btn)
    parent.reset_btn = QPushButton("停止游戏")
    parent.reset_btn.setEnabled(False)
    parent.reset_btn.clicked.connect(parent._on_reset_clicked)
    btn_layout.addWidget(parent.reset_btn)
    layout.addLayout(btn_layout)

    # 游戏状态
    status_group = QGroupBox("游戏状态")
    status_layout = QVBoxLayout(status_group)
    parent.game_status_label = QLabel("🔴 等待开始")
    status_layout.addWidget(parent.game_status_label)
    parent.turn_label = QLabel("当前回合: -")
    status_layout.addWidget(parent.turn_label)
    parent.think_timer_label = QLabel("思考用时: -")
    status_layout.addWidget(parent.think_timer_label)
    layout.addWidget(status_group)

    # 统计
    stats_group = QGroupBox("统计")
    stats_layout = QVBoxLayout(stats_group)
    parent.total_moves_label = QLabel("总步数:0")
    stats_layout.addWidget(parent.total_moves_label)
    parent.game_duration_label = QLabel("时间:0s")
    stats_layout.addWidget(parent.game_duration_label)
    parent.red_tokens_label = QLabel("红方用时:0s")
    stats_layout.addWidget(parent.red_tokens_label)
    parent.black_tokens_label = QLabel("黑方用时:0s")
    stats_layout.addWidget(parent.black_tokens_label)
    # 搜索节点统计
    parent.search_nodes_label = QLabel("搜索节点:0")
    stats_layout.addWidget(parent.search_nodes_label)
    layout.addWidget(stats_group)

    layout.addStretch()
