"""本地 DTM（Distance to Mate）残局库 — 回溯分析生成 + 紧凑存储

算法：
1. 枚举给定棋子集合的所有合法局面
2. 标记将杀死局面（DTM=0）
3. 反向迭代：对 DTM=N 的局面，找出所有可一步到达此局面的前驱局面
   - 攻击方的前驱：DTM = N+1（minimax：攻击方选最小 DTM 的走法）
   - 防守方的前驱：DTM 记录为"可走到当前局面的候选"
4. 重复直至收敛

存储格式（.dtm 文件）：
- 8 字节头：魔数 + 棋子数量 + 棋子集合签名
- 数据区：每个位置 1 字节
  - 0：未知/和棋
  - 1-127：当前走子方在 N 步内可杀（N = 0 即被将杀）
  - 129-255：当前走子方将在 N-128 步内被杀（N = 0 即杀对方）

Usage:
    from domain.egtb_local import DtmTable, probe_local
    dtm = probe_local(board, piece_count)  # 返回 (score, dtm) 或 None
"""

import os
import struct
import itertools
from typing import Optional, Tuple, Dict, Set, List

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT

# ── 常量 ──
DTM_MAGIC = b'DTMC\x01'    # DTM Chinese Chess v1
DTM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'egtb')
MAX_DTM = 126               # 最大 DTM 存储值
DRAW_VALUE = 0              # 和棋 / 未知

# 攻击子力类型（不含将/帥和防守子力士/相）
ATTACKER_TYPES = 'RNCP'     # 車馬炮兵
ATTACKER_TYPES_LOWER = 'rncp'

# 棋子值（用于排序）
_PIECE_ORDER = {'K': 0, 'k': 0, 'R': 1, 'r': 1, 'C': 2, 'c': 2,
                'N': 3, 'n': 3, 'P': 4, 'p': 4, 'A': 5, 'a': 5,
                'B': 6, 'b': 6}


def _piece_set_key(pieces: tuple) -> str:
    """棋子集合 → 文件名键，如 'KRk' → 红帅+红車+黑将"""
    return ''.join(sorted(pieces, key=lambda p: _PIECE_ORDER.get(p, 99)))


def _board_to_index(board: list) -> int:
    """将棋盘编码为整数索引（仅非空位置）。"""
    # 对指定的棋子集合，每个棋子的位置编码
    # 这里简化：用 Zobrist 风格的哈希
    h = 0
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p != '.':
                idx = r * BOARD_WIDTH + c
                h = h * 137 + idx * 31 + ord(p)
    return h & 0x7FFFFFFF


def _is_king_facing(board: list) -> bool:
    """检查将帅是否对面。"""
    # 找到两将
    red_king = black_king = None
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            if board[r][c] == 'K':
                red_king = (r, c)
            elif board[r][c] == 'k':
                black_king = (r, c)
    if not red_king or not black_king:
        return False
    if red_king[1] != black_king[1]:
        return False
    # 检查中间是否有遮挡
    min_r, max_r = min(red_king[0], black_king[0]), max(red_king[0], black_king[0])
    for r in range(min_r + 1, max_r):
        if board[r][red_king[1]] != '.':
            return False
    return True


