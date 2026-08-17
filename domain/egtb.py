"""中国象棋残局库查询 — 本地基础残局知识 + DTM 回溯分析表

提供 DTM (Depth to Mate) 查询：
- 本地基础残局判定（单子杀、困毙检测、常见必胜/必和局面）
- 本地 DTM 回溯分析表（≤4 子，精确）
- 无网络依赖：生产路径不使用 chessdb.cn 云库（旧云查询路径已移除，
  所有调用方均为本地查询，避免搜索叶节点同步 HTTP 卡死）
"""

from typing import Optional, Tuple

from domain.constants import EGTB_MAX_PIECES


def probe(board: list, current_player: int,
          piece_count: int = 32,
          material_counts: Optional[dict] = None) -> Optional[Tuple[float, int]]:
    """查询残局库 — 本地判定 + DTM 回溯表。

    Args:
        board: 10×9 棋盘
        current_player: 当前走子方 (1=红, 2=黑)
        piece_count: 棋盘上的棋子总数（调用方可预先计算）
        material_counts: 可选增量子力计数 {piece: count}（来自
            game._material_counts）。提供时先做 O(1) 快速否定：双方都
            有攻击子（車馬炮兵）时，本地库（单方攻子构型）必不命中，
            直接返回 None——省去搜索叶节点评估里每次的全盘扫描。

    Returns:
        None — 残局库中无此局面
        (score, dtm) — 评估分数（current_player 视角）和距离杀棋步数
          （score==0.0 即和棋；本地 DTM 表和棋返回 dtm=DTM_DRAW(255)）
    """
    # 只有子力 ≤ EGTB_MAX_PIECES 才查询
    if piece_count > EGTB_MAX_PIECES:
        return None

    # 快速否定：双方都有攻击子 → 本地 DTM 表（Kk + 红攻子 + 黑防守子）
    # 与启发式（单方有攻击子）均不匹配，直接返回 None
    if material_counts is not None:
        red_att = sum(material_counts.get(p, 0) for p in ('R', 'N', 'C', 'P'))
        black_att = sum(material_counts.get(p, 0) for p in ('r', 'n', 'c', 'p'))
        if red_att > 0 and black_att > 0:
            return None

    # ── 本地 DTM 回溯分析表（精确）──
    if piece_count <= 4:
        try:
            from domain.egtb_local import probe_local
            local_dtm = probe_local(board, piece_count, current_player)
            if local_dtm is not None:
                return local_dtm
        except Exception:
            pass

    # 本地基础判定（启发式规则）
    local = _local_egtb(board, current_player)
    if local is not None:
        return local

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

    # 多子：有車则必胜；但車馬/車炮（无第二个車/无兵）对士象全为官和。
    # 注意：車馬炮/車雙馬/車雙炮 vs 士象全 为必胜（多一个攻子可破士象全防守），
    # 只有单一辅助子（馬或炮）时才是官和 —— 条件必须限定 len(others) == 1。
    if has_rook:
        others = [p for p, _, _ in attackers if p != 'R']
        if (defender_advisors >= 2 and defender_bishops >= 2
                and len(others) == 1 and others[0] in ('N', 'C')):
            return (0.0, 0)  # 車馬/車炮 vs 士象全 → 官和
        # 車+兵 vs 士象全：兵未过河或沉底（老兵）时无法参与进攻，
        # 近似单車 vs 士象全 → 官和；过河且未沉底的兵可助車破防 → 必胜。
        # 过河/沉底判断与单兵分支（:321-323）口径一致。
        if (defender_advisors >= 2 and defender_bishops >= 2
                and others and all(p == 'P' for p in others)):
            pawn_inert = all(
                not ((r <= 4) if owner == 1 else (r >= 5))
                or ((r == 0) if owner == 1 else (r == 9))
                for p, r, c in attackers if p == 'P')
            if pawn_inert:
                return (0.0, 0)
        score = 85000 if owner == current_player else -85000
        return (score, 15)

    # 双炮 vs 孤将：互为炮架，必胜
    if (count == 2 and has_cannon
            and all(p == 'C' for p, _, _ in attackers)
            and defender_total == 0):
        score = 80000 if owner == current_player else -80000
        return (score, 20)

    # 其他含炮组合（馬炮/炮兵/双炮）：对士象全为官和（残局常识：
    # 炮+单辅助子破不了 2士2象 完整防守），防守残缺时才给必胜分。
    # 审查修正：旧实现无条件判 50000 必胜，把官和局面（如馬炮/炮兵
    # vs 士象全）误判为胜，搜索会据此主动兑子进入必和残局。
    if has_cannon and count >= 2:
        if defender_advisors >= 2 and defender_bishops >= 2:
            return (0.0, 0)
        score = 50000 if owner == current_player else -50000
        return (score, 25)

    # 其他组合（双馬/馬兵/双兵等）：对士象全为官和（审查修正，理由同上）；
    # 全兵且都未过河（或沉底）时无法成杀 → 和（与上面車+兵分支的
    # pawn_inert 口径一致）
    if count >= 2:
        if defender_advisors >= 2 and defender_bishops >= 2:
            return (0.0, 0)
        if all(p == 'P' for p, _, _ in attackers):
            all_inert = all(
                not ((r <= 4) if owner == 1 else (r >= 5))
                or ((r == 0) if owner == 1 else (r == 9))
                for p, r, c in attackers)
            if all_inert:
                return (0.0, 0)
        score = 30000 if owner == current_player else -30000
        return (score, 30)

    return None
