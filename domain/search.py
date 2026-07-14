"""中国象棋 Alpha-Beta 搜索引擎

特性：
- 迭代加深（Iterative Deepening）
- Alpha-Beta 剪枝
- 静态搜索（Quiescence Search）防止地平线效应
- 走法排序（MVV-LVA、杀手走法、历史启发）
- 时间控制
- 将军延伸（Check Extension）

性能优化：
- 叶子评估不生成走法（仅用棋子统计）
- 静态搜索仅搜索吃子走法
- 将军检测复用
"""

import time
from typing import Optional, Callable

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT
from domain.evaluation import (
    evaluate, evaluate_move_ordering, PIECE_VALUE,
)

# ══════════════════════════════════════════════════════════════════════════════
# 搜索配置
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_MAX_DEPTH = 4
DEFAULT_TIME_LIMIT = 5.0
DEFAULT_QUIESCENCE_DEPTH = 4    # 静态搜索最大额外深度
CHECK_EXTENSION_DEPTH = 1       # 将军时加深度（仅每分支一次，防止无限递归）


class SearchEngine:
    """中国象棋 Alpha-Beta 搜索引擎"""

    def __init__(self,
                 max_depth: int = DEFAULT_MAX_DEPTH,
                 time_limit: float = DEFAULT_TIME_LIMIT,
                 quiescence_depth: int = DEFAULT_QUIESCENCE_DEPTH,
                 progress_callback: Optional[Callable] = None):
        self.max_depth = max_depth
        self.time_limit = time_limit
        self.quiescence_depth = quiescence_depth
        self.progress_callback = progress_callback

        # 状态
        self._start_time: float = 0.0
        self._stop_flag: bool = False
        self._nodes_searched: int = 0
        self._best_move: Optional[tuple] = None
        self._best_score: float = 0.0

        # 走法排序表
        self._killer_moves: list = []
        self._history_table: dict = {}

        self._init_killers()

    def _init_killers(self):
        self._killer_moves = [[None, None] for _ in range(self.max_depth + 10)]

    # ── 公开接口 ──

    def search(self,
               game,
               player: int,
               on_progress: Optional[Callable] = None) -> Optional[tuple]:
        """主搜索入口 — 返回最佳走法 (fr, fc, tr, tc)。"""
        all_moves = game.get_all_legal_moves(player)
        if not all_moves:
            return None
        if len(all_moves) == 1:
            return all_moves[0]

        self._start_time = time.time()
        self._stop_flag = False
        self._nodes_searched = 0
        self._best_move = all_moves[0]
        self._best_score = float('-inf') if player == 1 else float('inf')
        self._init_killers()
        self._history_table = {}

        ordered_moves = self._order_moves(game.board, all_moves, player, 0)

        # 迭代加深
        for depth in range(1, self.max_depth + 1):
            if self._is_time_up():
                break

            current_best_move = None
            if player == 1:
                alpha, best_score = float('-inf'), float('-inf')
                for move in ordered_moves:
                    if self._is_time_up():
                        break
                    fr, fc, tr, tc = move
                    captured = self._make_move(game.board, fr, fc, tr, tc)
                    in_check = game._is_in_check(3 - player)
                    ext = CHECK_EXTENSION_DEPTH if in_check else 0
                    score = self._alpha_beta(
                        game, depth - 1 + ext, alpha, float('inf'),
                        3 - player)
                    self._unmake_move(game.board, fr, fc, tr, tc, captured)
                    if score > best_score:
                        best_score = score
                        current_best_move = move
                    alpha = max(alpha, score)
            else:
                alpha, beta = float('-inf'), float('inf')
                best_score = float('inf')
                for move in ordered_moves:
                    if self._is_time_up():
                        break
                    fr, fc, tr, tc = move
                    captured = self._make_move(game.board, fr, fc, tr, tc)
                    in_check = game._is_in_check(3 - player)
                    ext = CHECK_EXTENSION_DEPTH if in_check else 0
                    score = self._alpha_beta(
                        game, depth - 1 + ext, alpha, beta,
                        3 - player)
                    self._unmake_move(game.board, fr, fc, tr, tc, captured)
                    if score < best_score:
                        best_score = score
                        current_best_move = move
                    if best_score <= alpha:
                        break
                    beta = min(beta, score)

            if current_best_move is not None and not self._is_time_up():
                self._best_move = current_best_move
                self._best_score = best_score
                ordered_moves = self._promote_best(ordered_moves, current_best_move)

            if on_progress:
                on_progress(depth, self._best_score, self._best_move, self._nodes_searched)
            if self.progress_callback:
                self.progress_callback(depth, self._best_score, self._best_move,
                                      self._nodes_searched)

        return self._best_move

    # ── Alpha-Beta 核心 ──

    def _alpha_beta(self, game, depth: int, alpha: float, beta: float,
                    player: int, extended: bool = False) -> float:
        self._nodes_searched += 1

        # 超时截断检查（每 500 节点检查一次，减少时间调用开销）
        if self._nodes_searched % 500 == 0 and self._is_time_up():
            return self._fast_eval(game, player)

        # 叶子节点 → 进入静态搜索
        if depth <= 0:
            return self._quiescence(game, self.quiescence_depth,
                                    alpha, beta, player)

        # 生成走法
        moves = game.get_all_legal_moves(player)
        if not moves:
            if game._is_in_check(player):
                return float('-inf') if player == 1 else float('inf')
            return -50000 if player == 1 else 50000

        ordered_moves = self._order_moves(game.board, moves, player, depth)

        if player == 1:  # 红方 — 最大化
            best = float('-inf')
            for move in ordered_moves:
                if self._nodes_searched % 100 == 0 and self._is_time_up():
                    break
                fr, fc, tr, tc = move
                captured = self._make_move(game.board, fr, fc, tr, tc)
                in_check = game._is_in_check(3 - player)
                # 将军延伸：仅允许每分支一次，防止连续将军导致无限递归
                ext = CHECK_EXTENSION_DEPTH if (in_check and not extended) else 0
                score = self._alpha_beta(
                    game, depth - 1 + ext, alpha, beta, 3 - player,
                    extended=extended or (ext > 0))
                self._unmake_move(game.board, fr, fc, tr, tc, captured)
                best = max(best, score)
                alpha = max(alpha, score)
                if alpha >= beta:
                    self._record_killer(move, depth)
                    self._record_history(move, depth)
                    break
            return best
        else:  # 黑方 — 最小化
            best = float('inf')
            for move in ordered_moves:
                if self._nodes_searched % 100 == 0 and self._is_time_up():
                    break
                fr, fc, tr, tc = move
                captured = self._make_move(game.board, fr, fc, tr, tc)
                in_check = game._is_in_check(3 - player)
                ext = CHECK_EXTENSION_DEPTH if (in_check and not extended) else 0
                score = self._alpha_beta(
                    game, depth - 1 + ext, alpha, beta, 3 - player,
                    extended=extended or (ext > 0))
                self._unmake_move(game.board, fr, fc, tr, tc, captured)
                best = min(best, score)
                beta = min(beta, score)
                if alpha >= beta:
                    self._record_killer(move, depth)
                    self._record_history(move, depth)
                    break
            return best

    # ── 静态搜索 ──

    def _quiescence(self, game, depth: int, alpha: float, beta: float,
                    player: int) -> float:
        """静态搜索 — 仅搜索吃子走法，消除地平线效应。"""
        stand_pat = self._fast_eval(game, player)

        if player == 1:
            if stand_pat >= beta:
                return beta
            alpha = max(alpha, stand_pat)
        else:
            if stand_pat <= alpha:
                return alpha
            beta = min(beta, stand_pat)

        if depth <= 0:
            return stand_pat

        # 只生成吃子走法
        all_moves = game.get_all_legal_moves(player)
        captures = [(fr, fc, tr, tc) for fr, fc, tr, tc in all_moves
                     if game.board[tr][tc] != '.']

        if not captures:
            return stand_pat

        # 排序吃子走法
        ordered_captures = sorted(
            captures,
            key=lambda m: evaluate_move_ordering(
                game.board, m[0], m[1], m[2], m[3],
                game.board[m[0]][m[1]], game.board[m[2]][m[3]]),
            reverse=True,
        )

        for fr, fc, tr, tc in ordered_captures:
            captured_val = PIECE_VALUE.get(
                game.board[tr][tc].upper(), 0)
            if player == 1:
                if stand_pat + captured_val + 50 < alpha:
                    continue
            else:
                if stand_pat - captured_val - 50 > beta:
                    continue

            captured = self._make_move(game.board, fr, fc, tr, tc)
            self._nodes_searched += 1
            score = self._quiescence(
                game, depth - 1, alpha, beta, 3 - player)
            self._unmake_move(game.board, fr, fc, tr, tc, captured)

            if player == 1:
                if score >= beta:
                    return beta
                alpha = max(alpha, score)
            else:
                if score <= alpha:
                    return alpha
                beta = min(beta, score)

        return alpha if player == 1 else beta

    # ── 快速局面评估（不做走法生成） ──

    def _fast_eval(self, game, player: int) -> float:
        """快速评估 — 不生成走法，只用棋子统计近似。

        这是搜索的热路径，避免调用 get_all_legal_moves(~1.7ms)。
        """
        # 快速统计棋子数量（避免走法生成）
        red_pieces = 0
        black_pieces = 0
        red_material = 0
        black_material = 0
        board = game.board

        for r in range(BOARD_HEIGHT):
            for c in range(BOARD_WIDTH):
                p = board[r][c]
                if p == '.':
                    continue
                if p.isupper():
                    red_pieces += 1
                    red_material += PIECE_VALUE.get(p, 0)
                else:
                    black_pieces += 1
                    black_material += PIECE_VALUE.get(p.upper(), 0)

        # 判断是否残局
        endgame = (red_pieces + black_pieces) <= 14

        # 将军检测（始终检查双方）
        red_in_check = game._is_in_check(1)
        black_in_check = game._is_in_check(2)

        score = evaluate(
            board,
            legal_moves_red=0,   # 跳过走法生成
            legal_moves_black=0,  # 跳过走法生成
            red_in_check=red_in_check,
            black_in_check=black_in_check,
            endgame=endgame,
        )
        return score

    # ── 走法排序 ──

    def _order_moves(self, board: list, moves: list, player: int,
                     depth: int) -> list:
        """走法排序 — 提高 Alpha-Beta 剪枝效率"""
        def move_score(move):
            fr, fc, tr, tc = move
            piece = board[fr][fc]
            captured = board[tr][tc]
            s = 0

            if captured != '.':
                s += 100000 + evaluate_move_ordering(
                    board, fr, fc, tr, tc, piece, captured)

            killer_slot0 = self._killer_moves[depth][0]
            killer_slot1 = self._killer_moves[depth][1]
            if move == killer_slot0:
                s += 50000
            elif move == killer_slot1:
                s += 40000

            move_key = (fr, fc, tr, tc)
            s += self._history_table.get(move_key, 0)

            if piece.upper() == 'K' and captured == '.':
                s -= 3000

            return s

        return sorted(moves, key=move_score, reverse=True)

    def _record_killer(self, move: tuple, depth: int):
        if depth < len(self._killer_moves):
            if self._killer_moves[depth][0] != move:
                self._killer_moves[depth][1] = self._killer_moves[depth][0]
                self._killer_moves[depth][0] = move

    def _record_history(self, move: tuple, depth: int):
        move_key = (move[0], move[1], move[2], move[3])
        self._history_table[move_key] = (
            self._history_table.get(move_key, 0) + depth * depth
        )

    def _promote_best(self, moves: list, best_move: tuple) -> list:
        result = list(moves)
        try:
            idx = result.index(best_move)
            result.insert(0, result.pop(idx))
        except ValueError:
            pass
        return result

    # ── 走子执行/撤销 ──

    @staticmethod
    def _make_move(board: list, fr: int, fc: int,
                   tr: int, tc: int) -> str:
        captured = board[tr][tc]
        board[tr][tc] = board[fr][fc]
        board[fr][fc] = '.'
        return captured

    @staticmethod
    def _unmake_move(board: list, fr: int, fc: int,
                     tr: int, tc: int, captured: str):
        board[fr][fc] = board[tr][tc]
        board[tr][tc] = captured

    # ── 时间控制 ──

    def _is_time_up(self) -> bool:
        if self._stop_flag:
            return True
        return (time.time() - self._start_time) > self.time_limit

    def stop(self):
        self._stop_flag = True

    @property
    def nodes_searched(self) -> int:
        return self._nodes_searched

    @property
    def best_score(self) -> float:
        """搜索最终评分（正值=红优，负值=黑优）。"""
        return self._best_score
