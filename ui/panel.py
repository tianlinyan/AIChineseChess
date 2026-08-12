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
        "font-size: 12px; font-weight: bold; color: #7eb8da; "
        "padding: 2px 0 0px 4px;"
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


def _group_style(title_color: str = "#7eb8da") -> str:
    """统一的 QGroupBox 样式。"""
    return (
        "QGroupBox { font-weight: bold; font-size: 12px; color: #b0b0b0; "
        "border: 1px solid #444; margin-top: 6px; padding: 6px 6px 4px 6px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 8px; "
        f"padding: 0 4px; color: {title_color}; }}"
    )


def setup_left_expanded(parent) -> None:
    """在 parent (MainWindow) 上构建左侧展开面板的所有控件"""
    layout = QVBoxLayout(parent.left_expanded)
    layout.setContentsMargins(6, 0, 6, 4)
    layout.setSpacing(3)

    # ── 标题行 ──
    title_layout = QHBoxLayout()
    title_layout.setContentsMargins(0, 2, 0, 0)
    title = QLabel("🔴⚫ AI 中国象棋")
    title.setStyleSheet(
        "font-size: 12px; font-weight: bold; color: #4a6fa5; "
        "padding: 6px 0 2px 4px;"
    )
    title_layout.addWidget(title)
    parent.expand_collapse_btn = QPushButton("◀")
    parent.expand_collapse_btn.setFixedSize(22, 22)
    parent.expand_collapse_btn.setStyleSheet(
        "QPushButton { background: transparent; color: #666; border: none; font-size: 12px; }"
        "QPushButton:hover { color: #aaa; }"
    )
    parent.expand_collapse_btn.clicked.connect(parent.toggle_left_panel)
    title_layout.addWidget(parent.expand_collapse_btn, 0, Qt.AlignmentFlag.AlignTop)
    layout.addLayout(title_layout)

    layout.addWidget(_h_separator())

    # ── 🔴 红方（先手）──
    layout.addWidget(_section_label("🔴 红方（先手）"))
    parent.model1_combo = QComboBox()
    parent.model1_combo.setMinimumHeight(24)
    parent.model1_combo.currentIndexChanged.connect(parent.on_model1_changed)
    layout.addWidget(parent.model1_combo)

    # 红方 AI 引擎
    red_engine = QGroupBox("红方 AI 引擎")
    red_engine.setStyleSheet(_group_style("#e06060"))
    red_layout = QGridLayout(red_engine)
    red_layout.setVerticalSpacing(4)
    red_layout.setHorizontalSpacing(4)
    red_layout.setContentsMargins(6, 8, 6, 4)

    red_layout.addWidget(QLabel("模式:"), 0, 0)
    parent.red_ai_mode_combo = QComboBox()
    parent.red_ai_mode_combo.addItem("AI + 搜索", "hybrid")
    parent.red_ai_mode_combo.addItem("仅搜索", "search_only")
    parent.red_ai_mode_combo.addItem("仅 AI", "llm_only")
    parent.red_ai_mode_combo.setCurrentIndex(0)
    parent.red_ai_mode_combo.setMinimumHeight(24)
    parent.red_ai_mode_combo.currentIndexChanged.connect(parent.on_red_ai_mode_changed)
    red_layout.addWidget(parent.red_ai_mode_combo, 0, 1)

    red_layout.addWidget(QLabel("深度:"), 1, 0)
    parent.red_search_depth_spin = QSpinBox()
    parent.red_search_depth_spin.setRange(1, 8)
    parent.red_search_depth_spin.setValue(5)
    parent.red_search_depth_spin.setMinimumHeight(24)
    parent.red_search_depth_spin.setToolTip("红方搜索强度 1~8，越高越准越慢")
    parent.red_search_depth_spin.valueChanged.connect(parent.on_red_search_depth_changed)
    red_layout.addWidget(parent.red_search_depth_spin, 1, 1)

    parent.red_opening_book_check = QCheckBox("开局库")
    parent.red_opening_book_check.setChecked(True)
    parent.red_opening_book_check.setToolTip("红方使用开局库快速出子")
    parent.red_opening_book_check.stateChanged.connect(parent.on_red_opening_book_changed)
    red_layout.addWidget(parent.red_opening_book_check, 1, 2)

    layout.addWidget(red_engine)

    layout.addWidget(_h_separator())

    # ── ⚫ 黑方（后手）──
    layout.addWidget(_section_label("⚫ 黑方（后手）"))
    parent.model2_combo = QComboBox()
    parent.model2_combo.setMinimumHeight(24)
    parent.model2_combo.currentIndexChanged.connect(parent.on_model2_changed)
    layout.addWidget(parent.model2_combo)

    # 黑方 AI 引擎
    black_engine = QGroupBox("黑方 AI 引擎")
    black_engine.setStyleSheet(_group_style("#6060e0"))
    black_layout = QGridLayout(black_engine)
    black_layout.setVerticalSpacing(4)
    black_layout.setHorizontalSpacing(4)
    black_layout.setContentsMargins(6, 8, 6, 4)

    black_layout.addWidget(QLabel("模式:"), 0, 0)
    parent.black_ai_mode_combo = QComboBox()
    parent.black_ai_mode_combo.addItem("AI + 搜索", "hybrid")
    parent.black_ai_mode_combo.addItem("仅搜索", "search_only")
    parent.black_ai_mode_combo.addItem("仅 AI", "llm_only")
    parent.black_ai_mode_combo.setCurrentIndex(0)
    parent.black_ai_mode_combo.setMinimumHeight(24)
    parent.black_ai_mode_combo.currentIndexChanged.connect(parent.on_black_ai_mode_changed)
    black_layout.addWidget(parent.black_ai_mode_combo, 0, 1)

    black_layout.addWidget(QLabel("深度:"), 1, 0)
    parent.black_search_depth_spin = QSpinBox()
    parent.black_search_depth_spin.setRange(1, 8)
    parent.black_search_depth_spin.setValue(5)
    parent.black_search_depth_spin.setMinimumHeight(24)
    parent.black_search_depth_spin.setToolTip("黑方搜索强度 1~8，越高越准越慢")
    parent.black_search_depth_spin.valueChanged.connect(parent.on_black_search_depth_changed)
    black_layout.addWidget(parent.black_search_depth_spin, 1, 1)

    parent.black_opening_book_check = QCheckBox("开局库")
    parent.black_opening_book_check.setChecked(True)
    parent.black_opening_book_check.setToolTip("黑方使用开局库快速出子")
    parent.black_opening_book_check.stateChanged.connect(parent.on_black_opening_book_changed)
    black_layout.addWidget(parent.black_opening_book_check, 1, 2)

    layout.addWidget(black_engine)

    # ── AI 控制 ──
    ctrl_group = QGroupBox("AI 控制")
    ctrl_group.setStyleSheet(_group_style())
    ctrl_layout = QVBoxLayout(ctrl_group)
    ctrl_layout.setSpacing(3)
    ctrl_layout.setContentsMargins(6, 8, 6, 4)

    parent.vision_check = QCheckBox("图像输入（视觉模式）")
    parent.vision_check.setChecked(False)
    ctrl_layout.addWidget(parent.vision_check)

    parent.think_check = QCheckBox("think / no_think")
    parent.think_check.setChecked(True)
    ctrl_layout.addWidget(parent.think_check)

    parent.disable_think_check = QCheckBox("禁用 think（兼容模式）")
    parent.disable_think_check.setChecked(False)
    parent.disable_think_check.stateChanged.connect(parent.on_disable_think_changed)
    ctrl_layout.addWidget(parent.disable_think_check)

    layout.addWidget(ctrl_group)

    # ── 控制按钮 ──
    btn_layout = QVBoxLayout()
    btn_layout.setSpacing(3)

    parent.start_btn = QPushButton("▶  开始对弈")
    parent.start_btn.setMinimumHeight(28)
    parent.start_btn.setStyleSheet(
        "QPushButton { background-color: #4a9e5a; color: white; border: none; "
        "border-radius: 4px; padding: 4px; font-size: 12px; font-weight: bold; }"
        "QPushButton:hover { background-color: #3a8a4a; }"
        "QPushButton:disabled { background-color: #444; color: #777; }"
    )
    parent.start_btn.clicked.connect(parent.game_controller.start_game)
    btn_layout.addWidget(parent.start_btn)

    parent.pause_btn = QPushButton("⏸  暂停")
    parent.pause_btn.setMinimumHeight(26)
    parent.pause_btn.setEnabled(False)
    parent.pause_btn.clicked.connect(parent._on_pause_clicked)
    btn_layout.addWidget(parent.pause_btn)

    parent.reset_btn = QPushButton("⏹  停止游戏")
    parent.reset_btn.setMinimumHeight(26)
    parent.reset_btn.setEnabled(False)
    parent.reset_btn.setStyleSheet(
        "QPushButton { background-color: #a54a4a; color: white; border: none; "
        "border-radius: 4px; padding: 4px; font-size: 12px; }"
        "QPushButton:hover { background-color: #8a3a3a; }"
        "QPushButton:disabled { background-color: #444; color: #777; }"
    )
    parent.reset_btn.clicked.connect(parent._on_reset_clicked)
    btn_layout.addWidget(parent.reset_btn)

    layout.addLayout(btn_layout)

    layout.addWidget(_h_separator())

    # ── 游戏状态 ──
    status_group = QGroupBox("游戏状态")
    status_group.setStyleSheet(_group_style())
    status_layout = QVBoxLayout(status_group)
    status_layout.setSpacing(1)
    status_layout.setContentsMargins(6, 8, 6, 4)

    parent.game_status_label = QLabel("等待开始")
    parent.game_status_label.setStyleSheet(
        "color: #e0c070; font-weight: bold; font-size: 12px; padding: 0;"
    )
    status_layout.addWidget(parent.game_status_label)

    parent.turn_label = QLabel("当前回合: —")
    parent.turn_label.setStyleSheet("color: #ccc; font-size: 12px;")
    status_layout.addWidget(parent.turn_label)

    parent.total_moves_label = QLabel("总步数: 0")
    parent.total_moves_label.setStyleSheet("color: #aaa; font-size: 12px;")
    status_layout.addWidget(parent.total_moves_label)

    parent.think_timer_label = QLabel("思考用时: —")
    parent.think_timer_label.setStyleSheet("color: #888; font-size: 12px;")
    status_layout.addWidget(parent.think_timer_label)

    # 红黑棋力对比（同行）
    row1 = QHBoxLayout()
    row1.setSpacing(4)
    parent.red_material_label = QLabel("红: —")
    parent.red_material_label.setStyleSheet(
        "color: #e06060; font-size: 12px; font-weight: bold;")
    row1.addWidget(parent.red_material_label)
    parent.black_material_label = QLabel("黑: —")
    parent.black_material_label.setStyleSheet(
        "color: #6060e0; font-size: 12px; font-weight: bold;")
    row1.addWidget(parent.black_material_label)
    status_layout.addLayout(row1)

    # 红黑余子对比（同行）
    row2 = QHBoxLayout()
    row2.setSpacing(4)
    parent.red_pieces_label = QLabel("红子: —")
    parent.red_pieces_label.setStyleSheet("color: #e06060; font-size: 12px;")
    row2.addWidget(parent.red_pieces_label)
    parent.black_pieces_label = QLabel("黑子: —")
    parent.black_pieces_label.setStyleSheet("color: #6060e0; font-size: 12px;")
    row2.addWidget(parent.black_pieces_label)
    status_layout.addLayout(row2)

    layout.addWidget(status_group)

    # ── AI 计分（仲裁） ──
    score_group = QGroupBox("🏆 AI 计分 (仲裁)")
    score_group.setStyleSheet(_group_style("#f0c040"))
    score_layout = QVBoxLayout(score_group)
    score_layout.setSpacing(1)
    score_layout.setContentsMargins(6, 8, 6, 4)

    parent.ai_score_label = QLabel("得分: 0")
    parent.ai_score_label.setStyleSheet(
        "color: #f0c040; font-size: 12px; font-weight: bold; padding: 2px;"
    )
    parent.ai_score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    score_layout.addWidget(parent.ai_score_label)

    parent.ai_arbitration_count_label = QLabel("仲裁次数: 0")
    parent.ai_arbitration_count_label.setStyleSheet("color: #aaa; font-size: 12px;")
    parent.ai_arbitration_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    score_layout.addWidget(parent.ai_arbitration_count_label)

    parent.ai_score_detail = QLabel("一致 +1 | 不一致 0")
    parent.ai_score_detail.setStyleSheet("color: #888; font-size: 12px;")
    parent.ai_score_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
    score_layout.addWidget(parent.ai_score_detail)

    layout.addWidget(score_group)

    layout.addStretch()
