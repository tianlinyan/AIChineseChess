"""中国象棋 MCTS 搜索引擎

蒙特卡洛树搜索 (Monte Carlo Tree Search) — 选择性搜索，专注于有希望的分支。

算法（四阶段）：
  1. Selection — 从根节点沿 UCB1 最大路径下降
  2. Expansion  — 到达叶节点后扩展一个新子节点
  3. Simulation — 对新局面做快速评估（用评估函数替代随机模拟）
  4. Backprop   — 将评估值沿路径回传到根节点

特性：
  - UCB1 选择策略（探索/利用平衡）
  - 先验概率支持（LLM 引导搜索）
  - 时间控制（模拟次数+时间限制）
  - 评估函数驱动的模拟（比随机走子更准确）

注意：Selection 下降时会在工作局面上真实走子（SearchEngine._make_move），
Expansion/Simulation 作用于到达的叶局面，回溯后撤销 —— 树中每个节点
都对应真实局面，而不是始终评估根局面。
"""

import time
import math
import random
from typing import Optional, Dict, List, Tuple

from domain.constants import MCTS_TIME_LIMIT, MCTS_EXPLORATION
from domain.evaluation import evaluate
from domain.egtb import probe
from domain.search import SearchEngine, _engine_time_up, _engine_stop

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SIMULATIONS = 2000      # 默认模拟次数
DEFAULT_TIME_LIMIT = MCTS_TIME_LIMIT       # 时间上限（秒），从 constants.py 统一管理
DEFAULT_EXPLORATION = MCTS_EXPLORATION     # UCB1 探索参数，从 constants.py 统一管理


# ══════════════════════════════════════════════════════════════════════════════
# MCTS 节点
# ══════════════════════════════════════════════════════════════════════════════

class MCTSNode:
    """MCTS 树节点，用 __slots__ 优化内存"""
    __slots__ = ('move', 'parent', 'children', 'visits', 'value',
                 'prior', 'player', 'is_expanded', '__weakref__')

    def __init__(self, move: Optional[tuple] = None,
                 parent: Optional['MCTSNode'] = None,
                 player: int = 1,
                 prior: float = 1.0):
        self.move = move          # (fr, fc, tr, tc) 到达此节点的走法
        self.parent = parent
        self.children: List['MCTSNode'] = []
        self.visits: int = 0       # 访问次数
        self.value: float = 0.0    # 累计价值（从当前玩家视角）
        self.prior: float = prior  # 先验概率（LLM提供）
        self.player: int = player  # 此节点的走子方（轮到谁走）
        self.is_expanded: bool = False

    @property
    def avg_value(self) -> float:
        """平均价值"""
        if self.visits == 0:
            return 0.0
        return self.value / self.visits

    def best_child(self) -> 'MCTSNode':
        """返回最优子节点（访问次数最多）"""
        if not self.children:
            return None
        return max(self.children, key=lambda c: c.visits)


# ══════════════════════════════════════════════════════════════════════════════
# MCTS 搜索引擎
# ══════════════════════════════════════════════════════════════════════════════

