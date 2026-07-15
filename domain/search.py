"""中国象棋 Alpha-Beta 搜索引擎（Negamax + PVS）

特性：
- 迭代加深（Iterative Deepening）
- Alpha-Beta 剪枝 + 主变例搜索 (PVS, Principal Variation Search)
- Negamax 统一框架（消除红/黑双份代码）
- 置换表（Transposition Table）避免重复搜索
- 静态搜索（Quiescence Search）防止地平线效应
- 走法排序（MVV-LVA、杀手走法、历史启发、TT 最佳走法）
- 时间控制
- 将军延伸（Check Extension）
- 空着裁剪（Null Move Pruning）

评分约定（Negamax）：
- 所有内部评分从当前走子方（player）视角表示：正值 = 对 player 有利
- evaluate() 返回红方视角，在 _fast_eval 中转换为 player 视角
- 搜索公开接口 best_score 属性保持红方视角（向下兼容）

性能优化：
- 叶子评估不生成走法（仅用棋子统计）
- 静态搜索仅搜索吃子走法
- 将军检测复用
- 置换表缓存减少 30-50% 搜索节点
- PVS 零窗口搜索减少约 20% 搜索节点
"""

import time
from enum import IntEnum
from typing import Optional, Callable, NamedTuple

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT
from domain.evaluation import (
    evaluate, evaluate_move_ordering, PIECE_VALUE,
)


class TTFlag(IntEnum):
    """置换表条目类型"""
    EXACT = 0         # 精确值
    LOWER_BOUND = 1   # 下界（beta 截断产生）
    UPPER_BOUND = 2   # 上界（all-node 未提升 alpha 产生）


class TTEntry(NamedTuple):
    """置换表条目 — 使用 NamedTuple 节省内存"""
    depth: int          # 搜索深度
    score: float        # 评分（Negamax：从局面走子方视角）
    flag: TTFlag        # 条目类型
    best_move: tuple    # 最佳走法 (fr, fc, tr, tc)


# 置换表最大容量（条目数），超过后用 FIFO 策略淘汰旧条目
TT_MAX_SIZE = 1_000_000


