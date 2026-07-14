"""中国象棋游戏逻辑核心，管理棋盘状态、移动、胜负判断等"""

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT, PIECE_SYMBOLS


class ChineseChessGame:
    """中国象棋游戏逻辑核心"""

    # 棋子符号映射（从 domain.constants 导入，保留类属性以兼容旧引用）
    PIECE_SYMBOLS = PIECE_SYMBOLS

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
        self.winner = None
        self.last_move = None

    def reset(self):
        self.board = [row[:] for row in self.STANDARD_BOARD]
        self.current_player = 1
        self.moves = []
        self.game_over = False
        self.winner = None
        self.last_move = None

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

        piece = self.board[from_row][from_col]
        if piece == '.':
            return {'success': False, 'message': '起始位置无棋子'}

        owner = self.get_piece_owner(piece)
        if owner != self.current_player:
            return {'success': False, 'message': '不能移动对方的棋子'}

        target_piece = self.board[to_row][to_col]
        if target_piece != '.' and self.get_piece_owner(target_piece) == owner:
            return {'success': False, 'message': '目标位置已有己方棋子'}

        if not self._is_legal_move(piece, from_row, from_col, to_row, to_col):
            return {'success': False, 'message': '不合法的移动'}

        if self._would_be_illegal(from_row, from_col, to_row, to_col, owner):
            return {'success': False, 'message': '移动后己方将/帅会被将军或形成将帅对面'}

        captured = target_piece
        self.board[to_row][to_col] = piece
        self.board[from_row][from_col] = '.'

        self.last_move = (from_row, from_col, to_row, to_col, self.current_player)
        self.moves.append((from_row, from_col, to_row, to_col, self.current_player, captured, piece))

        opponent = 2 if self.current_player == 1 else 1
        if self._is_checkmated(opponent):
            self.game_over = True
            self.winner = self.current_player
            return {
                'success': True, 'game_over': True, 'winner': self.current_player,
                'message': f"{'红方' if self.current_player == 1 else '黑方'}将死对方获胜！"
            }

        if not self._has_any_legal_move(opponent):
            self.game_over = True
            self.winner = self.current_player
            return {
                'success': True, 'game_over': True, 'winner': self.current_player,
                'message': f"{'红方' if self.current_player == 1 else '黑方'}困毙对方获胜！"
            }

        self.current_player = opponent
        return {'success': True, 'game_over': False}

    # ── 将帅对面检测 ──
    def _is_king_facing(self):
        red_king_pos = None
        black_king_pos = None
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece == 'K':
                    red_king_pos = (r, c)
                elif piece == 'k':
                    black_king_pos = (r, c)
        if not red_king_pos or not black_king_pos:
            return False
        if red_king_pos[1] != black_king_pos[1]:
            return False
        min_row = min(red_king_pos[0], black_king_pos[0])
        max_row = max(red_king_pos[0], black_king_pos[0])
        for r in range(min_row + 1, max_row):
            if self.board[r][red_king_pos[1]] != '.':
                return False
        return True

    def _would_cause_king_facing(self, fr, fc, tr, tc):
        piece = self.board[fr][fc]
        target = self.board[tr][tc]
        self.board[tr][tc] = piece
        self.board[fr][fc] = '.'
        facing = self._is_king_facing()
        self.board[fr][fc] = piece
        self.board[tr][tc] = target
        return facing

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
        if piece_upper == 'K':  # 将/帅
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

        elif piece_upper == 'B':  # 相/象
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

        elif piece_upper == 'N':  # 马
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

        elif piece_upper == 'R':  # 车
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

        elif piece_upper == 'P':  # 兵/卒
            if piece.isupper():  # 红兵（前进=行号减小）
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
        illegal = self._is_in_check(player) or self._is_king_facing()
        self.board[fr][fc] = piece
        self.board[tr][tc] = target
        return illegal

    def _is_in_check(self, player):
        king_piece = 'K' if player == 1 else 'k'
        king_pos = None
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                if self.board[r][c] == king_piece:
                    king_pos = (r, c)
                    break
            if king_pos:
                break
        if not king_pos:
            return False
        kr, kc = king_pos
        opponent = 2 if player == 1 else 1
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece != '.' and self.get_piece_owner(piece) == opponent:
                    if self._is_legal_move(piece, r, c, kr, kc):
                        return True
        return False

    def _is_checkmated(self, player):
        if not self._is_in_check(player):
            return False
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece != '.' and self.get_piece_owner(piece) == player:
                    for tr in range(self.size_rows):
                        for tc in range(self.size_cols):
                            if self._is_legal_move(piece, r, c, tr, tc):
                                if not self._would_be_illegal(r, c, tr, tc, player):
                                    return False
        return True

    def _has_any_legal_move(self, player):
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece != '.' and self.get_piece_owner(piece) == player:
                    for tr in range(self.size_rows):
                        for tc in range(self.size_cols):
                            if self._is_legal_move(piece, r, c, tr, tc):
                                if not self._would_be_illegal(r, c, tr, tc, player):
                                    return True
        return False

    def get_all_legal_moves(self, player):
        moves = []
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece != '.' and self.get_piece_owner(piece) == player:
                    for tr in range(self.size_rows):
                        for tc in range(self.size_cols):
                            if self._is_legal_move(piece, r, c, tr, tc):
                                if not self._would_be_illegal(r, c, tr, tc, player):
                                    moves.append((r, c, tr, tc))
        return moves

    def get_board_state_string(self):
        s = "   " + " ".join(chr(65 + i) for i in range(self.size_cols)) + "\n"
        for r in range(self.size_rows):
            s += f"{r+1:2d} " + " ".join(self.board[r][c] for c in range(self.size_cols)) + "\n"
        return s

    def format_move_history(self):
        """格式化走子历史，包含棋子名称、坐标、吃子标记"""
        if not self.moves:
            return "暂无移动"
        lines = []
        for idx, (fr, fc, tr, tc, player, captured, piece) in enumerate(self.moves, 1):
            piece_name = self.PIECE_SYMBOLS.get(piece, piece)
            from_coord = f"{chr(65 + fc)}{fr + 1}"
            to_coord = f"{chr(65 + tc)}{tr + 1}"
            player_name = '红方' if player == 1 else '黑方'

            line = f"{idx}. {player_name} {piece_name} {from_coord}→{to_coord}"
            # 标注吃子
            if captured != '.':
                captured_name = self.PIECE_SYMBOLS.get(captured, captured)
                line += f" 吃{captured_name}"
            lines.append(line)
        return "\n".join(lines)

    # ── 辅助方法（供搜索和开局库使用） ──

    def get_piece_at(self, row: int, col: int) -> str:
        """获取指定位置的棋子。返回 '.' 表示空位。"""
        if self.in_board(row, col):
            return self.board[row][col]
        return '.'

    def is_endgame(self) -> bool:
        """判断是否进入残局阶段。

        启发式标准：总子力 <= 14（大约初始子力的一半）视为残局。
        残局中兵/卒和将/帅的估值策略需要调整。
        """
        count = 0
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                if self.board[r][c] != '.':
                    count += 1
        # 初始 32 子，<= 14 子 ≈ 残局
        return count <= 14

    def count_pieces(self, player: int = 0) -> int:
        """统计棋子数量。

        Args:
            player: 0=双方, 1=仅红方, 2=仅黑方
        """
        count = 0
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece == '.':
                    continue
                if player == 0:
                    count += 1
                elif player == 1 and self.is_red(piece):
                    count += 1
                elif player == 2 and self.is_black(piece):
                    count += 1
        return count

    def board_hash(self) -> int:
        """计算当前棋盘局面的哈希值（用于开局库查询和置换表）。

        使用 Zobrist-like 简化哈希：将每格的棋子字符转为整数加权。
        注意：此哈希不考虑走子方，仅用于识别局面。
        """
        h = 0
        for r in range(self.size_rows):
            for c in range(self.size_cols):
                piece = self.board[r][c]
                if piece != '.':
                    # 将棋子字符映射为唯一编号
                    piece_id = ord(piece) * 31 + r * 7 + c * 13
                    h ^= piece_id << ((r * self.size_cols + c) % 16)
        return h

    def get_move_key(self) -> tuple:
        """返回当前走子序列的关键字（用于开局库精确匹配）。

        Returns:
            ((fr,fc,tr,tc), ...) 走法序列元组
        """
        return tuple((m[0], m[1], m[2], m[3]) for m in self.moves)

    def get_board_copy(self) -> list:
        """返回棋盘副本（用于搜索等只读操作）"""
        return [row[:] for row in self.board]