def _is_in_check(board: list, player: int) -> bool:
    """简化将军检测（仅用于残局生成，不依赖 ChineseChessGame）。"""
    # 找到己方将
    king_char = 'K' if player == 1 else 'k'
    kr = kc = None
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            if board[r][c] == king_char:
                kr, kc = r, c
                break
        if kr is not None:
            break
    if kr is None:
        return False

    opponent_pieces = 'rnbcakp' if player == 1 else 'RNBCAP'
    # 简化检测：对方車/炮直线攻击，馬日字攻击，卒攻击
    # 車/炮（含帅对面）
    for dr, dc in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        r, c = kr + dr, kc + dc
        blocked = False
        while 0 <= r < BOARD_HEIGHT and 0 <= c < BOARD_WIDTH:
            p = board[r][c]
            if p == '.':
                r += dr; c += dc; continue
            if p in opponent_pieces:
                if p.upper() == 'R' or (p.upper() == 'K' and not blocked):
                    return True
                if p.upper() == 'C' and blocked:
                    return True
            blocked = True
            r += dr; c += dc

    # 馬
    for dr, dc in [(2,1),(2,-1),(-2,1),(-2,-1),(1,2),(1,-2),(-1,2),(-1,-2)]:
        r, c = kr + dr, kc + dc
        if 0 <= r < BOARD_HEIGHT and 0 <= c < BOARD_WIDTH:
            if board[r][c] in opponent_pieces and board[r][c].upper() == 'N':
                leg_r = kr + (dr // abs(dr) if dr != 0 else 0) if abs(dr) == 2 else kr
                leg_c = kc + (dc // abs(dc) if dc != 0 else 0) if abs(dc) == 2 else kc
                if abs(dr) == 2:
                    leg_r = kr + (1 if dr > 0 else -1)
                    leg_c = kc
                else:
                    leg_r = kr
                    leg_c = kc + (1 if dc > 0 else -1)
                if board[leg_r][leg_c] == '.':
                    return True

    # 兵/卒
    if player == 1:  # 红方，黑卒从下方攻击
        for dc in [-1, 1]:
            r, c = kr - 1, kc + dc
            if 0 <= r and 0 <= c < BOARD_WIDTH:
                if board[r][c] == 'p':
                    return True
        r, c = kr - 1, kc
        if r >= 0 and board[r][c] == 'p':
            return True
    else:  # 黑方，红兵从上方攻击
        for dc in [-1, 1]:
            r, c = kr + 1, kc + dc
            if r < BOARD_HEIGHT and 0 <= c < BOARD_WIDTH:
                if board[r][c] == 'P':
                    return True
        r, c = kr + 1, kc
        if r < BOARD_HEIGHT and board[r][c] == 'P':
            return True

    return False


def _generate_legal_positions(pieces: tuple) -> Dict[int, list]:
    """生成给定棋子集合的所有合法局面。

    使用 ChineseChessGame.move_piece() 验证合法性。
    将/帥固定在 palace，其余棋子枚举全盘空位。

    Returns:
        {position_hash: [board_2d_copy, ...]} 合法局面列表
    """
    from domain.game import ChineseChessGame

    positions = {}
    piece_list = list(pieces)

    # 分离将/帥和其他棋子
    kings = [p for p in piece_list if p in ('K', 'k')]
    others = [p for p in piece_list if p not in ('K', 'k')]

    # palace 位置
    red_palace = [(r, c) for r in range(7, 10) for c in range(3, 6)]
    black_palace = [(r, c) for r in range(0, 3) for c in range(3, 6)]
    all_squares = [(r, c) for r in range(BOARD_HEIGHT) for c in range(BOARD_WIDTH)]
    n = len(others)

    # 枚举 kings 位置
    for rk_pos in red_palace:
        for bk_pos in black_palace:
            if rk_pos == bk_pos:
                continue
            occupied = {rk_pos, bk_pos}
            available = [s for s in all_squares if s not in occupied]

            # 没有其他棋子
            if n == 0:
                board = [['.'] * BOARD_WIDTH for _ in range(BOARD_HEIGHT)]
                board[rk_pos[0]][rk_pos[1]] = 'K'
                board[bk_pos[0]][bk_pos[1]] = 'k'
                if not _is_king_facing(board) and not _is_in_check(board, 1) and not _is_in_check(board, 2):
                    h = _board_to_index(board)
                    positions[h] = [row[:] for row in board]
                continue

            if n == 1:
                for sq in available:
                    board = [['.'] * BOARD_WIDTH for _ in range(BOARD_HEIGHT)]
                    board[rk_pos[0]][rk_pos[1]] = 'K'
                    board[bk_pos[0]][bk_pos[1]] = 'k'
                    board[sq[0]][sq[1]] = others[0]
                    if _is_king_facing(board):
                        continue
                    # 验证：使用 ChineseChessGame
                    g = ChineseChessGame()
                    g.board = [r[:] for r in board]
                    g._king_pos[1] = rk_pos
                    g._king_pos[2] = bk_pos
                    # 检查走子方（当前是红方先走）不被将军
                    if not g.is_in_check(1) and not g.is_in_check(2):
                        h = _board_to_index(board)
                        positions[h] = [row[:] for row in board]
            elif n == 2:
                for i, sq1 in enumerate(available):
                    for sq2 in available[i+1:]:
                        board = [['.'] * BOARD_WIDTH for _ in range(BOARD_HEIGHT)]
                        board[rk_pos[0]][rk_pos[1]] = 'K'
                        board[bk_pos[0]][bk_pos[1]] = 'k'
                        board[sq1[0]][sq1[1]] = others[0]
                        board[sq2[0]][sq2[1]] = others[1]
                        if _is_king_facing(board):
                            continue
                        g = ChineseChessGame()
                        g.board = [r[:] for r in board]
                        g._king_pos[1] = rk_pos
                        g._king_pos[2] = bk_pos
                        if not g.is_in_check(1) and not g.is_in_check(2):
                            h = _board_to_index(board)
                            positions[h] = [row[:] for row in board]

    return positions


def _get_legal_moves_simple(board: list, player: int) -> List[Tuple[int,int,int,int]]:
    """简化的合法走法生成器（仅用于残局回溯分析）。

    生成所有合法走法，不依赖 ChineseChessGame。
    """
    moves = []
    my_pieces = 'KABNRCP' if player == 1 else 'kabnrcp'

    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p not in my_pieces:
                continue

            pu = p.upper()
            # 将/帥
            if pu == 'K':
                for dr, dc in [(1,0),(-1,0),(0,1),(0,-1)]:
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH):
                        continue
                    # palace check
                    if player == 1 and not (7 <= nr <= 9 and 3 <= nc <= 5):
                        continue
                    if player == 2 and not (0 <= nr <= 2 and 3 <= nc <= 5):
                        continue
                    target = board[nr][nc]
                    if target in my_pieces:
                        continue
                    moves.append((r, c, nr, nc))
                continue

            # 仕/士
            if pu == 'A':
                for dr, dc in [(1,1),(1,-1),(-1,1),(-1,-1)]:
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH):
                        continue
                    if player == 1 and not (7 <= nr <= 9 and 3 <= nc <= 5):
                        continue
                    if player == 2 and not (0 <= nr <= 2 and 3 <= nc <= 5):
                        continue
                    target = board[nr][nc]
                    if target in my_pieces:
                        continue
                    moves.append((r, c, nr, nc))
                continue

            # 相/象
            if pu == 'B':
                for dr, dc in [(2,2),(2,-2),(-2,2),(-2,-2)]:
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH):
                        continue
                    # 不能过河
                    if player == 1 and nr < 5:
                        continue
                    if player == 2 and nr > 4:
                        continue
                    # 象眼
                    eye_r = r + (1 if dr > 0 else -1)
                    eye_c = c + (1 if dc > 0 else -1)
                    if board[eye_r][eye_c] != '.':
                        continue
                    target = board[nr][nc]
                    if target in my_pieces:
                        continue
                    moves.append((r, c, nr, nc))
                continue

            # 馬
            if pu == 'N':
                for dr, dc, lr, lc in [(2,1,1,0),(2,-1,1,0),(-2,1,-1,0),(-2,-1,-1,0),
                                        (1,2,0,1),(1,-2,0,-1),(-1,2,0,1),(-1,-2,0,-1)]:
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH):
                        continue
                    leg_r, leg_c = r + lr, c + lc
                    if board[leg_r][leg_c] != '.':
                        continue
                    target = board[nr][nc]
                    if target in my_pieces:
                        continue
                    moves.append((r, c, nr, nc))
                continue

            # 車
            if pu == 'R':
                for dr, dc in [(1,0),(-1,0),(0,1),(0,-1)]:
                    nr, nc = r + dr, c + dc
                    while 0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH:
                        target = board[nr][nc]
                        if target in my_pieces:
                            break
                        moves.append((r, c, nr, nc))
                        if target != '.':
                            break
                        nr += dr; nc += dc
                continue

            # 炮
            if pu == 'C':
                for dr, dc in [(1,0),(-1,0),(0,1),(0,-1)]:
                    nr, nc = r + dr, c + dc
                    while 0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH:
                        target = board[nr][nc]
                        if target == '.':
                            moves.append((r, c, nr, nc))
                            nr += dr; nc += dc
                            continue
                        # 找到一个炮架，跳过它继续找吃子目标
                        nr += dr; nc += dc
                        while 0 <= nr < BOARD_HEIGHT and 0 <= nc < BOARD_WIDTH:
                            t2 = board[nr][nc]
                            if t2 != '.':
                                if t2 not in my_pieces:
                                    moves.append((r, c, nr, nc))
                                break
                            nr += dr; nc += dc
                        break
                continue

            # 兵/卒
            if pu == 'P':
                if player == 1:  # 红兵
                    # 前进
                    if r > 0:
                        nr = r - 1
                        if board[nr][c] not in my_pieces:
                            moves.append((r, c, nr, c))
                    # 过河后可以横移
                    if r <= 4:
                        for dc in [-1, 1]:
                            nc = c + dc
                            if 0 <= nc < BOARD_WIDTH and board[r][nc] not in my_pieces:
                                moves.append((r, c, r, nc))
                else:  # 黑卒
                    if r < 9:
                        nr = r + 1
                        if board[nr][c] not in my_pieces:
                            moves.append((r, c, nr, c))
                    if r >= 5:
                        for dc in [-1, 1]:
                            nc = c + dc
                            if 0 <= nc < BOARD_WIDTH and board[r][nc] not in my_pieces:
                                moves.append((r, c, r, nc))
                continue

    # 过滤走后自己被将军的走法
    legal = []
    for fr, fc, tr, tc in moves:
        piece = board[fr][fc]
        captured = board[tr][tc]
        board[tr][tc] = piece
        board[fr][fc] = '.'
        if not _is_in_check(board, player) and not _is_king_facing(board):
            legal.append((fr, fc, tr, tc))
        board[fr][fc] = piece
        board[tr][tc] = captured
    return legal


