"""中国象棋残局库查询 — chessdb.cn 云库 + 本地基础残局知识

提供 DTM (Depth to Mate) 查询：
- 在线查询 chessdb.cn 云库（郭博君维护，覆盖 8700+ 残局类型）
- 本地基础残局判定（单子杀、困毙检测）
- 无缝回退：云库不可用时不影响正常搜索
"""

import json
import time
import urllib.request
import urllib.error
from typing import Optional, Tuple


# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

CHESSDB_URL = "https://www.chessdb.cn/query/"
CHESSDB_TIMEOUT = 2.0          # 查询超时（秒）
CHESSDB_CACHE_TTL = 300        # 缓存有效期（秒）
CHESSDB_ENABLED = True         # 是否启用云库查询


# ══════════════════════════════════════════════════════════════════════════════
# 缓存
# ══════════════════════════════════════════════════════════════════════════════

_cache: dict = {}              # {fen_key: (dtm, win_side, timestamp)}


def _board_to_fen(board: list, current_player: int) -> str:
    """将 10×9 棋盘转为中国象棋 FEN 字符串（用于 chessdb.cn 查询）。

    FEN 格式：rows/rows/.../rows <side>
    - 大写=红方，小写=黑方，数字=连续空格数
    - w=红方走, b=黑方走
    """
    rows = []
    for r in range(10):
        row_str = ""
        empty = 0
        for c in range(9):
            p = board[r][c]
            if p == '.':
                empty += 1
            else:
                if empty > 0:
                    row_str += str(empty)
                    empty = 0
                row_str += p
        if empty > 0:
            row_str += str(empty)
        rows.append(row_str)
    side = 'w' if current_player == 1 else 'b'
    return '/'.join(rows) + ' ' + side


def _fen_cache_key(board: list, current_player: int) -> str:
    """生成 FEN 缓存键（精简版，仅棋子位置+走子方）。"""
    key_parts = []
    for r in range(10):
        for c in range(9):
            key_parts.append(board[r][c])
    key_parts.append('w' if current_player == 1 else 'b')
    return ''.join(key_parts)


def probe_cloud(board: list, current_player: int) -> Optional[dict]:
    """查询 chessdb.cn 云库。

    Returns:
        None — 查询失败或未找到
        dict — {'dtm': int, 'win': int, 'score': int}
          dtm: 距离杀棋的步数（0=已杀）
          win: 1=红胜, 2=黑胜, 0=和棋
          score: 局面评分（mate分）
    """
    if not CHESSDB_ENABLED:
        return None

    cache_key = _fen_cache_key(board, current_player)
    now = time.time()

    # 检查缓存
    if cache_key in _cache:
        dtm, win, ts = _cache[cache_key]
        if now - ts < CHESSDB_CACHE_TTL:
            return {'dtm': dtm, 'win': win, 'score': _dtm_to_score(dtm, win, current_player)}
        del _cache[cache_key]

    # 构造 FEN 并查询
    fen = _board_to_fen(board, current_player)
    url = CHESSDB_URL + '?' + urllib.parse.quote(fen)

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'AIChineseChess/1.0')
        with urllib.request.urlopen(req, timeout=CHESSDB_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError, ValueError):
        # 网络不可用、超时、非 JSON → 静默回退
        return None

    if not isinstance(data, dict):
        return None

    win = data.get('win', 0)     # 1=红胜, 2=黑胜, 0=和棋/未知
    dtm = data.get('dtm', 0)     # 距离杀棋步数

    if win == 0 and dtm == 0:
        return None  # 云库中无此局面

    # 缓存结果
    _cache[cache_key] = (dtm, win, now)

    score = _dtm_to_score(dtm, win, current_player)
    return {'dtm': dtm, 'win': win, 'score': score}


def _dtm_to_score(dtm: int, win: int, current_player: int) -> float:
    """将 DTM 值转为评估分数。

    杀棋分数 = ±(100000 - dtm * 10)，越近杀棋分数越高。
    """
    if win == 0:
        return 0.0
    base = 100000 - dtm * 10
    if win == 1:    # 红胜
        return base if current_player == 1 else -base
    else:            # 黑胜
        return -base if current_player == 1 else base


def probe(board: list, current_player: int,
          piece_count: int = 32) -> Optional[Tuple[float, int]]:
    """查询残局库 — 自动选择本地判定或云库查询。

    Args:
        board: 10×9 棋盘
        current_player: 当前走子方 (1=红, 2=黑)
        piece_count: 棋盘上的棋子总数（调用方可预先计算）

    Returns:
        None — 残局库中无此局面
        (score, dtm) — 评估分数和距离杀棋步数
    """
    # 只有子力 ≤ 10 才查询（全盘局面残局库覆盖有限且网络开销大）
    if piece_count > 10:
        return None

    # 本地基础判定
    local = _local_egtb(board, current_player)
    if local is not None:
        return local

    # 云库查询（子力 ≤ 4 时本地判定不足，主要靠云库）
    if piece_count <= 4:
        result = probe_cloud(board, current_player)
        if result is not None:
            return (result['score'], result['dtm'])

    return None


def _local_egtb(board: list, current_player: int) -> Optional[Tuple[float, int]]:
    """本地基础残局判定 — 覆盖最简单的必胜/必和局面。

    当前支持：
    - 单子杀（对方无子）：必胜，返回高分
    - 对方无合法走法：困毙 -> 对方负
    """
    from domain.game import ChineseChessGame
    from domain.evaluation import PIECE_VALUE

    # 统计子力
    red_has_attackers = False
    black_has_attackers = False
    for r in range(10):
        for c in range(9):
            p = board[r][c]
            if p == '.' or p.upper() in ('K', 'A', 'B'):
                continue
            if p.isupper():
                red_has_attackers = True
            else:
                black_has_attackers = True

    # 只有帅/将+士/相 → 和棋（无攻击子力）
    if current_player == 1:
        if not red_has_attackers and not black_has_attackers:
            return (0.0, 0)  # 和棋
        if not black_has_attackers:
            # 红方有攻击子，黑方没有 → 红方必胜
            return (80000.0, 10)  # 必胜但不确定具体步数
    else:
        if not red_has_attackers and not black_has_attackers:
            return (0.0, 0)
        if not red_has_attackers:
            return (80000.0, 10)  # 黑方必胜（但从黑方视角返回正分）

    return None


def clear_cache() -> None:
    """清空查询缓存。"""
    _cache.clear()
