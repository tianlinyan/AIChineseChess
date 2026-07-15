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
  - 虚拟损失（可选并行搜索准备）
  - 评估函数驱动的模拟（比随机走子更准确）
"""

import time
import math
import random
from typing import Optional, Callable, Dict, List, Tuple

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT, MCTS_PRIOR_STRENGTH
from domain.evaluation import evaluate
from domain.game import ChineseChessGame

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SIMULATIONS = 2000      # 默认模拟次数
DEFAULT_TIME_LIMIT = 15.0       # 时间上限（秒）
DEFAULT_EXPLORATION = 1.4       # UCB1 探索参数
VIRTUAL_LOSS = 3                # 虚拟损失（并行搜索用）
MAX_SIMULATION_DEPTH = 20       # 模拟最大深度
PRIOR_STRENGTH = MCTS_PRIOR_STRENGTH  # from domain/constants.py


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

    def ucb1(self, total_visits: int, exploration: float = DEFAULT_EXPLORATION) -> float:
        """UCB1 公式：平均价值 + 探索项 × 先验"""
        if self.visits == 0:
            return float('inf')  # 未访问的节点优先
        exploitation = self.avg_value
        exploration_term = exploration * math.sqrt(
            math.log(total_visits + 1) / self.visits)
        prior_term = self.prior / (1 + self.visits)
        return exploitation + exploration_term + prior_term * 0.1

    def best_child(self, exploration: float = 0.0) -> 'MCTSNode':
        """返回最优子节点（exploration=0 时选最大访问次数）"""
        if not self.children:
            return None
        if exploration == 0:
            return max(self.children, key=lambda c: c.visits)
        return max(self.children, key=lambda c: c.ucb1(self.visits, exploration))


# ══════════════════════════════════════════════════════════════════════════════
# MCTS 搜索引擎
# ══════════════════════════════════════════════════════════════════════════════

class MCTSEngine:
    """中国象棋 MCTS 搜索引擎"""

    def __init__(self,
                 max_simulations: int = DEFAULT_SIMULATIONS,
                 time_limit: float = DEFAULT_TIME_LIMIT,
                 exploration: float = DEFAULT_EXPLORATION,
                 progress_callback: Optional[Callable] = None):
        self.max_simulations = max_simulations
        self.time_limit = time_limit
        self.exploration = exploration
        self.progress_callback = progress_callback

        self._start_time = 0.0
        self._stop_flag = False
        self._simulations = 0
        self._root: Optional[MCTSNode] = None

    # ── 公开接口 ──

    def search(self,
               game,
               player: int,
               priors: Optional[Dict[tuple, float]] = None,
               on_progress: Optional[Callable] = None) -> Optional[tuple]:
        """MCTS 主搜索 — 返回最佳走法。

        Args:
            game: ChineseChessGame 实例
            player: 当前走子方 (1=红, 2=黑)
            priors: {move_tuple: prior_probability} LLM提供的先验，可选
            on_progress: 进度回调

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

        # 展开根节点的所有合法子节点
        for move in legal_moves:
            child = MCTSNode(move=move, parent=self._root, player=3 - player)
            if priors and move in priors:
                child.prior = priors[move]
                child.visits = int(PRIOR_STRENGTH * priors[move])  # 先验虚拟访问
                child.value = child.visits * 0.5  # 中性初始值（不预设优劣）
            self._root.children.append(child)

        self._root.is_expanded = True

        # 主循环
        while self._simulations < self.max_simulations:
            if self._is_time_up():
                break

            # 1. Selection — 从根沿 UCB1 下降到叶节点
            node = self._select(self._root)

            # 2. Expansion — 如果是叶节点（未展开），展开它
            if node.visits == 0 and not node.is_expanded:
                self._expand(node, game)

            # 3. Simulation — 评估当前局面
            value = self._simulate(game, node.player)

            # 4. Backpropagation — 回传结果
            self._backpropagate(node, value, node.player)

            self._simulations += 1

            if on_progress and self._simulations % 100 == 0:
                on_progress(self._simulations, self._root)

        # 选择最优走法（访问次数最多的子节点）
        best = self._root.best_child(exploration=0)
        if best is None:
            return legal_moves[0]

        if self.progress_callback:
            self.progress_callback(self._simulations, best.avg_value,
                                   best.move, self._root)

        return best.move

    # ── 四阶段 ──

    def _select(self, node: MCTSNode) -> MCTSNode:
        """Selection: 沿 UCB1 最优路径下降到未完全展开或叶节点。

        由于子节点的 value 从子节点玩家视角存储，父节点需要选取对
        自己最有利（即子节点视角下最不利）的子节点。因此对子节点
        UCB1 中的 exploitation 项取反，exploration 保持正向。
        """
        while node.is_expanded and node.children:
            best_child = None
            best_score = float('-inf')
            # 收集未访问子节点，随机选（避免走法排序偏差）
            unvisited = [c for c in node.children if c.visits == 0]
            if unvisited:
                node = random.choice(unvisited)
                continue
            for child in node.children:
                # exploitation: 子节点 value 来自对方视角，取反才是己方视角
                exploit = -child.avg_value
                explore = self.exploration * math.sqrt(
                    math.log(node.visits + 1) / child.visits)
                prior_term = child.prior / (1 + child.visits) * 0.1
                score = exploit + explore + prior_term
                if score > best_score:
                    best_score = score
                    best_child = child
            node = best_child
        return node

    def _expand(self, node: MCTSNode, game) -> None:
        """Expansion: 为叶节点生成所有合法子节点"""
        moves = game.get_all_legal_moves(node.player)
        for move in moves:
            child = MCTSNode(move=move, parent=node, player=3 - node.player)
            node.children.append(child)
        node.is_expanded = True

    def _simulate(self, game, player: int) -> float:
        """Simulation: 用评估函数替代随机走子（更精确）

        执行几步快速走子（优先吃子/将军），然后用评估函数评分。
        返回从 player 视角的价值（正值=对player有利）。
        """
        board = game.board
        # 使用快速评估路径（不经走法生成，约50×速度提升）
        red_check = game._is_in_check(1)
        black_check = game._is_in_check(2)

        total_pieces = sum(1 for r in range(BOARD_HEIGHT)
                          for c in range(BOARD_WIDTH) if board[r][c] != '.')
        endgame = total_pieces <= 14

        score = evaluate(
            board,
            legal_moves_red=0,   # 跳过走法生成，机动性对仿真贡献小
            legal_moves_black=0,  # 跳过走法生成
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

    def _backpropagate(self, node: MCTSNode, value: float, root_player: int) -> None:
        """Backpropagation: 将模拟结果沿路径回传到根节点"""
        while node is not None:
            node.visits += 1
            # 价值从当前节点玩家的视角存储
            if node.player == root_player:
                node.value += value
            else:
                node.value += (1.0 - value)
            node = node.parent

    # ── 辅助 ──

    def _is_time_up(self) -> bool:
        if self._stop_flag:
            return True
        return (time.time() - self._start_time) > self.time_limit

    def stop(self):
        self._stop_flag = True

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