class DtmTable:
    """单个棋子组合的 DTM 表。"""

    def __init__(self, pieces: tuple):
        self.pieces = pieces  # 如 ('K', 'R', 'k')
        self._dtm: Dict[int, int] = {}  # position_hash → dtm
        self._generated = False

    def generate(self) -> None:
        """回溯分析生成 DTM 表（使用 ChineseChessGame 做走法生成）。"""
        if self._generated:
            return

        from domain.game import ChineseChessGame

        key = _piece_set_key(self.pieces)
        print(f'  生成 DTM 表: {key} ({len(self.pieces)}子)...')

        # 第1步：生成所有合法局面
        positions = _generate_legal_positions(self.pieces)
        print(f'    合法局面数：{len(positions)}')
        if not positions:
            self._generated = True
            return

        # 构建 ChineseChessGame 用于走法生成
        g = ChineseChessGame()

        # 第2步：标记初始将杀死局面
        # 遍历所有局面，找被杀方的 DTM=0
        unprocessed = []  # [(hash, dtm, loser_perspective)]
        for h, board in positions.items():
            # 设置棋盘
            g.board = [r[:] for r in board]
            # 找到将/帥位置
            for r in range(10):
                for c in range(9):
                    if board[r][c] == 'K':
                        g._king_pos[1] = (r, c)
                    elif board[r][c] == 'k':
                        g._king_pos[2] = (r, c)

            for player in [1, 2]:
                if g.is_in_check(player):
                    moves = g.get_all_legal_moves(player)
                    if not moves:
                        self._dtm[h] = 0  # player 被将死
                        unprocessed.append((h, 0, player))
                        break

        print(f'    初始将死局面：{len(unprocessed)}')

        # 第3步：回溯迭代
        processed = set()
        iteration = 0
        while unprocessed:
            iteration += 1
            next_batch = []
            for pos_hash, dtm, loser in unprocessed:
                if pos_hash in processed:
                    continue
                processed.add(pos_hash)
                board = positions[pos_hash]

                if dtm >= MAX_DTM:
                    continue

                # 找前驱：对手（winner = 3-loser）走一步到当前局面
                winner = 3 - loser
                new_dtm = dtm + 1

                # 设置目标局面
                g.board = [r[:] for r in board]
                for r in range(10):
                    for c in range(9):
                        if board[r][c] == 'K':
                            g._king_pos[1] = (r, c)
                        elif board[r][c] == 'k':
                            g._king_pos[2] = (r, c)

                # 检查每个前驱局面：winner 能否一步到达当前局面
                for h2, pred_board in positions.items():
                    if h2 in processed or h2 in self._dtm:
                        continue
                    # 设置前驱局面
                    g2 = ChineseChessGame()
                    g2.board = [r[:] for r in pred_board]
                    for r in range(10):
                        for c in range(9):
                            if pred_board[r][c] == 'K':
                                g2._king_pos[1] = (r, c)
                            elif pred_board[r][c] == 'k':
                                g2._king_pos[2] = (r, c)

                    moves = g2.get_all_legal_moves(winner)
                    for fr, fc, tr, tc in moves:
                        # 模拟走子
                        piece = pred_board[fr][fc]
                        captured = pred_board[tr][tc]
                        pred_board[tr][tc] = piece
                        pred_board[fr][fc] = '.'
                        # 比较结果
                        match = all(pred_board[r][c] == board[r][c]
                                   for r in range(10) for c in range(9))
                        pred_board[fr][fc] = piece
                        pred_board[tr][tc] = captured
                        if match:
                            self._dtm[h2] = new_dtm
                            next_batch.append((h2, new_dtm, winner))
                            break

            unprocessed = next_batch
            if iteration % 5 == 0:
                print(f'    迭代 {iteration}: DTM≤{new_dtm}, '
                      f'已处理 {len(processed)}')

        self._generated = True
        covered = len(self._dtm)
        print(f'    完成：{covered}/{len(positions)} 局面已覆盖')


    def probe(self, board: list) -> Optional[int]:
        """查 DTM 表，返回走子方视角的 DTM 值或 None。

        Returns:
            None: 局面不在表中
            int: DTM 值 — 0=被将死, N=可在N步内杀（正数）
        """
        if not self._generated:
            return None
        h = _board_to_index(board)
        return self._dtm.get(h)

    def save(self, filepath: str) -> None:
        """保存为紧凑二进制文件。"""
        if not self._generated:
            return
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'wb') as f:
            f.write(DTM_MAGIC)
            f.write(struct.pack('<B', len(self.pieces)))
            f.write(struct.pack('<B', len(self._dtm) & 0xFF))
            f.write(struct.pack('<H', (len(self._dtm) >> 8) & 0xFFFF))
            for h, dtm in sorted(self._dtm.items()):
                f.write(struct.pack('<I', h))
                f.write(struct.pack('<B', min(dtm, 255)))

    def load(self, filepath: str) -> bool:
        """从紧凑二进制文件加载。"""
        if not os.path.isfile(filepath):
            return False
        with open(filepath, 'rb') as f:
            magic = f.read(5)
            if magic != DTM_MAGIC:
                return False
            num_pieces = struct.unpack('<B', f.read(1))[0]
            count_lo = struct.unpack('<B', f.read(1))[0]
            count_hi = struct.unpack('<H', f.read(2))[0]
            count = count_lo | (count_hi << 8)
            for _ in range(count):
                h = struct.unpack('<I', f.read(4))[0]
                dtm = struct.unpack('<B', f.read(1))[0]
                self._dtm[h] = dtm
        self._generated = True
        return True


