"""阶段 0 评测基准 — 评估能力升级前后的量化标尺

用法（普通 Python 脚本，非 pytest）：
    python scripts/eval_benchmark.py             # 相关性 / 符号一致率 / MAE
    python scripts/eval_benchmark.py --selfplay  # 额外跑小规模对弈胜率（慢）
    python scripts/eval_benchmark.py --positions 80   # 随机局面数（默认 60）

度量：
  1. 固定局面集（开局 / 中局 / 残局理论 / 子力失衡）+ 播种随机对弈局面，
     对比三种评估（全部红方视角厘兵，正值=红优）：
       - Pikafish eval（基准真值，UCI eval 命令，已实证 white side=红方）
       - 自研 NNUE（domain.nnue）
       - 手工评估（domain.evaluation.evaluate）
     指标：Pearson 相关系数、平均绝对误差（MAE）、符号一致率（判对谁优）。
  2. --selfplay：Pikafish(黑) vs MCTS(红，自研) 限步对弈，
     报告红胜/黑胜/和棋与净得分（默认关闭：自研搜索较慢）。
  3. 硬性验收：初始局面 Pikafish eval ≈ +30 厘兵（与引擎实测一致），
     且引擎可用性必须为真（防止"引擎死亡假可用"污染基准）。

产出：每次运行的数值即基线；评估升级（跳①/跳②）后重跑对比即可量化收益。
"""

import argparse
import math
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from domain.game import ChineseChessGame
from domain.fen import fen_to_board as board_from_fen, board_to_fen
from domain.mcts import MCTSEngine
from domain.pikafish import PikafishEngine
from domain.nnue import get_nnue
from domain.evaluation import evaluate

# ══════════════════════════════════════════════════════════════════════════
# 固定局面集（FEN，含走子方）— 覆盖开局/中局/残局理论/子力失衡
# 残局理论判例与 M-ENG-3 修复验证同源
# ══════════════════════════════════════════════════════════════════════════

FIXED_POSITIONS = [
    # (fen, 说明)
    ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w", "初始局面"),
    ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABN1 w", "红少一車(右侧)"),
    ("rnbakabn1/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR b", "黑少一車(右侧)"),
    ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/R1BAKABNR w", "红少一馬(col1)"),
    ("r1bakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w", "黑少一馬(col1)"),
    # 残局理论
    ("4k4/9/9/9/9/9/9/9/9/4K4 w", "双将(照面,故意非法)"),
    ("4k4/9/9/9/9/4R4/9/9/9/4K4 w", "单車vs孤将(将军位红走=不可能局面)"),
    ("4k4/9/9/9/9/R8/9/9/9/4K4 w", "单車vs孤将(双将同列照面,故意非法)"),
    ("3k5/9/9/9/9/R8/9/9/9/5K3 w", "单車vs孤将(双将不同列)"),
    ("3k5/9/9/9/9/r8/9/9/9/5K3 b", "黑車vs红孤将(黑走, 旋转路径)"),
    ("3k5/9/9/9/9/C8/9/9/9/5K3 w", "单炮vs孤将(官和)"),
    ("3k5/9/9/9/9/CC7/9/9/9/5K3 w", "双炮vs孤将(必胜)"),
    ("3k5/9/9/9/9/N8/9/9/9/5K3 w", "单馬vs孤将(必胜)"),
    ("2bakab2/9/9/9/9/9/9/9/4A4/2BK1AB2 w", "士象全vs士象全(官和)"),
    # 多子残局（M-ENG-3 判例：車馬炮必胜 / 車馬官和 / 車兵视兵位置）
    # 注意将帅须错列（红帅 col3，黑将 col4）避免照面被引擎拒绝
    ("2bakab2/9/9/9/9/9/9/R8/2N1A1C2/2BK1AB2 w", "車馬炮vs士象全(必胜)"),
    ("2bakab2/9/9/9/9/9/9/R8/2N1A4/2BK1AB2 w", "車馬vs士象全(可胜)"),
    ("2bakab2/9/9/4P4/9/9/9/R8/9/4K4 w", "車+过河兵vs士象全(必胜)"),
    ("2bakab2/9/9/9/9/4P4/9/R8/9/4K4 w", "車+未过河兵vs士象全(官和)"),
]


