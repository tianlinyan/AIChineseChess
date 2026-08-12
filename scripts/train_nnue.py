"""NNUE 风格神经网络训练脚本

用 Pikafish 评估作为训练标签，训练一个轻量前馈网络。
生成的数据文件（data/train_data.npz）和权重文件（engines/nnue_weights.bin）
可在后续重新加载复用。

用法：
    python scripts/train_nnue.py              # 从头训练
    python scripts/train_nnue.py --resume     # 继续训练
    python scripts/train_nnue.py --quick      # 快速模式（少量数据，测试用）

依赖：numpy（无需 PyTorch/TensorFlow — 用小批量梯度下降手写训练循环）
"""

import os
import sys
import time
import argparse
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.pikafish import PikafishEngine
from domain.nnue import NnueNet, INPUT_DIM, HIDDEN1_DIM, HIDDEN2_DIM, QA

# ── 训练配置 ──
QUICK_SAMPLES = 1000         # 快速模式：1000 样本
FULL_SAMPLES = 50000         # 完整模式：50,000 样本
BATCH_SIZE = 128
LEARNING_RATE = 0.01
EPOCHS = 50
WEIGHT_DECAY = 1e-4
VALIDATION_SPLIT = 0.1
DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         'data', 'train_data.npz')
WEIGHT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           'engines', 'nnue_weights.bin')


def generate_training_data(n_samples: int) -> tuple:
    """用 Pikafish 评估随机局面，生成训练数据。

    对每个样本：随机走 8-30 步棋到达一个中局/残局局面，
    用 Pikafish 搜索 0.5s 获取 NNUE 评估分作为标签。

    Returns:
        (features: np.ndarray (N, INPUT_DIM),
         scores: np.ndarray (N,))  — scores 是红方视角 centipawn/100
    """
    print(f'正在用 Pikafish 生成 {n_samples} 个训练样本...')
    engine = PikafishEngine()
    if not engine.available:
        print('⚠ Pikafish 不可用，无法生成训练数据')
        print(f'  {engine.error_msg}')
        return None, None

    features_list = []
    scores_list = []
    game = ChineseChessGame()

    batch_size = 200
    for i in range(n_samples):
        # 随机走子到达随机局面
        g = ChineseChessGame()
        n_moves = np.random.randint(4, 25)
        for _ in range(n_moves):
            moves = g.get_all_legal_moves(g.current_player)
            if not moves:
                break
            mv = moves[np.random.randint(len(moves))]
            g.move_piece(*mv)
            if g.game_over:
                break

        if g.game_over:
            continue

        # 用 Pikafish 评估
        player = g.current_player
        move = engine.search(g, player, time_ms=500)
        if move is None:
            continue

        # 从 MultiPV 获取评分
        top_moves = engine.get_top_moves_scores()
        if top_moves:
            best_score_cp = top_moves[0][1]  # centipawn，走子方视角
        else:
            continue

        # 转换为红方视角
        red_score = best_score_cp if player == 1 else -best_score_cp

        # 提取特征
        features = NnueNet.extract_features(g.board)
        features_list.append(features)
        scores_list.append(red_score / 100.0)  # 缩放

        if (i + 1) % batch_size == 0:
            print(f'  已生成 {i + 1}/{n_samples} 样本')

    engine.close()
    X = np.array(features_list, dtype=np.float32)
    y = np.array(scores_list, dtype=np.float32)
    print(f'  完成：{len(y)} 个有效样本')
    print(f'  评分范围：[{y.min():.1f}, {y.max():.1f}]')
    return X, y


