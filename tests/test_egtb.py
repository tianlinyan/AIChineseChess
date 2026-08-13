"""EGTB 本地残局表测试 — RA 表正确性验证

用法：python tests/test_egtb.py

覆盖：
1. 黄金局面链（黑将(0,3) 红車(0,4) 红帅(9,4)：黑先 DTM=4，机械验证两条防御线均困毙）
2. 颜色归一化（黑攻方局面旋转映射，旧版查不到表）
3. 精确和棋返回 (0.0, 0)（防守方可吃車/炮等子表全和局面）
4. 独立参考求解器（带路径环检测的递归 minimax）类别级比对：KkR 全表
5. 前向一致性判别器：KkR 全表 + 全部 4 子表抽样（含吃子子表解析）
6. 左右镜像 / 180° 旋转自检
7. 陈旧文件检测（v2 魔数/错签名/截断文件被拒绝）
8. 吃子子表解析抽样（KkRp 黑卒吃車 → KkP）
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.egtb_local import (DtmTable, DTM_DIR, DTM_MAGIC, DTM_DRAW,
                               DTM_ILLEGAL, TABLE_SETS, _file_matches,
                               _rotate_board, _kings_facing,
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
        filepath = os.path.join(DTM_DIR, f'{t.sig}.dtm')
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
    load_tables({'KkR'})
    # 黑車 vs 红帅：旧版查不到表（None），新版旋转后精确命中
    board_a = make_board([('K', 9, 3), ('k', 0, 4), ('r', 1, 3)])   # 黑攻方
    board_b = make_board([('K', 9, 4), ('k', 0, 5), ('R', 8, 5)])   # 旋转后的规范帧
    res_a = probe_local([row[:] for row in board_a], 3, 2)
    res_b = probe_local([row[:] for row in board_b], 3, 1)
    check(res_a is not None, f'黑攻方命中表（旧版返回 None）：{res_a}')
    check(res_b is not None, f'规范帧命中表：{res_b}')
    check(res_a == res_b, f'旋转对称同值：黑先 {res_a} == 红先 {res_b}')


# ═══ 3. 精确和棋返回 (0.0, 0)（修复"None→启发式误判必胜"）═══
def test_exact_draw():
    print('\n[3] 精确和棋')
    load_tables({'KkR', 'KkC'})
    # KkR：黑将可吃車 → 和棋（帅(7,3) 将(0,4) 車(0,5)，黑先）
    board = make_board([('K', 7, 3), ('k', 0, 4), ('R', 0, 5)])
    res = probe_local([row[:] for row in board], 3, 2)
    check(res == (0.0, 0), f'KkR 吃車逃逸 = (0.0, 0)（实得 {res}）')
    # 红先同局面应可胜
    res_red = probe_local([row[:] for row in board], 3, 1)
    check(res_red is not None and res_red[0] > 0 and res_red[1] >= 1,
          f'KkR 红先可胜（实得 {res_red}）')
    # KkC：单炮对孤将理论全和
    board_c = make_board([('K', 9, 4), ('k', 0, 3), ('C', 5, 5)])
    res_c = probe_local([row[:] for row in board_c], 3, 2)
    check(res_c == (0.0, 0), f'KkC 全和 = (0.0, 0)（实得 {res_c}）')


# ═══ 4. 独立参考求解器（带路径环检测，类别级比对）═══
_SOLVE_CACHE = {}


def solve(board, mover, path, cap):
    key = (tuple(''.join(row) for row in board), mover)
    if key in path:
        return ('draw', 0)
    if cap <= 0:
        return ('draw', 0)
    if key in _SOLVE_CACHE and _SOLVE_CACHE[key][2] >= cap:
        return _SOLVE_CACHE[key][:2]
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
        _SOLVE_CACHE[key] = ('lose', 0, cap)
        return ('lose', 0)
    best_lose = 0
    has_draw = False
    for fr, fc, tr, tc in moves:
        tg = board[tr][tc]
        if tg.upper() == 'K':
            _SOLVE_CACHE[key] = ('win', 1, cap)
            return ('win', 1)
        piece = board[fr][fc]
        board[tr][tc] = piece
        board[fr][fc] = '.'
        if tg != '.':
            res = ('draw', 0)   # 吃非王子 → 只剩两王 → 和棋
        else:
            res = solve(board, 3 - mover, path | {key}, cap - 1)
        board[fr][fc] = piece
        board[tr][tc] = tg
        v, d = res
        if v == 'lose':
            _SOLVE_CACHE[key] = ('win', d + 1, cap)
            return ('win', d + 1)
        if v == 'draw':
            has_draw = True
        else:
            if d + 1 > best_lose:
                best_lose = d + 1
    if has_draw:
        _SOLVE_CACHE[key] = ('draw', 0, cap)
        return ('draw', 0)
    _SOLVE_CACHE[key] = ('lose', best_lose, cap)
    return ('lose', best_lose)


_G = ChineseChessGame()


def solver_class(res):
    return res[0] if res[0] == 'draw' else ('win' if res[0] == 'win' else 'lose')


def table_class(dtm, loser, mover):
    if dtm == DTM_DRAW:
        return 'draw'
    return 'win' if loser != mover else 'lose'


def test_reference_solver():
    print('\n[4] 独立参考求解器（KkR 全表类别比对）')
    t = load_tables({'KkR'})['KkR']
    _SOLVE_CACHE.clear()
    viol = 0
    t0 = time.time()
    for pos_rank in range(t.num_positions):
        rk, bk, extra_sqs = t._unrank_position(pos_rank)
        board = make_board([('K', rk[0], rk[1]), ('k', bk[0], bk[1]),
                            ('R', extra_sqs[0][0], extra_sqs[0][1])])
        if t.dtm[pos_rank * 2] == DTM_ILLEGAL:
            continue
        for side in (0, 1):
            mover = side + 1
            stored = (t.dtm[pos_rank * 2 + side], t.loser[pos_rank * 2 + side])
            res = solve(board, mover, frozenset(), 16)
            if table_class(stored[0], stored[1], mover) != solver_class(res):
                print(f'    不一致: K{rk} k{bk} R{extra_sqs[0]} {"红" if mover == 1 else "黑"}走: '
                      f'表={stored} 求解={res}')
                viol += 1
                if viol >= 5:
                    break
        if viol >= 5:
            break
    check(viol == 0, f'KkR 全表求解器类别比对一致（违规 {viol}，{time.time() - t0:.0f}s）')


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


def test_forward_checker():
    print('\n[5] 前向一致性判别器（KkR 全表 + 4 子表抽样）')
    tables = load_tables()
    viol = 0
    # KkR 全表
    for pos_rank in range(tables['KkR'].num_positions):
        if tables['KkR'].dtm[pos_rank * 2] == DTM_ILLEGAL:
            continue
        for side in (0, 1):
            err = forward_check_state(tables['KkR'], pos_rank, side)
            if err:
                print('    违规:', err)
                viol += 1
    # 4 子表抽样（每表均匀取 40 个非照面局面）
    for pieces in TABLE_SETS:
        t = tables[DtmTable(pieces).sig]
        if t.k < 2:
            continue
        n = t.num_positions
        samples = [i for i in range(0, n, max(1, n // 40))]
        for pos_rank in samples:
            if t.dtm[pos_rank * 2] == DTM_ILLEGAL:
                continue
            for side in (0, 1):
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
    print('\n[6] 镜像 / 180° 旋转自检')
    tables = load_tables()
    bad = 0
    for pieces in TABLE_SETS:
        t = DtmTable(pieces)
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
                    continue
                mv = t.probe(mirror_board(board), side + 1)
                if mv != v:
                    print(f'    镜像不一致: {t.sig} {v} vs {mv}')
                    bad += 1
                # 180° 旋转 + 大小写互换 → 规范帧同值
                rot = _rotate_board(board)
                rv = t.probe(rot, 2 - side)
                if rv != v:
                    print(f'    旋转不一致: {t.sig} {v} vs {rv}')
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
        check(_file_matches(good, t.sig, t.num_positions), 'v3 完整文件识别')
        check(DtmTable(('K', 'R', 'k')).load(good), 'v3 完整文件加载')

        bad_v2 = os.path.join(td, 'v2.dtm')
        with open(bad_v2, 'wb') as f:
            f.write(b'DTMC\x02' + b'\x00' * 100)
        check(not _file_matches(bad_v2, t.sig, t.num_positions), 'v2 魔数被拒绝')

        bad_sig = os.path.join(td, 'bad_sig.dtm')
        with open(bad_sig, 'wb') as f:
            f.write(DTM_MAGIC + bytes([3]) + b'KkN' + b'\x00' * 50)
        check(not _file_matches(bad_sig, t.sig, t.num_positions), '签名不符被拒绝')

        bad_trunc = os.path.join(td, 'trunc.dtm')
        with open(bad_trunc, 'wb') as f:
            f.write(DTM_MAGIC + bytes([3]) + b'KkR' + b'\x00' * 10)
        check(not _file_matches(bad_trunc, t.sig, t.num_positions), '截断文件被拒绝')

        check(not DtmTable(('K', 'R', 'k')).load(bad_v2), 'load 拒绝 v2')


# ═══ 8. 吃子子表解析抽样（KkRp：黑卒吃車 → KkP；红車吃卒 → KkR）═══
def test_capture_resolution():
    print('\n[8] 吃子子表解析抽样')
    tables = load_tables({'KkR', 'KkP', 'KkRp'})
    t = tables['KkRp']
    # 局面：帅(9,4) 将(0,3) 車(5,4) 卒(4,4)，黑走，卒可吃車
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
            check(True, '黑卒吃車输线：无断言')


def main():
    t0 = time.time()
    test_golden_position()
    test_color_normalization()
    test_exact_draw()
    test_reference_solver()
    test_forward_checker()
    test_symmetry()
    test_stale_file_detection()
    test_capture_resolution()
    print(f'\n总计 {len(FAILURES)} 个失败（{time.time() - t0:.0f}s）')
    if FAILURES:
        sys.exit(1)


if __name__ == '__main__':
    main()
