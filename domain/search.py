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
from collections import OrderedDict
from enum import IntEnum
from typing import Optional, Callable, NamedTuple

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT, SEARCH_TIME_LIMIT, EGTB_MAX_PIECES, ENDGAME_PIECE_THRESHOLD
from domain.evaluation import (
    evaluate, evaluate_fast, evaluate_move_ordering,
    PIECE_VALUE, PIECE_VALUE_ENDGAME,
    RED_PST,
)
from domain.game import ZOBRIST_TABLE


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

    用 OrderedDict 实现 LRU 淘汰：最近访问的条目移至末尾，
    容量达上限时淘汰最久未用的条目（O(1)），无大规模内存分配。
    """

    def __init__(self) -> None:
        self._table: OrderedDict = OrderedDict()
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
        命中时将条目移至末尾（标记为最近使用）。

        Returns:
            (hit: bool, score: float, best_move: tuple | None)
        """
        self._probes += 1
        entry = self._table.get(hash_key)
        if entry is None:
            return False, 0.0, None
        # LRU: 标记为最近访问
        self._table.move_to_end(hash_key)
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
        """存入置换表。深度优先替换：同 hash 下保留深度更大的条目。

        LRU 淘汰：满容量时 pop 最旧条目（O(1)），消除旧 FIFO 的
        list(self._table.keys())[:N] 大规模内存分配。
        """
        existing = self._table.get(hash_key)
        if existing is not None:
            if existing.depth >= depth:
                return  # 已有更深或同深度的条目，保留
            # 删除旧条目，下面重新插入（更新深度+LRU位置）
            del self._table[hash_key]
        # 容量控制：LRU 淘汰最久未用的一个条目
        if len(self._table) >= TT_MAX_SIZE:
            self._table.popitem(last=False)  # O(1) FIFO → 实际即为 LRU
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

DEFAULT_MAX_DEPTH = 8
DEFAULT_TIME_LIMIT = SEARCH_TIME_LIMIT
DEFAULT_QUIESCENCE_DEPTH = 4    # 静态搜索最大额外深度
QS_EVASION_EXTRA_DEPTH = 4      # 被将军时 qs 允许超出深度上限的额外层数（防长将链无限递归）
CHECK_EXTENSION_DEPTH = 1       # 将军时加深度（仅每分支一次，防止无限递归）
NULL_MOVE_R = 2                 # 空着裁剪缩减因子
NULL_MOVE_MIN_DEPTH = 7         # 空着裁剪最小深度（验证深度=depth-1-R≥3；
                                # 浅验证误剪风险高，宁可不裁）
ZUGZWANG_PIECE_LIMIT = 8        # 少于该子力数不进行空着裁剪（防止逼着误判）

# ── LMR (Late Move Reduction) 配置 ──
LMR_BASE_REDUCTION = 1          # 基础缩减层数
LMR_FULL_DEPTH_MOVES = 4        # 前 N 个走法不缩减（全深度搜索）
LMR_MIN_DEPTH = 3               # 剩余深度 < 此值不缩减（浅层不减以免漏杀）

# Negamax 特殊分值
JIANGSHA_SCORE = 99999          # 将杀基础分
KUNBI_SCORE = 50000             # 困毙基础分
# |score| 超过此值视为杀/困分：存取置换表必须按 ply 折算
# （静态评估量级远低于此；EGTB 大分本身即杀棋距离分，折算方向一致）
MATE_TT_BOUND = KUNBI_SCORE - 10000