def train_network(X: np.ndarray, y: np.ndarray) -> NnueNet:
    """用小批量梯度下降训练网络。

    使用 MSE 损失 + ClippedReLU 激活（模拟量化推理环境）。
    """
    n = len(y)
    n_val = int(n * VALIDATION_SPLIT)
    n_train = n - n_val

    # 打乱
    idx = np.random.permutation(n)
    X, y = X[idx], y[idx]
    X_train, y_train = X[:n_train], y[:n_train]
    X_val, y_val = X[n_train:], y[n_train:]

    print(f'训练集 {n_train} 样本，验证集 {n_val} 样本')
    print(f'训练 {EPOCHS} epoch，batch={BATCH_SIZE}，lr={LEARNING_RATE}')

    # 初始化权重（Xavier 风格）
    rng = np.random.RandomState(42)
    scale1 = np.sqrt(2.0 / (INPUT_DIM + HIDDEN1_DIM))
    scale2 = np.sqrt(2.0 / (HIDDEN1_DIM + HIDDEN2_DIM))
    scale3 = np.sqrt(2.0 / (HIDDEN2_DIM + 1))

    w1 = rng.randn(INPUT_DIM, HIDDEN1_DIM).astype(np.float32) * scale1
    b1 = np.zeros(HIDDEN1_DIM, dtype=np.float32)
    w2 = rng.randn(HIDDEN1_DIM, HIDDEN2_DIM).astype(np.float32) * scale2
    b2 = np.zeros(HIDDEN2_DIM, dtype=np.float32)
    w3 = rng.randn(HIDDEN2_DIM).astype(np.float32) * scale3
    b3 = 0.0

    best_val_loss = float('inf')
    best_weights = None

    for epoch in range(EPOCHS):
        # 打乱训练集
        perm = np.random.permutation(n_train)
        X_train, y_train = X_train[perm], y_train[perm]

        total_loss = 0.0
        n_batches = 0

        for start in range(0, n_train, BATCH_SIZE):
            end = min(start + BATCH_SIZE, n_train)
            Xb = X_train[start:end]
            yb = y_train[start:end]

            # ── 前向传播 ──
            h1 = np.dot(Xb, w1) + b1
            h1_clipped = np.clip(h1, 0, QA)

            h2 = np.dot(h1_clipped, w2) + b2
            h2_clipped = np.clip(h2, 0, QA)

            y_pred = np.dot(h2_clipped, w3) + b3

            # ── MSE 损失 ──
            err = y_pred - yb
            loss = np.mean(err ** 2)
            total_loss += loss
            n_batches += 1

            # ── 反向传播 ──
            m = len(Xb)
            # 输出层梯度
            dy = (2.0 / m) * err
            dw3 = np.dot(h2_clipped.T, dy)
            db3 = np.sum(dy)
            dh2 = np.dot(dy, w3.T)

            # 第二隐藏层梯度（通过 ClippedReLU）
            dh2[h2 <= 0] = 0
            dh2[h2 >= QA] = 0

            dw2 = np.dot(h1_clipped.T, dh2)
            db2 = np.sum(dh2, axis=0)
            dh1 = np.dot(dh2, w2.T)

            # 第一隐藏层梯度
            dh1[h1 <= 0] = 0
            dh1[h1 >= QA] = 0

            # 稀疏输入梯度：只对非零输入更新权重
            dw1 = np.dot(Xb.T, dh1)
            db1 = np.sum(dh1, axis=0)

            # ── 权重更新（SGD + 权重衰减）──
            lr = LEARNING_RATE
            w1 -= lr * (dw1 + WEIGHT_DECAY * w1)
            b1 -= lr * db1
            w2 -= lr * (dw2 + WEIGHT_DECAY * w2)
            b2 -= lr * db2
            w3 -= lr * (dw3 + WEIGHT_DECAY * w3)
            b3 -= lr * db3

        # ── 验证 ──
        h1_v = np.clip(np.dot(X_val, w1) + b1, 0, QA)
        h2_v = np.clip(np.dot(h1_v, w2) + b2, 0, QA)
        y_v = np.dot(h2_v, w3) + b3
        val_loss = np.mean((y_v - y_val) ** 2)

        train_loss = total_loss / n_batches

        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f'  epoch {epoch + 1:3d}: train_loss={train_loss:.4f}, '
                  f'val_loss={val_loss:.4f}')

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_weights = (w1.copy(), b1.copy(), w2.copy(), b2.copy(),
                           w3.copy(), b3)

    # ── 构建最终网络 ──
    net = NnueNet.__new__(NnueNet)
    net._w1, net._b1, net._w2, net._b2, net._w3 = best_weights
    net._b3 = b3
    net._loaded = True

    # 验证集上的表现
    h1_f = np.clip(np.dot(X_val, net._w1) + net._b1, 0, QA)
    h2_f = np.clip(np.dot(h1_f, net._w2) + net._b2, 0, QA)
    y_f = np.dot(h2_f, net._w3) + net._b3
    mae = np.mean(np.abs(y_f - y_val))
    corr = np.corrcoef(y_f, y_val)[0, 1]

    print(f'\n训练完成：val_mae={mae:.3f} (×100=centipawn), '
          f'val_corr={corr:.4f}')
    return net


def main():
    parser = argparse.ArgumentParser(description='训练 NNUE 风格评估网络')
    parser.add_argument('--quick', action='store_true', help='快速测试模式')
    parser.add_argument('--resume', action='store_true', help='从已有数据继续')
    args = parser.parse_args()

    n_samples = QUICK_SAMPLES if args.quick else FULL_SAMPLES

    # 创建 data 目录
    data_dir = os.path.dirname(DATA_FILE)
    if data_dir and not os.path.isdir(data_dir):
        os.makedirs(data_dir, exist_ok=True)

    # 加载或生成数据
    if args.resume and os.path.isfile(DATA_FILE):
        print(f'从 {DATA_FILE} 加载已有训练数据...')
        d = np.load(DATA_FILE)
        X, y = d['X'], d['y']
    else:
        t0 = time.time()
        X, y = generate_training_data(n_samples)
        if X is None:
            return 1
        print(f'数据生成耗时 {time.time() - t0:.0f}s')

        # 保存数据供后续复用
        print(f'保存训练数据到 {DATA_FILE}...')
        np.savez_compressed(DATA_FILE, X=X, y=y)

    # 训练
    t0 = time.time()
    net = train_network(X, y)
    print(f'训练耗时 {time.time() - t0:.0f}s')

    # 保存权重
    net.save_weights(WEIGHT_FILE)
    print(f'权重已保存到 {WEIGHT_FILE}')
    print(f'文件大小：{os.path.getsize(WEIGHT_FILE):,} bytes')

    return 0


if __name__ == '__main__':
    sys.exit(main())
