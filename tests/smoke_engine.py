"""无 GUI 引擎冒烟测试 — 每阶段改动后运行

用法：python tests/smoke_engine.py
覆盖：走法生成、Alpha-Beta、MCTS（验证真搜索）、本地 EGTB、开局库、哈希一致性。
任何断言失败或非零退出即不通过。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.search import SearchEngine
from domain.mcts import MCTSEngine
from domain import egtb
from domain.openings import get_opening_move, OPENING_LINES
from domain.constants import NATURAL_LIMIT_MOVES

FAILED = []


def check(name: str, cond: bool, detail: str = ''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        FAILED.append(name)


def empty_board():
    return [['.'] * 9 for _ in range(10)]


def make_game_with_board(board, player=1):
    """按给定棋盘构造 game（同步将位缓存与 Zobrist 哈希）。"""
    game = ChineseChessGame()
    game.board = [row[:] for row in board]
    game.current_player = player
    for r in range(10):
        for c in range(9):
            if game.board[r][c] == 'K':
                game._king_pos[1] = (r, c)
            elif game.board[r][c] == 'k':
                game._king_pos[2] = (r, c)
    game.recompute_hash()
    return game


def tactical_game():
    """构造战术局面：红車 (5,0)，黑車 (9,0) 同列无阻挡，红方可白吃。
    注意：白吃走法 (5,0,9,0) 在生成顺序中【不是】第一个（第一个是 (5,0,0,0)），
    这样才能区分"真搜索"与"伪搜索恒返回首个生成走法"。红走。"""
    board = empty_board()
    board[0][4] = 'k'
    board[8][3] = 'K'   # 双将不同列；黑将在 (0,5) 有逃生格，避免意外形成杀棋
    board[5][0] = 'R'
    board[9][0] = 'r'
    return make_game_with_board(board, 1)


TACTICAL_CAPTURE = (5, 0, 9, 0)


def test_movegen():
    game = ChineseChessGame()
    moves = game.get_all_legal_moves(1)
    # 初始局面红方标准合法走法数 = 44
    check('初始局面走法数=44', len(moves) == 44, f'实际 {len(moves)}')
    # 走一步后哈希变化
    h1 = game.position_hash()
    fr, fc, tr, tc = moves[0]
    game.move_piece(fr, fc, tr, tc)
    check('走子后 position_hash 变化', game.position_hash() != h1)

    # Zobrist 增量维护一致性：随机对弈 60 步，每步校验 增量值 == 全量重算
    import random as _rnd
    rng = _rnd.Random(42)
    game2 = ChineseChessGame()
    ok, detail = True, ''
    for ply in range(60):
        if game2._zobrist != game2._compute_zobrist():
            ok, detail = False, f'第 {ply} 步增量哈希漂移'
            break
        legal = game2.get_all_legal_moves(game2.current_player)
        if not legal or game2.game_over:
            break
        game2.move_piece(*rng.choice(legal))
    check('Zobrist 增量/全量一致（60 步）', ok, detail)

    # 将位反向检测 vs 暴力法 逐点对比（随机 30 局面）
    from domain.evaluation import evaluate  # noqa: F401  (确认模块可导入)
    ok2, detail2 = _cross_check_is_in_check(rng)
    check('is_in_check 反向检测与暴力法一致', ok2, detail2)


def _brute_in_check(game, player):
    """旧版暴力将军检测（对方全子 × _is_legal_move），仅测试用。"""
    king = 'K' if player == 1 else 'k'
    kr = kc = None
    for r in range(10):
        for c in range(9):
            if game.board[r][c] == king:
                kr, kc = r, c
    if kr is None:
        return False
    opp = 3 - player
    for r in range(10):
        for c in range(9):
            p = game.board[r][c]
            if p != '.' and game.get_piece_owner(p) == opp:
                if game._is_legal_move(p, r, c, kr, kc):
                    return True
    return False


def _cross_check_is_in_check(rng):
    """随机对弈局面下对比新旧将军检测，含搜形/残局。"""
    for trial in range(30):
        game = ChineseChessGame()
        for _ in range(rng.randint(0, 90)):
            legal = game.get_all_legal_moves(game.current_player)
            if not legal or game.game_over:
                break
            game.move_piece(*rng.choice(legal))
        for player in (1, 2):
            new = game.is_in_check(player)
            old = _brute_in_check(game, player)
            if new != old:
                return False, (f'局面 {trial} player={player} '
                               f'新={new} 旧={old}')
    return True, ''


def test_alpha_beta():
    game = ChineseChessGame()
    eng = SearchEngine(max_depth=3, time_limit=10.0)
    t0 = time.time()
    move = eng.search(game, 1)
    dt = time.time() - t0
    legal = game.get_all_legal_moves(1)
    check('Alpha-Beta 初始局面返回合法走法', move in legal, f'{move} 用时{dt:.1f}s 节点{eng.nodes_searched}')

    game2 = tactical_game()
    eng2 = SearchEngine(max_depth=3, time_limit=10.0)
    move2 = eng2.search(game2, 1)
    check('Alpha-Beta 找到白吃車', move2 == TACTICAL_CAPTURE, f'实际 {move2}')


def test_mcts_real_search():
    """MCTS 修复验证：必须能利用局面差异找到白吃車（伪搜索时各走法等值）。"""
    game = tactical_game()
    eng = MCTSEngine(max_simulations=600, time_limit=10.0)
    move = eng.search(game, 1)
    check('MCTS 找到白吃車', move == TACTICAL_CAPTURE, f'实际 {move}')

    # 根局面合法性 + 调用方棋盘不被污染
    game2 = ChineseChessGame()
    before = game2.get_board_state_string()
    eng2 = MCTSEngine(max_simulations=200, time_limit=5.0)
    move2 = eng2.search(game2, 1)
    check('MCTS 初始局面返回合法走法', move2 in game2.get_all_legal_moves(1))
    check('MCTS 不污染调用方棋盘',
          game2.get_board_state_string() == before)

    # LLM 先验引导：给【非吃車】走法极高误导性先验，搜索仍应选中吃車
    # （先验直接转成虚拟访问数；若把高先验给正确答案本身则恒通过、
    #   测不出"被 LLM 误导时搜索仍稳健"这一意图）
    game3 = tactical_game()
    eng3 = MCTSEngine(max_simulations=600, time_limit=10.0)
    move3 = eng3.search(game3, 1, priors={(5, 0, 0, 0): 0.9})
    check('MCTS 带误导先验仍找到白吃車', move3 == TACTICAL_CAPTURE,
          f'实际 {move3}')


def test_egtb_local():
    # 显式 allow_cloud=False：冒烟测试不得隐式依赖网络
    # （当前用例均走本地分支；一旦启发式改动漏到云查询，测试会变 flaky）
    # 双方仅将 → 和棋
    board = empty_board()
    board[0][4] = 'k'
    board[9][4] = 'K'
    res = egtb.probe(board, 1, 2, allow_cloud=False)
    check('EGTB 双将=和', res == (0.0, 0), f'实际 {res}')

    # 单車 vs 孤将 → 必胜（大分）
    board2 = empty_board()
    board2[0][4] = 'k'
    board2[9][4] = 'K'
    board2[5][5] = 'R'
    res2 = egtb.probe(board2, 1, 3, allow_cloud=False)
    check('EGTB 单車vs孤将=胜', res2 is not None and res2[0] > 50000, f'实际 {res2}')

    # 单馬 vs 孤将 → 单马必胜孤将（修复后应为胜分）
    board3 = empty_board()
    board3[0][4] = 'k'
    board3[9][4] = 'K'
    board3[5][5] = 'N'
    res3 = egtb.probe(board3, 1, 3, allow_cloud=False)
    check('EGTB 单馬vs孤将=胜', res3 is not None and res3[0] > 1000, f'实际 {res3}')

    # 单車 vs 士象全 → 官和（修复后不得判 80000 胜）
    board4 = empty_board()
    board4[0][4] = 'k'
    board4[0][3] = 'a'
    board4[0][5] = 'a'
    board4[0][2] = 'b'
    board4[0][6] = 'b'
    board4[9][4] = 'K'
    board4[5][5] = 'R'
    res4 = egtb.probe(board4, 1, 7, allow_cloud=False)
    check('EGTB 单車vs士象全≠必胜',
          res4 is None or res4[0] < 50000, f'实际 {res4}')

    # ── 单卒分支（结论经 chessdb.cn 云库实测核对）──
    # 过河未到底 vs 孤将 → 胜
    board5 = empty_board()
    board5[0][4] = 'k'
    board5[9][4] = 'K'
    board5[4][4] = 'P'   # 红卒过河（r<=4）未到底
    res5 = egtb.probe(board5, 1, 3, allow_cloud=False)
    check('EGTB 过河卒vs孤将=胜', res5 is not None and res5[0] > 1000,
          f'实际 {res5}')

    # 过河卒 vs 单士 → 和（有防守子）
    board6 = empty_board()
    board6[0][4] = 'k'
    board6[0][3] = 'a'
    board6[9][4] = 'K'
    board6[4][4] = 'P'
    res6 = egtb.probe(board6, 1, 4, allow_cloud=False)
    check('EGTB 过河卒vs单士=和', res6 is not None and res6[0] == 0.0,
          f'实际 {res6}')

    # 底线老兵 vs 孤将 → 和（沉底无杀伤力）
    board7 = empty_board()
    board7[0][4] = 'k'
    board7[9][3] = 'K'   # 双将不同列，避免白脸将
    board7[0][0] = 'P'   # 红卒沉底（r==0）
    res7 = egtb.probe(board7, 1, 3, allow_cloud=False)
    check('EGTB 底线老兵vs孤将=和', res7 is not None and res7[0] == 0.0,
          f'实际 {res7}')


def test_natural_limit():
    """自然限着：连续 120 步未吃子判和；第 120 步将杀优先（限着失效）。"""
    board = empty_board()
    board[0][4] = 'k'
    board[9][3] = 'K'
    board[5][0] = 'R'
    g = make_game_with_board(board, 1)
    g.moves_since_capture = NATURAL_LIMIT_MOVES - 1
    r = g.move_piece(5, 0, 5, 1)   # 未吃子的安静走法 → 达到 120 步
    check('自然限着 120 步判和',
          r['success'] and r.get('game_over') and g.winner == 0,
          f"实际 {r.get('message')}")

    # 同一限着计数下，将杀优先于限着（竞赛规则原文）
    board2 = empty_board()
    board2[0][4] = 'k'
    board2[1][8] = 'R'
    board2[5][0] = 'R'
    board2[9][3] = 'K'
    g2 = make_game_with_board(board2, 1)
    g2.moves_since_capture = NATURAL_LIMIT_MOVES - 1
    r2 = g2.move_piece(5, 0, 0, 0)  # 双車一步杀
    check('限着步上将杀优先获胜',
          r2['success'] and r2.get('game_over') and g2.winner == 1,
          f"实际 {r2.get('message')}")


def test_openings():
    game = ChineseChessGame()
    move = get_opening_move(game.get_move_key())
    legal = game.get_all_legal_moves(1)
    check('开局库首步合法', move in legal or move is None,
          f'实际 {move}')

    # 逐线全程走子：每条开局线的每一步都必须合法落子
    # （线名/着法标注按标准路数校正后，用此守住坐标数据的合法性）
    bad = []
    for name, line in OPENING_LINES.items():
        g = ChineseChessGame()
        for mv in line:
            r = g.move_piece(*mv)
            if not r['success']:
                bad.append(f"{name} {mv}: {r.get('message')}")
                break
    check(f'开局库 {len(OPENING_LINES)} 条线全程走子合法', not bad,
          '; '.join(bad[:3]))


def test_self_play():
    """深度2的 Alpha-Beta 自我对弈 10 步无异常。"""
    game = ChineseChessGame()
    eng = SearchEngine(max_depth=2, time_limit=5.0)
    ok = True
    detail = ''
    try:
        for _ in range(10):
            if game.game_over:
                break
            player = game.current_player
            move = eng.search(game, player)
            if move is None:
                break
            r = game.move_piece(*move)
            if not r['success']:
                ok = False
                detail = f"引擎走出非法走法 {move}: {r.get('message')}"
                break
    except Exception as e:  # noqa
        ok = False
        detail = repr(e)
    check('Alpha-Beta 自我对弈 10 步', ok, detail)


if __name__ == '__main__':
    test_movegen()
    test_alpha_beta()
    test_mcts_real_search()
    test_egtb_local()
    test_natural_limit()
    test_openings()
    test_self_play()
    if FAILED:
        print(f'\n✗ {len(FAILED)} 项失败: {FAILED}')
        sys.exit(1)
    print('\n✓ 全部通过')