# ── 全局缓存 ──
_tables: Dict[str, DtmTable] = {}
_loaded_piece_sets: Set[str] = set()


def probe_local(board: list, piece_count: int) -> Optional[Tuple[float, int]]:
    """查询本地 DTM 残局库。

    Args:
        board: 10×9 棋盘
        piece_count: 棋盘上的棋子总数

    Returns:
        None: 无匹配的本地表
        (score, dtm): score 是走子方视角的 centipawn，dtm 是杀棋距离
    """
    if piece_count > 4:  # 仅支持 ≤4 子（可扩展）
        return None

    # 找出棋盘上的棋子
    pieces = []
    current_player = None  # 需要从外部传入
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p != '.':
                pieces.append(p)

    key = _piece_set_key(tuple(pieces))

    # 尝试加载
    if key not in _tables:
        filepath = os.path.join(DTM_DIR, f'{key}.dtm')
        table = DtmTable(tuple(pieces))
        if not table.load(filepath):
            return None
        _tables[key] = table

    dtm = _tables[key].probe(board)
    if dtm is None:
        return None

    # 转换为 score：接近将死时分数更高
    # DTM 值已经是走子方视角
    score = 99900 - dtm * 100  # 对齐 JIANGSHA_SCORE 的数量级
    return (float(score), dtm)


