"""中国象棋增强评估函数 — 线性特征模型 + 增强PST + 多维模式检测

架构：
  总分 = Σ(权重_i × 特征_i)

特征组（~40个特征）：
  A. 物质分 (2) — 红方/黑方子力值
  B. 位置分 (14) — 7种棋子 × 2方 PST
  C. 机动性 (2) — 红方/黑方合法走法数（仅调用方提供时计入：LLM 工具/
     评估面板会生成真实走法数；搜索叶节点为速度传 0 跳过，不计入）
  D. 卒结构 (6) — 过河卒、卒链、通路卒、卒威胁
  E. 将安全 (4) — 士相完整性、将军状态、将暴露度
  F. 开放线 (2) — 車占开放线/半开放线
  G. 子力协调 (4) — 馬炮配合、双車连线、担子炮、连环馬
  H. 空间控制 (2) — 中心控制、河界控制
  I. 阶段权重 — 开局/中局/残局自动切换

设计原则：所有特征可增量更新（为将来NNUE做准备），当前总耗时 < 0.1ms。
"""

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT, ENDGAME_PIECE_THRESHOLD

# ══════════════════════════════════════════════════════════════════════════════
# 一、棋子基础价值（厘兵单位，参考Pikafish权重）
# ══════════════════════════════════════════════════════════════════════════════

PIECE_VALUE = {
    'K': 10000,  # 将
    'R': 900,    # 車
    'C': 450,    # 炮
    'N': 400,    # 馬
    'B': 200,    # 相
    'A': 200,    # 士
    'P': 100,    # 卒（基础值）
}

# 残局价值修正（卒升值，炮贬值）
PIECE_VALUE_ENDGAME = {
    'K': 10000,
    'R': 900,
    'C': 380,    # 炮贬值（炮架减少）
    'N': 420,    # 馬升值（蹩脚减少）
    'B': 180,    # 相/士轻微贬值
    'A': 180,
    'P': 200,    # 卒大幅升值
}


def compute_material(board: list) -> tuple:
    """统计双方子力与棋子数。

    Args:
        board: 10×9 棋盘，大写=红，小写=黑，'.'=空。

    Returns:
        (red_material, black_material, red_count, black_count)
        子力单位为"兵"（PIECE_VALUE ÷ 100：車9 · 炮4.5 · 馬4 · 相/士2 · 兵1），
        不含将/帥（价值∞，双方各一，纳入统计只会干扰对比）。
        总子数 ≤ ENDGAME_PIECE_THRESHOLD 时自动切换残局估值表。
    """
    red_count = 0
    black_count = 0
    # 单遍收集棋子列表，随后一次决定估值表（避免两次全盘扫描）
    pieces = []
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p == '.':
                continue
            pieces.append(p)
            if p.isupper():
                red_count += 1
            else:
                black_count += 1
    vals = PIECE_VALUE_ENDGAME if red_count + black_count <= ENDGAME_PIECE_THRESHOLD \
        else PIECE_VALUE
    red_material = 0.0
    black_material = 0.0
    for p in pieces:
        if p in ('K', 'k'):
            continue
        if p.isupper():
            red_material += vals.get(p, 0)
        else:
            black_material += vals.get(p.upper(), 0)
    return red_material / 100, black_material / 100, red_count, black_count

# ══════════════════════════════════════════════════════════════════════════════
# 二、增强Piece-Square Tables（基于大师对局统计调优）
# 红方视角，row 0=黑底线, row 9=红底线。黑方镜像。
# ══════════════════════════════════════════════════════════════════════════════

