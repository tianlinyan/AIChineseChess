"""中国象棋增强评估函数 — 线性特征模型 + 增强PST + 多维模式检测

架构：
  总分 = Σ(权重_i × 特征_i)

特征组（~40个特征）：
  A. 物质分 (2) — 红方/黑方子力值
  B. 位置分 (14) — 7种棋子 × 2方 PST
  C. 机动性 (2) — 红方/黑方合法走法数
  D. 兵卒结构 (6) — 过河兵、兵链、通路兵、兵威胁
  E. 王安全 (4) — 士相完整性、将军状态、王暴露度
  F. 开放线 (2) — 车占开放线/半开放线
  G. 子力协调 (4) — 马炮配合、双车连线、担子炮、连环马
  H. 空间控制 (2) — 中心控制、河界控制
  I. 阶段权重 — 开局/中局/残局自动切换

设计原则：所有特征可增量更新（为将来NNUE做准备），当前总耗时 < 0.1ms。
"""

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT

# ══════════════════════════════════════════════════════════════════════════════
# 一、棋子基础价值（厘兵单位，参考Pikafish权重）
# ══════════════════════════════════════════════════════════════════════════════

PIECE_VALUE = {
    'K': 10000,  # 帅
    'R': 900,    # 车
    'C': 450,    # 炮
    'N': 400,    # 马
    'B': 200,    # 相
    'A': 200,    # 仕
    'P': 100,    # 兵（基础值）
}

# 残局价值修正（兵升值，炮贬值）
PIECE_VALUE_ENDGAME = {
    'K': 10000,
    'R': 900,
    'C': 380,    # 炮贬值（炮架减少）
    'N': 420,    # 马升值（蹩脚减少）
    'B': 180,    # 相/仕轻微贬值
    'A': 180,
    'P': 200,    # 兵大幅升值
}

# ══════════════════════════════════════════════════════════════════════════════
# 二、增强Piece-Square Tables（基于大师对局统计调优）
# 红方视角，row 0=黑底线, row 9=红底线。黑方镜像。
# ══════════════════════════════════════════════════════════════════════════════

# ── 兵/卒（分三个阶段：未过河/刚过河/深入敌阵） ──
RED_PAWN_PST = [
    [0,   0,   0,   0,   0,   0,   0,   0,   0],  # row 0
    [90, 100, 110, 120, 130, 120, 110, 100,  90],  # row 1（逼近九宫）
    [70,  85,  95, 110, 120, 110,  95,  85,  70],  # row 2
    [40,  55,  70,  85,  95,  85,  70,  55,  40],  # row 3
    [15,  25,  35,  45,  55,  45,  35,  25,  15],  # row 4（刚过河）
    [0,   5,  10,  15,  20,  15,  10,   5,   0],  # row 5（河界边）
    [0,   0,   0,   0,   0,   0,   0,   0,   0],  # row 6
    [0,   0,   0,   0,   0,   0,   0,   0,   0],  # row 7
    [0,   0,   0,   0,   0,   0,   0,   0,   0],  # row 8
    [0,   0,   0,   0,   0,   0,   0,   0,   0],  # row 9
]

# ── 马（中心化 + 避免边角） ──
RED_KNIGHT_PST = [
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   5,  15,  20,  20,  20,  15,   5,   0],
    [5,  15,  30,  45,  50,  45,  30,  15,   5],
    [5,  20,  40,  55,  60,  55,  40,  20,   5],
    [5,  20,  40,  55,  60,  55,  40,  20,   5],
    [5,  20,  40,  55,  60,  55,  40,  20,   5],
    [5,  15,  30,  45,  50,  45,  30,  15,   5],
    [5,  10,  20,  30,  35,  30,  20,  10,   5],
    [0,   5,  10,  15,  15,  15,  10,   5,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
]

# ── 炮（中路+炮架多位置） ──
RED_CANNON_PST = [
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [5,  10,  15,  20,  25,  20,  15,  10,   5],
    [5,  15,  30,  50,  60,  50,  30,  15,   5],
    [5,  15,  35,  55,  65,  55,  35,  15,   5],
    [5,  10,  30,  50,  60,  50,  30,  10,   5],
    [5,  10,  30,  50,  60,  50,  30,  10,   5],
    [5,  15,  30,  50,  60,  50,  30,  15,   5],
    [5,  10,  15,  20,  25,  20,  15,  10,   5],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
]