def gen_random_positions(rng: random.Random, n: int) -> list:
    """播种随机对弈生成 n 个中局/残局局面（含走子方）。"""
    out = []
    while len(out) < n:
        g = ChineseChessGame()
        plies = rng.randint(20, 80)
        for _ in range(plies):
            moves = g.get_all_legal_moves(g.current_player)
            if not moves or g.game_over:
                break
            g.move_piece(*rng.choice(moves))
        if g.game_over:
            continue
        out.append((g.get_board_copy(), g.current_player))
    return out


def hand_eval(board, player) -> float:
    """手工评估（红方视角厘兵），与 evaluate_position 工具同参数。"""
    tmp = ChineseChessGame()
    tmp.board = [row[:] for row in board]
    tmp.current_player = player
    red_moves = tmp.get_all_legal_moves(1)
    black_moves = tmp.get_all_legal_moves(2)
    total = sum(1 for r in range(10) for c in range(9) if board[r][c] != '.')
    return evaluate(
        board,
        legal_moves_red=len(red_moves),
        legal_moves_black=len(black_moves),
        red_in_check=tmp.is_in_check(1),
        black_in_check=tmp.is_in_check(2),
        endgame=total <= 14,
    )


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 2:
        return float('nan')
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return float('nan')
    return cov / math.sqrt(vx * vy)


def run_metrics(pf, nnue, positions) -> dict:
    """对局面集跑三种评估，返回指标。"""
    pf_scores, nnue_scores, hand_scores, labels = [], [], [], []
    n_skip = 0
    for board, player in positions:
        s_pf = pf.evaluate_fen(board, player)
        if s_pf is None:
            n_skip += 1
            continue
        s_nn = nnue.evaluate(board) if nnue is not None else 0.0
        s_hand = hand_eval(board, player)
        pf_scores.append(s_pf)
        nnue_scores.append(s_nn)
        hand_scores.append(s_hand)
        labels.append((board, player))

    def _metrics(pred):
        if not pf_scores:
            return float('nan'), float('nan'), float('nan')
        r = pearson(pf_scores, pred)
        mae = statistics.mean(abs(a - b) for a, b in zip(pf_scores, pred))
        # 符号一致率：a*b>0 判同号（0 视为中性，双方同为 0 不计一致，
        # 避免把 0 误归入负号）
        sign_ok = sum(1 for a, b in zip(pf_scores, pred)
                      if a * b > 0) / len(pf_scores)
        return r, mae, sign_ok

    return {
        'n': len(pf_scores), 'n_skip': n_skip,
        'pf_mean': statistics.mean(pf_scores),
        'nnue': _metrics(nnue_scores),
        'hand': _metrics(hand_scores),
        'labels': labels, 'pf_scores': pf_scores,
        'nnue_scores': nnue_scores, 'hand_scores': hand_scores,
    }


def run_selfplay(pf, sims: int, games: int, max_plies: int,
                 movetime_ms: int) -> dict:
    """Pikafish(黑) vs MCTS(红) 限步对弈。自研引擎较慢，默认不跑。"""
    results = []
    for gi in range(games):
        g = ChineseChessGame()
        engine = MCTSEngine(max_simulations=sims, time_limit=5.0)
        plies = 0
        while not g.game_over and plies < max_plies:
            player = g.current_player
            if player == 1:      # 红 = 自研 MCTS
                mv = engine.search(g, player)
            else:                # 黑 = Pikafish
                mv = pf.search(g, player, time_ms=movetime_ms)
            if not mv:
                break
            r = g.move_piece(*mv)
            if not r['success']:
                break
            plies += 1
            if plies % 20 == 0:
                print(f'    [game {gi+1}] plies={plies} '
                      f'fen={board_to_fen_short(g)}')
        results.append(g.winner if g.game_over else 0)
        print(f'  game {gi+1}/{games}: winner={results[-1]} '
              f'(1=红自研胜 2=黑Pikafish胜 0=和)')
    red = results.count(1)
    black = results.count(2)
    draw = results.count(0)
    return {'games': games, 'red_win': red, 'black_win': black,
            'draw': draw, 'net': (red - black) / games}


def board_to_fen_short(g) -> str:
    return board_to_fen(g.board, g.current_player)[:50]


