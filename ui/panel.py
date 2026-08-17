"""左侧面板 UI 构建"""

import functools

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QPushButton, QCheckBox,
    QLabel, QGroupBox, QSpinBox, QFrame,
)

from domain.constants import SEARCH_MAX_DEPTH, DEFAULT_SEARCH_DEPTH, OPENING_BOOK_ENABLED
from ui.theme import PANEL_BG_STYLE


def _section_label(text: str, color: str = "#8ec8e8") -> QLabel:
    """统一的小节标题样式（红/黑方标题可传不同颜色）。"""
    label = QLabel(text)
    label.setStyleSheet(
        f"font-size: 13px; font-weight: bold; color: {color}; "
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


def _spacer(height: int) -> QFrame:
    """垂直空白间距。"""
    sp = QFrame()
    sp.setFixedHeight(height)
    sp.setStyleSheet("background: transparent; border: none;")
    return sp


def _section_gap(layout, top: int = 2) -> None:
    """小节间隔：上间距 + 分隔线 + 下间距（多处布局共用）。"""
    layout.addWidget(_spacer(top))
    layout.addWidget(_h_separator())
    layout.addWidget(_spacer(4))


def _build_model_combo(parent, layout, attr: str) -> None:
    """构建模型下拉（红/黑各一），控件对象名 parent.{attr}。"""
    combo = QComboBox()
    combo.setMinimumHeight(26)
    combo.setStyleSheet(_combo_style())
    combo.currentIndexChanged.connect(parent._on_model_changed)
    setattr(parent, attr, combo)
    layout.addWidget(combo)


def _group_style(title_color: str = "#7eb8da") -> str:
    """统一的 QGroupBox 样式。"""
    return (
        "QGroupBox {"
        "  font-weight: bold; font-size: 12px; color: #c0c0c0;"
        "  background-color: #1e1e22;"
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
        "  background-color: #252528; color: #d0d0d0;"
        "  border: 1px solid #444; border-radius: 3px;"
        "  padding: 2px 6px; font-size: 13px;"
        "}"
        "QComboBox:hover { border-color: #666; }"
        "QComboBox::drop-down { border: none; width: 18px; }"
        "QComboBox QAbstractItemView {"
        "  background-color: #252528; color: #d0d0d0;"
        "  selection-background-color: #3a5070; border: 1px solid #444;"
        "}"
    )


def _spinbox_style() -> str:
    """数字输入框统一样式。"""
    return (
        "QSpinBox {"
        "  background-color: #252528; color: #d0d0d0;"
        "  border: 1px solid #444; border-radius: 3px;"
        "  padding-top: 2px; padding-bottom: 2px;"
        "  padding-left: 4px; padding-right: 22px;"
        "  font-size: 13px; min-width: 60px;"
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


BTN_PRIMARY_STYLE = (
    "QPushButton {"
    "  background-color: #3a7a4a; color: white; border: none;"
    "  border-radius: 4px; padding: 6px; font-size: 13px; font-weight: bold;"
    "}"
    "QPushButton:hover { background-color: #4a9a5a; }"
    "QPushButton:pressed { background-color: #2a6a3a; }"
    "QPushButton:disabled { background-color: #333; color: #666; }"
)

BTN_DANGER_STYLE = (
    "QPushButton {"
    "  background-color: #8a3a3a; color: white; border: none;"
    "  border-radius: 4px; padding: 5px; font-size: 13px;"
    "}"
    "QPushButton:hover { background-color: #a54a4a; }"
    "QPushButton:pressed { background-color: #6a2a2a; }"
    "QPushButton:disabled { background-color: #333; color: #666; }"
)

BTN_DEFAULT_STYLE = (
    "QPushButton {"
    "  background-color: #3a3a40; color: #c0c0c0; border: 1px solid #555;"
    "  border-radius: 4px; padding: 5px; font-size: 13px;"
    "}"
    "QPushButton:hover { background-color: #4a4a50; border-color: #777; }"
    "QPushButton:pressed { background-color: #2a2a30; }"
    "QPushButton:disabled { background-color: #333; color: #666; border-color: #333; }"
)


def _build_engine_group(parent, side: str, label_color: str,
                        title_color: str) -> QGroupBox:
    """构建一方引擎组：模式下拉、搜索深度、开局库。

    控件对象名按 side 生成（parent.{side}_ai_mode_combo 等），
    信号经 functools.partial 绑定 side 传给 MainWindow 的合并槽。
    """
    engine = QGroupBox(f"{'红方' if side == 'red' else '黑方'}引擎")
    engine.setStyleSheet(_group_style(title_color))
    grid = QGridLayout(engine)
    grid.setVerticalSpacing(5)
    grid.setHorizontalSpacing(6)
    grid.setContentsMargins(8, 10, 8, 6)

    lbl = QLabel("模式:")
    lbl.setStyleSheet(f"color: {label_color}; font-size: 13px;")
    grid.addWidget(lbl, 0, 0)
    ai_mode_combo = QComboBox()
    ai_mode_combo.addItem("AI + 搜索", "hybrid")
    ai_mode_combo.addItem("仅搜索", "search_only")
    ai_mode_combo.addItem("仅 AI", "llm_only")
    ai_mode_combo.setCurrentIndex(0)
    ai_mode_combo.setMinimumHeight(26)
    ai_mode_combo.setStyleSheet(_combo_style())
    ai_mode_combo.currentIndexChanged.connect(
        functools.partial(parent._on_ai_mode_changed, side=side))
    setattr(parent, f'{side}_ai_mode_combo', ai_mode_combo)
    grid.addWidget(ai_mode_combo, 0, 1)

    dlbl = QLabel("深度:")
    dlbl.setStyleSheet(f"color: {label_color}; font-size: 13px;")
    grid.addWidget(dlbl, 1, 0)
    search_depth_spin = QSpinBox()
    search_depth_spin.setRange(1, SEARCH_MAX_DEPTH)
    search_depth_spin.setMinimumHeight(26)
    search_depth_spin.setStyleSheet(_spinbox_style())
    search_depth_spin.setToolTip(
        f"{'红方' if side == 'red' else '黑方'}搜索强度 1~{SEARCH_MAX_DEPTH}")
    search_depth_spin.valueChanged.connect(
        functools.partial(parent._on_search_depth_changed, side=side))
    search_depth_spin.setValue(DEFAULT_SEARCH_DEPTH)  # connect 之后设值，确保信号送达
    setattr(parent, f'{side}_search_depth_spin', search_depth_spin)
    grid.addWidget(search_depth_spin, 1, 1)

    opening_book_check = QCheckBox("开局库")
    opening_book_check.setStyleSheet(_check_style())
    opening_book_check.stateChanged.connect(
        functools.partial(parent._on_opening_book_changed, side=side))
    # connect 之后设值：初始状态经信号同步到 controller（与 spinbox 的
    # 做法一致），并跟随 OPENING_BOOK_ENABLED 常量，避免 UI 默认勾选与
    # controller 默认关闭不一致
    opening_book_check.setChecked(OPENING_BOOK_ENABLED)
    setattr(parent, f'{side}_opening_book_check', opening_book_check)
    grid.addWidget(opening_book_check, 1, 2)

    return engine


# ══════════════════════════════════════════════════════════════════════════════
# 主构建函数
# ══════════════════════════════════════════════════════════════════════════════


def setup_left_expanded(parent) -> None:
    """在 parent (MainWindow) 上构建左侧展开面板的所有控件"""
    parent.left_expanded.setStyleSheet(PANEL_BG_STYLE)

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
    _section_gap(layout)

    # ══════════════════════════════════════════════════════════════
    # 🔴 红方
    # ══════════════════════════════════════════════════════════════
    layout.addWidget(_section_label("🔴 红方（先手）"))
    _build_model_combo(parent, layout, 'model1_combo')

    layout.addWidget(_spacer(2))

    layout.addWidget(_build_engine_group(
        parent, 'red', '#c0a0a0', '#e06060'))

    _section_gap(layout)

    # ══════════════════════════════════════════════════════════════
    # ⚫ 黑方
    # ══════════════════════════════════════════════════════════════
    # ⚫ 黑方（标题色与思考日志黑方一致 #61afef）
    layout.addWidget(_section_label("⚫ 黑方（后手）", "#61afef"))
    _build_model_combo(parent, layout, 'model2_combo')

    layout.addWidget(_spacer(2))

    layout.addWidget(_build_engine_group(
        parent, 'black', '#61afef', '#61afef'))

    _section_gap(layout, 6)

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

    # 显示 AI 思考过程（reasoning_content 推理文本）：默认不勾选
    # （日志只显示正式回复；勾选后连推理一起显示，便于复盘）
    parent.show_think_check = QCheckBox("显示 AI 思考过程（含推理）")
    parent.show_think_check.setChecked(False)
    parent.show_think_check.setStyleSheet(_check_style())
    parent.show_think_check.setToolTip(
        "勾选后思考日志显示模型的推理过程（reasoning_content）；"
        "默认关闭，只显示正式回复。")
    ctrl_layout.addWidget(parent.show_think_check)

    # AI点评（仅搜索/人类）：人类落子或 AI 以"仅搜索"模式落子完成后，
    # 由 AI点评该步棋；解析完成前对局暂停等待（默认不勾选）
    parent.ai_commentary_check = QCheckBox("AI点评（仅搜索/人类）")
    parent.ai_commentary_check.setChecked(False)
    parent.ai_commentary_check.setStyleSheet(_check_style())
    parent.ai_commentary_check.setToolTip(
        "勾选后：人类落子、或 AI 以\"仅搜索\"模式落子完成后，"
        "AI 将解析该步棋（本步评析 / 双方棋势分析 / 其他招式）；"
        "解析完成前，对局暂停等待，方可进行下一步。")
    ctrl_layout.addWidget(parent.ai_commentary_check)

    layout.addWidget(ctrl_group)

    layout.addWidget(_spacer(6))

    # ══════════════════════════════════════════════════════════════
    # 控制按钮
    # ══════════════════════════════════════════════════════════════
    btn_layout = QVBoxLayout()
    btn_layout.setSpacing(4)

    parent.start_btn = QPushButton("▶  开始对弈")
    parent.start_btn.setMinimumHeight(30)
    parent.start_btn.setStyleSheet(BTN_PRIMARY_STYLE)
    # clicked(bool) 会给槽传一个多余参数；用 lambda 屏蔽，避免未来
    # start_game 加参时因签名不匹配直接 TypeError
    parent.start_btn.clicked.connect(
        lambda: parent.game_controller.start_game())
    btn_layout.addWidget(parent.start_btn)

    parent.pause_btn = QPushButton("⏸  暂停")
    parent.pause_btn.setMinimumHeight(28)
    parent.pause_btn.setEnabled(False)
    parent.pause_btn.setStyleSheet(BTN_DEFAULT_STYLE)
    parent.pause_btn.clicked.connect(parent._on_pause_clicked)
    btn_layout.addWidget(parent.pause_btn)

    parent.reset_btn = QPushButton("⏹  停止游戏")
    parent.reset_btn.setMinimumHeight(28)
    parent.reset_btn.setEnabled(False)
    parent.reset_btn.setStyleSheet(BTN_DANGER_STYLE)
    parent.reset_btn.clicked.connect(parent._on_reset_clicked)
    btn_layout.addWidget(parent.reset_btn)

    layout.addLayout(btn_layout)

    _section_gap(layout, 6)

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
        "color: #61afef; font-size: 13px; font-weight: bold;")
    row1.addWidget(parent.black_material_label)
    status_layout.addLayout(row1)

    row2 = QHBoxLayout()
    row2.setSpacing(8)
    parent.red_pieces_label = QLabel("红子: —")
    parent.red_pieces_label.setStyleSheet("color: #d06060; font-size: 13px;")
    row2.addWidget(parent.red_pieces_label)
    parent.black_pieces_label = QLabel("黑子: —")
    parent.black_pieces_label.setStyleSheet("color: #61afef; font-size: 13px;")
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