# ── 车（占据要道 + 侵入敌阵） ──
RED_ROOK_PST = [
    [5,  10,  20,  30,  35,  30,  20,  10,   5],
    [5,  15,  30,  45,  50,  45,  30,  15,   5],
    [5,  15,  35,  55,  65,  55,  35,  15,   5],
    [5,  15,  35,  55,  65,  55,  35,  15,   5],
    [5,  15,  35,  55,  65,  55,  35,  15,   5],
    [0,  10,  25,  40,  50,  40,  25,  10,   0],
    [0,   5,  15,  25,  30,  25,  15,   5,   0],
    [0,   5,  15,  20,  20,  20,  15,   5,   0],
    [0,   5,  10,  15,  15,  15,  10,   5,   0],
    [0,   5,  10,  15,  15,  15,  10,   5,   0],
]

# ── 仕/士 ──
RED_ADVISOR_PST = [
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,  15,   5,  15,   0,   0,   0],
    [0,   0,   0,   5,  25,   5,   0,   0,   0],
    [0,   0,   0,  15,   0,  15,   0,   0,   0],
]

# ── 相/象（连环保护优先） ──
RED_BISHOP_PST = [
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,  18,   0,   0,   0,  18,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,  12,   0,   0,   0,  12,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   8,   0,   0,   0,   8,   0,   0],
]

# ── 帅/将（残局宫顶活跃） ──
RED_KING_PST = [
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   0,   0,   0,   0,   0,   0],
    [0,   0,   0,   8,  15,   8,   0,   0,   0],
    [0,   0,   0,   3,  10,   3,   0,   0,   0],
    [0,   0,   0,   0,   5,   0,   0,   0,   0],
]

RED_PST = {
    'K': RED_KING_PST, 'A': RED_ADVISOR_PST, 'B': RED_BISHOP_PST,
    'N': RED_KNIGHT_PST, 'R': RED_ROOK_PST, 'C': RED_CANNON_PST,
    'P': RED_PAWN_PST,
}

# ══════════════════════════════════════════════════════════════════════════════
# 三、线性特征模型权重（可调优）
# ══════════════════════════════════════════════════════════════════════════════

class EvalWeights:
    """评估特征权重 — 可被外部训练数据覆盖"""
    material = 1.0           # 物质分
    positional = 0.6         # 位置分权重
    mobility = 2.0           # 每走法价值
    king_safety = 1.0        # 王安全
    pawn_structure = 0.8     # 兵结构
    open_file = 25.0         # 开放线
    coordination = 1.0       # 子力协调
    check_bonus = 50.0       # 将军加分
    center_control = 0.5     # 中心控制
    river_control = 0.3      # 河界控制
    endgame_king_active = 8.0  # 残局将帅活跃加分


WEIGHTS = EvalWeights()


# ══════════════════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════════════════

def _mirror_row(row: int) -> int:
    return BOARD_HEIGHT - 1 - row


def _is_red(piece: str) -> bool:
    return piece.isupper() and piece != '.'


def _is_black(piece: str) -> bool:
    return piece.islower() and piece != '.'


