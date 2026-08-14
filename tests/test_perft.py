"""Perft 走法计数基准测试

验证走法生成器在几个关键局面上的 perft(N) 值与已知标准一致。
Perft (performance test / move path enumeration) 是国际象棋/中国象棋引擎
走法生成正确性的黄金标准。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame

failures = []

def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        failures.append(name)


def perft(game, player, depth):
    """Count leaf nodes at the given depth (minimax tree size)."""
    if depth == 0:
        return 1
    moves = game.get_all_legal_moves(player)
    if depth == 1:
        return len(moves)
    count = 0
    for fr, fc, tr, tc in moves:
        board = game.board
        piece = board[fr][fc]
        captured = board[tr][tc]
        board[tr][tc] = piece
        board[fr][fc] = '.'
        # 维护 king_pos 以便 is_in_check 正确
        saved_k1 = game._king_pos.get(1)
        saved_k2 = game._king_pos.get(2)
        if piece == 'K':
            game._king_pos[1] = (tr, tc)
        elif piece == 'k':
            game._king_pos[2] = (tr, tc)
        count += perft(game, 3 - player, depth - 1)
        # undo
        board[fr][fc] = piece
        board[tr][tc] = captured
        if piece == 'K':
            game._king_pos[1] = saved_k1
        elif piece == 'k':
            game._king_pos[2] = saved_k2
    return count


def test_initial_perft():
    """初始局面 perft 值。"""
    g = ChineseChessGame()

    # perft(1) = 44（中国象棋初始局面合法走法数）
    t0 = time.time()
    n1 = perft(g, 1, 1)
    t1 = time.time() - t0
    check('初始 perft(1) = 44', n1 == 44,
          f'actual={n1} time={t1:.2f}s')

    # perft(2): 红走 44 种，黑应对 → 总数。钉死精确值，<5% 计数偏差
    # 会被宽容区间掩盖（这正是 perft 最该捕获的走法生成错误类型）
    t0 = time.time()
    n2 = perft(g, 1, 2)
    t2 = time.time() - t0
    check('初始 perft(2) = 1920', n2 == 1920,
          f'actual={n2} time={t2:.2f}s')

    # perft(3) — 钉死精确黄金值（真实值 79666）
    t0 = time.time()
    n3 = perft(g, 1, 3)
    t3 = time.time() - t0
    check('初始 perft(3) = 79666', n3 == 79666,
          f'actual={n3} time={t3:.2f}s')


def test_capture_moves_subset():
    """get_capture_moves 返回结果 ⊆ get_all_legal_moves。"""
    g = ChineseChessGame()
    for _ in range(10):
        all_moves = set(g.get_all_legal_moves(g.current_player))
        cap_moves = set(g.get_capture_moves(g.current_player))
        extra = cap_moves - all_moves
        if extra:
            check('吃子走法 ⊆ 全部走法', False, f'extra={extra}')
            return
        # 走一步棋推进局面
        moves = g.get_all_legal_moves(g.current_player)
        if not moves:
            break
        import random
        g.move_piece(*random.choice(moves))
        if g.game_over:
            break
    check('capture_moves subset of all_moves (10 steps)', True)


if __name__ == '__main__':
    test_initial_perft()
    test_capture_moves_subset()

    if failures:
        print(f'\nFAIL: {len(failures)} failures:')
        for f in failures:
            print(f'  - {f}')
        sys.exit(1)
    print('\nAll passed')
