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
import urllib.parse
from typing import Optional, Tuple

from domain.fen import board_to_fen
from domain.constants import EGTB_MAX_PIECES, EGTB_CLOUD_MAX_PIECES


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
    fen = board_to_fen(board, current_player)
    url = CHESSDB_URL + '?fen=' + urllib.parse.quote(fen, safe='')

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
    base = 99999 - dtm * 10  # 与 search.JIANGSHA_SCORE 一致
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
    # 只有子力 ≤ EGTB_MAX_PIECES 才查询
    if piece_count > EGTB_MAX_PIECES:
        return None

    # 本地基础判定
    local = _local_egtb(board, current_player)
    if local is not None:
        return local

    # 云库查询
    if piece_count <= EGTB_CLOUD_MAX_PIECES:
        result = probe_cloud(board, current_player)
        if result is not None:
            return (result['score'], result['dtm'])

    return None


def _local_egtb(board: list, current_player: int) -> Optional[Tuple[float, int]]:
    """本地基础残局判定 — 覆盖常见必胜/必和局面。

    支持：
    - 无攻击子力双方 → 和棋
    - 一方有攻击子对一方无 → 必胜（需验证能否赢）
    - 单車必胜、单馬不和、单炮不和
    - 双車/車炮/車馬必胜
    """
    red_attackers = []   # 红方攻击子力列表 (piece, row, col)
    black_attackers = [] # 黑方攻击子力列表

    for r in range(10):
        for c in range(9):
            p = board[r][c]
            if p == '.' or p.upper() in ('K', 'A', 'B'):
                continue
            if p.isupper():
                red_attackers.append((p.upper(), r, c))
            else:
                black_attackers.append((p.upper(), r, c))

    red_count = len(red_attackers)
    black_count = len(black_attackers)

    # ── 双方无攻击子 → 和棋 ──
    if red_count == 0 and black_count == 0:
        return (0.0, 0)

    # ── 单方有攻击子 → 判定能否必胜 ──
    if black_count == 0:
        return _can_win(red_attackers, current_player, 1)
    if red_count == 0:
        return _can_win(black_attackers, current_player, 2)

    return None


def _can_win(attackers: list, current_player: int, owner: int) -> Optional[Tuple[float, int]]:
    """判断一组攻击子力能否必胜。

    中国象棋残局常识：
    - 单車 → 必胜
    - 单馬 → 必和（无法将死）
    - 单炮 → 必和（无炮架）
    - 单卒 → 需具体判断（过河未过河、是否被阻挡）
    - 双車/車炮/車馬/双炮有架 → 必胜
    """
    has_rook = any(p == 'R' for p, _, _ in attackers)
    has_cannon = any(p == 'C' for p, _, _ in attackers)
    has_knight = any(p == 'N' for p, _, _ in attackers)
    has_pawn = any(p == 'P' for p, _, _ in attackers)
    count = len(attackers)

    # 单子
    if count == 1:
        p, r, c = attackers[0]
        if p == 'R':
            score = 80000 if owner == current_player else -80000
            return (score, 20)  # 单车必胜，~20步
        if p in ('N', 'C'):
            return (0.0, 0)     # 单马/单炮 → 和棋
        if p == 'P':
            # 单卒：过河且未被阻挡 → 可能赢；否则和
            crossed = (r <= 4) if owner == 1 else (r >= 5)
            if crossed:
                score = 5000 if owner == current_player else -5000
                return (score, 30)
            return (0.0, 0)     # 未过河卒 → 和棋

    # 多子：有車则必胜
    if has_rook:
        score = 85000 if owner == current_player else -85000
        return (score, 15)

    # 双炮无車：可能不够赢，给中等优势分
    if has_cannon and count >= 2:
        score = 50000 if owner == current_player else -50000
        return (score, 25)

    # 其他组合：有子力优势但不确定
    if count >= 2:
        score = 30000 if owner == current_player else -30000
        return (score, 30)

    return None


def clear_cache() -> None:
    """清空查询缓存。"""
    _cache.clear()