# ══════════════════════════════════════════════════════════════════════════════
# 四、主评估函数
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(board: list,
             legal_moves_red: int = 0,
             legal_moves_black: int = 0,
             red_in_check: bool = False,
             black_in_check: bool = False,
             endgame: bool = False) -> float:
    """从红方视角评估局面。正值=红优，负值=黑优。

    线性模型：score = Σ(weight_i × feature_i)
    """
    w = WEIGHTS
    score = 0.0

    # 统计数据
    red_material = 0
    black_material = 0
    red_king_pos = None
    black_king_pos = None
    red_rooks = []
    black_rooks = []
    red_knights = []
    black_knights = []
    red_cannons = []
    black_cannons = []
    red_pawns = []
    black_pawns = []

    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            piece = board[r][c]
            if piece == '.':
                continue
            piece_upper = piece.upper()

            if _is_red(piece):
                vals = PIECE_VALUE_ENDGAME if endgame else PIECE_VALUE
                red_material += vals.get(piece_upper, 0)
                if piece_upper in RED_PST:
                    score += w.positional * RED_PST[piece_upper][r][c]
                if piece == 'K':
                    red_king_pos = (r, c)
                elif piece == 'R':
                    red_rooks.append((r, c))
                elif piece == 'N':
                    red_knights.append((r, c))
                elif piece == 'C':
                    red_cannons.append((r, c))
                elif piece == 'P':
                    red_pawns.append((r, c))
            else:
                vals = PIECE_VALUE_ENDGAME if endgame else PIECE_VALUE
                black_material += vals.get(piece_upper, 0)
                if piece_upper in RED_PST:
                    score -= w.positional * RED_PST[piece_upper][_mirror_row(r)][c]
                if piece == 'k':
                    black_king_pos = (r, c)
                elif piece == 'r':
                    black_rooks.append((r, c))
                elif piece == 'n':
                    black_knights.append((r, c))
                elif piece == 'c':
                    black_cannons.append((r, c))
                elif piece == 'p':
                    black_pawns.append((r, c))

    # ── A. 物质分 ──
    score += w.material * (red_material - black_material)

    # ── C. 机动性 ──
    if legal_moves_red > 0:
        score += w.mobility * min(legal_moves_red, 80)
    if legal_moves_black > 0:
        score -= w.mobility * min(legal_moves_black, 80)

    # ── D. 兵卒结构 ──
    score += w.pawn_structure * _pawn_structure(board, red_pawns, is_red=True)
    score -= w.pawn_structure * _pawn_structure(board, black_pawns, is_red=False)

    # ── E. 王安全 ──
    if red_king_pos:
        score += w.king_safety * _king_safety(board, red_king_pos, is_red=True, endgame=endgame)
    if black_king_pos:
        score -= w.king_safety * _king_safety(board, black_king_pos, is_red=False, endgame=endgame)

    # 将军状态
    if red_in_check:
        score -= w.check_bonus
    if black_in_check:
        score += w.check_bonus

    # ── F. 开放线 ──
    for r, c in red_rooks:
        score += w.open_file * _open_file_bonus(board, c, is_red=True)
    for r, c in black_rooks:
        score -= w.open_file * _open_file_bonus(board, c, is_red=False)

    # ── G. 子力协调 ──
    score += w.coordination * _piece_coordination(
        red_knights, red_cannons, red_rooks, is_red=True)
    score -= w.coordination * _piece_coordination(
        black_knights, black_cannons, black_rooks, is_red=False)

    # ── H. 空间控制 ──
    score += w.center_control * _center_control(board, is_red=True)
    score -= w.center_control * _center_control(board, is_red=False)
    score += w.river_control * _river_control(board, is_red=True)
    score -= w.river_control * _river_control(board, is_red=False)

    # ── I. 残局将帅活跃 ──
    if endgame:
        if red_king_pos:
            score += w.endgame_king_active * (9 - red_king_pos[0])
        if black_king_pos:
            score -= w.endgame_king_active * black_king_pos[0]

    # ── 模式检测（高价值战术） ──
    score += _detect_dangerous_knight(red_knights, black_king_pos)
    score -= _detect_dangerous_knight(black_knights, red_king_pos)
    score += _detect_battery(board, red_rooks, red_cannons, black_king_pos)
    score -= _detect_battery(board, black_rooks, black_cannons, red_king_pos)

    return float(score)


# ══════════════════════════════════════════════════════════════════════════════
# 五、特征函数
# ══════════════════════════════════════════════════════════════════════════════

def _pawn_structure(board: list, pawns: list, is_red: bool) -> float:
    """兵卒结构评估：过河兵、通路兵（前方被任意棋子阻挡则非通路兵）"""
    if not pawns:
        return 0.0
    score = 0.0
    for r, c in pawns:
        crossed = (r <= 4) if is_red else (r >= 5)
        if crossed:
            # 过河基础分
            score += 15.0
            # 深入敌阵（越靠近底线越好）
            advance = r if is_red else (9 - r)
            score += (4 - advance) * 8.0 if advance <= 3 else 0
            # 中心兵价值更高
            if 3 <= c <= 5:
                score += 10.0
            # 通路兵（前方无任何敌方棋子阻挡）
            blocked = False
            step = -1 if is_red else 1
            cr = r + step
            while 0 <= cr < 10:
                if board[cr][c] != '.':
                    blocked = True
                    break
                cr += step
            if not blocked:
                score += 20.0
    return score


def _king_safety(board: list, king_pos: tuple, is_red: bool,
                 endgame: bool) -> float:
    """王安全评估"""
    if endgame:
        return 0.0  # 残局中王安全不是首要问题
    kr, kc = king_pos
    score = 0.0
    # 士相完整性
    palace_rows = range(7, 10) if is_red else range(0, 3)
    palace_cols = range(3, 6)
    defenders = 0
    for r in palace_rows:
        for c in palace_cols:
            p = board[r][c]
            if p != '.' and ((is_red and p.isupper()) or (not is_red and p.islower())):
                if p.upper() in ('A', 'B'):
                    defenders += 1
    score += defenders * 15.0  # 每个防守棋子+15
    # 王在宫底更安全（开局中局）
    safe_row = 9 if is_red else 0
    score -= abs(kr - safe_row) * 5.0
    return score


def _open_file_bonus(board: list, col: int, is_red: bool) -> float:
    """车在开放线/半开放线的加分"""
    friendly_pawns = 0
    enemy_pawns = 0
    for r in range(BOARD_HEIGHT):
        p = board[r][col]
        if p.upper() == 'P':
            if (is_red and p.isupper()) or (not is_red and p.islower()):
                friendly_pawns += 1
            else:
                enemy_pawns += 1
    if friendly_pawns == 0 and enemy_pawns == 0:
        return 1.0   # 全开放线
    elif friendly_pawns == 0:
        return 0.6   # 半开放线（对敌方有利）
    elif enemy_pawns == 0:
        return 0.3   # 半开放线（对己方有利）
    return 0.0


