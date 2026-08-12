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
        "font-size: 13px; font-weight: bold; color: #8ec8e8; "
        "padding: 4px 0 1px 6px;"
    )
    return label


def _h_separator() -> QFrame:
    """水平分隔线。"""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setStyleSheet("color: #3a3a3a;")
    line.setFixedHeight(1)
    return line


def _spacer(height: int = 4) -> QFrame:
    """垂直空白间距。"""
    sp = QFrame()
    sp.setFixedHeight(height)
    sp.setStyleSheet("background: transparent; border: none;")
    return sp


def _group_style(title_color: str = "#7eb8da") -> str:
    """统一的 QGroupBox 样式。"""
    return (
        "QGroupBox {"
        "  font-weight: bold; font-size: 12px; color: #c0c0c0;"
        "  background-color: #2a2a2d;"
        "  border: 1px solid #3a3a3a; border-radius: 4px;"
        "  margin-top: 8px; padding: 8px 8px 6px 8px;"
        "}"
        "QGroupBox::title {"
        "  subcontrol-origin: margin; left: 10px;"
        f" padding: 0 5px; color: {title_color};"
        "}"
    )


def _combo_style() -> str:
    """下拉框统一样式。"""
    return (
        "QComboBox {"
        "  background-color: #333338; color: #d0d0d0;"
        "  border: 1px solid #444; border-radius: 3px;"
        "  padding: 2px 6px; font-size: 13px;"
        "}"
        "QComboBox:hover { border-color: #666; }"
        "QComboBox::drop-down { border: none; width: 18px; }"
        "QComboBox QAbstractItemView {"
        "  background-color: #333338; color: #d0d0d0;"
        "  selection-background-color: #3a5070; border: 1px solid #444;"
        "}"
    )


def _spinbox_style() -> str:
    """数字输入框统一样式。"""
    return (
        "QSpinBox {"
        "  background-color: #333338; color: #d0d0d0;"
        "  border: 1px solid #444; border-radius: 3px;"
        "  padding: 2px 4px; font-size: 13px;"
        "}"
        "QSpinBox:hover { border-color: #666; }"
    )


def _check_style() -> str:
    """复选框统一样式。"""
    return (
        "QCheckBox { color: #b0b0b0; font-size: 13px; spacing: 6px; }"
        "QCheckBox:hover { color: #d0d0d0; }"
        "QCheckBox::indicator { width: 14px; height: 14px; }"
    )


def _btn_primary_style() -> str:
    """主按钮（开始）。"""
    return (
        "QPushButton {"
        "  background-color: #3a7a4a; color: white; border: none;"
        "  border-radius: 4px; padding: 6px; font-size: 13px; font-weight: bold;"
        "}"
        "QPushButton:hover { background-color: #4a9a5a; }"
        "QPushButton:pressed { background-color: #2a6a3a; }"
        "QPushButton:disabled { background-color: #333; color: #666; }"
    )


def _btn_danger_style() -> str:
    """危险按钮（停止）。"""
    return (
        "QPushButton {"
        "  background-color: #8a3a3a; color: white; border: none;"
        "  border-radius: 4px; padding: 5px; font-size: 13px;"
        "}"
        "QPushButton:hover { background-color: #a54a4a; }"
        "QPushButton:pressed { background-color: #6a2a2a; }"
        "QPushButton:disabled { background-color: #333; color: #666; }"
    )


