"""轻量 NNUE 风格神经网络评估引擎

架构：HalfKP 风格特征 + 双隐藏层前馈网络
- 输入：棋子在 90 格上的 14 种类型分布（1260 维稀疏特征）
- 隐藏层1：256 神经元，ClippedReLU（范围 0-255，模拟量化推理）
- 隐藏层2：32 神经元，ClippedReLU
- 输出：1 标量（centipawn，红方视角）

设计目标：
- 纯 Python + numpy 推理，无需 PyTorch/TensorFlow
- 权重以紧凑二进制格式存储（int16 量化）
- 评估速度 <1ms/局面（单线程 numpy）
- 当权重文件不存在时自动回退到手工评估函数

Usage:
    from domain.nnue import NnueNet
    net = NnueNet('engines/nnue_weights.bin')  # None if file missing
    if net is not None:
        score = net.evaluate(board)  # 红方视角 centipawn
"""

import os
import struct
import numpy as np
from typing import Optional

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT

# ── 网络架构常量 ──
INPUT_DIM = 1260          # 90 格 × 14 种棋子类型
HIDDEN1_DIM = 256         # 第一隐藏层
HIDDEN2_DIM = 32          # 第二隐藏层
QA = 255                   # ClippedReLU 量化上限
QB = 64                    # 权重/偏置量化缩放因子

# 棋子类型 → 特征索引偏移
_PIECE_TYPE_INDEX = {
    'K': 0, 'A': 1, 'B': 2, 'N': 3, 'R': 4, 'C': 5, 'P': 6,
    'k': 7, 'a': 8, 'b': 9, 'n': 10, 'r': 11, 'c': 12, 'p': 13,
}

# 权重文件魔数
_NNUE_MAGIC = b'CCNN\x01'  # Chinese Chess NN, version 1


class NnueNet:
    """轻量神经网络评估引擎。

    当权重文件不可用时，构造返回 None（调用方回退到手评）。
    """

    def __init__(self, weight_path: Optional[str] = None) -> None:
        """加载 NNUE 权重文件。

        Args:
            weight_path: 权重文件路径。None 时自动查找 engines/nnue_weights.bin
        """
        if weight_path is None:
            weight_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'engines', 'nnue_weights.bin')

        self._w1: Optional[np.ndarray] = None   # (INPUT_DIM, HIDDEN1_DIM)
        self._b1: Optional[np.ndarray] = None   # (HIDDEN1_DIM,)
        self._w2: Optional[np.ndarray] = None   # (HIDDEN1_DIM, HIDDEN2_DIM)
        self._b2: Optional[np.ndarray] = None   # (HIDDEN2_DIM,)
        self._w3: Optional[np.ndarray] = None   # (HIDDEN2_DIM,)
        self._b3: float = 0.0

        self._loaded = False
        if os.path.isfile(weight_path):
            try:
                self._load_weights(weight_path)
                self._loaded = True
            except Exception:
                pass  # 解析失败，保持未加载状态

    @property
    def available(self) -> bool:
        """权重是否已成功加载。"""
        return self._loaded

    # ── 文件 I/O ──

    def _load_weights(self, path: str) -> None:
        """从二进制文件加载量化权重。"""
        with open(path, 'rb') as f:
            data = f.read()

        magic = data[:5]
        if magic != _NNUE_MAGIC:
            raise ValueError(f'无效的 NNUE 权重文件：魔数不匹配')

        offset = 5
        # 反量化辅助函数
        def read_vec(count: int):
            nonlocal offset
            arr = np.frombuffer(data, dtype=np.int16, count=count, offset=offset)
            offset += count * 2
            return (arr.astype(np.float32) / QB).copy()

        def read_scalar():
            nonlocal offset
            val = struct.unpack_from('<h', data, offset)[0]
            offset += 2
            return float(val) / QB

        # 读取各层
        self._w1 = read_vec(INPUT_DIM * HIDDEN1_DIM).reshape(INPUT_DIM, HIDDEN1_DIM)
        self._b1 = read_vec(HIDDEN1_DIM)
        self._w2 = read_vec(HIDDEN1_DIM * HIDDEN2_DIM).reshape(HIDDEN1_DIM, HIDDEN2_DIM)
        self._b2 = read_vec(HIDDEN2_DIM)
        self._w3 = read_vec(HIDDEN2_DIM)
        self._b3 = read_scalar()

    def save_weights(self, path: str) -> None:
        """将网络权重保存为二进制文件（用于训练后导出）。"""
        def write_vec(arr: np.ndarray, fp):
            quant = np.clip(np.round(arr * QB), -32768, 32767).astype(np.int16)
            fp.write(quant.tobytes())

        def write_scalar(val: float, fp):
            q = int(np.clip(round(val * QB), -32768, 32767))
            fp.write(struct.pack('<h', q))

        with open(path, 'wb') as f:
            f.write(_NNUE_MAGIC)
            write_vec(self._w1, f)
            write_vec(self._b1, f)
            write_vec(self._w2, f)
            write_vec(self._b2, f)
            write_vec(self._w3, f)
            write_scalar(self._b3, f)

    # ── 特征提取 ──

    @staticmethod
    def extract_features(board: list) -> np.ndarray:
        """从棋盘提取稀疏特征向量。

        Returns:
            (INPUT_DIM,) float32 数组，每个位置 0.0 或 1.0
        """
        features = np.zeros(INPUT_DIM, dtype=np.float32)
        for r in range(BOARD_HEIGHT):
            for c in range(BOARD_WIDTH):
                piece = board[r][c]
                if piece == '.':
                    continue
                type_idx = _PIECE_TYPE_INDEX.get(piece, -1)
                if type_idx < 0:
                    continue
                square_idx = r * BOARD_WIDTH + c
                feature_idx = type_idx * 90 + square_idx
                if feature_idx < INPUT_DIM:
                    features[feature_idx] = 1.0
        return features

    # ── 前向传播 ──

    def evaluate(self, board: list) -> float:
        """评估棋盘局面，返回红方视角 centipawn 评分。

        正值 = 红优，负值 = 黑优。
        无权重文件时返回 0.0。
        """
        if not self._loaded:
            return 0.0

        features = self.extract_features(board)

        # 第一隐藏层：input → hidden1 + ClippedReLU
        hidden1 = np.dot(features, self._w1) + self._b1
        np.clip(hidden1, 0, QA, out=hidden1)

        # 第二隐藏层：hidden1 → hidden2 + ClippedReLU
        hidden2 = np.dot(hidden1, self._w2) + self._b2
        np.clip(hidden2, 0, QA, out=hidden2)

        # 输出层：hidden2 → scalar
        score = float(np.dot(hidden2, self._w3) + self._b3)

        # 缩放到 centipawn（训练时标签为 Pikafish centipawn / 100）
        return score * 100.0


# ── 全局单例 ──
_nnue_net: Optional[NnueNet] = None
_nnue_checked: bool = False


def get_nnue() -> Optional[NnueNet]:
    """获取全局 NNUE 评估器单例（延迟加载）。"""
    global _nnue_net, _nnue_checked
    if not _nnue_checked:
        _nnue_net = NnueNet()
        _nnue_checked = True
    return _nnue_net if _nnue_net.available else None
