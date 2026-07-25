"""评估函数正确性与一致性测试

验证 evaluate() 的对称性、evaluate_fast() 与 evaluate() 的一致性、
已知残局评估方向、增量缓存与全量计算等价。
"""

import os
import sys
import random as _rnd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.search import SearchEngine
from domain.evaluation import (
    evaluate, evaluate_fast, PIECE_VALUE, PIECE_VALUE_ENDGAME, RED_PST,
    compute_material,
)
from domain.constants import BOARD_HEIGHT, BOARD_WIDTH, ENDGAME_PIECE_THRESHOLD

failures = []

def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        failures.append(name)


def flip_board(board):
    """镜像翻转棋盘：行 0↔9，颜色取反。返回 (flipped_board, flipped_player)。"""
    flipped = [['.' for _ in range(BOARD_WIDTH)] for _ in range(BOARD_HEIGHT)]
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p == '.':
                continue
            flipped[BOARD_HEIGHT - 1 - r][c] = p.swapcase()
    return flipped


def random_board(rng, moves_range=(0, 30)):
    """生成一个随机中局棋盘。返回 (board, current_player)。"""
    g = ChineseChessGame()
    n = rng.randint(*moves_range)
    for _ in range(n):
        moves = g.get_all_legal_moves(g.current_player)
        if not moves:
            break
        g.move_piece(*rng.choice(moves))
        if g.game_over:
            break
    return [row[:] for row in g.board], g.current_player


def get_cache_values(game):
    """从 game 的增量缓存获取 evaluate_fast 所需的参数。"""
    total = game._red_piece_count + game._black_piece_count
    endgame = total <= ENDGAME_PIECE_THRESHOLD
    vals = PIECE_VALUE_ENDGAME if endgame else PIECE_VALUE
    red_mat = sum(vals.get(p.upper(), 0) * cnt
                  for p, cnt in game._material_counts.items()
                  if p.isupper() and p != 'K')
    black_mat = sum(vals.get(p.upper(), 0) * cnt
                    for p, cnt in game._material_counts.items()
                    if p.islower() and p != 'k')
    return {
        'red_material': red_mat,
        'black_material': black_mat,
        'red_pst_score': game._red_pst_score,
        'black_pst_score': game._black_pst_score,
        'red_in_check': game.is_in_check(1),
        'black_in_check': game.is_in_check(2),
        'endgame': endgame,
    }


def test_symmetry():
    """对称性：evaluate(board, red_to_move) == -evaluate(flipped_board, black_to_move)。"""
    rng = _rnd.Random(42)

    # 初始局面
    g = ChineseChessGame()
    b = g.board
    fb = flip_board(b)
    s1 = evaluate(b, endgame=False)
    s2 = evaluate(fb, endgame=False)
    check('初始局面对称性', abs(s1 + s2) < 0.01,
          f'score={s1:.1f} flipped={s2:.1f} sum={s1+s2:.2f}')

    # 50 个随机中局局面
    for i in range(50):
        board, player = random_board(rng, moves_range=(2, 20))
        fb = flip_board(board)
        # 原局面用原 player、翻面后用另一 player 的视角
        # 在原局面红方视角的评分 == -翻面后黑方视角的评分
        s1 = evaluate(board, endgame=False)
        s2 = evaluate(fb, endgame=False)
        if abs(s1 + s2) > 1.0:
            check(f'对称性 #{i+1}', False,
                  f'score={s1:.1f} flipped={s2:.1f} sum={s1+s2:.2f}')
            return
    check('对称性（50个随机局面）', True)


def test_initial_zero():
    """初始局面对称，评估值应极小。"""
    g = ChineseChessGame()
    s = evaluate(g.board, endgame=False)
    check('初始局面评估 ≈ 0', abs(s) < 20, f'score={s:.1f}')


def test_fast_consistency():
    """evaluate_fast() 与 evaluate() 对同一局面返回相同结果。"""
    rng = _rnd.Random(77)
    for i in range(100):
        board, player = random_board(rng, moves_range=(0, 25))
        # 创建临时 game 获取增量缓存
        tmp = ChineseChessGame.from_snapshot(
            board, player,
            {1: (9, 4), 2: (0, 4)}  # dummy king_pos，from_snapshot 内部会 _recompute_incremental
        )
        # 修正 king_pos 扫描
        for r in range(10):
            for c in range(9):
                p = board[r][c]
                if p == 'K':
                    tmp._king_pos[1] = (r, c)
                elif p == 'k':
                    tmp._king_pos[2] = (r, c)

        cache = get_cache_values(tmp)
        s_full = evaluate(board,
                          legal_moves_red=0, legal_moves_black=0,
                          red_in_check=cache['red_in_check'],
                          black_in_check=cache['black_in_check'],
                          endgame=cache['endgame'])
        s_fast = evaluate_fast(board,
                               red_material=cache['red_material'],
                               black_material=cache['black_material'],
                               red_pst_score=cache['red_pst_score'],
                               black_pst_score=cache['black_pst_score'],
                               red_in_check=cache['red_in_check'],
                               black_in_check=cache['black_in_check'],
                               endgame=cache['endgame'])
        if abs(s_full - s_fast) > 0.01:
            check(f'evaluate_fast 一致性 #{i+1}', False,
                  f'full={s_full:.1f} fast={s_fast:.1f}')
            return
    check('evaluate_fast 一致性（100个随机局面）', True)


def test_known_endgames():
    """已知残局评估方向正确。"""
    # 单车必胜孤将
    board = [['.' for _ in range(9)] for _ in range(10)]
    board[0][4] = 'k'    # 黑将在 (0,4)
    board[5][4] = 'R'    # 红车在 (5,4)
    board[9][4] = 'K'    # 红帅在 (9,4)
    g = ChineseChessGame.from_snapshot(board, 1, {1: (9, 4), 2: (0, 4)})
    s = evaluate(board, endgame=True)
    check('单车必胜：红优', s > 500, f'score={s:.1f}')

    # 单炮必和（单炮无炮架无法将死，理论是和棋，
    # 但引擎不查表情况下炮有 450cp 子力优势，所以只是"红优但不至于必胜"）
    board2 = [['.' for _ in range(9)] for _ in range(10)]
    board2[0][4] = 'k'
    board2[5][4] = 'C'   # 红炮
    board2[9][4] = 'K'
    s2 = evaluate(board2, endgame=True)
    check('单炮局面：红方子力占优', s2 > 0, f'score={s2:.1f}')

    # 红优初始局面（红先）
    g3 = ChineseChessGame()
    s3 = evaluate(g3.board, endgame=False)
    check('初始局面红方略优', s3 > -10, f'score={s3:.1f}')


if __name__ == '__main__':
    test_symmetry()
    test_initial_zero()
    test_fast_consistency()
    test_known_endgames()

    if failures:
        print(f'\nFAIL: {len(failures)} failures:')
        for f in failures:
            print(f'  - {f}')
        sys.exit(1)
    print('\nAll passed')
