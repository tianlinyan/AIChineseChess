"""阶段 2 蒸馏数据管线 — Pikafish 自对弈生成高质量训练数据

替代旧 `train_nnue.generate_training_data` 的随机对弈教师模型：
随机对弈产生大量畸形局面；本脚本用 Pikafish 真实自对弈产生自然对局，
每个局面用 Pikafish `eval`（evaluate_fen，红方视角厘兵）打软标签——
落实 train_nnue.py 文档头"用 Pikafish 评估作为训练标签"的原始意图。

用法（普通 Python 脚本，非 pytest）：
    python scripts/gen_selfplay.py --games 5 --quick     # 快速验证（几局）
    python scripts/gen_selfplay.py --games 200 --movetime 100   # 正式（后台跑）
    python scripts/gen_selfplay.py --label result        # 胜负标签（±1/0）

输出：data/selfplay_data.npz
  X        (N, 1260) float32  稀疏特征（与 NnueNet.extract_features 一致）
  y        (N,) float32       红方视角标签：
                                eval 模式 = Pikafish eval 厘兵 / 100（+0.30 = 红优 0.3 兵）
                                result 模式 = ±1/0（红胜/黑胜/和，红方视角）
  fens     (N,) object        每样本 FEN（调试/去重用）
  results  (N,) int8          红方视角对局结果 ±1/0
  game_ids (N,) int32         样本所属对局编号
  mirror 标志默认开启：180° 旋转 + 红黑互换（rotate180_swap，本地实现），
  镜像样本 y 取反 —— 颜色对称直接编码进数据，样本量 ×2。

验收（脚本末尾自动报告）：
  1. 胜负/和棋分布与平均步数（和棋率过高说明对局异常）
  2. 镜像对称性：随机抽 10 个镜像样本，|eval(镜像) + eval(原局)| 应接近 0
  3. 标签一致性：抽样重查 evaluate_fen 与已存标签一致
  4. Pikafish 引擎可用性硬检查（防假可用污染数据）
"""

import argparse
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from domain.game import ChineseChessGame
from domain.pikafish import PikafishEngine
from domain.nnue import NnueNet
from domain.fen import board_to_fen, fen_to_board


def rotate180_swap(board):
    """棋盘旋转 180° + 红黑互换（颜色对称镜像，原 egtb_local._rotate_board）。"""
    rot = [['.'] * 9 for _ in range(10)]
    for r in range(10):
        for c in range(9):
            p = board[r][c]
            if p != '.':
                rot[9 - r][8 - c] = p.swapcase()
    return rot

DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         'data', 'selfplay_data.npz')


def play_game(pf, rng: random.Random, movetime_ms: int,
              max_plies: int) -> tuple:
    """Pikafish 自对弈一局，返回 (records, winner, plies)。

    records: [(board_copy, player_at_position), ...] 每步走子前的局面
    winner: 1=红胜 2=黑胜 0=和（来自 move_piece 判决）
    """
    g = ChineseChessGame()
    # 开局多样性：随机 2~4 手（避免全部对局同一起点）
    for _ in range(rng.randint(2, 5)):
        moves = g.get_all_legal_moves(g.current_player)
        if not moves or g.game_over:
            break
        g.move_piece(*rng.choice(moves))

    records = []
    plies = 0
    while not g.game_over and plies < max_plies:
        records.append((g.get_board_copy(), g.current_player))
        mv = pf.search(g, g.current_player, time_ms=movetime_ms)
        if not mv:
            break  # 引擎无走法（异常），本局截断
        result = g.move_piece(*mv)
        if not result['success']:
            break
        plies += 1
    return records, g.winner, plies


