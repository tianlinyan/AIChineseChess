"""中国象棋游戏逻辑核心，管理棋盘状态、移动、胜负判断等"""

import random as _random

from domain.constants import (BOARD_WIDTH, BOARD_HEIGHT, PIECE_SYMBOLS,
                              ENDGAME_PIECE_THRESHOLD, NATURAL_LIMIT_MOVES)

# ── Zobrist 哈希表（固定种子，进程内可复现）──
# 旧实现是 ord(piece)*31 + r*7 + c*13 的线性组合加权，不同棋子/格子
# 容易碰撞（置换表命中错误分数、重复检测误判）。标准 Zobrist：
# 每个 (棋子, 格子) 一个 64 位随机数，哈希 = 各棋子异或。
_PIECE_TYPES = 'KABNRCPkabnrcp'
_z_rng = _random.Random(20260723)
ZOBRIST_TABLE = {p: [_z_rng.getrandbits(64) for _ in range(90)]
                 for p in _PIECE_TYPES}
_ZOBRIST_SIDE = {1: _z_rng.getrandbits(64), 2: _z_rng.getrandbits(64)}


class ChineseChessGame:
    """中国象棋游戏逻辑核心"""

    # 标准初始棋盘（9列，10行），行0为黑方底线，行9为红方底线
    STANDARD_BOARD = [
        ['r', 'n', 'b', 'a', 'k', 'a', 'b', 'n', 'r'],
        ['.', '.', '.', '.', '.', '.', '.', '.', '.'],
        ['.', 'c', '.', '.', '.', '.', '.', 'c', '.'],
        ['p', '.', 'p', '.', 'p', '.', 'p', '.', 'p'],
        ['.', '.', '.', '.', '.', '.', '.', '.', '.'],
        ['.', '.', '.', '.', '.', '.', '.', '.', '.'],
        ['P', '.', 'P', '.', 'P', '.', 'P', '.', 'P'],
        ['.', 'C', '.', '.', '.', '.', '.', 'C', '.'],
        ['.', '.', '.', '.', '.', '.', '.', '.', '.'],
        ['R', 'N', 'B', 'A', 'K', 'A', 'B', 'N', 'R']
    ]

    def __init__(self):
        self.size_rows = BOARD_HEIGHT
        self.size_cols = BOARD_WIDTH
        self.board = [row[:] for row in self.STANDARD_BOARD]
        self.current_player = 1          # 1 红方（先手），2 黑方（后手）
        self.moves = []
        self.game_over = False
        self.winner = None     # None=进行中, 1=红胜, 2=黑胜, 0=和棋
        self.last_move = None
        self._position_history: list = []  # 走子历史哈希，用于着法重复检测
        self._move_checks: list = []       # 并行记录：（走子方, 走后对方是否被将军），用于长将检测
        self.total_moves_count = 0        # 总步数（自游戏开始计，reset 清零）
        self.moves_since_capture = 0      # 自然限着计数（连续未吃子步数，120 步判和）
        # 将位置缓存（O(1) 将军检测，move_piece 时增量更新）
        self._king_pos = {1: (9, 4), 2: (0, 4)}
        # Zobrist 哈希（move_piece / 搜索 make/unmake 增量维护）
        self._zobrist = self._compute_zobrist()
        # ── 增量评估缓存（搜索 make/unmake 增量维护，叶子节点直接读取跳过扫描）──
        self._material_counts: dict = {}   # {piece_char: count}，红大写/黑小写
        self._red_piece_count: int = 0
        self._black_piece_count: int = 0
        self._red_pst_score: float = 0.0   # 红方 PST 总分
        self._black_pst_score: float = 0.0  # 黑方 PST 总分
        self._recompute_incremental()       # 从初始棋盘填充

    def reset(self):
        self.board = [row[:] for row in self.STANDARD_BOARD]
        self.current_player = 1
        self.moves = []
        self.game_over = False
        self.winner = None
        self.last_move = None
        self._position_history = []
        self._move_checks = []
        self.total_moves_count = 0
        self.moves_since_capture = 0  # 自然限着计数（连续未吃子步数，120 步判和）
        self._king_pos = {1: (9, 4), 2: (0, 4)}
        self._zobrist = self._compute_zobrist()
        self._recompute_incremental()  # 重置增量评估缓存

    @classmethod
    def from_snapshot(cls, board, player, king_pos):
        """从棋盘快照创建临时游戏对象（搜索/工具执行用）。"""
        g = cls()
        g.board = board
        g.current_player = player
        g._king_pos = dict(king_pos)
        g.recompute_hash()
        g._recompute_incremental()  # 从快照棋盘重建增量缓存
        return g

    def is_red(self, piece):
        return piece.isupper()

    def is_black(self, piece):
        return piece.islower()

    def get_piece_owner(self, piece):
        if piece == '.':
            return 0
        return 1 if piece.isupper() else 2

    def in_board(self, row, col):
        return 0 <= row < self.size_rows and 0 <= col < self.size_cols

    def move_piece(self, from_row, from_col, to_row, to_col):
        if self.game_over:
            return {'success': False, 'message': '游戏已结束'}

        # 边界防御
        if not self.in_board(from_row, from_col) or not self.in_board(to_row, to_col):
            return {'success': False, 'message': '坐标超出棋盘范围'}

        piece = self.board[from_row][from_col]
        if piece == '.':
            return {'success': False, 'message': '起始位置无棋子'}

        owner = self.get_piece_owner(piece)
        if owner != self.current_player:
            return {'success': False, 'message': '不能移动对方的棋子'}

        target_piece = self.board[to_row][to_col]
        # 防御：禁止吃对方的将（将杀判定应在吃之前结束游戏）
        if target_piece.upper() == 'K':
            return {'success': False, 'message': '不能直接吃掉对方的将'}
        if target_piece != '.' and self.get_piece_owner(target_piece) == owner:
            return {'success': False, 'message': '目标位置已有己方棋子'}

        if not self._is_legal_move(piece, from_row, from_col, to_row, to_col):
            return {'success': False, 'message': '不合法的移动'}

        if self._would_be_illegal(from_row, from_col, to_row, to_col, owner):
            return {'success': False, 'message': '移动后己方将会被将军或形成将帅对面'}

        captured = target_piece
        self.board[to_row][to_col] = piece
        self.board[from_row][from_col] = '.'

        # 增量维护 Zobrist 哈希
        _zi_from = from_row * self.size_cols + from_col
        _zi_to = to_row * self.size_cols + to_col
        self._zobrist ^= (ZOBRIST_TABLE[piece][_zi_from]
                          ^ ZOBRIST_TABLE[piece][_zi_to])
        if captured != '.':
            self._zobrist ^= ZOBRIST_TABLE[captured][_zi_to]

        # 将移动时增量更新缓存
        if piece == 'K':
            self._king_pos[1] = (to_row, to_col)
        elif piece == 'k':
            self._king_pos[2] = (to_row, to_col)

        # ── 增量评估缓存维护 ──
        from domain.evaluation import RED_PST
        _pu = piece.upper()
        if _pu in RED_PST:
            if piece.isupper():
                self._red_pst_score -= RED_PST[_pu][from_row][from_col]
                self._red_pst_score += RED_PST[_pu][to_row][to_col]
            else:
                self._black_pst_score -= RED_PST[_pu][BOARD_HEIGHT - 1 - from_row][from_col]
                self._black_pst_score += RED_PST[_pu][BOARD_HEIGHT - 1 - to_row][to_col]
        if captured != '.':
            self._material_counts[captured] = self._material_counts.get(captured, 0) - 1
            if captured.isupper():
                self._red_piece_count -= 1
            else:
                self._black_piece_count -= 1
            _cu = captured.upper()
            if _cu in RED_PST:
                if captured.isupper():
                    self._red_pst_score -= RED_PST[_cu][to_row][to_col]
                else:
                    self._black_pst_score -= RED_PST[_cu][BOARD_HEIGHT - 1 - to_row][to_col]

        self.last_move = (from_row, from_col, to_row, to_col, self.current_player)
        self.moves.append((from_row, from_col, to_row, to_col, self.current_player, captured, piece))
        self.total_moves_count += 1
        # 自然限着计数：吃子清零，未吃子累进（竞赛规则：120 步未吃子判和）
        self.moves_since_capture = (0 if captured != '.'
                                    else self.moves_since_capture + 1)

        # 记录走子后的局面哈希 + 是否将军（着法重复/长将检测）
        self._position_history.append(self.position_hash())
        opponent = 2 if self.current_player == 1 else 1
        # 显式记录走子方：不依赖索引奇偶（500 条截断后奇偶会整体翻转）
        self._move_checks.append((self.current_player, self._is_in_check(opponent)))
        # 保留最近 500 条
        if len(self._position_history) > 500:
            self._position_history = self._position_history[-500:]
            self._move_checks = self._move_checks[-500:]

        # ── 着法重复检测：同局面第3次出现 → 长将/和棋判决 ──
        rep_verdict = self._check_repetition()
        if rep_verdict is not None:
            self.game_over = True
            if rep_verdict == 0:
                self.winner = 0  # 和棋
                return {'success': True, 'game_over': True, 'winner': 0,
                        'message': '着法重复三次 — 和棋'}
            else:
                self.winner = 3 - rep_verdict  # rep_verdict=1(红犯规)→winner=2(黑胜)
                loser_name = '红方' if rep_verdict == 1 else '黑方'
                return {'success': True, 'game_over': True,
                        'winner': self.winner,
                        'message': f'{loser_name}长将犯规，判负！'}

        # ── 将杀/困毙判定：一次生成对方走法，两种结局共用 ──
        if not self.get_all_legal_moves(opponent):
            self.game_over = True
            self.winner = self.current_player
            player_name = '红方' if self.current_player == 1 else '黑方'
            if self._is_in_check(opponent):
                msg = f'{player_name}将死对方获胜！'
            else:
                msg = f'{player_name}困毙对方获胜！'
            return {'success': True, 'game_over': True,
                    'winner': self.current_player, 'message': msg}

        # ── 自然限着：连续 120 步未吃子判和（竞赛规则）──
        # 将杀/困毙优先：第 120 步同时将死/困毙对方则限着失效（规则原文）
        # 注：搜索的 _make_move 不经过 move_piece，搜索内部不感知该规则
        if self.moves_since_capture >= NATURAL_LIMIT_MOVES:
            self.game_over = True
            self.winner = 0
            return {'success': True, 'game_over': True, 'winner': 0,
                    'message': f'{NATURAL_LIMIT_MOVES} 步未吃子 — 自然限着，和棋'}

        # 双方无攻击子力 → 和棋（只剩将+士+相，无車馬炮兵）
        if self._no_attacking_pieces():
            self.game_over = True
            self.winner = 0
            return {'success': True, 'game_over': True, 'winner': 0,
                    'message': '双方均无攻击子力 — 和棋'}

        self.current_player = opponent
        return {'success': True, 'game_over': False}

    # ── 将帅对面检测 ──
    def _is_king_facing(self):
        """检测双方将是否对面（中间无棋子阻挡）。

        优先使用缓存位置（O(1)），缓存失效时回退全盘扫描。
        """
        red_shuai_pos = self._king_pos.get(1)
        black_jiang_pos = self._king_pos.get(2)

        # 缓存验证
        if (not red_shuai_pos or not black_jiang_pos
                or self.board[red_shuai_pos[0]][red_shuai_pos[1]] != 'K'
                or self.board[black_jiang_pos[0]][black_jiang_pos[1]] != 'k'):
            # 回退全盘扫描并修复缓存
            red_shuai_pos = black_jiang_pos = None
            for r in range(self.size_rows):
                for c in range(self.size_cols):
                    piece = self.board[r][c]
                    if piece == 'K':
                        red_shuai_pos = (r, c)
                        self._king_pos[1] = (r, c)
                    elif piece == 'k':
                        black_jiang_pos = (r, c)
                        self._king_pos[2] = (r, c)
            if not red_shuai_pos or not black_jiang_pos:
                return False

        if red_shuai_pos[1] != black_jiang_pos[1]:
            return False
        min_row = min(red_shuai_pos[0], black_jiang_pos[0])
        max_row = max(red_shuai_pos[0], black_jiang_pos[0])
        for r in range(min_row + 1, max_row):
            if self.board[r][red_shuai_pos[1]] != '.':
                return False
        return True

    # ── 走子规则 ──
    def _is_legal_move(self, piece, fr, fc, tr, tc):
        if piece == '.':
            return False
        if fr == tr and fc == tc:    # 零步移动始终非法
            return False
        if not self.in_board(tr, tc):  # 目标必须在棋盘内
            return False
        # 不能吃己方棋子
        target = self.board[tr][tc]
        if target != '.' and self.get_piece_owner(target) == self.get_piece_owner(piece):
            return False
        dr = tr - fr
        dc = tc - fc

        piece_upper = piece.upper()
        if piece_upper == 'K':  # 将
            if abs(dr) + abs(dc) != 1:
                return False
            if self.is_red(piece):
                return 7 <= tr <= 9 and 3 <= tc <= 5
            else:
                return 0 <= tr <= 2 and 3 <= tc <= 5

        elif piece_upper == 'A':  # 士
            if abs(dr) != 1 or abs(dc) != 1:
                return False
            if self.is_red(piece):
                return 7 <= tr <= 9 and 3 <= tc <= 5
            else:
                return 0 <= tr <= 2 and 3 <= tc <= 5

        elif piece_upper == 'B':  # 相
            if abs(dr) != 2 or abs(dc) != 2:
                return False
            er = fr + dr // 2
            ec = fc + dc // 2
            if self.board[er][ec] != '.':
                return False
            if self.is_red(piece):
                return tr >= 5
            else:
                return tr <= 4

        elif piece_upper == 'N':  # 馬
            if (abs(dr), abs(dc)) not in [(1, 2), (2, 1)]:
                return False
            if abs(dr) == 2:
                block_r = fr + dr // 2
                block_c = fc
            else:
                block_r = fr
                block_c = fc + dc // 2
            if self.board[block_r][block_c] != '.':
                return False
            return True

        elif piece_upper == 'R':  # 車
            if dr != 0 and dc != 0:
                return False
            step_r = 0 if dr == 0 else (1 if dr > 0 else -1)
            step_c = 0 if dc == 0 else (1 if dc > 0 else -1)
            r, c = fr + step_r, fc + step_c
            while (r, c) != (tr, tc):
                if not self.in_board(r, c):
                    return False
                if self.board[r][c] != '.':
                    return False
                r += step_r
                c += step_c
            return True

        elif piece_upper == 'C':  # 炮
            if dr != 0 and dc != 0:
                return False
            step_r = 0 if dr == 0 else (1 if dr > 0 else -1)
            step_c = 0 if dc == 0 else (1 if dc > 0 else -1)
            r, c = fr + step_r, fc + step_c
            obstacles = 0
            while (r, c) != (tr, tc):
                if not self.in_board(r, c):
                    return False
                if self.board[r][c] != '.':
                    obstacles += 1
                r += step_r
                c += step_c
            target = self.board[tr][tc]
            if target == '.':
                return obstacles == 0
            else:
                return obstacles == 1

        elif piece_upper == 'P':  # 卒
            if piece.isupper():  # 红卒（前进=行号减小）
                if tr > fr:       # 不能后退（严格大于，允许横走 tr==fr）
                    return False
                crossed = fr <= 4
            else:  # 黑卒（前进=行号增大）
                if tr < fr:       # 不能后退（严格小于，允许横走 tr==fr）
                    return False
                crossed = fr >= 5
            if abs(dr) + abs(dc) != 1:
                return False
            if dc != 0 and not crossed:
                return False
            return True

        return False

    def _would_be_illegal(self, fr, fc, tr, tc, player):
        piece = self.board[fr][fc]
        target = self.board[tr][tc]
        self.board[tr][tc] = piece
        self.board[fr][fc] = '.'

        # 如果临时移动了将，更新缓存以保证 _is_in_check 正确
        saved_pos = None
        if piece.upper() == 'K':
            saved_pos = self._king_pos.get(player)
            self._king_pos[player] = (tr, tc)

        illegal = self._is_in_check(player) or self._is_king_facing()

        # 恢复缓存
        if saved_pos is not None:
            self._king_pos[player] = saved_pos

        self.board[fr][fc] = piece
        self.board[tr][tc] = target
        return illegal

    def _is_in_check(self, player):
        """检测 player 方是否被将军。

        从将位反向检测：車/炮四条射线、馬 8 个攻击位（验蹩腿）、
        兵/卒 3 个攻击位 —— O(~20) 替代"对方全子 × 走法校验"的 O(90×16)。
        与旧暴力版语义完全等价（士/相不出九宫/不过河不可能攻击到对方将，
        双将永不相邻，将帅对面由 _is_king_facing 单独处理）。
        将位置优先用缓存，缓存失效时回退全盘扫描修复。
        """
        king_piece = 'K' if player == 1 else 'k'
        kr, kc = self._king_pos.get(player, (None, None))

        # 缓存验证（处理外部直接赋值 board 导致缓存失效的情况）
        if kr is None or self.board[kr][kc] != king_piece:
            # 回退全盘扫描并修复缓存
            for r in range(self.size_rows):
                for c in range(self.size_cols):
                    if self.board[r][c] == king_piece:
                        kr, kc = r, c
                        self._king_pos[player] = (r, c)
                        break
                else:
                    continue
                break
            else:
                return False  # 将不在棋盘上（不应出现）

        board = self.board
        if player == 1:
            opp_rook, opp_cannon, opp_knight, opp_pawn = 'r', 'c', 'n', 'p'
        else:
            opp_rook, opp_cannon, opp_knight, opp_pawn = 'R', 'C', 'N', 'P'

        # ── 車 / 炮：四条射线（車打直线，炮隔一子）──
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            r, c = kr + dr, kc + dc
            screened = False
            while self.in_board(r, c):
                p = board[r][c]
                if p != '.':
                    if not screened:
                        if p == opp_rook:
                            return True
                        screened = True
                    else:
                        if p == opp_cannon:
                            return True
                        break
                r += dr
                c += dc

        # ── 馬：8 个攻击位（验蹩马腿）──
        # 蹩腿格在"马一侧"：纵向跳时腿在马的同一列、横向跳时在马的同一行
        for dr, dc in ((2, 1), (2, -1), (-2, 1), (-2, -1),
                       (1, 2), (1, -2), (-1, 2), (-1, -2)):
            r, c = kr + dr, kc + dc
            if self.in_board(r, c) and board[r][c] == opp_knight:
                if abs(dr) == 2:
                    leg_r, leg_c = kr + dr // 2, kc + dc
                else:
                    leg_r, leg_c = kr + dr, kc + dc // 2
                if board[leg_r][leg_c] == '.':
                    return True

        # ── 兵/卒：正面一格 + 过河后横向 ──
        if player == 1:
            # 黑卒向下攻（行号增大），横向攻击要求卒已过河（行≥5）
            if kr > 0 and board[kr - 1][kc] == 'p':
                return True
            if kr >= 5:
                if kc > 0 and board[kr][kc - 1] == 'p':
                    return True
                if kc < self.size_cols - 1 and board[kr][kc + 1] == 'p':
                    return True
        else:
            # 红兵向上攻（行号减小），横向攻击要求兵已过河（行≤4）
            if kr < self.size_rows - 1 and board[kr + 1][kc] == 'P':
                return True
            if kr <= 4:
                if kc > 0 and board[kr][kc - 1] == 'P':
                    return True
                if kc < self.size_cols - 1 and board[kr][kc + 1] == 'P':
                    return True
        return False

    def get_all_legal_moves(self, player):
        """生成全部合法走法（定向生成 + 应将校验）。

        按棋种生成候选目标：車/炮沿四条射线步进、馬 8 个日字（验蹩腿）、
        相 4 个田字（验塞眼+不过河）、士/将宫内 4 格、兵/卒 3 个方向，
        再逐一 _would_be_illegal 校验。与旧"全 90 格 × _is_legal_move"
        实现的走法集合完全等价（tests/compare_movegen.py 对拍验证）。
        """
        moves = []
        board = self.board
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = board[r][c]
                if piece == '.' or self.get_piece_owner(piece) != player:
                    continue
                upper = piece.upper()
                if upper == 'R':
                    self._gen_ray_moves(moves, r, c, player, cannon=False)
                elif upper == 'C':
                    self._gen_ray_moves(moves, r, c, player, cannon=True)
                elif upper == 'N':
                    self._gen_knight_moves(moves, r, c, player)
                elif upper == 'B':
                    self._gen_bishop_moves(moves, r, c, player)
                elif upper == 'A':
                    self._gen_advisor_moves(moves, r, c, player)
                elif upper == 'K':
                    self._gen_king_moves(moves, r, c, player)
                elif upper == 'P':
                    self._gen_pawn_moves(moves, r, c, player)
        return moves

    def get_capture_moves(self, player):
        """只生成吃子走法（目标格有对方棋子）— 静止搜索专用。

        通过过滤 get_all_legal_moves 的结果实现，与显式 captures_only
        参数生成的结果完全等价。
        """
        return [m for m in self.get_all_legal_moves(player)
                if self.board[m[2]][m[3]] != '.']

    # ── 定向走法生成辅助（captures_only=True 时只保留吃子）──

    def _append_if_legal(self, moves, fr, fc, tr, tc, player,
                         captures_only=False):
        target = self.board[tr][tc]
        if target == '.':
            if captures_only:
                return
        elif self.get_piece_owner(target) == player:
            return
        if not self._would_be_illegal(fr, fc, tr, tc, player):
            moves.append((fr, fc, tr, tc))

    def _gen_ray_moves(self, moves, r, c, player, cannon,
                       captures_only=False):
        """車：直线步进遇子即止（可吃）；炮：不吃子走空格，隔一子吃子。"""
        board = self.board
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            screened = False
            while self.in_board(nr, nc):
                t = board[nr][nc]
                if not cannon:
                    if t == '.':
                        if not captures_only:
                            self._append_if_legal(
                                moves, r, c, nr, nc, player)
                    else:
                        if self.get_piece_owner(t) != player:
                            self._append_if_legal(
                                moves, r, c, nr, nc, player)
                        break
                else:
                    if not screened:
                        if t == '.':
                            if not captures_only:
                                self._append_if_legal(
                                    moves, r, c, nr, nc, player)
                        else:
                            screened = True
                    else:
                        if t != '.':
                            if self.get_piece_owner(t) != player:
                                self._append_if_legal(
                                    moves, r, c, nr, nc, player)
                            break
                nr += dr
                nc += dc

    def _gen_knight_moves(self, moves, r, c, player, captures_only=False):
        board = self.board
        for dr, dc in ((2, 1), (2, -1), (-2, 1), (-2, -1),
                       (1, 2), (1, -2), (-1, 2), (-1, -2)):
            tr, tc = r + dr, c + dc
            if not self.in_board(tr, tc):
                continue
            # 蹩马腿
            if abs(dr) == 2:
                leg_r, leg_c = r + dr // 2, c
            else:
                leg_r, leg_c = r, c + dc // 2
            if board[leg_r][leg_c] != '.':
                continue
            self._append_if_legal(moves, r, c, tr, tc, player,
                                  captures_only)

    def _gen_bishop_moves(self, moves, r, c, player, captures_only=False):
        red = self.is_red(self.board[r][c])
        for dr, dc in ((2, 2), (2, -2), (-2, 2), (-2, -2)):
            tr, tc = r + dr, c + dc
            if not self.in_board(tr, tc):
                continue
            # 相不过河
            if red and tr < 5 or not red and tr > 4:
                continue
            # 塞象眼
            if self.board[r + dr // 2][c + dc // 2] != '.':
                continue
            self._append_if_legal(moves, r, c, tr, tc, player,
                                  captures_only)

    def _gen_advisor_moves(self, moves, r, c, player, captures_only=False):
        red = self.is_red(self.board[r][c])
        row_lo, row_hi = (7, 9) if red else (0, 2)
        for dr, dc in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            tr, tc = r + dr, c + dc
            if not (row_lo <= tr <= row_hi and 3 <= tc <= 5):
                continue
            self._append_if_legal(moves, r, c, tr, tc, player,
                                  captures_only)

    def _gen_king_moves(self, moves, r, c, player, captures_only=False):
        red = self.is_red(self.board[r][c])
        row_lo, row_hi = (7, 9) if red else (0, 2)
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tr, tc = r + dr, c + dc
            if not (row_lo <= tr <= row_hi and 3 <= tc <= 5):
                continue
            self._append_if_legal(moves, r, c, tr, tc, player,
                                  captures_only)

    def _gen_pawn_moves(self, moves, r, c, player, captures_only=False):
        red = self.is_red(self.board[r][c])
        # 前进方向：红兵行号减小，黑卒行号增大
        fwd = -1 if red else 1
        tr = r + fwd
        if self.in_board(tr, c):
            self._append_if_legal(moves, r, c, tr, c, player,
                                  captures_only)
        # 过河后才能横走：红兵 r<=4，黑卒 r>=5
        crossed = r <= 4 if red else r >= 5
        if crossed:
            for tc in (c - 1, c + 1):
                if self.in_board(r, tc):
                    self._append_if_legal(moves, r, c, r, tc, player,
                                          captures_only)

    def get_board_state_string(self):
        s = "   " + " ".join(chr(65 + i) for i in range(self.size_cols)) + "\n"
        for r in range(self.size_rows):
            s += f"{r+1:2d} " + " ".join(self.board[r][c] for c in range(self.size_cols)) + "\n"
        return s

    def format_move_history(self, max_items: int = 0):
        """格式化走子历史，包含棋子名称、坐标、吃子标记。

        max_items > 0 时只保留最近 N 手（编号保持原始连续），
        前缀标注省略条数 —— 用于提示词中控制 token 成本。
        """
        if not self.moves:
            return "暂无移动"
        moves = self.moves
        omitted = 0
        if max_items > 0 and len(moves) > max_items:
            omitted = len(moves) - max_items
            moves = moves[-max_items:]
        lines = []
        if omitted:
            lines.append(f"…（前 {omitted} 手略）…")
        for idx, (fr, fc, tr, tc, player, captured, piece) in enumerate(
                moves, omitted + 1):
            piece_name = PIECE_SYMBOLS.get(piece, piece)
            from_coord = f"{chr(65 + fc)}{fr + 1}"
            to_coord = f"{chr(65 + tc)}{tr + 1}"
            player_name = '红方' if player == 1 else '黑方'

            line = f"{idx}. {player_name} {piece_name} {from_coord}→{to_coord}"
            # 标注吃子
            if captured != '.':
                captured_name = PIECE_SYMBOLS.get(captured, captured)
                line += f" 吃{captured_name}"
            lines.append(line)
        return "\n".join(lines)

    # ── 辅助方法（供搜索和开局库使用） ──

    def is_endgame(self) -> bool:
        """判断是否进入残局阶段。

        启发式标准：总子力 <= ENDGAME_PIECE_THRESHOLD（初始 32 子的一半左右）
        视为残局。残局中卒和将的估值策略需要调整。
        """
        return (self._red_piece_count + self._black_piece_count) <= ENDGAME_PIECE_THRESHOLD

    def count_pieces(self, player: int = 0) -> int:
        """统计棋子数量。O(1) 使用增量缓存。

        Args:
            player: 0=双方, 1=仅红方, 2=仅黑方
        """
        if player == 0:
            return self._red_piece_count + self._black_piece_count
        elif player == 1:
            return self._red_piece_count
        elif player == 2:
            return self._black_piece_count
        return 0

    def _recompute_incremental(self) -> None:
        """全量重算增量评估缓存（初始化/棋盘被外部替换/frozensnapshot 时调用）。

        扫描全部 90 格计算 _material_counts、红/黑棋子计数、PST 总分。
        之后搜索的 _make_move/_unmake_move 增量维护这些字段，
        叶子节点直接读取而无需全盘扫描。
        """
        from domain.evaluation import PIECE_VALUE, RED_PST
        self._material_counts = {}
        self._red_piece_count = 0
        self._black_piece_count = 0
        self._red_pst_score = 0.0
        self._black_pst_score = 0.0
        for r in range(BOARD_HEIGHT):
            for c in range(BOARD_WIDTH):
                p = self.board[r][c]
                if p == '.':
                    continue
                self._material_counts[p] = self._material_counts.get(p, 0) + 1
                if p.isupper():
                    self._red_piece_count += 1
                else:
                    self._black_piece_count += 1
                pu = p.upper()
                if pu in RED_PST:
                    if p.isupper():
                        self._red_pst_score += RED_PST[pu][r][c]
                    else:
                        self._black_pst_score += RED_PST[pu][BOARD_HEIGHT - 1 - r][c]

    def _compute_zobrist(self) -> int:
        """从当前棋盘全量计算 Zobrist 哈希（初始化/棋盘被外部替换时用）。"""
        h = 0
        table = ZOBRIST_TABLE
        cols = self.size_cols
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                p = self.board[r][c]
                if p != '.':
                    h ^= table[p][r * cols + c]
        return h

    def recompute_hash(self) -> None:
        """board 被外部直接替换/修改后调用，重建增量 Zobrist 哈希。

        正常走子（move_piece）和搜索的 make/unmake 都会自动增量维护，
        只有绕过这两者直接改 board 的调用方需要显式重建。
        """
        self._zobrist = self._compute_zobrist()

    def position_hash(self) -> int:
        """计算包含走子方的局面哈希（用于置换表去重）。

        同一棋盘但不同走子方视为不同局面，用 current_player 搅动哈希。
        """
        return self._zobrist ^ _ZOBRIST_SIDE[self.current_player]

    def _check_repetition(self):
        """检测局面重复，按 Pikafish 规则判决。

        Returns:
            None — 无重复
            0    — 三局面重复，和棋（允许循环或双方同责）
            1    — 红方长将犯规，黑胜
            2    — 黑方长将犯规，红胜

        长将判定：循环序列中某一方的每一步均为将军 → 该方犯规判负。
        长捉判定：暂未实现，遇长捉按和棋处理。
        """
        if len(self._position_history) < 5:
            return None
        current = self.position_hash()

        # 找到所有出现位置
        indices = [i for i, h in enumerate(self._position_history) if h == current]
        if len(indices) < 3:
            return None

        # 取最近 3 次出现，分析中间的循环序列
        i1, i2, i3 = indices[-3], indices[-2], indices[-1]

        # 长将检测：循环中每步是否将军
        # _move_checks[j] = (走子方, 走后对方是否被将军)
        # 即第 j 步的走子方在将军；归属直接读元组，不用索引奇偶
        red_all_checks = True
        black_all_checks = True
        for j in range(i1, i3):
            mover, is_check = self._move_checks[j]
            if mover == 1:   # 红方走的步
                if not is_check:
                    red_all_checks = False
            else:            # 黑方走的步
                if not is_check:
                    black_all_checks = False

        if red_all_checks and not black_all_checks:
            return 1  # 红方长将
        if black_all_checks and not red_all_checks:
            return 2  # 黑方长将

        return 0  # 三局面重复，和棋

    def get_move_key(self) -> tuple:
        """返回当前走子序列的关键字（用于开局库精确匹配）。

        Returns:
            ((fr,fc,tr,tc), ...) 走法序列元组
        """
        return tuple((m[0], m[1], m[2], m[3]) for m in self.moves)

    def _no_attacking_pieces(self) -> bool:
        """双方均无攻击子力（車馬炮兵）→ 和棋。"""
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                p = self.board[r][c]
                if p.upper() in ('R', 'N', 'C', 'P'):
                    return False
        return True

    def get_board_copy(self) -> list:
        """返回棋盘副本（用于搜索等只读操作）"""
        return [row[:] for row in self.board]
