"""中国象棋残局库查询 — chessdb.cn 云库 + 本地基础残局知识

提供 DTM (Depth to Mate) 查询：
- 在线查询 chessdb.cn 云库（郭博君维护，覆盖 8700+ 残局类型）
- 本地基础残局判定（单子杀、困毙检测）
- 无缝回退：云库不可用时不影响正常搜索
"""

import re
import sys
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

CHESSDB_URL = "https://www.chessdb.cn/chessdb.php"
CHESSDB_TIMEOUT = 1.0          # 查询超时（秒）
CHESSDB_CACHE_TTL = 300        # 正缓存有效期（秒）
CHESSDB_NEG_CACHE_TTL = 60     # 负缓存（未找到/查询失败）有效期（秒）
CHESSDB_ENABLED = True         # 是否启用云库查询
CACHE_MAX_SIZE = 5000          # 正缓存最大条目数（超出淘汰最旧）
NEG_CACHE_MAX_SIZE = 5000      # 负缓存最大条目数（与正缓存同上限，防长会话膨胀）
CLOUD_FAIL_BREAKER_COUNT = 3   # 连续网络失败次数上限，达到后熔断
CLOUD_BREAKER_SECONDS = 120    # 熔断后暂停云查询的秒数


# ══════════════════════════════════════════════════════════════════════════════
# 缓存
# ══════════════════════════════════════════════════════════════════════════════

_cache: dict = {}              # {fen_key: (dtm, win_side, timestamp)}
_neg_cache: dict = {}          # {fen_key: timestamp} 未命中/失败负缓存
_cloud_fail_count = 0          # 连续网络失败计数（熔断器）
_cloud_disabled_until = 0.0    # 熔断截止时间戳


def _fen_cache_key(board: list, current_player: int) -> str:
    """生成 FEN 缓存键（精简版，仅棋子位置+走子方）。"""
    key_parts = []
    for r in range(10):
        for c in range(9):
            key_parts.append(board[r][c])
    key_parts.append('w' if current_player == 1 else 'b')
    return ''.join(key_parts)


def _neg_cache_put(key: str, ts: float) -> None:
    """写负缓存（容量上限：满时先清扫过期项，仍满则淘汰最旧）。"""
    if len(_neg_cache) >= NEG_CACHE_MAX_SIZE:
        expired = [k for k, t in _neg_cache.items()
                   if ts - t >= CHESSDB_NEG_CACHE_TTL]
        for k in expired:
            del _neg_cache[k]
    if len(_neg_cache) >= NEG_CACHE_MAX_SIZE:
        _neg_cache.pop(next(iter(_neg_cache)))
    _neg_cache[key] = ts


