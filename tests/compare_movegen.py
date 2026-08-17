"""走法生成对拍 — 新实现 vs 基线（baseline_movegen.jsonl）

用法：python tests/compare_movegen.py
逐局面重建棋盘，比较 get_all_legal_moves 的走法集合与基线是否 100% 一致。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.fen import fen_to_board as board_from_fen

BASELINE_PATH = os.path.join(os.path.dirname(__file__), 'baseline_movegen.jsonl')


def main():
    # 基线缺失或为空时不能静默通过：对拍测试的意义在于有真实对照数据，
    # 空基线会让循环不执行、打印"100% 一致"并退出码 0，完全失去回归保护。
    if not os.path.exists(BASELINE_PATH):
        print(f'[FAIL] 基线文件不存在：{BASELINE_PATH}')
        print('  请先运行 python tests/movegen_baseline.py 生成基线')
        sys.exit(1)

    total = 0
    mismatches = 0
    with open(BASELINE_PATH, encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            rec = json.loads(line)
            board, player = board_from_fen(rec['fen'])
            game = ChineseChessGame()
            game.board = board
            game.current_player = player
            # 将位缓存靠 _is_in_check 的校验回退自动修复，无需手动同步
            new_moves = sorted(game.get_all_legal_moves(player))
            expected = [tuple(m) for m in rec['moves']]
            got = [tuple(m) for m in new_moves]
            total += 1
            ok = got == expected
            # 吃子生成等价性：get_capture_moves == 全量走法中目标有子的
            if ok:
                expected_caps = sorted(
                    m for m in expected if board[m[2]][m[3]] != '.')
                got_caps = sorted(game.get_capture_moves(player))
                if got_caps != expected_caps:
                    ok = False
                    print(f'[{line_no}] 吃子生成不一致 FEN={rec["fen"]}')
                    print(f'  期望: {expected_caps[:6]}')
                    print(f'  实际: {got_caps[:6]}')
            if not ok:
                mismatches += 1
                exp_set, got_set = set(expected), set(got)
                print(f'[{line_no}] 不一致 FEN={rec["fen"]}')
                print(f'  基线有新无: {sorted(exp_set - got_set)[:6]}')
                print(f'  新有基线无: {sorted(got_set - exp_set)[:6]}')
                if mismatches >= 5:
                    print('…（ mismatch 过多，提前终止）')
                    break
    print(f'\n共 {total} 个局面，不一致 {mismatches} 个')
    if total == 0:
        print(f'[FAIL] 基线文件为空：{BASELINE_PATH}')
        print('  请先运行 python tests/movegen_baseline.py 生成基线')
        sys.exit(1)
    if mismatches:
        sys.exit(1)
    print('[PASS] 走法生成与基线 100% 一致')


if __name__ == '__main__':
    main()