def generate_all_4piece() -> None:
    """生成所有 4 子及以下残局的 DTM 表。"""
    os.makedirs(DTM_DIR, exist_ok=True)

    # 定义所有 4 子及以下的残局类型
    # 格式：(红方攻击子, 黑方攻击子)，将/帥各一自动包含
    # 3 子：单攻击子对孤将
    three_piece = [
        ('R', ''),  # 单車
        ('N', ''),  # 单馬
        ('C', ''),  # 单炮
        ('P', ''),  # 单兵
    ]
    # 4 子：单车对士、单车对象等
    four_piece = [
        ('R', 'A'), ('R', 'B'),  # 车对单士/单象
        ('R', 'AA'), ('R', 'AB'), ('R', 'BB'),  # 车对双士/士象/双象
        ('N', 'A'), ('N', 'B'),  # 马对单士/单象
        ('R', 'P'), ('N', 'P'), ('C', 'P'),  # 对单卒
        ('RR', ''), ('RN', ''), ('RC', ''),  # 双攻击子
    ]

    for red_attackers, black_attackers in three_piece + four_piece:
        pieces = ['K'] + list(red_attackers) + ['k'] + list(black_attackers)
        key = _piece_set_key(tuple(pieces))
        filepath = os.path.join(DTM_DIR, f'{key}.dtm')
        if os.path.isfile(filepath):
            print(f'跳过（已存在）: {key}')
            continue
        table = DtmTable(tuple(pieces))
        try:
            table.generate()
            if table._dtm:
                table.save(filepath)
                print(f'  保存: {filepath} ({len(table._dtm)} 条目)')
        except Exception as e:
            print(f'  错误: {e}')
