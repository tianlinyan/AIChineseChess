"""TT 时间截止存储标记回归测试（P1 修复验证）

用法：python tests/test_tt_cutoff.py

背景：_alpha_beta 的走法循环被 _is_time_up() 中断后，部分搜索的 best
只是下界（未搜索走法可能更优）。修复前按 best > orig_alpha ? EXACT :
UPPER_BOUND 存入 TT，污染后续 probe；修复后统一存 LOWER_BOUND
（probe 的 LOWER_BOUND 分支仅在 score ≥ beta 时用作剪枝，语义安全）。

方法：桩化递归（固定子节点分值）+ 控制 _nodes_searched 使走法循环的
时间检查（每 100 节点）恰好在第 1 个走法搜索完、第 2 个走法之前命中，
确定性触发"部分搜索截断"路径，断言 TT 标记为 LOWER_BOUND。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.search import SearchEngine, TTFlag

FAILED = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        FAILED.append(name)


class FakeGame:
    """最小 game 桩：3 个合法走法、无将军、固定哈希。"""
    current_player = 1
    board = [['.'] * 9 for _ in range(10)]
    _red_piece_count = 0
    _black_piece_count = 0

    def position_hash(self):
        return 12345

    def get_all_legal_moves(self, player):
        return [(0, 0, 1, 0), (0, 1, 1, 1), (0, 2, 1, 2)]

    def is_in_check(self, player):
        return False


def run_partial_cutoff_search():
    """构造部分搜索截断：第 1 个走法搜索完（best 已更新），
    第 2 个走法前的时间检查命中 → timed_out break。"""
    engine = SearchEngine(max_depth=8, time_limit=100)
    engine._start_time = time.time()
    engine._stop_flag = False
    engine._nodes_searched = 0
    # 走法循环的时间检查（nodes % 100 == 0 门控）一旦放行即超时
    engine._is_time_up = lambda: True

    # 桩化递归：顶层走真实 _alpha_beta（走法循环），子节点固定返回 15，
    # 每次子调用 +99 节点 → 第 1 个走法搜索完后 nodes 恰为 100，
    # 第 2 个走法前的检查（100 % 100 == 0）触发超时 break
    real_ab = SearchEngine._alpha_beta
    calls = {'n': 0}

    def fake_ab(self, game, depth, alpha, beta, player, extended=False):
        calls['n'] += 1
        if calls['n'] > 1:
            self._nodes_searched += 99
            return 15
        return real_ab(self, game, depth, alpha, beta, player, extended)

    engine._alpha_beta = fake_ab.__get__(engine, SearchEngine)
    engine._fast_eval = lambda game, player: 0.0
    engine._order_moves = lambda board, moves, player, depth, tt_move=None: list(moves)
    engine._make_move = lambda game, fr, fc, tr, tc: '.'
    engine._unmake_move = lambda game, fr, fc, tr, tc, captured: None

    game = FakeGame()
    score = engine._alpha_beta(game, 8, float('-inf'), float('inf'), 1)
    return engine, score


def main():
    engine, score = run_partial_cutoff_search()

    check('部分搜索返回已搜索子集的最佳分（-15）',
          score == -15, f'实际 {score}')

    entry = engine._tt._table.get(12345)
    check('截断节点仍存 TT（下界有剪枝价值）', entry is not None)
    if entry is not None:
        check('标记为 LOWER_BOUND（非 EXACT/UPPER_BOUND）',
              entry.flag == TTFlag.LOWER_BOUND,
              f'实际 {entry.flag.name}')
        check('存储分值 = 已搜索子集 best（-15）',
              entry.score == -15, f'实际 {entry.score}')
        check('best_move 指向已搜索的最优走法',
              entry.best_move == (0, 0, 1, 0), f'实际 {entry.best_move}')

    # 对照：无超时（全部走法搜完）时标记不受影响
    engine2 = SearchEngine(max_depth=8, time_limit=100)
    engine2._start_time = time.time()
    engine2._stop_flag = False
    engine2._nodes_searched = 0
    engine2._is_time_up = lambda: False
    real_ab = SearchEngine._alpha_beta
    calls2 = {'n': 0}

    def fake_ab2(self, game, depth, alpha, beta, player, extended=False):
        calls2['n'] += 1
        if calls2['n'] > 1:
            self._nodes_searched += 1
            return 15
        return real_ab(self, game, depth, alpha, beta, player, extended)

    engine2._alpha_beta = fake_ab2.__get__(engine2, SearchEngine)
    engine2._fast_eval = lambda game, player: 0.0
    engine2._order_moves = lambda board, moves, player, depth, tt_move=None: list(moves)
    engine2._make_move = lambda game, fr, fc, tr, tc: '.'
    engine2._unmake_move = lambda game, fr, fc, tr, tc, captured: None

    engine2._alpha_beta(FakeGame(), 8, float('-inf'), float('inf'), 1)
    entry2 = engine2._tt._table.get(12345)
    check('对照：完整搜索仍按原规则标记（EXACT）',
          entry2 is not None and entry2.flag == TTFlag.EXACT,
          f'实际 {entry2.flag.name if entry2 else "无条目"}')

    if FAILED:
        print(f'\nFAILED {len(FAILED)} 项: {FAILED}')
        sys.exit(1)
    print('\n全部通过')


if __name__ == '__main__':
    main()