def main():
    ap = argparse.ArgumentParser(description='评估基准：自研 vs Pikafish')
    ap.add_argument('--positions', type=int, default=60,
                    help='随机对弈局面数（默认 60）')
    ap.add_argument('--seed', type=int, default=20260814)
    ap.add_argument('--selfplay', action='store_true',
                    help='额外跑 Pikafish vs 自研搜索对弈（慢）')
    ap.add_argument('--games', type=int, default=3, help='自对弈局数')
    ap.add_argument('--sims', type=int, default=300, help='自研 MCTS 每步模拟次数')
    args = ap.parse_args()

    # ── 引擎可用性硬检查（防"引擎死亡假可用"污染基准）──
    pf = PikafishEngine()
    if not pf.available:
        print(f'[FAIL] Pikafish 不可用: {pf.error_msg}')
        sys.exit(1)
    print(f'[OK] Pikafish 就绪（多线程评测请勿与 GUI 同时运行）')

    nnue = get_nnue()
    print(f'[OK] 自研 NNUE: {"已加载" if nnue else "未加载(回退 0.0)"}')

    # ── 硬性验收：初始局面评估必须与引擎实测一致 ──
    init_board, init_player = board_from_fen(FIXED_POSITIONS[0][0])
    s_init = pf.evaluate_fen(init_board, init_player)
    print(f'[验收] 初始局面 Pikafish eval = {s_init} 厘兵 '
          f'(期望 +30，容差 ±5)')
    if s_init is None or abs(s_init - 30.0) > 5.0:
        print(f'[FAIL] 初始局面评估异常（{s_init}）— 引擎或解析器问题')
        pf.close()
        sys.exit(1)
    print('[PASS] 初始局面验收通过')

    # ── 局面集 ──
    fixed = [board_from_fen(fen) for fen, _ in FIXED_POSITIONS]
    # 固定局面合法性硬检查：除注释标注"故意非法"的外，全部必须被引擎
    # 接受（evaluate_fen 非 None），否则残局覆盖会静默丢失
    for (fen, desc), (board, player) in zip(FIXED_POSITIONS, fixed):
        if '故意非法' in desc:
            continue
        if pf.evaluate_fen(board, player) is None:
            print(f'[FAIL] 固定局面被引擎拒绝: {desc}\n  fen={fen}')
            pf.close()
            sys.exit(1)
    print(f'[PASS] 固定局面合法性检查通过（{len(fixed)} 个，'
          f'{sum(1 for _, d in FIXED_POSITIONS if "故意非法" in d)} 个故意非法除外）')
    rng = random.Random(args.seed)
    rand = gen_random_positions(rng, args.positions)
    positions = fixed + rand
    print(f'[数据] 局面集: 固定 {len(fixed)} + 随机 {len(rand)} = {len(positions)}')

    # ── 度量 ──
    t0 = time.time()
    m = run_metrics(pf, nnue, positions)
    dt = time.time() - t0
    print(f'[度量] {m["n"]} 个有效局面（跳过 {m["n_skip"]}），耗时 {dt:.0f}s')
    print(f'       Pikafish 评分范围: {min(m["pf_scores"]):.0f} ~ '
          f'{max(m["pf_scores"]):.0f}（均值 {m["pf_mean"]:.0f}）')
    for name, key in (('自研NNUE', 'nnue'), ('手工评估', 'hand')):
        r, mae, sign = m[key]
        print(f'       {name} vs Pikafish: Pearson r={r:+.3f} '
              f'| MAE={mae:.0f}厘兵 | 符号一致率={sign*100:.0f}%')

    # 最大的评估分歧点（供定位改进方向）
    worst = sorted(zip(m['labels'], m['pf_scores'],
                       m['nnue_scores'], m['hand_scores']),
                   key=lambda t: abs(t[1] - t[3]), reverse=True)[:5]
    print('       分歧最大的 5 个局面（Pikafish vs 手工评估）：')
    for (board, player), s_pf, s_nn, s_hand in worst:
        fen = board_to_fen(board, player)
        print(f'         fen={fen[:40]}… pf={s_pf:+.0f} '
              f'nnue={s_nn:+.0f} hand={s_hand:+.0f}')

    # ── 自对弈（可选）──
    if args.selfplay:
        print('[对弈] Pikafish(黑) vs MCTS(红)…')
        sp = run_selfplay(pf, args.sims, args.games, 60, 200)
        print(f'[对弈] 红(自研){sp["red_win"]} 胜 / 黑(Pikafish)'
              f'{sp["black_win"]} 胜 / 和 {sp["draw"]}'
              f'（净得分 {sp["net"]:+.2f}）')

    pf.close()
    print('完成。数值即基线，评估升级后重跑对比。')


if __name__ == '__main__':
    main()
