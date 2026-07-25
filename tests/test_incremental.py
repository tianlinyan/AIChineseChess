"""增量评估缓存一致性测试

验证 ChineseChessGame 的增量缓存（_material_counts, _red/black_piece_count,
_red/black_pst_score）与全量 _recompute_incremental() 结果一致。
"""

import os
import sys
import random as _rnd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.search import SearchEngine
from domain.evaluation import PIECE_VALUE, RED_PST

BOARD_HEIGHT = 10


def _mirror_row(row):
    return BOARD_HEIGHT - 1 - row


failures = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        failures.append(name)


def _full_recompute(game):
    """全量重算增量字段（与 _recompute_incremental 相同逻辑），返回字典供比较。"""
    mc = {}
    rc = bc = 0
    rpst = bpst = 0.0
    for r in range(10):
        for c in range(9):
            p = game.board[r][c]
            if p == '.':
                continue
            mc[p] = mc.get(p, 0) + 1
            if p.isupper():
                rc += 1
            else:
                bc += 1
            pu = p.upper()
            if pu in RED_PST:
                if p.isupper():
                    rpst += RED_PST[pu][r][c]
                else:
                    bpst += RED_PST[pu][_mirror_row(r)][c]
    return mc, rc, bc, rpst, bpst


def _assert_caches_equal(game, label):
    """验证 game 上的增量缓存与全量重算一致。"""
    mc, rc, bc, rpst, bpst = _full_recompute(game)
    ok = True
    if game._material_counts != mc:
        check(f'{label} _material_counts', False,
              f'cached={dict(game._material_counts)} expected={mc}')
        ok = False
    if game._red_piece_count != rc:
        check(f'{label} _red_piece_count', False,
              f'cached={game._red_piece_count} expected={rc}')
        ok = False
    if game._black_piece_count != bc:
        check(f'{label} _black_piece_count', False,
              f'cached={game._black_piece_count} expected={bc}')
        ok = False
    if abs(game._red_pst_score - rpst) > 0.01:
        check(f'{label} _red_pst_score', False,
              f'cached={game._red_pst_score} expected={rpst}')
        ok = False
    if abs(game._black_pst_score - bpst) > 0.01:
        check(f'{label} _black_pst_score', False,
              f'cached={game._black_pst_score} expected={bpst}')
        ok = False
    if ok:
        check(label, True)


def test_initial_state():
    """初始局面：_recompute_incremental() 后增量字段与全量扫描一致。"""
    g = ChineseChessGame()
    _assert_caches_equal(g, '初始局面')


def test_random_play_sequence():
    """随机走子序列：每步走子后增量字段与全量重算一致。"""
    rng = _rnd.Random(42)
    g = ChineseChessGame()
    for step in range(100):
        moves = g.get_all_legal_moves(g.current_player)
        if not moves:
            break
        fr, fc, tr, tc = rng.choice(moves)
        result = g.move_piece(fr, fc, tr, tc)
        if not result['success']:
            check(f'随机走子 第{step+1}步', False, result.get('message', ''))
            break
        _assert_caches_equal(g, f'随机走子 第{step+1}步')
        if result.get('game_over'):
            break


def test_make_unmake_cycle():
    """_make_move → _unmake_move 一循环后增量字段恢复原值。"""
    rng = _rnd.Random(42)
    g = ChineseChessGame()
    # 保存初始值
    mc_before = dict(g._material_counts)
    rc_before = g._red_piece_count
    bc_before = g._black_piece_count
    rpst_before = g._red_pst_score
    bpst_before = g._black_pst_score

    for _ in range(50):
        moves = g.get_all_legal_moves(g.current_player)
        if not moves:
            break
        fr, fc, tr, tc = rng.choice(moves)
        captured = SearchEngine._make_move(g, fr, fc, tr, tc)
        SearchEngine._unmake_move(g, fr, fc, tr, tc, captured)

        # 验证所有字段恢复
        if g._material_counts != mc_before:
            check('make/unmake _material_counts 恢复', False)
            break
        if g._red_piece_count != rc_before:
            check('make/unmake _red_piece_count 恢复', False)
            break
        if g._black_piece_count != bc_before:
            check('make/unmake _black_piece_count 恢复', False)
            break
        if abs(g._red_pst_score - rpst_before) > 0.01:
            check('make/unmake _red_pst_score 恢复', False)
            break
        if abs(g._black_pst_score - bpst_before) > 0.01:
            check('make/unmake _black_pst_score 恢复', False)
            break
    else:
        check('make/unmake 循环（50次）恢复一致', True)


def test_from_snapshot():
    """from_snapshot() 后增量字段正确。"""
    g = ChineseChessGame()
    # 走几步棋
    rng = _rnd.Random(99)
    for _ in range(5):
        moves = g.get_all_legal_moves(g.current_player)
        if not moves:
            break
        fr, fc, tr, tc = rng.choice(moves)
        g.move_piece(fr, fc, tr, tc)

    # 快照
    snap = ChineseChessGame.from_snapshot(
        g.get_board_copy(), g.current_player, g._king_pos)
    _assert_caches_equal(snap, 'from_snapshot')


def test_count_pieces_consistency():
    """count_pieces() O(1) 版本与旧全扫版本一致。"""
    rng = _rnd.Random(42)
    for test_idx in range(20):
        g = ChineseChessGame()
        # 随机走几步
        for _ in range(rng.randint(0, 10)):
            moves = g.get_all_legal_moves(g.current_player)
            if not moves:
                break
            g.move_piece(*rng.choice(moves))
            if g.game_over:
                break

        # O(1) count_pieces
        c0 = g.count_pieces(0)
        c1 = g.count_pieces(1)
        c2 = g.count_pieces(2)

        # 全扫验证
        s0 = s1 = s2 = 0
        for r in range(10):
            for c in range(9):
                p = g.board[r][c]
                if p == '.':
                    continue
                s0 += 1
                if p.isupper():
                    s1 += 1
                else:
                    s2 += 1

        if c0 != s0 or c1 != s1 or c2 != s2:
            check(f'count_pieces 测试{test_idx+1}', False,
                  f'O(1):({c0},{c1},{c2}) vs scan:({s0},{s1},{s2})')
            return
    check('count_pieces 一致性（20次）', True)


def test_is_endgame_consistency():
    """is_endgame() O(1) 版与全扫版一致。"""
    rng = _rnd.Random(77)
    for test_idx in range(20):
        g = ChineseChessGame()
        for _ in range(rng.randint(0, 20)):
            moves = g.get_all_legal_moves(g.current_player)
            if not moves:
                break
            g.move_piece(*rng.choice(moves))
            if g.game_over:
                break

        # O(1)
        result = g.is_endgame()
        # 全扫
        total = sum(1 for r in range(10) for c in range(9) if g.board[r][c] != '.')
        expected = total <= 14
        if result != expected:
            check(f'is_endgame 测试{test_idx+1}', False,
                  f'O(1)={result} scan={expected} total={total}')
            return
    check('is_endgame 一致性（20次）', True)


if __name__ == '__main__':
    test_initial_state()
    test_random_play_sequence()
    test_make_unmake_cycle()
    test_from_snapshot()
    test_count_pieces_consistency()
    test_is_endgame_consistency()

    if failures:
        print(f'\nFAIL: {len(failures)} failures:')
        for f in failures:
            print(f'  - {f}')
        sys.exit(1)
    print('\nAll passed')