def probe_cloud(board: list, current_player: int) -> Optional[dict]:
    """查询 chessdb.cn 云库（chessdb.php?action=queryall API）。

    响应为管道分隔文本（非 JSON），按 score 降序排列，首条即最佳走法：
      move:c0c8,score:29999,rank:2,note:! (W-M-0001)|move:...
    note 中的 (W/D/L-M-NNNN) 为**走子方视角**的胜/和/负与 DTM。

    Returns:
        None — 查询失败或未找到
        dict — {'dtm': int, 'win': int, 'score': int}
          dtm: 距离杀棋的步数（0=已杀/和棋）
          win: 1=红胜, 2=黑胜, 0=和棋
          score: 局面评分（mate分，current_player 视角）
    """
    global _cloud_fail_count, _cloud_disabled_until
    if not CHESSDB_ENABLED:
        return None

    cache_key = _fen_cache_key(board, current_player)
    now = time.time()

    # 检查正缓存
    if cache_key in _cache:
        dtm, win, ts = _cache[cache_key]
        if now - ts < CHESSDB_CACHE_TTL:
            return {'dtm': dtm, 'win': win, 'score': _dtm_to_score(dtm, win, current_player)}
        del _cache[cache_key]

    # 检查负缓存（未找到/失败的结果也缓存，避免同一局面反复发 HTTP）
    if cache_key in _neg_cache:
        ts = _neg_cache[cache_key]
        if now - ts < CHESSDB_NEG_CACHE_TTL:
            return None
        del _neg_cache[cache_key]

    # 熔断器：连续网络失败过多，暂停云查询一段时间
    if now < _cloud_disabled_until:
        return None

    # 构造 FEN 并查询
    fen = board_to_fen(board, current_player)
    url = (CHESSDB_URL + '?action=queryall&board='
           + urllib.parse.quote(fen, safe=''))

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'AIChineseChess/1.0')
        with urllib.request.urlopen(req, timeout=CHESSDB_TIMEOUT) as resp:
            text = resp.read().decode('utf-8', errors='replace')
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        # 网络不可用、超时 → 负缓存 + 熔断计数
        print(f"[EGTB] 云库查询失败: {e}", file=sys.stderr, flush=True)
        _neg_cache_put(cache_key, now)
        _cloud_fail_count += 1
        if _cloud_fail_count >= CLOUD_FAIL_BREAKER_COUNT:
            _cloud_disabled_until = now + CLOUD_BREAKER_SECONDS
            _cloud_fail_count = 0
        return None

    _cloud_fail_count = 0  # 成功通信，重置熔断计数

    # 解析首条（最佳）走法的 note 字段；空响应 / "invalid board" /
    # 无 note → 云库中无此局面，负缓存（通信正常，不计熔断）
    first = text.strip().split('|', 1)[0]
    m = re.search(r'\(([WDL])-M-(\d+)\)', first)
    if m is None:
        _neg_cache_put(cache_key, now)
        return None

    outcome, dtm_str = m.group(1), m.group(2)
    if outcome == 'W':      # 走子方胜
        win = current_player
        dtm = int(dtm_str)
    elif outcome == 'L':    # 走子方负
        win = 3 - current_player
        dtm = int(dtm_str)
    else:                   # 和棋
        win = 0
        dtm = 0

    # 缓存结果（容量上限：淘汰最旧条目）
    if len(_cache) >= CACHE_MAX_SIZE:
        _cache.pop(next(iter(_cache)))
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
          piece_count: int = 32,
          allow_cloud: bool = True) -> Optional[Tuple[float, int]]:
    """查询残局库 — 自动选择本地判定或云库查询。

    Args:
        board: 10×9 棋盘
        current_player: 当前走子方 (1=红, 2=黑)
        piece_count: 棋盘上的棋子总数（调用方可预先计算）
        allow_cloud: 是否允许 chessdb.cn 云查询。搜索/MCTS 的叶节点
            必须传 False（同步 HTTP 会让搜索瘫痪）；UI 层单次查询
            或根节点预取可用 True。

    Returns:
        None — 残局库中无此局面
        (score, dtm) — 评估分数（current_player 视角）和距离杀棋步数
    """
    # 只有子力 ≤ EGTB_MAX_PIECES 才查询
    if piece_count > EGTB_MAX_PIECES:
        return None

    # 本地基础判定
    local = _local_egtb(board, current_player)
    if local is not None:
        return local

    # 云库查询
    if allow_cloud and piece_count <= EGTB_CLOUD_MAX_PIECES:
        result = probe_cloud(board, current_player)
        if result is not None:
            return (result['score'], result['dtm'])

    return None


