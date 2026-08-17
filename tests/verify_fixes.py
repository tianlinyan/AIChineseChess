"""严重项修复的回归验证（需 engines/pikafish.exe）。

用法：PYTHONIOENCODING=utf-8 python tests/verify_fixes.py
覆盖：
1. Pikafish 坐标系：初始 FEN 与 startpos 逐字一致、perft 1 = 44、
   bestmove 映射后合法、兵残局走法合法
2. EGTB 云库：已移除（云查询路径已从 domain/egtb.py 删除）
3. MCTS 终端局面：一步杀局面（>10 子，EGTB 范围外）必须选出杀着
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.fen import board_to_fen
from domain.pikafish import _uci_to_tuple

failures = []


def check(name, cond, detail=''):
    status = '✓' if cond else '✗'
    print(f"{status} {name}" + (f" — {detail}" if detail else ''))
    if not cond:
        failures.append(name)


# ── 1. Pikafish ──
g = ChineseChessGame()
fen = board_to_fen(g.board, 1)
expected = ('rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/'
            'P1P1P1P1P/1C5C1/9/RNBAKABNR w')
check("初始 FEN 与 Pikafish startpos 逐字一致", fen == expected, fen)


def pikafish_run(commands):
    """启动引擎，依次发送命令，收集 stdout 直到 bestmove 或超时。"""
    proc = subprocess.Popen(
        ['engines/pikafish.exe'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
        encoding='utf-8', errors='replace')
    lines = []

    def send(cmd):
        proc.stdin.write(cmd + '\n')
        proc.stdin.flush()

    send('uci')
    # 等 uciok
    while True:
        line = proc.stdout.readline()
        if not line or 'uciok' in line:
            break
    send('isready')
    while True:
        line = proc.stdout.readline()
        if not line or 'readyok' in line:
            break
    for cmd in commands:
        send(cmd)
    # 读到 bestmove 为止（perft 时没有 bestmove，读到 Nodes searched）
    import time
    deadline = time.time() + 20
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.strip())
        if line.startswith('bestmove') or 'Nodes searched' in line:
            break
    proc.kill()
    return lines


# perft 1 = 44
lines = pikafish_run([f'position fen {fen}', 'go perft 1'])
total = None
for ln in lines:
    if 'Nodes searched' in ln:
        total = int(ln.split(':')[-1].strip())
check("Pikafish perft 1 = 44", total == 44, f"实际={total}")

# bestmove 合法（初始局面）
lines = pikafish_run([f'position fen {fen}', 'go movetime 500'])
bm = next((ln.split()[1] for ln in lines if ln.startswith('bestmove')), None)
move = _uci_to_tuple(bm) if bm else None
legal = g.get_all_legal_moves(1)
check("初始局面 bestmove 映射后合法", move in legal,
      f"uci={bm} move={move}")

# 兵残局：红兵内部 (2,4)，红先——引擎走法必须合法（修复前兵走法全非法）
b = [['.'] * 9 for _ in range(10)]
b[0][4] = 'k'
b[2][4] = 'P'
b[9][4] = 'K'
g2 = ChineseChessGame()
g2.board = b
g2.current_player = 1
g2._king_pos = {1: (9, 4), 2: (0, 4)}
g2.recompute_hash()
fen2 = board_to_fen(b, 1)
lines = pikafish_run([f'position fen {fen2}', 'go movetime 500'])
bm2 = next((ln.split()[1] for ln in lines if ln.startswith('bestmove')), None)
move2 = _uci_to_tuple(bm2) if bm2 else None
check("兵残局 bestmove 映射后合法", move2 in g2.get_all_legal_moves(1),
      f"uci={bm2} move={move2}")

# ── 2. EGTB 云库测试已移除（云查询路径已从 domain/egtb.py 删除，本段依赖 probe_cloud 不再存在）──

# ── 3. MCTS 一步杀 ──
# 注意：必须用 >10 子（EGTB_MAX_PIECES）的局面——≤10 子时 _simulate
# 走残局库本地启发式，被将杀方本就得 ≈0 分，测不到终端失明修复。
from domain.mcts import MCTSEngine

# 红双車一步杀（R(5,0)->(0,0)），黑方卒炮无法垫将/吃车，共 11 子
bM = [['.'] * 9 for _ in range(10)]
bM[0][4] = 'k'
bM[1][8] = 'R'
bM[5][0] = 'R'
bM[9][3] = 'K'
bM[2][1] = 'p'
bM[2][2] = 'p'
bM[2][4] = 'p'
bM[2][6] = 'p'
bM[3][3] = 'p'
bM[3][5] = 'c'
bM[3][6] = 'c'
gM = ChineseChessGame()
gM.board = bM
gM.current_player = 1
gM._king_pos = {1: (9, 3), 2: (0, 4)}
gM.recompute_hash()
eng = MCTSEngine(max_simulations=800, time_limit=8)
mate_move = eng.search(gM, 1)
# 验证 (5,0)->(0,0) 确为一步杀
gV = ChineseChessGame()
gV.board = [row[:] for row in bM]
gV.current_player = 1
gV._king_pos = {1: (9, 3), 2: (0, 4)}
gV.recompute_hash()
gV.move_piece(5, 0, 0, 0)
is_mate = gV.game_over and gV.winner == 1
check("前置校验：(5,0)->(0,0) 确为一步杀", is_mate)
check("MCTS 选出一步杀", mate_move == (5, 0, 0, 0),
      f"实际={mate_move}")

print()
if failures:
    print(f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("全部验证通过")