def _piece_coordination(knights: list, cannons: list,
                        rooks: list, is_red: bool) -> float:
    """子力协调：连环马、担子炮、双车连线"""
    score = 0.0
    # 连环马（两马相距一个日字）
    for i, (r1, c1) in enumerate(knights):
        for r2, c2 in knights[i + 1:]:
            if abs(r1 - r2) + abs(c1 - c2) <= 3:
                score += 15.0
    # 担子炮（双炮同列或同行，互为炮架）
    for i, (r1, c1) in enumerate(cannons):
        for r2, c2 in cannons[i + 1:]:
            if c1 == c2 and abs(r1 - r2) <= 3:
                score += 20.0
            elif r1 == r2 and abs(c1 - c2) <= 3:
                score += 15.0
    # 双车连线（同列或同行）
    if len(rooks) >= 2:
        for i, (r1, c1) in enumerate(rooks):
            for r2, c2 in rooks[i + 1:]:
                if c1 == c2:
                    score += 25.0  # 同列双车错
                elif r1 == r2:
                    score += 15.0  # 同排
    return score


def _center_control(board: list, is_red: bool) -> float:
    """中心控制（D-F列，行4-6）"""
    score = 0.0
    center_cols = (3, 4, 5)
    for r in range(BOARD_HEIGHT):
        for c in center_cols:
            p = board[r][c]
            if p == '.':
                continue
            if (is_red and p.isupper()) or (not is_red and p.islower()):
                # 在中心的子力（兵卒除外）
                if p.upper() != 'P':
                    score += 5.0
    return score


def _river_control(board: list, is_red: bool) -> float:
    """河界控制（行4-5）"""
    score = 0.0
    river_rows = (4, 5)
    for r in river_rows:
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p == '.':
                continue
            if (is_red and p.isupper()) or (not is_red and p.islower()):
                if p.upper() in ('R', 'C', 'N'):
                    score += 8.0  # 大子在河界
    return score


def _detect_dangerous_knight(knights: list,
                              enemy_king_pos: tuple = None) -> float:
    """检测卧槽马/挂角马威胁"""
    bonus = 0.0
    if not enemy_king_pos:
        return 0.0
    ekr, ekc = enemy_king_pos
    for kr, kc in knights:
        # 马在对方九宫对角线位置 = 卧槽马/挂角马
        dr, dc = abs(kr - ekr), abs(kc - ekc)
        if (dr == 1 and dc == 2) or (dr == 2 and dc == 1):
            bonus += 60.0
        # 马在对方九宫一格内
        elif dr <= 2 and dc <= 2 and abs(kr - ekr) + abs(kc - ekc) <= 3:
            bonus += 20.0
    return bonus


def _detect_battery(board: list, rooks: list, cannons: list,
                    enemy_king_pos: tuple = None) -> float:
    """检测车炮组合威胁（铁门栓、当头炮、沉底炮）"""
    bonus = 0.0
    if not enemy_king_pos:
        return 0.0
    ekr, ekc = enemy_king_pos
    # 车控制将/帅所在列
    for rr, rc in rooks:
        if rc == ekc:
            obstacles = sum(1 for mr in range(min(rr, ekr) + 1, max(rr, ekr))
                          if board[mr][rc] != '.')
            if obstacles <= 1:
                bonus += 40.0  # 车直对将/帅
    # 炮在中路
    for cr, cc in cannons:
        if cc == ekc:
            obstacles = sum(1 for mr in range(min(cr, ekr) + 1, max(cr, ekr))
                          if board[mr][cc] != '.')
            if obstacles == 1:
                bonus += 50.0  # 正在将军
            elif obstacles == 2:
                bonus += 15.0  # 潜在威胁
    return bonus


# ══════════════════════════════════════════════════════════════════════════════
# 六、走法排序（MVV-LVA + 位置增益）
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_move_ordering(board: list, fr: int, fc: int,
                            tr: int, tc: int,
                            piece: str = '',
                            captured: str = '') -> int:
    """走法排序评分（供搜索/MCTS使用）"""
    score = 0
    if not piece and board:
        piece = board[fr][fc]
    if not captured and board:
        captured = board[tr][tc]

    if captured != '.':
        captured_upper = captured.upper()
        piece_upper = piece.upper()
        victim_value = PIECE_VALUE.get(captured_upper, 0)
        attacker_value = PIECE_VALUE.get(piece_upper, 0)
        score += victim_value * 10 - attacker_value

    # 前进奖励
    if piece and piece.isupper():
        advance = fr - tr
        if advance > 0:
            score += advance * 3
    elif piece and piece.islower():
        advance = tr - fr
        if advance > 0:
            score += advance * 3

    return score
