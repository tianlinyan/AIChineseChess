"""无 GUI 引擎冒烟测试 — 每阶段改动后运行

用法：python tests/smoke_engine.py
覆盖：走法生成、MCTS（验证真搜索）、自然限着、开局库、哈希一致性、自弈。
任何断言失败或非零退出即不通过。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.mcts import MCTSEngine
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
    """按给定棋盘构造 game（同步将位缓存、Zobrist 哈希与增量缓存）。"""
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
    # 重建增量缓存：否则携带标准棋盘的陈旧 material_counts/PST，
    # 后续断言绝对评估值/is_endgame 会拿到错误数据
    game._recompute_incremental()
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

    # LLM 先验引导：给【非吃車】走法 (5,0,0,0) 极高误导性先验、其余走法
    # 压低，搜索仍应选中吃車——验证 PUCT 能翻盘先验误导。
    # （priors.get(move, 1.0)：未指定走法默认 1.0，必须显式压低其余走法，
    #   否则误导走法 0.9 反而低于默认 1.0，测试名不副实。）
    game3 = tactical_game()
    legal3 = game3.get_all_legal_moves(1)
    priors = {m: 0.1 for m in legal3}
    priors[(5, 0, 0, 0)] = 0.9
    eng3 = MCTSEngine(max_simulations=2000, time_limit=10.0)
    move3 = eng3.search(game3, 1, priors=priors)
    check('MCTS 带误导先验仍找到白吃車', move3 == TACTICAL_CAPTURE,
          f'实际 {move3}')


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
    """MCTS 自我对弈 10 步无异常（走法全部合法）。"""
    game = ChineseChessGame()
    ok = True
    detail = ''
    try:
        for _ in range(10):
            if game.game_over:
                break
            player = game.current_player
            eng = MCTSEngine(max_simulations=150, time_limit=3.0)
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
    check('MCTS 自我对弈 10 步', ok, detail)


if __name__ == '__main__':
    test_movegen()
    test_mcts_real_search()
    test_natural_limit()
    test_openings()
    test_self_play()
    if FAILED:
        print(f'\nFAILED {len(FAILED)} 项: {FAILED}')
        sys.exit(1)
    print('\n全部通过')
