"""走法生成基线采集 — 用当前实现 dump 局面+合法走法集合，供重写后对拍

用法：python tests/movegen_baseline.py [局面数]
输出：tests/baseline_movegen.jsonl，每行 {"fen":..., "player":..., "moves":[[fr,fc,tr,tc],...]}

注意：必须在走法生成重写【之前】运行本脚本采集基线。
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.fen import board_to_fen

OUT_PATH = os.path.join(os.path.dirname(__file__), 'baseline_movegen.jsonl')


def collect(target_positions: int = 3000, seed: int = 20260723) -> None:
    rng = random.Random(seed)
    game = ChineseChessGame()
    seen_fens = set()
    records = []

    while len(records) < target_positions:
        game.reset()
        # 随机对弈一整局，沿途采样局面
        max_plies = 160
        for _ in range(max_plies):
            if game.game_over:
                break
            player = game.current_player
            moves = game.get_all_legal_moves(player)
            if not moves:
                break
            fen = board_to_fen(game.board, player)
            if fen not in seen_fens:
                seen_fens.add(fen)
                records.append({
                    'fen': fen,
                    'player': player,
                    'moves': sorted(list(m) for m in moves),
                })
            fr, fc, tr, tc = rng.choice(moves)
            game.move_piece(fr, fc, tr, tc)

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'已采集 {len(records)} 个局面 → {OUT_PATH}')


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    collect(n)