def _tt_score_to_relative(score: float, ply: int) -> float:
    """杀/困分存表前按 ply 折算为相对当前节点的值。

    杀分里编码的是"距本次搜索根的距离"；同一局面在不同迭代、不同
    换位路径下 ply 不同，不折算会让跨迭代共享的杀棋步数失真。
    probe 命中时按当前 ply 反向还原（见 _alpha_beta 的 TT 查找）。
    """
    if score > MATE_TT_BOUND:
        return score + ply
    if score < -MATE_TT_BOUND:
        return score - ply
    return score


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

        ordered_moves = None

        try:
            # 迭代加深（Negamax 统一框架——不再区分红/黑）
            for depth in range(1, self.max_depth + 1):
                if self._is_time_up():
                    break

                alpha = float('-inf')
                beta = float('inf')
                best_score = float('-inf')
                current_best_move = None

                # 每层迭代完整重排（上轮最佳作 TT 走法 + killer + history），
                # 旧实现只把上轮最佳提前，积累的排序信息全被浪费
                ordered_moves = self._order_moves(
                    game.board, all_moves, player, 0,
                    tt_move=self._best_move if depth > 1 else None)

                for i, move in enumerate(ordered_moves):
                    if self._is_time_up():
                        break
                    fr, fc, tr, tc = move
                    captured = self._make_move(game, fr, fc, tr, tc)
                    in_check = game.is_in_check(3 - player)
                    ext = CHECK_EXTENSION_DEPTH if in_check else 0
                    # Negamax：递归返回 3-player 视角，取反得 player 视角
                    if i == 0:
                        # 第一个走法（大概率最优）：全窗口搜索
                        score = -self._alpha_beta(
                            game, depth - 1 + ext, -beta, -alpha, 3 - player)
                    else:
                        # 根节点 PVS：零窗口试探，优于预期再全窗口重搜
                        score = -self._alpha_beta(
                            game, depth - 1 + ext, -alpha - 1, -alpha,
                            3 - player)
                        if score > alpha:
                            score = -self._alpha_beta(
                                game, depth - 1 + ext, -beta, -alpha,
                                3 - player)
                    self._unmake_move(game, fr, fc, tr, tc, captured)

                    if score > best_score:
                        best_score = score
                        current_best_move = move
                    alpha = max(alpha, score)

                if current_best_move is not None and not self._is_time_up():
                    self._best_move = current_best_move
                    # _best_score 对外保持红方视角（正值=红优）
                    self._best_score = best_score if player == 1 else -best_score

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
            # 杀/困分存表时已按 ply 折算为相对当前节点的值，
            # 命中须按本次 ply 还原（否则跨迭代/换位的杀棋步数失真）
            ply = self.max_depth - depth
            if tt_score > MATE_TT_BOUND:
                return tt_score - ply
            if tt_score < -MATE_TT_BOUND:
                return tt_score + ply
            return tt_score

        # ── 空着裁剪 (Null Move Pruning) ──
        # 如果跳过己方回合（让对手连走两步）仍无法被击败，
        # 说明局面太好，可以直接剪枝。
        in_check_before_nmp = game.is_in_check(player)
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
            if game.is_in_check(player):
                # 将杀 — 距离根节点越近（depth 剩余越大）越差
                return -(JIANGSHA_SCORE - (self.max_depth - depth))
            # 困毙（同样输棋，但评分略轻）
            return -(KUNBI_SCORE - (self.max_depth - depth))

        ordered_moves = self._order_moves(game.board, moves, player, depth, tt_move)

        best = float('-inf')
        best_move = None
        orig_alpha = alpha

        for i, move in enumerate(ordered_moves):
            if self._nodes_searched % 100 == 0 and self._is_time_up():
                break

            fr, fc, tr, tc = move
            captured = self._make_move(game, fr, fc, tr, tc)
            in_check = game.is_in_check(3 - player)
            # 将军延伸：仅允许每分支一次，防止连续将军导致无限递归
            ext = CHECK_EXTENSION_DEPTH if (in_check and not extended) else 0

            if i == 0:
                # 第一个走法（大概率是 PV 节点）：全窗口搜索
                score = -self._alpha_beta(
                    game, depth - 1 + ext, -beta, -alpha, 3 - player,
                    extended=extended or (ext > 0))
            else:
                # PVS：零窗口搜索试探（alpha, alpha+1）
                # LMR：安静走法且序号靠后时用缩减深度试探
                is_quiet = (captured == '.')
                if (is_quiet and depth >= LMR_MIN_DEPTH
                        and i >= LMR_FULL_DEPTH_MOVES and ext == 0):
                    reduction = LMR_BASE_REDUCTION + (i - LMR_FULL_DEPTH_MOVES) // 4
                    reduced_depth = max(1, depth - 1 + ext - reduction)
                    score = -self._alpha_beta(
                        game, reduced_depth, -alpha - 1, -alpha,
                        3 - player, extended=extended or (ext > 0))
                    if score > alpha:
                        # LMR 结果优于预期 → 重新全深度零窗口搜索
                        score = -self._alpha_beta(
                            game, depth - 1 + ext, -alpha - 1, -alpha,
                            3 - player, extended=extended or (ext > 0))
                else:
                    score = -self._alpha_beta(
                        game, depth - 1 + ext, -alpha - 1, -alpha,
                        3 - player, extended=extended or (ext > 0))
                if score > alpha and score < beta:
                    # 走法优于预期 → 重新全窗口搜索
                    score = -self._alpha_beta(
                        game, depth - 1 + ext, -beta, -alpha, 3 - player,
                        extended=extended or (ext > 0))

            self._unmake_move(game, fr, fc, tr, tc, captured)

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
                # 杀/困分按 ply 折算为相对值（probe 命中时反向还原）
                self._tt.store(hash_key, depth,
                               _tt_score_to_relative(
                                   score, self.max_depth - depth),
                               TTFlag.LOWER_BOUND, move)
                return best

        # 所有走法搜索完毕
        if best > orig_alpha:
            flag = TTFlag.EXACT
        else:
            flag = TTFlag.UPPER_BOUND  # 未提升 alpha → 上界（true score <= best）
        self._tt.store(hash_key, depth,
                       _tt_score_to_relative(best, self.max_depth - depth),
                       flag, best_move)
        return best

    # ── 静态搜索（Negamax） ──

    def _quiescence(self, game, depth: int, alpha: float, beta: float,
                    player: int) -> float:
        """静态搜索（Negamax 版本）— 仅搜索吃子走法，消除地平线效应。

        返回从 player 视角的评分。
        被将军时禁止 stand_pat：必须搜索全部应将走法，无走法 = 被将杀，
        否则深度边界的将杀会被系统性漏判。
        """
        # 同步 game.current_player（与 _alpha_beta 一致）
        game.current_player = player

        # 超时截断
        if self._nodes_searched % 200 == 0 and self._is_time_up():
            return self._fast_eval(game, player)

        in_check = game.is_in_check(player)
        stand_pat = self._fast_eval(game, player)

        if in_check:
            # 被将军：生成全部合法应将走法（不限吃子）
            evasions = game.get_all_legal_moves(player)
            if not evasions:
                # 将杀 — 离根越近越糟（depth 越大越接近 qs 入口）
                return -(JIANGSHA_SCORE
                         - (self.max_depth + self.quiescence_depth - depth))
            if depth <= -QS_EVASION_EXTRA_DEPTH:
                # 将军链过长（长将类线路）：截断递归，退化为静态评估，
                # 防止连续将军导致 qs 无限延伸
                return stand_pat
            ordered_moves = sorted(
                evasions,
                key=lambda m: evaluate_move_ordering(
                    game.board, m[0], m[1], m[2], m[3],
                    game.board[m[0]][m[1]], game.board[m[2]][m[3]]),
                reverse=True,
            )
            best = float('-inf')
            for i, (fr, fc, tr, tc) in enumerate(ordered_moves):
                if i % 50 == 0 and self._is_time_up():
                    break
                captured = self._make_move(game, fr, fc, tr, tc)
                self._nodes_searched += 1
                score = -self._quiescence(
                    game, depth - 1, -beta, -alpha, 3 - player)
                self._unmake_move(game, fr, fc, tr, tc, captured)
                if score >= beta:
                    return beta
                best = max(best, score)
                alpha = max(alpha, score)
            return alpha if best != float('-inf') else stand_pat

        if stand_pat >= beta:
            return beta
        alpha = max(alpha, stand_pat)

        if depth <= 0:
            return stand_pat

        # 只生成吃子走法（定向生成，跳过非吃子的应将校验）
        captures = game.get_capture_moves(player)

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

        for i, (fr, fc, tr, tc) in enumerate(ordered_captures):
            # 超时截断（每 50 次检查一次）
            if i % 50 == 0 and self._is_time_up():
                return alpha

            # Delta 剪枝：即使吃掉这个子也达不到 alpha，跳过
            captured_val = PIECE_VALUE.get(
                game.board[tr][tc].upper(), 0)
            if stand_pat + captured_val + 50 < alpha:
                continue

            captured = self._make_move(game, fr, fc, tr, tc)
            self._nodes_searched += 1
            # Negamax 递归：对手视角取反
            score = -self._quiescence(
                game, depth - 1, -beta, -alpha, 3 - player)
            self._unmake_move(game, fr, fc, tr, tc, captured)

            if score >= beta:
                return beta
            alpha = max(alpha, score)

        return alpha

    # ── 快速局面评估（Negamax：返回 player 视角） ──

    def _fast_eval(self, game, player: int) -> float:
        """快速评估 — 不生成走法，返回从 player 视角的评分。

        使用增量缓存的 material/PST/count 跳过全盘统计扫描，
        仅用 evaluate_fast() 做棋盘扫描收集棋子位置用于关系特征。
        evaluate() 始终返回红方视角，此处根据 player 转换。
        """
        board = game.board
        total_pieces = game._red_piece_count + game._black_piece_count

        # ── 残局库查询（仅本地库：搜索叶节点禁止同步联网）──
        if total_pieces <= EGTB_MAX_PIECES:
            try:
                from domain.egtb import probe
                egtb_result = probe(board, player, total_pieces,
                                    allow_cloud=False)
                if egtb_result is not None:
                    return egtb_result[0]
            except Exception:
                pass

        endgame = total_pieces <= ENDGAME_PIECE_THRESHOLD

        # 将军检测（始终检查双方）—— O(~20) per side，已在 _king_pos 缓存加速
        red_in_check = game.is_in_check(1)
        black_in_check = game.is_in_check(2)

        # 从 _material_counts 字典 × 正确阶段估值表 计算物质分
        vals = PIECE_VALUE_ENDGAME if endgame else PIECE_VALUE
        red_material = sum(vals.get(p.upper(), 0) * cnt
                           for p, cnt in game._material_counts.items()
                           if p.isupper() and p != 'K')
        black_material = sum(vals.get(p.upper(), 0) * cnt
                             for p, cnt in game._material_counts.items()
                             if p.islower() and p != 'k')

        score = evaluate_fast(
            board,
            red_material=red_material,
            black_material=black_material,
            red_pst_score=game._red_pst_score,
            black_pst_score=game._black_pst_score,
            red_in_check=red_in_check,
            black_in_check=black_in_check,
            endgame=endgame,
        )
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

    # ── 走子执行/撤销 ──

    @staticmethod
    def _make_move(game, fr: int, fc: int,
                   tr: int, tc: int) -> str:
        """在 game 上执行走法并同步 _king_pos 缓存与 Zobrist 哈希，
        返回被吃棋子。

        注意：不修改 current_player、不走合法性校验，仅供搜索内部
        （及 MCTS / worker 的临时局面）配合 _unmake_move 成对使用。
        """
        board = game.board
        piece = board[fr][fc]
        captured = board[tr][tc]
        board[tr][tc] = piece
        board[fr][fc] = '.'
        # 动将时同步缓存，避免后续 _is_in_check 全部回退全盘扫描
        if piece == 'K':
            game._king_pos[1] = (tr, tc)
        elif piece == 'k':
            game._king_pos[2] = (tr, tc)
        zi_from = fr * game.size_cols + fc
        zi_to = tr * game.size_cols + tc
        game._zobrist ^= (ZOBRIST_TABLE[piece][zi_from]
                          ^ ZOBRIST_TABLE[piece][zi_to])
        if captured != '.':
            game._zobrist ^= ZOBRIST_TABLE[captured][zi_to]

        # ── 增量评估缓存：PST + _material_counts + piece_count ──
        _pu = piece.upper()
        if _pu in RED_PST:
            # 移动方 PST：减去旧位置，加上新位置
            if piece.isupper():
                game._red_pst_score -= RED_PST[_pu][fr][fc]
                game._red_pst_score += RED_PST[_pu][tr][tc]
            else:
                game._black_pst_score -= RED_PST[_pu][BOARD_HEIGHT - 1 - fr][fc]
                game._black_pst_score += RED_PST[_pu][BOARD_HEIGHT - 1 - tr][tc]
        if captured != '.':
            # 被吃子计数/PST
            game._material_counts[captured] = game._material_counts.get(captured, 0) - 1
            if captured.isupper():
                game._red_piece_count -= 1
            else:
                game._black_piece_count -= 1
            _cu = captured.upper()
            if _cu in RED_PST:
                if captured.isupper():
                    game._red_pst_score -= RED_PST[_cu][tr][tc]
                else:
                    game._black_pst_score -= RED_PST[_cu][BOARD_HEIGHT - 1 - tr][tc]

        return captured

    @staticmethod
    def _unmake_move(game, fr: int, fc: int,
                     tr: int, tc: int, captured: str):
        """撤销 _make_move 的走法并恢复 _king_pos 缓存与 Zobrist 哈希。"""
        board = game.board
        piece = board[tr][tc]
        board[fr][fc] = piece
        board[tr][tc] = captured
        if piece == 'K':
            game._king_pos[1] = (fr, fc)
        elif piece == 'k':
            game._king_pos[2] = (fr, fc)
        zi_from = fr * game.size_cols + fc
        zi_to = tr * game.size_cols + tc
        game._zobrist ^= (ZOBRIST_TABLE[piece][zi_from]
                          ^ ZOBRIST_TABLE[piece][zi_to])
        if captured != '.':
            game._zobrist ^= ZOBRIST_TABLE[captured][zi_to]

        # ── 增量评估缓存：逆操作 —— 恢复被吃子，移动子回原位 ──
        _pu = piece.upper()
        if _pu in RED_PST:
            # PST：减去新位置，加回旧位置（与 _make_move 相反）
            if piece.isupper():
                game._red_pst_score -= RED_PST[_pu][tr][tc]
                game._red_pst_score += RED_PST[_pu][fr][fc]
            else:
                game._black_pst_score -= RED_PST[_pu][BOARD_HEIGHT - 1 - tr][tc]
                game._black_pst_score += RED_PST[_pu][BOARD_HEIGHT - 1 - fr][fc]
        if captured != '.':
            # 恢复被吃子
            game._material_counts[captured] = game._material_counts.get(captured, 0) + 1
            if captured.isupper():
                game._red_piece_count += 1
            else:
                game._black_piece_count += 1
            _cu = captured.upper()
            if _cu in RED_PST:
                if captured.isupper():
                    game._red_pst_score += RED_PST[_cu][tr][tc]
                else:
                    game._black_pst_score += RED_PST[_cu][BOARD_HEIGHT - 1 - tr][tc]

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
