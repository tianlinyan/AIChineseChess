"""EGTB 本地残局表测试 — RA 表正确性验证

用法：python tests/test_egtb.py

覆盖：
1. 黄金局面链（黑将(0,3) 红車(0,4) 红帅(9,4)：黑先 DTM=4，机械验证两条防御线均困毙）
2. 颜色归一化（黑攻方局面旋转映射，含黑攻子对 → KkRP、双方攻子 → KkRp）
3. 精确和棋返回 (0.0, 255)；不可能局面（对方已被将军、走子方行棋）返回 None
4. 独立参考求解器（带路径环检测的递归 minimax）类别级比对：KkR 全表 + 4 子表抽样
   （环/深度/预算截断的结论跳过，不作比）
5. 前向一致性判别器：KkR 全表 + 全部 4 子表抽样（含吃子子表解析）
6. 左右镜像 / 180° 旋转自检 + probe_local 规范化层旋转同值
7. 陈旧文件检测（v2/v3 魔数/错签名/截断文件/CRC 内容损坏被拒绝）
8. 吃子子表解析抽样（KkRp 黑卒吃車 → KkP；KkRR 黑王吃車 → KkR 输线）
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.egtb_local import (DtmTable, DTM_MAGIC, DTM_DRAW, DTM_ILLEGAL,
                               TABLE_SETS, _file_matches, _rotate_board,
                               _kings_facing, _sig_filename, _dtm_filepath,
                               probe_local)
from domain.game import ChineseChessGame

FAILURES = []


def check(cond, msg):
    if cond:
        print(f'  PASS: {msg}')
    else:
        print(f'  FAIL: {msg}')
        FAILURES.append(msg)


def make_board(pieces):
    """pieces: [(piece_char, r, c), ...] → 棋盘。"""
    board = [['.'] * 9 for _ in range(10)]
    for p, r, c in pieces:
        board[r][c] = p
    return board


def load_tables(sigs=None):
    """按依赖序装载所需表到内存（优先文件，缺文件则生成）。"""
    tables = {}
    for pieces in TABLE_SETS:
        t = DtmTable(pieces)
        if sigs is not None and t.sig not in sigs:
            continue
        filepath = _dtm_filepath(t.sig)
        if not t.load(filepath):
            t.generate(tables)
        t.sub_tables = tables
        tables[t.sig] = t
    return tables


# ═══ 1. 黄金局面链（审查员独立计算：黑先 DTM=4，两条防御线均困毙）═══
def test_golden_position():
    print('\n[1] 黄金局面链')
    t = load_tables({'KkR'})['KkR']

    # P：黑将(0,3) 红車(0,4) 红帅(9,4)，黑先被将军，唯一逃逸 (1,3) → 真值 DTM=4
    board = make_board([('K', 9, 4), ('k', 0, 3), ('R', 0, 4)])
    res = t.probe(board, 2)
    check(res == (4, 2), f'P 黑先 = (4, 2)（实得 {res}）')
    score, dtm = probe_local([row[:] for row in board], 3, 2)
    check(dtm == 4 and score < 0, f'P probe_local 黑先 dtm=4 负分（实得 {score}, {dtm}）')

    # 中间态1：黑(1,3) 車(0,4) 帅(9,4)，红走 → 红 DTM=3
    board1 = make_board([('K', 9, 4), ('k', 1, 3), ('R', 0, 4)])
    check(t.probe(board1, 1) == (3, 2), f'中间态1 红走 = (3, 2)（实得 {t.probe(board1, 1)}）')

    # 中间态2：黑(2,3) 車(1,4) 帅(9,4)，红走 → 红 DTM=1
    board2 = make_board([('K', 9, 4), ('k', 2, 3), ('R', 1, 4)])
    check(t.probe(board2, 1) == (1, 2), f'中间态2 红走 = (1, 2)（实得 {t.probe(board2, 1)}）')

    # 终态：黑(2,3) 車(1,4) 帅(8,4)，黑走 → 困毙 L_0
    board3 = make_board([('K', 8, 4), ('k', 2, 3), ('R', 1, 4)])
    check(t.probe(board3, 2) == (0, 2), f'终态困毙 黑走 = (0, 2)（实得 {t.probe(board3, 2)}）')


# ═══ 2. 颜色归一化（黑攻方 → 旋转映射到红攻方规范帧）═══
def test_color_normalization():
    print('\n[2] 颜色归一化')
    load_tables({'KkR', 'KkRP', 'KkRp'})
    # 黑車 vs 红帅（黑未将军红，合法局面）：旧版查不到表（None），新版旋转后精确命中
    board_a = make_board([('K', 9, 3), ('k', 0, 4), ('r', 5, 5)])   # 黑攻方
    board_b = make_board([('K', 9, 4), ('k', 0, 5), ('R', 4, 3)])   # 旋转后的规范帧
    res_a = probe_local([row[:] for row in board_a], 3, 2)
    res_b = probe_local([row[:] for row in board_b], 3, 1)
    check(res_a is not None, f'黑攻方命中表（旧版返回 None）：{res_a}')
    check(res_b is not None, f'规范帧命中表：{res_b}')
    check(res_a == res_b, f'旋转对称同值：黑先 {res_a} == 红先 {res_b}')

    # 黑攻子对（車+卒）→ 旋转命中 KkRP（修复后红攻子对表已恢复）
    board_c = make_board([('K', 9, 4), ('k', 0, 3), ('r', 5, 0), ('p', 5, 4)])
    res_c = probe_local([row[:] for row in board_c], 4, 2)
    check(res_c is not None, f'黑攻子对命中 KkRP 表：{res_c}')

    # 双方都有攻子（红兵+黑車）：未旋转签名无表 → 旋转命中 KkRp（审查#5 修复验证）
    board_d = make_board([('K', 9, 4), ('k', 0, 3), ('P', 4, 4), ('r', 1, 3)])
    res_d = probe_local([row[:] for row in board_d], 4, 2)
    res_dr = probe_local(_rotate_board(board_d), 4, 1)
    check(res_d is not None, f'双方攻子局面旋转命中 KkRp：{res_d}')
    check(res_d == res_dr, f'双方攻子旋转同值：{res_d} == {res_dr}')


# ═══ 3. 精确和棋返回 (0.0, 255)；不可能局面返回 None ═══
def test_exact_draw():
    print('\n[3] 精确和棋 / 不可能局面')
    load_tables({'KkR', 'KkC'})
    # KkR：黑将可吃車 → 和棋（帅(7,3) 将(0,4) 車(0,5)，黑先）
    board = make_board([('K', 7, 3), ('k', 0, 4), ('R', 0, 5)])
    res = probe_local([row[:] for row in board], 3, 2)
    check(res == (0.0, DTM_DRAW), f'KkR 吃車逃逸 = (0.0, {DTM_DRAW})（实得 {res}）')
    # 同局面红走：黑将已处于将军 = 不可能局面 → None，不再捏造 W_1
    res_red = probe_local([row[:] for row in board], 3, 1)
    check(res_red is None, f'不可能局面（黑将被将军、红走）返回 None（实得 {res_red}）')
    # 合法红先胜局（黄金中间态1）：红先可胜
    board_win = make_board([('K', 9, 4), ('k', 1, 3), ('R', 0, 4)])
    res_win = probe_local([row[:] for row in board_win], 3, 1)
    check(res_win is not None and res_win[0] > 0 and res_win[1] >= 1,
          f'KkR 红先可胜（合法局面，实得 {res_win}）')
    # KkC：单炮对孤将理论全和
    board_c = make_board([('K', 9, 4), ('k', 0, 3), ('C', 5, 5)])
    res_c = probe_local([row[:] for row in board_c], 3, 2)
    check(res_c == (0.0, DTM_DRAW), f'KkC 全和 = (0.0, {DTM_DRAW})（实得 {res_c}）')


# ═══ 4. 独立参考求解器（带路径环检测，类别级比对）═══
_SOLVE_CACHE = {}
_SOLVE_NODES = 0
_SOLVE_BUDGET = 5000        # 单次 solve 节点预算：超出按截断和棋返回（比对时跳过）


def sub_resolve_after(table, board, mover, fr, fc, tr, tc):
    """应用吃子走法 → 子表解析 → 恢复。返回 (outcome, plies)。"""
    piece = board[fr][fc]
    tg = board[tr][tc]
    board[tr][tc] = piece
    board[fr][fc] = '.'
    res = table._resolve_substate(board, mover)
    board[fr][fc] = piece
    board[tr][tc] = tg
    return res


def solve(board, mover, path, cap, table=None, budget=None):
    """独立参考求解器：递归 minimax + 路径环检测 + 节点预算 + 深度截断。

    返回 (v, d, tainted, cap_limited)：
      v/d 为类别与距离（'win'/'lose'/'draw'）；
      tainted=True 表示值依赖祖先集（子树内有环截断），不入缓存；
      cap_limited=True 表示子树被深度/预算截断，其结论不可用于比对。
    缓存条目为 (v, d, cap, tainted, cap_limited)：标志随缓存一起复用，
    防止截断结论以"干净"姿态参与比对。吃子走法经 table._resolve_substate
    解析（与生成器同一语义，无环/截断问题）。
    budget: 节点预算（默认 _SOLVE_BUDGET）。全表比对（KkR）需大预算
    一次暖全图，否则冷启动根节点永远截断、缓存条目永久带截断标志。
    """
    global _SOLVE_NODES
    if budget is None:
        budget = _SOLVE_BUDGET
    key = (tuple(''.join(row) for row in board), mover)
    if key in path:
        return ('draw', 0, True, False)      # 环截断：依赖祖先集
    if cap <= 0:
        return ('draw', 0, False, True)      # 深度截断：不可比对
    if key in _SOLVE_CACHE and _SOLVE_CACHE[key][2] >= cap:
        e = _SOLVE_CACHE[key]
        return e[0], e[1], e[3], e[4]
    _SOLVE_NODES += 1
    if _SOLVE_NODES > budget:
        return ('draw', 0, False, True)      # 预算截断：不可比对
    g = _G
    g.board = board
    g._king_pos = {1: None, 2: None}
    for r in range(10):
        for c in range(9):
            p = board[r][c]
            if p == 'K':
                g._king_pos[1] = (r, c)
            elif p == 'k':
                g._king_pos[2] = (r, c)
    moves = g.get_all_legal_moves(mover)
    if not moves:
        _SOLVE_CACHE[key] = ('lose', 0, cap, False, False)
        return ('lose', 0, False, False)
    best_lose = 0
    has_draw = False
    tainted = False
    cap_limited = False
    for fr, fc, tr, tc in moves:
        tg = board[tr][tc]
        if tg.upper() == 'K':
            _SOLVE_CACHE[key] = ('win', 1, cap, False, False)
            return ('win', 1, False, False)
        if tg != '.':
            # 吃子：子表解析。sub_resolve_after 自行应用/恢复走法，
            # 此处绝不能预先动子——否则解析的是残缺棋盘（吃子方
            # 棋子被删），所有吃子线都会被误判成和棋
            if table is None:
                # 无子表可解析：保守和棋（KkR 无吃子线，不可达）
                has_draw = True
            else:
                outcome, plies = sub_resolve_after(
                    table, board, mover, fr, fc, tr, tc)
                if outcome == 'win':
                    # 吃子胜线：与祖先集/深度无关，精确且可缓存
                    _SOLVE_CACHE[key] = ('win', plies, cap, False, False)
                    return ('win', plies, False, False)
                if outcome == 'lose':
                    if plies > best_lose:
                        best_lose = plies
                else:
                    has_draw = True
            continue
        piece = board[fr][fc]
        board[tr][tc] = piece
        board[fr][fc] = '.'
        res = solve(board, 3 - mover, path | {key}, cap - 1, table, budget)
        board[fr][fc] = piece
        board[tr][tc] = tg
        v, d, t, cl = res
        if t:
            tainted = True
        if cl:
            cap_limited = True
        if v == 'lose':
            # 赢结论只依赖这条已穷尽的输线：输子无和棋子（环/截断
            # 只会产生和棋），故赢结论恒干净，与其它兄弟分支无关
            _SOLVE_CACHE[key] = ('win', d + 1, cap, False, False)
            return ('win', d + 1, False, False)
        if v == 'draw':
            has_draw = True
        else:
            if d + 1 > best_lose:
                best_lose = d + 1
    if has_draw:
        result = ('draw', 0)
        if not tainted:
            _SOLVE_CACHE[key] = (result[0], result[1], cap, tainted, cap_limited)
        return result[0], result[1], tainted, cap_limited
    # 输结论 = 全部子走法皆赢；赢结论恒干净 → 输结论恒干净
    _SOLVE_CACHE[key] = ('lose', best_lose, cap, False, False)
    return ('lose', best_lose, False, False)


_G = ChineseChessGame()


def solver_class(res):
    return res[0] if res[0] == 'draw' else ('win' if res[0] == 'win' else 'lose')


def table_class(dtm, loser, mover):
    if dtm == DTM_DRAW:
        return 'draw'
    return 'win' if loser != mover else 'lose'


def compare_solve(t, board, mover, stored, cap, budget=None):
    """参考求解器比对单状态。返回 None=不可比（深度/预算截断），
    或 (一致, 求解结果)。

    环截断（tainted）结果照常比对：赢/输结论要么已找到赢线、要么已
    穷尽全部走法，环截断只可能把和棋当和棋（与表的不动点和棋语义一致）。
    """
    global _SOLVE_NODES
    _SOLVE_NODES = 0
    res = solve(board, mover, frozenset(), cap, t, budget)
    v, d, tainted, cap_limited = res
    if cap_limited:
        return None                      # 深度/预算截断：不可比
    return (table_class(stored[0], stored[1], mover) == solver_class((v, d)),
            (v, d))


def test_reference_solver():
    print('\n[4] 独立参考求解器（KkR 全表类别比对）')
    t = load_tables({'KkR'})['KkR']
    _SOLVE_CACHE.clear()
    viol = compared = skipped = 0
    t0 = time.time()
    for pos_rank in range(t.num_positions):
        rk, bk, extra_sqs = t._unrank_position(pos_rank)
        board = make_board([('K', rk[0], rk[1]), ('k', bk[0], bk[1]),
                            ('R', extra_sqs[0][0], extra_sqs[0][1])])
        for side in (0, 1):
            sid = pos_rank * 2 + side
            if t.dtm[sid] == DTM_ILLEGAL:
                continue          # 照面/不可能局面
            mover = side + 1
            stored = (t.dtm[sid], t.loser[sid])
            # 大预算：KkR 状态图 ~1.3 万节点 × 分支，一次暖全图，
            # 否则冷启动根节点永远截断、缓存条目永久带截断标志
            r = compare_solve(t, board, mover, stored, 16, 200000)
            if r is None:
                skipped += 1
                continue
            compared += 1
            ok, sres = r
            if not ok:
                print(f'    不一致: K{rk} k{bk} R{extra_sqs[0]} '
                      f'{"红" if mover == 1 else "黑"}走: 表={stored} 求解={sres}')
                viol += 1
                if viol >= 5:
                    break
        if viol >= 5:
            break
    check(viol == 0,
          f'KkR 全表求解器类别比对一致（比对 {compared}，跳过 {skipped}，'
          f'违规 {viol}，{time.time() - t0:.0f}s）')


def test_reference_solver_sampled():
    """4 子表抽样参考比对（审查#6：新表独立交叉验证）。

    深度/预算截断结论跳过（环截断结论仍可靠，照常比对）；表值 =
    和棋的状态也跳过（和棋求解需穷尽全树，交由 [5] 前向一致性
    判别器全表覆盖）。
    """
    print('\n[4b] 参考求解器抽样比对（4 子表）')
    tables = load_tables()
    bad = compared = skipped = 0
    t0 = time.time()
    for pieces in TABLE_SETS:
        t = tables[DtmTable(pieces).sig]
        if t.k < 2:
            continue
        _SOLVE_CACHE.clear()
        n = t.num_positions
        samples = [i for i in range(0, n, max(1, n // 20))]
        for pos_rank in samples:
            rk, bk, extra_sqs = t._unrank_position(pos_rank)
            board = make_board([('K', rk[0], rk[1]), ('k', bk[0], bk[1])] +
                               [(p, r, c) for p, (r, c) in zip(t.extras, extra_sqs)])
            for side in (0, 1):
                sid = pos_rank * 2 + side
                if t.dtm[sid] == DTM_ILLEGAL:
                    continue          # 照面/不可能局面
                if t.dtm[sid] == DTM_DRAW:
                    skipped += 1      # 和棋求解昂贵，由前向判别器覆盖
                    continue
                mover = side + 1
                stored = (t.dtm[sid], t.loser[sid])
                r = compare_solve(t, board, mover, stored, 10)
                if r is None:
                    skipped += 1
                    continue
                compared += 1
                ok, sres = r
                if not ok:
                    print(f'    不一致: {t.sig} K{rk} k{bk} '
                          f'{list(zip(t.extras, extra_sqs))} {"红" if mover == 1 else "黑"}走: '
                          f'表={stored} 求解={sres}')
                    bad += 1
                    if bad >= 5:
                        break
            if bad >= 5:
                break
        if bad >= 5:
            break
    check(bad == 0,
          f'参考求解器抽样比对通过（比对 {compared}，跳过 {skipped}，'
          f'违规 {bad}，{time.time() - t0:.0f}s）')


# ═══ 5. 前向一致性判别器（minimax 语义一步前瞻，含吃子子表解析）═══
def forward_check_state(table, pos_rank, side):
    """返回违规描述或 None。"""
    g = _G
    rk, bk, extra_sqs = table._unrank_position(pos_rank)
    board = make_board([('K', rk[0], rk[1]), ('k', bk[0], bk[1])] +
                       [(p, r, c) for p, (r, c) in zip(table.extras, extra_sqs)])
    g.board = board
    g._king_pos = {1: rk, 2: bk}
    sid = pos_rank * 2 + side
    d = table.dtm[sid]
    l = table.loser[sid]
    m = side + 1
    moves = g.get_all_legal_moves(m)
    ctx = f'{table.sig} K{rk} k{bk} {list(zip(table.extras, extra_sqs))} {"红" if m == 1 else "黑"}走 存值=({d},{l})'

    if d == DTM_DRAW:
        # D：无 L 后继（否则应 W）；且存在 D 后继或吃子逃逸（否则应 L）
        has_escape = False
        for fr, fc, tr, tc in moves:
            tg = board[tr][tc]
            if tg.upper() == 'K':
                return f'{ctx} D 却可吃王'
            if tg != '.':
                outcome, _ = sub_resolve_after(table, board, m, fr, fc, tr, tc)
                if outcome == 'draw':
                    has_escape = True
                elif outcome == 'win':
                    return f'{ctx} D 却有吃子胜线（应 W）'
                continue
            piece = board[fr][fc]
            board[tr][tc] = piece
            board[fr][fc] = '.'
            res = table.probe(board, 3 - m)
            board[fr][fc] = piece
            board[tr][tc] = tg
            if res is None:
                return f'{ctx} D 后继 probe None'
            dt, lt = res
            if dt == DTM_DRAW:
                has_escape = True
            elif lt == 3 - m:
                return f'{ctx} D 却有 L 后继 ({dt},{lt})（应 W）'
        if not has_escape:
            return f'{ctx} D 但无任何逃逸（应 L）'
        return None
    if d == 0:
        if moves:
            return f'{ctx} L_0 却有走法'
        return None
    if l == m:
        # L(d)：每步走后继都是对手胜且总 ply ≤ d
        for fr, fc, tr, tc in moves:
            tg = board[tr][tc]
            if tg == '.':
                piece = board[fr][fc]
                board[tr][tc] = piece
                board[fr][fc] = '.'
                res = table.probe(board, 3 - m)
                board[fr][fc] = piece
                board[tr][tc] = tg
                if res is None:
                    return f'{ctx} L 后继 probe None'
                dt, lt = res
                if lt != m:
                    return f'{ctx} L 有非对手胜后继 ({dt},{lt})'
                if dt + 1 > d:
                    return f'{ctx} L({d}) 有后继 W({dt}) 超出 {d - 1}'
            elif tg.upper() == 'K':
                return f'{ctx} L 却可吃王（应 W_1）'
            else:
                outcome, plies = sub_resolve_after(table, board, m, fr, fc, tr, tc)
                if outcome != 'lose':
                    return f'{ctx} L 吃子后继非对手胜（{outcome}）'
                if plies > d:
                    return f'{ctx} L({d}) 吃子输线 {plies} 超出'
        return None
    # W(d)：存在后继 L(d-1)（同 loser）或吃王 d==1 或吃子子表胜（总 ply==d）
    for fr, fc, tr, tc in moves:
        tg = board[tr][tc]
        if tg.upper() == 'K':
            if d == 1:
                return None
            continue
        if tg != '.':
            outcome, plies = sub_resolve_after(table, board, m, fr, fc, tr, tc)
            if outcome == 'win' and plies == d:
                return None
            continue
        piece = board[fr][fc]
        board[tr][tc] = piece
        board[fr][fc] = '.'
        res = table.probe(board, 3 - m)
        board[fr][fc] = piece
        board[tr][tc] = tg
        if res is not None and res == (d - 1, l):
            return None
    return f'{ctx} W({d}) 无 L({d - 1})/吃王/吃子胜线'


def test_forward_checker():
    print('\n[5] 前向一致性判别器（KkR 全表 + 4 子表抽样）')
    tables = load_tables()
    viol = 0
    # KkR 全表
    for pos_rank in range(tables['KkR'].num_positions):
        for side in (0, 1):
            if tables['KkR'].dtm[pos_rank * 2 + side] == DTM_ILLEGAL:
                continue          # 照面/不可能局面
            err = forward_check_state(tables['KkR'], pos_rank, side)
            if err:
                print('    违规:', err)
                viol += 1
    # 4 子表抽样（每表均匀取 40 个局面）
    for pieces in TABLE_SETS:
        t = tables[DtmTable(pieces).sig]
        if t.k < 2:
            continue
        n = t.num_positions
        samples = [i for i in range(0, n, max(1, n // 40))]
        for pos_rank in samples:
            for side in (0, 1):
                if t.dtm[pos_rank * 2 + side] == DTM_ILLEGAL:
                    continue
                err = forward_check_state(t, pos_rank, side)
                if err:
                    print('    违规:', err)
                    viol += 1
                    if viol > 10:
                        break
            if viol > 10:
                break
        if viol > 10:
            break
    check(viol == 0, f'前向一致性判别器通过（违规 {viol}）')


# ═══ 6. 镜像 / 旋转自检 ═══
def mirror_board(board):
    return [row[::-1] for row in board]


def test_symmetry():
    """镜像 / 180° 旋转对称。

    表级只能验证左右镜像（180° 旋转会把红帅移出九宫行 7-9，超出
    稠密索引的规范帧定义）；180° 旋转对称由 probe_local 规范化层
    验证：换色旋转 + 走子方翻转后必须同值。
    """
    print('\n[6] 镜像 / 180° 旋转自检')
    tables = load_tables()
    bad = 0
    for t in tables.values():
        n = t.num_positions
        samples = [i for i in range(0, n, max(1, n // 30))]
        for pos_rank in samples:
            rk, bk, extra_sqs = t._unrank_position(pos_rank)
            board = make_board([('K', rk[0], rk[1]), ('k', bk[0], bk[1])] +
                               [(p, r, c) for p, (r, c) in zip(t.extras, extra_sqs)])
            if _kings_facing(board, rk, bk):
                continue
            for side in (0, 1):
                v = t.probe(board, side + 1)
                if v is None:
                    continue          # 不可能局面（对方已被将军）
                mv = t.probe(mirror_board(board), side + 1)
                if mv != v:
                    print(f'    镜像不一致: {t.sig} {v} vs {mv}')
                    bad += 1
                # 规范化层：换色 180° 旋转 + 走子方翻转后 probe_local 同值
                pv = probe_local([row[:] for row in board], t.k + 2, side + 1)
                rpv = probe_local(_rotate_board(board), t.k + 2, 2 - side)
                if pv != rpv:
                    print(f'    规范化旋转不一致: {t.sig} {pv} vs {rpv}')
                    bad += 1
                if bad > 5:
                    break
            if bad > 5:
                break
        if bad > 5:
            break
    check(bad == 0, f'镜像/旋转对称通过（不一致 {bad}）')


# ═══ 7. 陈旧文件检测 ═══
def test_stale_file_detection():
    print('\n[7] 陈旧文件检测')
    import tempfile
    t = DtmTable(('K', 'R', 'k'))
    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, 'good.dtm')
        t.dtm = bytearray([255]) * t.num_states
        t.loser = bytearray(t.num_states)
        t.loaded = True
        t.save(good)
        check(_file_matches(good, t.sig, t.num_positions), 'v4 完整文件识别')
        check(DtmTable(('K', 'R', 'k')).load(good), 'v4 完整文件加载')

        bad_v2 = os.path.join(td, 'v2.dtm')
        with open(bad_v2, 'wb') as f:
            f.write(b'DTMC\x02' + b'\x00' * 100)
        check(not _file_matches(bad_v2, t.sig, t.num_positions), 'v2 魔数被拒绝')

        bad_v3 = os.path.join(td, 'v3.dtm')
        with open(bad_v3, 'wb') as f:
            f.write(b'DTMC\x03' + b'\x00' * 100)
        check(not _file_matches(bad_v3, t.sig, t.num_positions), 'v3 魔数被拒绝')

        bad_sig = os.path.join(td, 'bad_sig.dtm')
        with open(bad_sig, 'wb') as f:
            f.write(DTM_MAGIC + bytes([3]) + b'KkN' + b'\x00' * 50)
        check(not _file_matches(bad_sig, t.sig, t.num_positions), '签名不符被拒绝')

        bad_trunc = os.path.join(td, 'trunc.dtm')
        with open(bad_trunc, 'wb') as f:
            f.write(DTM_MAGIC + bytes([3]) + b'KkR' + b'\x00' * 10)
        check(not _file_matches(bad_trunc, t.sig, t.num_positions), '截断文件被拒绝')

        # 内容损坏（个别槽位被改写，结构仍完整）→ CRC 校验拒绝
        bad_crc = os.path.join(td, 'bad_crc.dtm')
        with open(good, 'rb') as f:
            raw = bytearray(f.read())
        raw[-1] ^= 0xFF
        with open(bad_crc, 'wb') as f:
            f.write(bytes(raw))
        check(not _file_matches(bad_crc, t.sig, t.num_positions), 'CRC 内容损坏被拒绝')
        check(not DtmTable(('K', 'R', 'k')).load(bad_crc), 'load 拒绝内容损坏')

        check(not DtmTable(('K', 'R', 'k')).load(bad_v2), 'load 拒绝 v2')

    # 大小写不敏感文件系统安全：全部表文件名（含 KkRA/KkRa 等大小写对）
    # 必须互不冲突，否则保存时互相覆盖
    names = [_sig_filename(DtmTable(p).sig) for p in TABLE_SETS]
    check(len(set(n.lower() for n in names)) == len(names),
          f'表文件名大小写不敏感唯一（{len(names)} 个）')
    check(_sig_filename('KkRA') == 'KkRA' and _sig_filename('KkRa') == 'KkR_a',
          '_sig_filename 大小写对映射正确')


# ═══ 8. 吃子子表解析抽样（KkRp 黑卒吃車 → KkP；KkRR 黑王吃車 → KkR）═══
def test_capture_resolution():
    print('\n[8] 吃子子表解析抽样')
    tables = load_tables({'KkR', 'KkP', 'KkRp', 'KkRR'})
    t = tables['KkRp']
    # ── 胜线：局面 帅(9,4) 将(0,3) 車(5,4) 卒(4,4)，黑走，卒可吃車 ──
    board = make_board([('K', 9, 4), ('k', 0, 3), ('R', 5, 4), ('p', 4, 4)])
    g = _G
    g.board = board
    g._king_pos = {1: (9, 4), 2: (0, 3)}
    # 黑卒吃車后的子表（KkP 旋转）值
    piece = board[4][4]
    cap = board[5][4]
    board[5][4] = piece
    board[4][4] = '.'
    sub_outcome, sub_plies = t._resolve_substate(board, 2)
    board[4][4] = piece
    board[5][4] = cap
    print(f'    黑卒吃車 → 子表解析: {sub_outcome}, {sub_plies}')
    # 主表该状态的值必须与子解析自洽（类别一致；胜线 dtm == sub_plies 时不劣化）
    rank_info = t.probe(board, 2)
    print(f'    主表 黑走 值: {rank_info}')
    check(rank_info is not None, '主表命中')
    if rank_info is not None:
        d, l = rank_info
        mover = 2
        cls = table_class(d, l, mover)
        if sub_outcome == 'win':
            check(cls == 'win' and d <= sub_plies, f'黑卒吃車取胜线：W 且 dtm≤{sub_plies}')
        elif sub_outcome == 'draw':
            check(cls in ('win', 'draw'), '黑卒吃車和棋线：非 L')
        else:
            # 输线不强制 L（可能有其他逃逸），仅要求 L 时 dtm 不劣化
            if cls == 'lose':
                check(d >= sub_plies, f'黑卒吃車输线：L 且 dtm≥{sub_plies}')

    # ── 输线（审查#12 修复验证）：黑王吃車后恰为黄金局面中间态1 →
    #    KkR 红走 (3,2)，黑输线总 ply = 4 ──
    t2 = tables['KkRR']
    board2 = make_board([('K', 9, 4), ('k', 0, 3), ('R', 1, 3), ('R', 0, 4)])
    piece2 = board2[0][3]
    cap2 = board2[1][3]
    board2[1][3] = piece2
    board2[0][3] = '.'
    sub_outcome2, sub_plies2 = t2._resolve_substate(board2, 2)
    board2[0][3] = piece2
    board2[1][3] = cap2
    print(f'    黑王吃車 → 子表解析: {sub_outcome2}, {sub_plies2}')
    check(sub_outcome2 == 'lose' and sub_plies2 == 4,
          f'黑王吃車输线 = lose/4（实得 {sub_outcome2}, {sub_plies2}）')
    rank_info2 = t2.probe(board2, 2)
    print(f'    主表 黑走 值: {rank_info2}')
    if rank_info2 is not None and table_class(rank_info2[0], rank_info2[1], 2) == 'lose':
        check(rank_info2[0] >= sub_plies2,
              f'主表 L 值不劣于吃子输线：dtm {rank_info2[0]} >= {sub_plies2}')


def main():
    t0 = time.time()
    test_golden_position()
    test_color_normalization()
    test_exact_draw()
    test_reference_solver()
    test_reference_solver_sampled()
    test_forward_checker()
    test_symmetry()
    test_stale_file_detection()
    test_capture_resolution()
    print(f'\n总计 {len(FAILURES)} 个失败（{time.time() - t0:.0f}s）')
    if FAILURES:
        sys.exit(1)


if __name__ == '__main__':
    main()