def _btn_default_style() -> str:
    """普通按钮（暂停）。"""
    return (
        "QPushButton {"
        "  background-color: #3a3a40; color: #c0c0c0; border: 1px solid #555;"
        "  border-radius: 4px; padding: 5px; font-size: 13px;"
        "}"
        "QPushButton:hover { background-color: #4a4a50; border-color: #777; }"
        "QPushButton:pressed { background-color: #2a2a30; }"
        "QPushButton:disabled { background-color: #333; color: #666; border-color: #333; }"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主构建函数
# ══════════════════════════════════════════════════════════════════════════════


def setup_left_expanded(parent) -> None:
    """在 parent (MainWindow) 上构建左侧展开面板的所有控件"""
    parent.left_expanded.setStyleSheet("background-color: #2d2d2d;")

    layout = QVBoxLayout(parent.left_expanded)
    layout.setContentsMargins(8, 0, 8, 6)
    layout.setSpacing(0)

    # ── 标题行 ──
    title_layout = QHBoxLayout()
    title_layout.setContentsMargins(0, 6, 0, 2)
    title = QLabel("🔴⚫ AI 中国象棋")
    title.setStyleSheet(
        "font-size: 20px; font-weight: bold; color: #5a8fc5; "
        "padding: 4px 0 2px 6px;"
    )
    title_layout.addWidget(title)
    parent.expand_collapse_btn = QPushButton("◀")
    parent.expand_collapse_btn.setFixedSize(20, 20)
    parent.expand_collapse_btn.setStyleSheet(
        "QPushButton { background: transparent; color: #555; border: none; "
        "font-size: 13px; }"
        "QPushButton:hover { color: #999; }"
    )
    parent.expand_collapse_btn.clicked.connect(parent.toggle_left_panel)
    title_layout.addWidget(parent.expand_collapse_btn, 0, Qt.AlignmentFlag.AlignTop)
    layout.addLayout(title_layout)
    layout.addWidget(_spacer(2))
    layout.addWidget(_h_separator())
    layout.addWidget(_spacer(4))

    # ══════════════════════════════════════════════════════════════
    # 🔴 红方
    # ══════════════════════════════════════════════════════════════
    layout.addWidget(_section_label("🔴 红方（先手）"))
    parent.model1_combo = QComboBox()
    parent.model1_combo.setMinimumHeight(26)
    parent.model1_combo.setStyleSheet(_combo_style())
    parent.model1_combo.currentIndexChanged.connect(parent.on_model1_changed)
    layout.addWidget(parent.model1_combo)

    layout.addWidget(_spacer(2))

    red_engine = QGroupBox("红方引擎")
    red_engine.setStyleSheet(_group_style("#e06060"))
    red_layout = QGridLayout(red_engine)
    red_layout.setVerticalSpacing(5)
    red_layout.setHorizontalSpacing(6)
    red_layout.setContentsMargins(8, 10, 8, 6)

    rlbl = QLabel("模式:")
    rlbl.setStyleSheet("color: #c0a0a0; font-size: 13px;")
    red_layout.addWidget(rlbl, 0, 0)
    parent.red_ai_mode_combo = QComboBox()
    parent.red_ai_mode_combo.addItem("AI + 搜索", "hybrid")
    parent.red_ai_mode_combo.addItem("仅搜索", "search_only")
    parent.red_ai_mode_combo.addItem("仅 AI", "llm_only")
    parent.red_ai_mode_combo.setCurrentIndex(0)
    parent.red_ai_mode_combo.setMinimumHeight(26)
    parent.red_ai_mode_combo.setStyleSheet(_combo_style())
    parent.red_ai_mode_combo.currentIndexChanged.connect(parent.on_red_ai_mode_changed)
    red_layout.addWidget(parent.red_ai_mode_combo, 0, 1)

    rdlbl = QLabel("深度:")
    rdlbl.setStyleSheet("color: #c0a0a0; font-size: 13px;")
    red_layout.addWidget(rdlbl, 1, 0)
    parent.red_search_depth_spin = QSpinBox()
    parent.red_search_depth_spin.setRange(1, 8)
    parent.red_search_depth_spin.setValue(5)
    parent.red_search_depth_spin.setMinimumHeight(26)
    parent.red_search_depth_spin.setStyleSheet(_spinbox_style())
    parent.red_search_depth_spin.setToolTip("红方搜索强度 1~8")
    parent.red_search_depth_spin.valueChanged.connect(parent.on_red_search_depth_changed)
    red_layout.addWidget(parent.red_search_depth_spin, 1, 1)

    parent.red_opening_book_check = QCheckBox("开局库")
    parent.red_opening_book_check.setChecked(True)
    parent.red_opening_book_check.setStyleSheet(_check_style())
    parent.red_opening_book_check.stateChanged.connect(parent.on_red_opening_book_changed)
    red_layout.addWidget(parent.red_opening_book_check, 1, 2)

    layout.addWidget(red_engine)

    layout.addWidget(_spacer(6))
    layout.addWidget(_h_separator())
    layout.addWidget(_spacer(4))

    # ══════════════════════════════════════════════════════════════
    # ⚫ 黑方
    # ══════════════════════════════════════════════════════════════
    layout.addWidget(_section_label("⚫ 黑方（后手）"))
    parent.model2_combo = QComboBox()
    parent.model2_combo.setMinimumHeight(26)
    parent.model2_combo.setStyleSheet(_combo_style())
    parent.model2_combo.currentIndexChanged.connect(parent.on_model2_changed)
    layout.addWidget(parent.model2_combo)

    layout.addWidget(_spacer(2))

    black_engine = QGroupBox("黑方引擎")
    black_engine.setStyleSheet(_group_style("#6060e0"))
    black_layout = QGridLayout(black_engine)
    black_layout.setVerticalSpacing(5)
    black_layout.setHorizontalSpacing(6)
    black_layout.setContentsMargins(8, 10, 8, 6)

    blbl = QLabel("模式:")
    blbl.setStyleSheet("color: #a0a0c0; font-size: 13px;")
    black_layout.addWidget(blbl, 0, 0)
    parent.black_ai_mode_combo = QComboBox()
    parent.black_ai_mode_combo.addItem("AI + 搜索", "hybrid")
    parent.black_ai_mode_combo.addItem("仅搜索", "search_only")
    parent.black_ai_mode_combo.addItem("仅 AI", "llm_only")
    parent.black_ai_mode_combo.setCurrentIndex(0)
    parent.black_ai_mode_combo.setMinimumHeight(26)
    parent.black_ai_mode_combo.setStyleSheet(_combo_style())
    parent.black_ai_mode_combo.currentIndexChanged.connect(parent.on_black_ai_mode_changed)
    black_layout.addWidget(parent.black_ai_mode_combo, 0, 1)

    bdlbl = QLabel("深度:")
    bdlbl.setStyleSheet("color: #a0a0c0; font-size: 13px;")
    black_layout.addWidget(bdlbl, 1, 0)
    parent.black_search_depth_spin = QSpinBox()
    parent.black_search_depth_spin.setRange(1, 8)
    parent.black_search_depth_spin.setValue(5)
    parent.black_search_depth_spin.setMinimumHeight(26)
    parent.black_search_depth_spin.setStyleSheet(_spinbox_style())
    parent.black_search_depth_spin.setToolTip("黑方搜索强度 1~8")
    parent.black_search_depth_spin.valueChanged.connect(parent.on_black_search_depth_changed)
    black_layout.addWidget(parent.black_search_depth_spin, 1, 1)

    parent.black_opening_book_check = QCheckBox("开局库")
    parent.black_opening_book_check.setChecked(True)
    parent.black_opening_book_check.setStyleSheet(_check_style())
    parent.black_opening_book_check.stateChanged.connect(parent.on_black_opening_book_changed)
    black_layout.addWidget(parent.black_opening_book_check, 1, 2)

    layout.addWidget(black_engine)

    layout.addWidget(_spacer(6))
    layout.addWidget(_h_separator())
    layout.addWidget(_spacer(4))

    # ══════════════════════════════════════════════════════════════
    # AI 控制
    # ══════════════════════════════════════════════════════════════
    ctrl_group = QGroupBox("AI 控制")
    ctrl_group.setStyleSheet(_group_style())
    ctrl_layout = QVBoxLayout(ctrl_group)
    ctrl_layout.setSpacing(3)
    ctrl_layout.setContentsMargins(8, 10, 8, 6)

    parent.vision_check = QCheckBox("图像输入（视觉模式）")
    parent.vision_check.setChecked(False)
    parent.vision_check.setStyleSheet(_check_style())
    ctrl_layout.addWidget(parent.vision_check)

    parent.think_check = QCheckBox("think / no_think")
    parent.think_check.setChecked(True)
    parent.think_check.setStyleSheet(_check_style())
    ctrl_layout.addWidget(parent.think_check)

    parent.disable_think_check = QCheckBox("禁用 think（兼容模式）")
    parent.disable_think_check.setChecked(False)
    parent.disable_think_check.setStyleSheet(_check_style())
    parent.disable_think_check.stateChanged.connect(parent.on_disable_think_changed)
    ctrl_layout.addWidget(parent.disable_think_check)

    layout.addWidget(ctrl_group)

    layout.addWidget(_spacer(6))

    # ══════════════════════════════════════════════════════════════
    # 控制按钮
    # ══════════════════════════════════════════════════════════════
    btn_layout = QVBoxLayout()
    btn_layout.setSpacing(4)

    parent.start_btn = QPushButton("▶  开始对弈")
    parent.start_btn.setMinimumHeight(30)
    parent.start_btn.setStyleSheet(_btn_primary_style())
    parent.start_btn.clicked.connect(parent.game_controller.start_game)
    btn_layout.addWidget(parent.start_btn)

    parent.pause_btn = QPushButton("⏸  暂停")
    parent.pause_btn.setMinimumHeight(28)
    parent.pause_btn.setEnabled(False)
    parent.pause_btn.setStyleSheet(_btn_default_style())
    parent.pause_btn.clicked.connect(parent._on_pause_clicked)
    btn_layout.addWidget(parent.pause_btn)

    parent.reset_btn = QPushButton("⏹  停止游戏")
    parent.reset_btn.setMinimumHeight(28)
    parent.reset_btn.setEnabled(False)
    parent.reset_btn.setStyleSheet(_btn_danger_style())
    parent.reset_btn.clicked.connect(parent._on_reset_clicked)
    btn_layout.addWidget(parent.reset_btn)

    layout.addLayout(btn_layout)

    layout.addWidget(_spacer(6))
    layout.addWidget(_h_separator())
    layout.addWidget(_spacer(4))

    # ══════════════════════════════════════════════════════════════
    # 游戏状态
    # ══════════════════════════════════════════════════════════════
    status_group = QGroupBox("游戏状态")
    status_group.setStyleSheet(_group_style())
    status_layout = QVBoxLayout(status_group)
    status_layout.setSpacing(2)
    status_layout.setContentsMargins(8, 10, 8, 6)

    parent.game_status_label = QLabel("等待开始")
    parent.game_status_label.setStyleSheet(
        "color: #e0c070; font-weight: bold; font-size: 13px;")
    status_layout.addWidget(parent.game_status_label)

    parent.turn_label = QLabel("当前回合: —")
    parent.turn_label.setStyleSheet("color: #bbb; font-size: 13px;")
    status_layout.addWidget(parent.turn_label)

    parent.total_moves_label = QLabel("总步数: 0")
    parent.total_moves_label.setStyleSheet("color: #999; font-size: 13px;")
    status_layout.addWidget(parent.total_moves_label)

    parent.think_timer_label = QLabel("思考用时: —")
    parent.think_timer_label.setStyleSheet("color: #777; font-size: 13px;")
    status_layout.addWidget(parent.think_timer_label)

    row1 = QHBoxLayout()
    row1.setSpacing(8)
    parent.red_material_label = QLabel("红: —")
    parent.red_material_label.setStyleSheet(
        "color: #e06060; font-size: 13px; font-weight: bold;")
    row1.addWidget(parent.red_material_label)
    parent.black_material_label = QLabel("黑: —")
    parent.black_material_label.setStyleSheet(
        "color: #6060e0; font-size: 13px; font-weight: bold;")
    row1.addWidget(parent.black_material_label)
    status_layout.addLayout(row1)

    row2 = QHBoxLayout()
    row2.setSpacing(8)
    parent.red_pieces_label = QLabel("红子: —")
    parent.red_pieces_label.setStyleSheet("color: #d06060; font-size: 13px;")
    row2.addWidget(parent.red_pieces_label)
    parent.black_pieces_label = QLabel("黑子: —")
    parent.black_pieces_label.setStyleSheet("color: #6060d0; font-size: 13px;")
    row2.addWidget(parent.black_pieces_label)
    status_layout.addLayout(row2)

    layout.addWidget(status_group)

    layout.addWidget(_spacer(4))

    # ══════════════════════════════════════════════════════════════
    # AI 计分
    # ══════════════════════════════════════════════════════════════
    score_group = QGroupBox("🏆 AI 计分 (仲裁)")
    score_group.setStyleSheet(_group_style("#f0c040"))
    score_layout = QVBoxLayout(score_group)
    score_layout.setSpacing(1)
    score_layout.setContentsMargins(8, 10, 8, 6)

    parent.ai_score_label = QLabel("得分: 0")
    parent.ai_score_label.setStyleSheet(
        "color: #f0c040; font-size: 18px; font-weight: bold; padding: 2px;")
    parent.ai_score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    score_layout.addWidget(parent.ai_score_label)

    parent.ai_arbitration_count_label = QLabel("仲裁次数: 0")
    parent.ai_arbitration_count_label.setStyleSheet("color: #999; font-size: 13px;")
    parent.ai_arbitration_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    score_layout.addWidget(parent.ai_arbitration_count_label)

    parent.ai_score_detail = QLabel("一致 +1 | 不一致 0")
    parent.ai_score_detail.setStyleSheet("color: #777; font-size: 13px;")
    parent.ai_score_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
    score_layout.addWidget(parent.ai_score_detail)

    layout.addWidget(score_group)

    layout.addStretch()