def _local_egtb(board: list, current_player: int) -> Optional[Tuple[float, int]]:
    """本地基础残局判定 — 覆盖常见必胜/必和局面。

    支持：
    - 无攻击子力双方 → 和棋
    - 一方有攻击子对一方无 → 按残局常识判定（结合防守方士象数量）
    - 单車必胜（对士象全为官和）、单馬必胜孤将、双炮必胜孤将
    - 双車/車炮/車馬必胜
    """
    red_attackers = []   # 红方攻击子力列表 (piece, row, col)
    black_attackers = [] # 黑方攻击子力列表
    red_advisors = red_bishops = 0      # 红方士/相数量（防守力）
    black_advisors = black_bishops = 0  # 黑方士/象数量

    for r in range(10):
        for c in range(9):
            p = board[r][c]
            if p == '.' or p.upper() == 'K':
                continue
            if p.upper() == 'A':
                if p.isupper():
                    red_advisors += 1
                else:
                    black_advisors += 1
                continue
            if p.upper() == 'B':
                if p.isupper():
                    red_bishops += 1
                else:
                    black_bishops += 1
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
        return _can_win(red_attackers, current_player, 1,
                        black_advisors, black_bishops)
    if red_count == 0:
        return _can_win(black_attackers, current_player, 2,
                        red_advisors, red_bishops)

    return None


def _can_win(attackers: list, current_player: int, owner: int,
             defender_advisors: int = 0,
             defender_bishops: int = 0) -> Optional[Tuple[float, int]]:
    """判断一组攻击子力能否必胜（结合防守方士/象数量）。

    中国象棋残局常识：
    - 单車 → 必胜；但对士象全（2士2象）是官和
    - 单馬 → 必胜孤将、可胜单士；有象防守则和
    - 单炮 → 必和（无炮架）
    - 单卒 → 需具体判断（过河未过河、是否被阻挡）
    - 双炮（互为炮架）→ 必胜孤将
    - 双車/車炮/車馬 → 必胜
    """
    has_rook = any(p == 'R' for p, _, _ in attackers)
    has_cannon = any(p == 'C' for p, _, _ in attackers)
    count = len(attackers)
    defender_total = defender_advisors + defender_bishops

    # 单子
    if count == 1:
        p, r, c = attackers[0]
        if p == 'R':
            # 单車 vs 士象全 → 官和
            if defender_advisors >= 2 and defender_bishops >= 2:
                return (0.0, 0)
            score = 80000 if owner == current_player else -80000
            return (score, 20)  # 单车必胜，~20步
        if p == 'N':
            # 单馬必胜孤将；单士可擒；有象则和
            if defender_total == 0:
                score = 40000 if owner == current_player else -40000
                return (score, 30)
            if defender_advisors == 1 and defender_bishops == 0:
                score = 20000 if owner == current_player else -20000
                return (score, 40)
            return (0.0, 0)
        if p == 'C':
            return (0.0, 0)     # 单炮（无炮架）→ 和棋
        if p == 'P':
            # 单卒：仅"过河未到底 vs 孤将"可胜；有防守子（士/象）或
            # 老兵（沉底）均为和棋（经 chessdb.cn 云库实测核对）
            crossed = (r <= 4) if owner == 1 else (r >= 5)
            at_bottom = (r == 0) if owner == 1 else (r == 9)
            if crossed and not at_bottom and defender_total == 0:
                score = 5000 if owner == current_player else -5000
                return (score, 30)
            return (0.0, 0)     # 未过河/老兵/有防守子 → 和棋

    # 多子：有車则必胜
    if has_rook:
        score = 85000 if owner == current_player else -85000
        return (score, 15)

    # 双炮 vs 孤将：互为炮架，必胜
    if (count == 2 and has_cannon
            and all(p == 'C' for p, _, _ in attackers)
            and defender_total == 0):
        score = 80000 if owner == current_player else -80000
        return (score, 20)

    # 其他含炮组合：可能不够赢，给中等优势分
    if has_cannon and count >= 2:
        score = 50000 if owner == current_player else -50000
        return (score, 25)

    # 其他组合：有子力优势但不确定
    if count >= 2:
        score = 30000 if owner == current_player else -30000
        return (score, 30)

    return None


def clear_cache() -> None:
    """清空查询缓存（含负缓存）。"""
    _cache.clear()
    _neg_cache.clear()