def main():
    ap = argparse.ArgumentParser(description='Pikafish 自对弈蒸馏数据生成')
    ap.add_argument('--games', type=int, default=200, help='对局数')
    ap.add_argument('--movetime', type=int, default=100,
                    help='每步 Pikafish 搜索毫秒（越大棋力越强、越慢）')
    ap.add_argument('--max-plies', type=int, default=120,
                    help='单局最大半回合数（防长对局失控）')
    ap.add_argument('--seed', type=int, default=20260814)
    ap.add_argument('--label', choices=('eval', 'result'), default='eval',
                    help='标签模式：eval=Pikafish 静态评估（默认）；'
                         'result=对局结果 ±1/0')
    ap.add_argument('--mirror', action='store_true', default=True,
                    help='180°+红黑互换镜像增强（默认开）')
    ap.add_argument('--no-mirror', dest='mirror', action='store_false')
    ap.add_argument('--out', default=DATA_FILE, help='输出 npz 路径')
    ap.add_argument('--quick', action='store_true',
                    help='快速验证：3 局 + movetime 50ms')
    args = ap.parse_args()

    if args.quick:
        args.games = 3
        args.movetime = 50

    # ── 引擎硬检查（防假可用）──
    pf = PikafishEngine()
    if not pf.available:
        print(f'[FAIL] Pikafish 不可用: {pf.error_msg}')
        sys.exit(1)
    print(f'[OK] Pikafish 就绪（{args.games} 局 × {args.movetime}ms/步）')

    rng = random.Random(args.seed)
    stats = {'red_win': 0, 'black_win': 0, 'draw': 0, 'total_plies': 0}
    xs, ys, fens, results, game_ids = [], [], [], [], []

    t0 = time.time()
    for gi in range(args.games):
        records, winner, plies = play_game(pf, rng, args.movetime,
                                           args.max_plies)
        res = 1 if winner == 1 else (-1 if winner == 2 else 0)
        if winner == 1:
            stats['red_win'] += 1
        elif winner == 2:
            stats['black_win'] += 1
        else:
            stats['draw'] += 1
        stats['total_plies'] += plies

        n_sample = 0
        for board, player in records:
            if args.label == 'eval':
                s = pf.evaluate_fen(board, player)
                if s is None:
                    continue  # 引擎故障/解析失败：跳过该样本
                y = float(s) / 100.0
            else:
                y = float(res)
            xs.append(NnueNet.extract_features(board))
            ys.append(y)
            fens.append(board_to_fen(board, player))
            results.append(res)
            game_ids.append(gi)
            n_sample += 1

        if (gi + 1) % 10 == 0 or gi == args.games - 1:
            print(f'  局 {gi + 1}/{args.games}：本局 {n_sample} 样本，'
                  f'累计 {len(ys)}（{time.time() - t0:.0f}s）')

    if not ys:
        print('[FAIL] 未产生任何样本（引擎全程失败？）')
        pf.close()
        sys.exit(1)

    # ── 镜像增强：180° + 红黑互换，y 取反 ──
    n_orig = len(ys)
    if args.mirror:
        x2, y2, f2, r2, g2 = [], [], [], [], []
        for i, fen in enumerate(fens):
            board, _player = fen_to_board(fen)
            mirrored = rotate180_swap(board)
            x2.append(NnueNet.extract_features(mirrored))
            y2.append(-ys[i])
            f2.append(board_to_fen(mirrored, 3 - _player))
            r2.append(-results[i])
            g2.append(game_ids[i])
        xs += x2
        ys += y2
        fens += f2
        results += r2
        game_ids += g2

    X = np.array(xs, dtype=np.float32)
    y = np.array(ys, dtype=np.float32)
    results_arr = np.array(results, dtype=np.int8)
    game_ids_arr = np.array(game_ids, dtype=np.int32)
    # 定长字节数组而非 object 数组：np.load 默认 allow_pickle=False
    # 也能读取，训练脚本 --resume 只读 X/y 不受影响，调试时可直接读 fens
    # S96：实测真实对局最长 FEN 70 字符，理论极值（10 行每行均匀分散
    # 子力）可达 ~85，留足余量防截断；保存前硬断言防静默截断
    max_fen = max(len(f) for f in fens)
    assert max_fen < 96, f'FEN 超长 {max_fen} ≥ 96，需增大 fens 定长'
    fens_arr = np.array([f.encode('utf-8') for f in fens], dtype='S96')

    out_dir = os.path.dirname(args.out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    np.savez_compressed(args.out, X=X, y=y, fens=fens_arr,
                        results=results_arr, game_ids=game_ids_arr)
    print(f'[保存] {args.out}（{len(y)} 样本'
          f'{", 含镜像 ×2" if args.mirror else ""}）')

    # ── 验收报告 ──
    games_done = stats['red_win'] + stats['black_win'] + stats['draw']
    print(f'[验收] 对局分布：红胜 {stats["red_win"]} / 黑胜 '
          f'{stats["black_win"]} / 和 {stats["draw"]} '
          f'（共 {games_done} 局，平均 {stats["total_plies"] // max(games_done,1)} 半回合/局）')
    if args.label == 'eval':
        print(f'[验收] 标签范围 [{y.min():+.3f}, {y.max():+.3f}]（兵单位，'
              f'红优为正）')
        # 镜像对称性：抽前 10 个原局重查 eval，与镜像标签符号相反
        bad = 0
        for i in range(min(10, n_orig)):
            board, player = fen_to_board(fens[i])
            s_orig = pf.evaluate_fen(board, player)
            if s_orig is None:
                continue
            s_mirror = pf.evaluate_fen(rotate180_swap(board), 3 - player)
            if s_mirror is None:
                continue
            if abs(s_orig + s_mirror) > 30.0:  # 0.3 兵容差
                bad += 1
                print(f'    镜像不对称: {fens[i][:40]}… '
                      f'orig={s_orig:+.0f} mirror={s_mirror:+.0f}')
        print(f'[验收] 镜像对称性（前 10 样本，|orig+mirror|≤30 厘兵）：'
              f'{10 - bad}/10 通过')
    # 标签一致性：抽样 5 个重查 evaluate_fen
    if args.label == 'eval':
        random.seed(1)
        ok = 0
        for i in random.sample(range(n_orig), min(5, n_orig)):
            board, player = fen_to_board(fens[i])
            s = pf.evaluate_fen(board, player)
            if s is not None and abs(s / 100.0 - ys[i]) < 1e-6:
                ok += 1
        print(f'[验收] 标签与 eval 一致（抽样 {ok}/'
              f'{min(5, n_orig)}）')

    pf.close()
    print('完成。数据可直接供阶段 3 训练使用 '
          '（train_nnue.py 扩展 --data 加载）。')


if __name__ == '__main__':
    main()