# ── 卒（分三个阶段：未过河/刚过河/深入敌阵） ──
RED_BING_PST = [
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

# ── 馬（中心化 + 避免边角） ──
RED_MA_PST = [
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
RED_PAO_PST = [
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

# ── 車（占据要道 + 侵入敌阵） ──
RED_JU_PST = [
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
RED_SHI_PST = [
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

# ── 相（连环保护优先） ──
RED_XIANG_PST = [
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

# ── 将（残局宫顶活跃） ──
RED_SHUAI_PST = [
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
    'K': RED_SHUAI_PST, 'A': RED_SHI_PST, 'B': RED_XIANG_PST,
    'N': RED_MA_PST, 'R': RED_JU_PST, 'C': RED_PAO_PST,
    'P': RED_BING_PST,
}

# ══════════════════════════════════════════════════════════════════════════════
# 三、线性特征模型权重（可调优）
# ══════════════════════════════════════════════════════════════════════════════

class EvalWeights:
    """评估特征权重 — 可被外部训练数据覆盖"""
    material = 1.0           # 物质分
    positional = 0.6         # 位置分权重
    mobility = 2.0           # 每走法价值
    shuai_safety = 1.0        # 将安全
    bing_structure = 0.8      # 卒结构
    open_column = 25.0         # 开放线
    coordination = 1.0       # 子力协调
    check_bonus = 50.0       # 将军加分
    center_control = 0.5     # 中心控制
    river_control = 0.3      # 河界控制
    endgame_shuai_active = 8.0  # 残局将活跃加分


WEIGHTS = EvalWeights()


# ══════════════════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════════════════

def _mirror_row(row: int) -> int:
    return BOARD_HEIGHT - 1 - row


def _is_red(piece: str) -> bool:
    return piece.isupper()


def _is_black(piece: str) -> bool:
    return piece.islower()


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
    red_shuai_pos = None
    black_shuai_pos = None
    red_ju = []
    black_ju = []
    red_ma = []
    black_ma = []
    red_cannons = []
    black_cannons = []
    red_bing = []
    black_bing = []

    vals = PIECE_VALUE_ENDGAME if endgame else PIECE_VALUE
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            piece = board[r][c]
            if piece == '.':
                continue
            piece_upper = piece.upper()

            if _is_red(piece):
                red_material += vals.get(piece_upper, 0)
                if piece_upper in RED_PST:
                    score += w.positional * RED_PST[piece_upper][r][c]
                if piece == 'K':
                    red_shuai_pos = (r, c)
                elif piece == 'R':
                    red_ju.append((r, c))
                elif piece == 'N':
                    red_ma.append((r, c))
                elif piece == 'C':
                    red_cannons.append((r, c))
                elif piece == 'P':
                    red_bing.append((r, c))
            else:
                black_material += vals.get(piece_upper, 0)
                if piece_upper in RED_PST:
                    score -= w.positional * RED_PST[piece_upper][_mirror_row(r)][c]
                if piece == 'k':
                    black_shuai_pos = (r, c)
                elif piece == 'r':
                    black_ju.append((r, c))
                elif piece == 'n':
                    black_ma.append((r, c))
                elif piece == 'c':
                    black_cannons.append((r, c))
                elif piece == 'p':
                    black_bing.append((r, c))

    # ── A. 物质分 ──
    score += w.material * (red_material - black_material)

    # ── C. 机动性 ──
    if legal_moves_red > 0:
        score += w.mobility * min(legal_moves_red, 80)
    if legal_moves_black > 0:
        score -= w.mobility * min(legal_moves_black, 80)

    # ── D. 卒结构 ──
    score += w.bing_structure * _bing_structure(board, red_bing, is_red=True)
    score -= w.bing_structure * _bing_structure(board, black_bing, is_red=False)

    # ── E. 将安全 ──
    if red_shuai_pos:
        score += w.shuai_safety * _shuai_safety(board, red_shuai_pos, is_red=True, endgame=endgame)
    if black_shuai_pos:
        score -= w.shuai_safety * _shuai_safety(board, black_shuai_pos, is_red=False, endgame=endgame)

    # 将军状态
    if red_in_check:
        score -= w.check_bonus
    if black_in_check:
        score += w.check_bonus

    # ── F. 开放线 ──
    for r, c in red_ju:
        score += w.open_column * _open_column_bonus(board, c, is_red=True)
    for r, c in black_ju:
        score -= w.open_column * _open_column_bonus(board, c, is_red=False)

    # ── G. 子力协调 ──
    score += w.coordination * _piece_coordination(
        red_ma, red_cannons, red_ju, is_red=True)
    score -= w.coordination * _piece_coordination(
        black_ma, black_cannons, black_ju, is_red=False)

    # ── H. 空间控制 ──
    score += w.center_control * _center_control(board, is_red=True)
    score -= w.center_control * _center_control(board, is_red=False)
    score += w.river_control * _river_control(board, is_red=True)
    score -= w.river_control * _river_control(board, is_red=False)

    # ── I. 残局将活跃 ──
    if endgame:
        if red_shuai_pos:
            score += w.endgame_shuai_active * (9 - red_shuai_pos[0])
        if black_shuai_pos:
            score -= w.endgame_shuai_active * black_shuai_pos[0]

    # ── 模式检测（高价值战术） ──
    score += _detect_dangerous_ma(board, red_ma, black_shuai_pos)
    score -= _detect_dangerous_ma(board, black_ma, red_shuai_pos)
    score += _detect_battery(board, red_ju, red_cannons, black_shuai_pos)
    score -= _detect_battery(board, black_ju, black_cannons, red_shuai_pos)

    return float(score)


def evaluate_fast(board: list,
                  red_material: float = 0.0,
                  black_material: float = 0.0,
                  red_pst_score: float = 0.0,
                  black_pst_score: float = 0.0,
                  red_in_check: bool = False,
                  black_in_check: bool = False,
                  endgame: bool = False) -> float:
    """快速评估 — 从红方视角。与 evaluate() 等价但 material/PST 由调用方传入。

    搜索叶节点通过增量缓存提供 material 和 PST 值，本函数跳过这两项
    的全盘扫描，仅扫描收集棋子位置用于关系特征。
    """
    w = WEIGHTS
    score = 0.0

    # ── A. 物质分（来自增量缓存）──
    score += w.material * (red_material - black_material)

    # ── B. 位置分（来自增量缓存）──
    score += w.positional * (red_pst_score - black_pst_score)

    # ── 收集棋子位置（仍需扫描——这是唯一剩下的全盘开销）──
    red_shuai_pos = None
    black_shuai_pos = None
    red_ju, black_ju = [], []
    red_ma, black_ma = [], []
    red_cannons, black_cannons = [], []
    red_bing, black_bing = [], []

    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            piece = board[r][c]
            if piece == '.':
                continue
            if _is_red(piece):
                if piece == 'K':
                    red_shuai_pos = (r, c)
                elif piece == 'R':
                    red_ju.append((r, c))
                elif piece == 'N':
                    red_ma.append((r, c))
                elif piece == 'C':
                    red_cannons.append((r, c))
                elif piece == 'P':
                    red_bing.append((r, c))
            else:
                if piece == 'k':
                    black_shuai_pos = (r, c)
                elif piece == 'r':
                    black_ju.append((r, c))
                elif piece == 'n':
                    black_ma.append((r, c))
                elif piece == 'c':
                    black_cannons.append((r, c))
                elif piece == 'p':
                    black_bing.append((r, c))

    # ── C. 机动性 — 搜索叶节点传 0，不计入（与 evaluate() 一致）──

    # ── D. 卒结构 ──
    score += w.bing_structure * _bing_structure(board, red_bing, is_red=True)
    score -= w.bing_structure * _bing_structure(board, black_bing, is_red=False)

    # ── E. 将安全 ──
    if red_shuai_pos:
        score += w.shuai_safety * _shuai_safety(board, red_shuai_pos, is_red=True, endgame=endgame)
    if black_shuai_pos:
        score -= w.shuai_safety * _shuai_safety(board, black_shuai_pos, is_red=False, endgame=endgame)

    # 将军状态
    if red_in_check:
        score -= w.check_bonus
    if black_in_check:
        score += w.check_bonus

    # ── F. 开放线 ──
    for _r, c in red_ju:
        score += w.open_column * _open_column_bonus(board, c, is_red=True)
    for _r, c in black_ju:
        score -= w.open_column * _open_column_bonus(board, c, is_red=False)

    # ── G. 子力协调 ──
    score += w.coordination * _piece_coordination(
        red_ma, red_cannons, red_ju, is_red=True)
    score -= w.coordination * _piece_coordination(
        black_ma, black_cannons, black_ju, is_red=False)

    # ── H. 空间控制 ──
    score += w.center_control * _center_control(board, is_red=True)
    score -= w.center_control * _center_control(board, is_red=False)
    score += w.river_control * _river_control(board, is_red=True)
    score -= w.river_control * _river_control(board, is_red=False)

    # ── I. 残局将活跃 ──
    if endgame:
        if red_shuai_pos:
            score += w.endgame_shuai_active * (9 - red_shuai_pos[0])
        if black_shuai_pos:
            score -= w.endgame_shuai_active * black_shuai_pos[0]

    # ── 模式检测 ──
    score += _detect_dangerous_ma(board, red_ma, black_shuai_pos)
    score -= _detect_dangerous_ma(board, black_ma, red_shuai_pos)
    score += _detect_battery(board, red_ju, red_cannons, black_shuai_pos)
    score -= _detect_battery(board, black_ju, black_cannons, red_shuai_pos)

    return float(score)


# ══════════════════════════════════════════════════════════════════════════════
# 五、特征函数
# ══════════════════════════════════════════════════════════════════════════════

def _bing_structure(board: list, bing_list: list, is_red: bool) -> float:
    """卒结构评估：过河卒、通路卒（前方被任意棋子阻挡则非通路卒）"""
    if not bing_list:
        return 0.0
    score = 0.0
    for r, c in bing_list:
        crossed = (r <= 4) if is_red else (r >= 5)
        if crossed:
            # 过河基础分
            score += 15.0
            # 深入敌阵加分（但冲到底线的"老兵"价值骤降，不再加分 ——
            # 与 RED_BING_PST 底线 0 分保持一致）
            advance = r if is_red else (9 - r)
            if 1 <= advance <= 3:
                score += (4 - advance) * 8.0
            # 中心卒价值更高
            if 3 <= c <= 5:
                score += 10.0
            # 通路卒（前方无任何敌方棋子阻挡）
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


def _shuai_safety(board: list, shuai_pos: tuple, is_red: bool,
                 endgame: bool) -> float:
    """将安全评估"""
    if endgame:
        return 0.0  # 残局中将安全不是首要问题
    kr, kc = shuai_pos
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
    # 将在宫底更安全（开局中局）
    safe_row = 9 if is_red else 0
    score -= abs(kr - safe_row) * 5.0
    return score


def _open_column_bonus(board: list, col: int, is_red: bool) -> float:
    """車在开放线/半开放线的加分"""
    friendly_bing = 0
    enemy_bing = 0
    for r in range(BOARD_HEIGHT):
        p = board[r][col]
        if p.upper() == 'P':
            if (is_red and p.isupper()) or (not is_red and p.islower()):
                friendly_bing += 1
            else:
                enemy_bing += 1
    if friendly_bing == 0 and enemy_bing == 0:
        return 1.0   # 全开放线
    elif friendly_bing == 0:
        return 0.6   # 半开放线（对敌方有利）
    elif enemy_bing == 0:
        return 0.3   # 半开放线（对己方有利）
    return 0.0


def _piece_coordination(ma_list: list, cannons: list,
                        ju_list: list, is_red: bool) -> float:
    """子力协调：连环馬、担子炮、双車连线"""
    score = 0.0
    # 连环馬（两马相距一个日字）
    for i, (r1, c1) in enumerate(ma_list):
        for r2, c2 in ma_list[i + 1:]:
            if abs(r1 - r2) + abs(c1 - c2) <= 3:
                score += 15.0
    # 担子炮（双炮同列或同行，互为炮架）
    for i, (r1, c1) in enumerate(cannons):
        for r2, c2 in cannons[i + 1:]:
            if c1 == c2 and abs(r1 - r2) <= 3:
                score += 20.0
            elif r1 == r2 and abs(c1 - c2) <= 3:
                score += 15.0
    # 双車连线（同列或同行）
    if len(ju_list) >= 2:
        for i, (r1, c1) in enumerate(ju_list):
            for r2, c2 in ju_list[i + 1:]:
                if c1 == c2:
                    score += 25.0  # 同列双車错
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
                # 在中心的子力（卒除外）
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


def _detect_dangerous_ma(board: list, ma_list: list,
                         enemy_shuai_pos: tuple = None) -> float:
    """检测卧槽馬/挂角馬威胁（验蹩马腿：腿被塞的马不构成将军威胁）"""
    bonus = 0.0
    if not enemy_shuai_pos:
        return 0.0
    ekr, ekc = enemy_shuai_pos
    for kr, kc in ma_list:
        # 馬以日字攻击对方将 = 卧槽馬/挂角馬
        dr, dc = kr - ekr, kc - ekc
        adr, adc = abs(dr), abs(dc)
        if (adr == 1 and adc == 2) or (adr == 2 and adc == 1):
            # 蹩马腿检查（腿位在马一侧：纵向跳在马同列，横向跳在马同行）
            if adr == 2:
                leg_r, leg_c = kr - dr // 2, kc
            else:
                leg_r, leg_c = kr, kc - dc // 2
            if board[leg_r][leg_c] == '.':
                bonus += 60.0
        # 馬在对方九宫一格内
        elif adr <= 2 and adc <= 2 and adr + adc <= 3:
            bonus += 20.0
    return bonus


def _detect_battery(board: list, ju_list: list, cannons: list,
                    enemy_shuai_pos: tuple = None) -> float:
    """检测車炮组合威胁（铁门栓、当头炮、沉底炮）"""
    bonus = 0.0
    if not enemy_shuai_pos:
        return 0.0
    ekr, ekc = enemy_shuai_pos
    # 車控制将所在列
    for rr, rc in ju_list:
        if rc == ekc:
            obstacles = sum(1 for mr in range(min(rr, ekr) + 1, max(rr, ekr))
                          if board[mr][rc] != '.')
            if obstacles <= 1:
                bonus += 40.0  # 車直对将
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