class MCTSEngine:
    """中国象棋 MCTS 搜索引擎"""

    def __init__(self,
                 max_simulations: int = DEFAULT_SIMULATIONS,
                 time_limit: float = DEFAULT_TIME_LIMIT,
                 exploration: float = DEFAULT_EXPLORATION):
        self.max_simulations = max_simulations
        self.time_limit = time_limit
        self.exploration = exploration

        self._start_time = 0.0
        self._stop_flag = False
        self._simulations = 0
        self._root: Optional[MCTSNode] = None

    # ── 公开接口 ──

    def search(self,
               game,
               player: int,
               priors: Optional[Dict[tuple, float]] = None) -> Optional[tuple]:
        """MCTS 主搜索 — 返回最佳走法。

        Args:
            game: ChineseChessGame 实例
            player: 当前走子方 (1=红, 2=黑)
            priors: {move_tuple: prior_probability} LLM提供的先验，可选

        Returns:
            最佳走法 (fr, fc, tr, tc) 或 None
        """
        legal_moves = game.get_all_legal_moves(player)
        if not legal_moves:
            return None
        if len(legal_moves) == 1:
            return legal_moves[0]

        self._start_time = time.time()
        self._stop_flag = False
        self._simulations = 0

        # 构建根节点
        self._root = MCTSNode(player=player)

        # ── NNUE 引导的先验分布 ──
        if priors is None:
            priors = self._compute_nnue_priors(game, legal_moves, player)

        # 展开根节点的所有合法子节点
        # PUCT 公式通过 P * sqrt(N) / (1 + visits) 自然加权先验，
        # 无需虚拟访问（UCB1 时代的人工偏移在 PUCT 下反而扭曲统计）
        for move in legal_moves:
            prior = priors.get(move, 1.0) if priors else 1.0
            child = MCTSNode(move=move, parent=self._root, player=3 - player,
                           prior=prior)
            self._root.children.append(child)

        self._root.is_expanded = True

        # 工作局面副本：Selection/Expansion/Simulation 在其上真实走子，
        # 不污染调用方的 game（棋子在棋盘上的移动见 _select/_expand）
        work = game.snapshot()
        work.current_player = player

        # 主循环
        while self._simulations < self.max_simulations:
            if self._is_time_up():
                break

            # 1. Selection — 从根沿 UCB1 下降到叶节点，沿途在 work 上走子
            node, path, captured_list = self._select(self._root, work)

            try:
                # 2. Expansion — 叶节点未展开则在当前（真实）局面上展开
                if not node.is_expanded:
                    self._expand(node, work)

                # 3. Simulation — 评估到达的叶局面
                if not node.children:
                    value = 0.0
                else:
                    value = self._simulate(work, node.player)
            finally:
                # 撤销路径走子，恢复根局面（即使 simulate 异常也必须执行）
                for move, captured in zip(reversed(path), reversed(captured_list)):
                    SearchEngine._unmake_move(work, *move, captured)

            # 4. Backpropagation — 回传结果
            self._backpropagate(node, value, node.player)

            self._simulations += 1

        # 选择最优走法（访问次数最多的子节点）
        best = self._root.best_child()
        if best is None:
            return legal_moves[0]

        return best.move

    # ── 四阶段 ──

    def _select(self, node: MCTSNode, game) -> tuple:
        """Selection: 沿 PUCT 最优路径下降到叶节点。

        PUCT 公式（AlphaZero）：
          U(s,a) = Q(s,a) + c_puct * P(s,a) * sqrt(N_parent) / (1 + N_child)

        沿途在 game 上真实执行走法（调用方负责在模拟后按逆序 unmake）。
        子节点 value 从子节点玩家视角存储，父节点选取对己方最有利的
        子节点（即对子节点取反）。

        Returns:
            (叶节点, 路径走法列表, 每步被吃子列表)
        """
        path = []
        captured_list = []
        while node.is_expanded and node.children:
            # 收集未访问子节点，按 prior + 小随机噪声选（避免走法排序偏差）
            unvisited = [c for c in node.children if c.visits == 0]
            if unvisited:
                # 有 prior 的优先，但加噪声避免确定性的排序偏差
                best_u = max(unvisited, key=lambda c: c.prior + random.random() * 0.01)
                node = best_u
                path.append(node.move)
                captured_list.append(SearchEngine._make_move(game, *node.move))
                break
            # PUCT 选择
            best_child = None
            best_score = float('-inf')
            sqrt_parent = math.sqrt(node.visits)
            for child in node.children:
                # Q(s,a): 从父节点视角的价值（子节点 value 为对手视角，取反）
                exploit = -child.avg_value
                # PUCT 探索项: P * sqrt(N_parent) / (1 + N_child)
                explore = (self.exploration * child.prior * sqrt_parent
                          / (1 + child.visits))
                score = exploit + explore
                if score > best_score:
                    best_score = score
                    best_child = child
            node = best_child
            path.append(node.move)
            captured_list.append(SearchEngine._make_move(game, *node.move))
        return node, path, captured_list

    def _expand(self, node: MCTSNode, game) -> None:
        """Expansion: 为叶节点生成所有合法子节点"""
        moves = game.get_all_legal_moves(node.player)
        for move in moves:
            child = MCTSNode(move=move, parent=node, player=3 - node.player)
            node.children.append(child)
        node.is_expanded = True

    def _simulate(self, game, player: int) -> float:
        """Simulation: 用评估函数替代随机走子（更精确）

        优先查残局库（精确DTM），不可用时回退评估函数。
        返回从 player 视角的价值（正值=对player有利）。
        """
        board = game.board

        endgame = game.is_endgame()

        # ── 残局库查询（仅本地库；probe 内部自守卫子力上限）──
        try:
            egtb_result = probe(
                board, player, game._red_piece_count + game._black_piece_count,
                game._material_counts)
            if egtb_result is not None:
                # probe 返回的 score 已是 player 视角，直接归一化，
                # 不要像下面 evaluate() 那样再按 player 翻转
                score = egtb_result[0]
                return 1.0 / (1.0 + math.exp(-score / 1000.0))
        except Exception:
            pass

        red_check = game.is_in_check(1)
        black_check = game.is_in_check(2)

        score = evaluate(
            board,
            red_in_check=red_check,
            black_in_check=black_check,
            endgame=endgame,
        )

        # 归一化到 [0, 1] 区间（从 player 视角）
        # 原始 score: 正值=红优
        # player=1(红): value = sigmoid(score/1000)
        # player=2(黑): value = sigmoid(-score/1000)
        normalized = 1.0 / (1.0 + math.exp(-score / 1000.0))
        if player == 2:
            normalized = 1.0 - normalized
        return normalized

    def _backpropagate(self, node: MCTSNode, value: float, leaf_player: int) -> None:
        """Backpropagation: 将模拟结果沿路径回传到根节点。

        value 从 leaf_player（被模拟局面的走子方）视角；路径上各节点
        按自己的走子方交替取 1-value。
        """
        while node is not None:
            node.visits += 1
            # 价值从当前节点玩家的视角存储
            if node.player == leaf_player:
                node.value += value
            else:
                node.value += (1.0 - value)
            node = node.parent

    # ── 辅助 ──

    def _compute_nnue_priors(self, game, legal_moves: list,
                             player: int) -> Optional[Dict[tuple, float]]:
        """用 NNUE 快速评估每个候选走法，生成 softmax 先验分布。

        对每个候选走法：临时走子 → NNUE 评估 → 撤销走子 → softmax。
        无 NNUE 时返回 None（MCTS 使用均匀先验）。
        """
        try:
            from domain.nnue import get_nnue
            nnue = get_nnue()
            if nnue is None:
                return None
        except Exception:
            return None

        scores = {}
        board = game.board
        for move in legal_moves:
            fr, fc, tr, tc = move
            captured = SearchEngine._make_move(game, fr, fc, tr, tc)
            try:
                score = nnue.evaluate(board)  # 红方视角 centipawn
            finally:
                SearchEngine._unmake_move(game, fr, fc, tr, tc, captured)
            # 转为走子方视角
            scores[move] = score if player == 1 else -score

        if not scores:
            return None

        # Softmax 转换（temperature=50 centipawn 使分布合理尖锐）
        max_score = max(scores.values())
        total = 0.0
        priors = {}
        for move, s in scores.items():
            priors[move] = math.exp((s - max_score) / 50.0)
            total += priors[move]
        if total > 0:
            for move in priors:
                priors[move] /= total

        return priors

    def _is_time_up(self) -> bool:
        return _engine_time_up(self)

    def stop(self):
        _engine_stop(self)

    @property
    def simulations(self) -> int:
        return self._simulations

    def get_top_moves(self, n: int = 5) -> List[Tuple[tuple, int, float]]:
        """返回访问次数最多的前 N 个走法"""
        if not self._root or not self._root.children:
            return []
        sorted_children = sorted(self._root.children,
                                 key=lambda c: c.visits, reverse=True)
        result = []
        for child in sorted_children[:n]:
            result.append((child.move, child.visits, child.avg_value))
        return result