class TranspositionTable:
    """置换表 — 缓存已搜索过的局面，避免跨分支重复计算。

    用 Python dict 实现，key 为 position_hash(), value 为 TTEntry。
    容量达上限时淘汰最旧的一半条目（FIFO），防止内存无限增长。
    """

    def __init__(self) -> None:
        self._table: dict = {}
        self._hits: int = 0
        self._probes: int = 0

    def clear(self) -> None:
        self._table.clear()
        self._hits = 0
        self._probes = 0

    def probe(self, hash_key: int, depth: int,
              alpha: float, beta: float) -> tuple:
        """查找置换表。

        Negamax 版本：alpha/beta/score 均以当前局面走子方视角表示。
        置换表中存储的 score 也以存储时走子方视角表示——同一局面的
        position_hash 包含走子方，因此自动保持视角一致。

        Returns:
            (hit: bool, score: float, best_move: tuple | None)
        """
        self._probes += 1
        entry = self._table.get(hash_key)
        if entry is None:
            return False, 0.0, None
        if entry.depth < depth:
            return False, 0.0, entry.best_move  # 深度不够，但 best_move 可用于排序

        self._hits += 1
        score = entry.score
        if entry.flag == TTFlag.EXACT:
            return True, score, entry.best_move
        elif entry.flag == TTFlag.LOWER_BOUND:
            if score >= beta:
                return True, score, entry.best_move
        elif entry.flag == TTFlag.UPPER_BOUND:
            if score <= alpha:
                return True, score, entry.best_move
        return False, 0.0, entry.best_move  # 值在窗口内无法使用，但 best_move 可用

    def store(self, hash_key: int, depth: int, score: float,
              flag: TTFlag, best_move: tuple) -> None:
        """存入置换表。深度优先替换：同 hash 下保留深度更大的条目。"""
        existing = self._table.get(hash_key)
        if existing is not None and existing.depth >= depth:
            return  # 已有更深或同深度的条目，保留
        # 容量控制：达到上限时淘汰最旧的一半条目（FIFO 策略）
        if len(self._table) >= TT_MAX_SIZE:
            keys_to_evict = list(self._table.keys())[:len(self._table) // 2]
            for key in keys_to_evict:
                del self._table[key]
        self._table[hash_key] = TTEntry(depth, score, flag, best_move)

    @property
    def hit_rate(self) -> float:
        """命中率 (0.0 ~ 1.0)"""
        if self._probes == 0:
            return 0.0
        return self._hits / self._probes

    @property
    def size(self) -> int:
        return len(self._table)

# ══════════════════════════════════════════════════════════════════════════════
# 搜索配置
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_MAX_DEPTH = 5
DEFAULT_TIME_LIMIT = 15.0
DEFAULT_QUIESCENCE_DEPTH = 4    # 静态搜索最大额外深度
CHECK_EXTENSION_DEPTH = 1       # 将军时加深度（仅每分支一次，防止无限递归）
NULL_MOVE_R = 3                 # 空着裁剪缩减因子（>2 以保证验证深度足够）
NULL_MOVE_MIN_DEPTH = 4         # 空着裁剪最小深度（R+1）
ZUGZWANG_PIECE_LIMIT = 8        # 少于该子力数不进行空着裁剪（防止逼着误判）

# Negamax 特殊分值
MATE_SCORE = 99999              # 将杀基础分
STALEMATE_SCORE = 50000         # 困毙基础分（中国象棋中困毙=输）


class SearchEngine:
    """中国象棋 Alpha-Beta 搜索引擎（Negamax + PVS）"""

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
        self._best_score: float = 0.0   # 红方视角（向下兼容）

        # 走法排序表
        self._killer_moves: list = []
        self._history_table: dict = {}
        self._tt = TranspositionTable()

        self._init_killers()

    def _init_killers(self):
        self._killer_moves = [[None, None] for _ in range(self.max_depth + 10)]

    # ── 公开接口 ──

    def search(self,
               game,
               player: int,
               on_progress: Optional[Callable] = None) -> Optional[tuple]:
        """主搜索入口 — 返回最佳走法 (fr, fc, tr, tc)。

        Negamax 统一根节点：无论红方黑方，均用同一套最大化逻辑。
        _best_score 保持红方视角以兼容外部调用方。
        """
        all_moves = game.get_all_legal_moves(player)
        if not all_moves:
            return None
        if len(all_moves) == 1:
            return all_moves[0]

        # 保存并恢复 game.current_player —— 搜索内部会修改它以保证
        # position_hash() 使用正确的走子方，但不应外泄副作用
        saved_current_player = game.current_player

        self._start_time = time.time()
        self._stop_flag = False
        self._nodes_searched = 0
        self._best_move = all_moves[0]
        self._best_score = 0.0
        self._init_killers()
        self._history_table = {}
        self._tt.clear()

        ordered_moves = self._order_moves(game.board, all_moves, player, 0)

        try:
            # 迭代加深（Negamax 统一框架——不再区分红/黑）
            for depth in range(1, self.max_depth + 1):
                if self._is_time_up():
                    break

                alpha = float('-inf')
                beta = float('inf')
                best_score = float('-inf')
                current_best_move = None

                for move in ordered_moves:
                    if self._is_time_up():
                        break
                    fr, fc, tr, tc = move
                    captured = self._make_move(game.board, fr, fc, tr, tc)
                    in_check = game._is_in_check(3 - player)
                    ext = CHECK_EXTENSION_DEPTH if in_check else 0
                    # Negamax：递归返回 3-player 视角，取反得 player 视角
                    score = -self._alpha_beta(
                        game, depth - 1 + ext, -beta, -alpha, 3 - player)
                    self._unmake_move(game.board, fr, fc, tr, tc, captured)

                    if score > best_score:
                        best_score = score
                        current_best_move = move
                    alpha = max(alpha, score)

                if current_best_move is not None and not self._is_time_up():
                    self._best_move = current_best_move
                    # _best_score 对外保持红方视角（正值=红优）
                    self._best_score = best_score if player == 1 else -best_score
                    ordered_moves = self._promote_best(ordered_moves, current_best_move)

                if on_progress:
                    on_progress(depth, self._best_score, self._best_move,
                               self._nodes_searched)
                if self.progress_callback:
                    self.progress_callback(depth, self._best_score, self._best_move,
                                          self._nodes_searched)
        finally:
            # 恢复 game.current_player（即使搜索抛异常也恢复）
            game.current_player = saved_current_player

        return self._best_move

    # ── Alpha-Beta + PVS 核心（Negamax） ──

    def _alpha_beta(self, game, depth: int, alpha: float, beta: float,
                    player: int, extended: bool = False) -> float:
        """Alpha-Beta + PVS（Negamax 版本）。

        所有分值（alpha, beta, 返回值, TT 中存储的 score）均以当前
        player 视角表示。返回值为正 = 对 player 有利。

        PVS 策略：第一个走法全窗口搜索，后续走法先用零窗口试探；
        仅当试探显示走法可能优于当前最佳时才重新全窗口搜索。
        """
        self._nodes_searched += 1

        # 同步 game.current_player → 确保 position_hash() 使用正确的走子方
        # （_make_move / _unmake_move 不更新 current_player，需在此显式同步）
        game.current_player = player

        # 超时截断检查（每 500 节点检查一次，减少时间调用开销）
        if self._nodes_searched % 500 == 0 and self._is_time_up():
            return self._fast_eval(game, player)

        # ── 置换表查找 ──
        hash_key = game.position_hash()
        hit, tt_score, tt_move = self._tt.probe(hash_key, depth, alpha, beta)
        if hit:
            return tt_score

        # ── 空着裁剪 (Null Move Pruning) ──
        # 如果跳过己方回合（让对手连走两步）仍无法被击败，
        # 说明局面太好，可以直接剪枝。
        in_check_before_nmp = game._is_in_check(player)
        if (not in_check_before_nmp
                and depth >= NULL_MOVE_MIN_DEPTH
                and not extended):
            piece_count = sum(1 for r in range(BOARD_HEIGHT)
                            for c in range(BOARD_WIDTH)
                            if game.board[r][c] != '.')
            if piece_count >= ZUGZWANG_PIECE_LIMIT:
                saved_player = game.current_player
                game.current_player = 3 - player
                try:
                    # 零窗口搜索对手最佳应对；取反得己方视角
                    null_score = -self._alpha_beta(
                        game, depth - 1 - NULL_MOVE_R,
                        -beta, -beta + 1, 3 - player)
                finally:
                    game.current_player = saved_player
                if null_score >= beta:
                    return beta  # 局面太好，剪枝

        # 叶子节点 → 进入静态搜索
        if depth <= 0:
            return self._quiescence(game, self.quiescence_depth,
                                    alpha, beta, player)

        # 生成走法
        moves = game.get_all_legal_moves(player)
        if not moves:
            # 中国象棋：无合法走法 = 输棋（将杀或困毙）
            if game._is_in_check(player):
                # 将杀 — 距离根节点越近（depth 剩余越大）越差
                return -(MATE_SCORE - (self.max_depth - depth))
            # 困毙（同样输棋，但评分略轻）
            return -(STALEMATE_SCORE - (self.max_depth - depth))

        ordered_moves = self._order_moves(game.board, moves, player, depth, tt_move)

        best = float('-inf')
        best_move = None
        orig_alpha = alpha

        for i, move in enumerate(ordered_moves):
            if self._nodes_searched % 100 == 0 and self._is_time_up():
                break

            fr, fc, tr, tc = move
            captured = self._make_move(game.board, fr, fc, tr, tc)
            in_check = game._is_in_check(3 - player)
            # 将军延伸：仅允许每分支一次，防止连续将军导致无限递归
            ext = CHECK_EXTENSION_DEPTH if (in_check and not extended) else 0

            if i == 0:
                # 第一个走法（大概率是 PV 节点）：全窗口搜索
                score = -self._alpha_beta(
                    game, depth - 1 + ext, -beta, -alpha, 3 - player,
                    extended=extended or (ext > 0))
            else:
                # PVS：零窗口搜索试探（alpha, alpha+1）
                score = -self._alpha_beta(
                    game, depth - 1 + ext, -alpha - 1, -alpha, 3 - player,
                    extended=extended or (ext > 0))
                if score > alpha and score < beta:
                    # 走法优于预期 → 重新全窗口搜索
                    score = -self._alpha_beta(
                        game, depth - 1 + ext, -beta, -alpha, 3 - player,
                        extended=extended or (ext > 0))

            self._unmake_move(game.board, fr, fc, tr, tc, captured)

            if score > best:
                best = score
                best_move = move

            alpha = max(alpha, score)
            if alpha >= beta:
                # Beta 截断 — 当前走法对 player 太好，对手不会让此局面发生
                self._record_killer(move, depth)
                self._record_history(move, depth)
                # 存入置换表：beta 截断 → 下界（true score >= score）
                # 存触发截断的 score（而非 best），确保 best_move 与 score 一致
                self._tt.store(hash_key, depth, score, TTFlag.LOWER_BOUND, move)
                return best

        # 所有走法搜索完毕
        if best > orig_alpha:
            flag = TTFlag.EXACT
        else:
            flag = TTFlag.UPPER_BOUND  # 未提升 alpha → 上界（true score <= best）
        self._tt.store(hash_key, depth, best, flag, best_move)
        return best

    # ── 静态搜索（Negamax） ──

    def _quiescence(self, game, depth: int, alpha: float, beta: float,
                    player: int) -> float:
        """静态搜索（Negamax 版本）— 仅搜索吃子走法，消除地平线效应。

        返回从 player 视角的评分。
        """
        # 同步 game.current_player（与 _alpha_beta 一致）
        game.current_player = player
        stand_pat = self._fast_eval(game, player)

        if stand_pat >= beta:
            return beta
        alpha = max(alpha, stand_pat)

        if depth <= 0:
            return stand_pat

        # 只生成吃子走法
        all_moves = game.get_all_legal_moves(player)
        captures = [(fr, fc, tr, tc) for fr, fc, tr, tc in all_moves
                     if game.board[tr][tc] != '.']

        if not captures:
            return stand_pat

        # 排序吃子走法（MVV-LVA）
        ordered_captures = sorted(
            captures,
            key=lambda m: evaluate_move_ordering(
                game.board, m[0], m[1], m[2], m[3],
                game.board[m[0]][m[1]], game.board[m[2]][m[3]]),
            reverse=True,
        )

        for fr, fc, tr, tc in ordered_captures:
            # Delta 剪枝：即使吃掉这个子也达不到 alpha，跳过
            captured_val = PIECE_VALUE.get(
                game.board[tr][tc].upper(), 0)
            if stand_pat + captured_val + 50 < alpha:
                continue

            captured = self._make_move(game.board, fr, fc, tr, tc)
            self._nodes_searched += 1
            # Negamax 递归：对手视角取反
            score = -self._quiescence(
                game, depth - 1, -beta, -alpha, 3 - player)
            self._unmake_move(game.board, fr, fc, tr, tc, captured)

            if score >= beta:
                return beta
            alpha = max(alpha, score)

        return alpha

    # ── 快速局面评估（Negamax：返回 player 视角） ──

    def _fast_eval(self, game, player: int) -> float:
        """快速评估 — 不生成走法，返回从 player 视角的评分。

        evaluate() 始终返回红方视角，此处根据 player 转换。
        残局库查询也使用 player 参数以保证视角一致。
        """
        board = game.board

        # 统计总子力（用于残局判定）
        red_pieces = 0
        black_pieces = 0
        for r in range(BOARD_HEIGHT):
            for c in range(BOARD_WIDTH):
                p = board[r][c]
                if p == '.':
                    continue
                if p.isupper():
                    red_pieces += 1
                else:
                    black_pieces += 1

        total_pieces = red_pieces + black_pieces

        # ── 残局库查询 ──
        if total_pieces <= 10:
            try:
                from domain.egtb import probe
                egtb_result = probe(board, player, total_pieces)
                if egtb_result is not None:
                    # probe 返回的 score 已是 player 视角
                    return egtb_result[0]
            except ImportError:
                pass  # 模块不可用时静默跳过

        # 判断是否残局
        endgame = total_pieces <= 14

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
        # evaluate() 返回红方视角 → 转为 player 视角
        return score if player == 1 else -score

    # ── 走法排序 ──

    def _order_moves(self, board: list, moves: list, player: int,
                     depth: int, tt_move: tuple = None) -> list:
        """走法排序 — 提高 Alpha-Beta 剪枝效率

        排序优先级：TT 最佳 > 吃子(MVV-LVA) > 杀手 > 历史启发
        """
        def move_score(move):
            fr, fc, tr, tc = move
            piece = board[fr][fc]
            captured = board[tr][tc]
            s = 0

            # TT 最佳走法优先（最高优先级）
            if move == tt_move:
                s += 200000

            if captured != '.':
                s += 100000 + evaluate_move_ordering(
                    board, fr, fc, tr, tc, piece, captured)

            if depth < len(self._killer_moves):
                killer_slot0 = self._killer_moves[depth][0]
                killer_slot1 = self._killer_moves[depth][1]
            else:
                killer_slot0 = killer_slot1 = None
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

    @property
    def tt_hit_rate(self) -> float:
        """置换表命中率 (0.0 ~ 1.0)。"""
        return self._tt.hit_rate

    @property
    def tt_size(self) -> int:
        """置换表条目数。"""
        return self._tt.size
