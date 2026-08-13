"""本地 DTM（Distance to Mate）残局库 — 逆向回推分析（retrograde analysis）

状态 = (棋盘, 走子方)。棋盘由 81 个将帅九宫对 × 其余子的稠密组合索引唯一确定：
    pos_rank = kp_index × placements_per_pair + placement_rank
    state_id = pos_rank × 2 + side_bit   （side_bit: 0=规范帧红走，1=黑走）
rank/unrank 均为闭式组合数公式（O(90) 算术），无哈希、无碰撞。

算法（真逆向回推，攻防双方回合交替的 minimax 语义）：
1. Pass 1 逐状态枚举走法一次（复用单个 ChineseChessGame）：
   - 对方已处于将军（走法列表含吃王）→ 不可能局面：标记非法状态，
     不进种子、不建边（对局中不可能轮到走子方行棋；防止把吃王捏造成
     W_1 种子污染合法状态）
   - 无合法走法 → 将死/困毙，L_0（dtm=0, loser=走子方，与对局层语义一致）
   - 吃子 → 子表解析：和棋→draw_escape；子表方负→cap_win；子表方胜→max_sub_loss
     （cap_win/max_sub_loss 存储的是含吃子这一手的总 ply）
   - 非吃子 → 反向边，cnt++
2. Pass 2 按 ply 桶固定点：
   - W_n = L_{n-1} 的前驱（首触即最小 DTM）+ 吃子子表胜种子桶（按桶序处理保证最小性）
   - L_n = cnt 归零（同表后继全部为对手 W）且无 draw_escape 且无 cap_win 的状态，
     dtm = max(trigger_ply+1, max_sub_loss)
   - 固定点后未赋值 = 和棋（255）
3. 照面/不可能局面槽位 = 254（probe 返回 None）

存储格式（.dtm 文件 v4）：
- 头：5B 魔数 b'DTMC\\x04' + 1B 签名长度 + ASCII 签名（如 b'KkR'、b'KkRp'）
      + u32 LE num_positions + u32 LE body CRC32（内容完整性校验）
- 文件名 = _sig_filename(sig)：小写防守子前插 '_'（如 KkRa → KkR_a），
  避免 KkRA/KkRa 等大小写对在 Windows 大小写不敏感文件系统上互相覆盖
- 体：num_positions × 2 条 × (u8 dtm, u8 loser)，先 side_bit=0 后 side_bit=1
  - dtm 0..253 真实；254=照面/非法状态；255=和棋（loser 恒 0）
  - loser: 1=红负 2=黑负（规范帧）

规范帧：红方为攻方（红持 R/N/C/P，黑持 a/b/p 防守子）。查询时黑方为攻方的
棋盘旋转 180° + 大小写互换映射到规范帧（修复旧版黑方攻子局面查不到表的缺陷）。

Usage:
    from domain.egtb_local import DtmTable, probe_local
    result = probe_local(board, piece_count, current_player)  # (score, dtm) 或 None
    生成：python -m domain.egtb_local [--only KkR] [--force]
"""

import os
import struct
import time
import zlib
from array import array
from typing import Dict, List, Optional, Tuple

from domain.constants import BOARD_WIDTH, BOARD_HEIGHT

# ── 常量 ──
DTM_MAGIC = b'DTMC\x04'        # DTM Chinese Chess v4（稠密索引 + 走子方 + 和棋哨兵 + CRC32）
DTM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'egtb')
DTM_ILLEGAL = 254              # 照面/非法状态槽（probe 返回 None）
DTM_DRAW = 255                 # 和棋（loser 恒 0）
MAX_REAL_DTM = 250             # 生成后断言：真实 DTM 上限（防截断误标和棋）

# 攻击子力类型（不含将/帥和防守子力士/相）
ATTACKER_TYPES = 'RNCP'

# 规范帧类型序（R<C<N<P<a<b<p），决定签名排序与 rank 公式的 o=0 基准
_TYPE_ORDER = {'R': 0, 'C': 1, 'N': 2, 'P': 3, 'a': 4, 'b': 5, 'p': 6}

# 21 个 ≤4 子表（顺序 = 依赖顺序：子数升序，吃子解析依赖先行的表）
# 红方攻子在前（大写），黑方防守子在后（小写）
# 第 2 组（双攻子 vs 孤将）必须在单攻子 vs 单防守子之前：后者的红方
# 吃子线落到前者（如 KkRa 红車吃士 → KkRA），缺失会导致吃子胜线被
# 误判为和棋逃逸
TABLE_SETS = [
    # 3 子：单攻子 vs 孤将
    ('K', 'R', 'k'),            # KkR   单車对孤将
    ('K', 'N', 'k'),            # KkN   单馬对孤将
    ('K', 'C', 'k'),            # KkC   单炮对孤将（理论全和，RA 证实）
    ('K', 'P', 'k'),            # KkP   单兵对孤将（仅巧胜局面可胜）
    # 4 子：双攻子 vs 孤将（吃子解析依赖：防守子表的红吃子线落在这里）
    ('K', 'R', 'P', 'k'),       # KkRP  車兵对孤将
    ('K', 'R', 'A', 'k'),       # KkRA  車士对孤将
    ('K', 'R', 'B', 'k'),       # KkRB  車相对孤将
    ('K', 'N', 'A', 'k'),       # KkNA  馬士对孤将
    ('K', 'N', 'B', 'k'),       # KkNB  馬相对孤将
    ('K', 'N', 'P', 'k'),       # KkNP  馬兵对孤将
    ('K', 'C', 'P', 'k'),       # KkCP  炮兵对孤将
    # 4 子：单攻子 vs 单防守子
    ('K', 'R', 'k', 'a'),       # KkRa  車对单士
    ('K', 'R', 'k', 'b'),       # KkRb  車对单象
    ('K', 'R', 'k', 'p'),       # KkRp  車对单卒
    ('K', 'N', 'k', 'a'),       # KkNa  馬对单士
    ('K', 'N', 'k', 'b'),       # KkNb  馬对单象
    ('K', 'N', 'k', 'p'),       # KkNp  馬对单卒
    ('K', 'C', 'k', 'p'),       # KkCp  炮对单卒
    # 4 子：双車/車馬/車炮
    ('K', 'R', 'R', 'k'),       # KkRR  双車对孤将
    ('K', 'R', 'N', 'k'),       # KkRN  車馬对孤将
    ('K', 'R', 'C', 'k'),       # KkRC  車炮对孤将
]


# ── 几何工具 ──

def _flat(r: int, c: int) -> int:
    return r * BOARD_WIDTH + c


def _upos(flat_sq: int, rk_flat: int, bk_flat: int) -> int:
    """U 序号：88 格 = 90 格去掉两王格（行主序扫描）。"""
    u = flat_sq
    if flat_sq > rk_flat:
        u -= 1
    if flat_sq > bk_flat:
        u -= 1
    return u


def _usq_flat(u: int, rk_flat: int, bk_flat: int) -> int:
    """U 序号 → 90 格 flat（_upos 的逆，闭式 O(1)）。

    U 序号跳过两王格（flat 严格大于王格才减 1），故逆映射：
      u < lo → flat=u；lo ≤ u ≤ hi-2 → flat=u+1；u ≥ hi-1 → flat=u+2
    （lo/hi 为王格 flat 序；hi=lo+1 时中间段为空，u=lo 直接 +2）。
    """
    lo, hi = (rk_flat, bk_flat) if rk_flat < bk_flat else (bk_flat, rk_flat)
    if u < lo:
        return u
    if u <= hi - 2:
        return u + 1
    return u + 2


def _kp_index(rk: Tuple[int, int], bk: Tuple[int, int]) -> int:
    """将帅九宫位置 → 0..80；任一将不在宫内返回 -1。"""
    rr, rc = rk
    br, bc = bk
    if not (7 <= rr <= 9 and 3 <= rc <= 5):
        return -1
    if not (0 <= br <= 2 and 3 <= bc <= 5):
        return -1
    return ((rr - 7) * 3 + (rc - 3)) * 9 + (br * 3 + (bc - 3))


def _kings_facing(board: List[List[str]],
                  rk: Tuple[int, int], bk: Tuple[int, int]) -> bool:
    """两将同列且中间无子 → 非法局面（飞将）。"""
    if rk[1] != bk[1]:
        return False
    lo, hi = (rk[0], bk[0]) if rk[0] < bk[0] else (bk[0], rk[0])
    for r in range(lo + 1, hi):
        if board[r][rk[1]] != '.':
            return False
    return True


def _rotate_board(board: List[List[str]]) -> List[List[str]]:
    """旋转 180° + 大小写互换（黑攻方 → 红攻方规范帧）。"""
    rot = [['.'] * BOARD_WIDTH for _ in range(BOARD_HEIGHT)]
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            p = board[r][c]
            if p != '.':
                rot[BOARD_HEIGHT - 1 - r][BOARD_WIDTH - 1 - c] = p.swapcase()
    return rot


# ── 残局表 ──

class DtmTable:
    """单个棋子组合的 DTM 表（稠密组合索引，无哈希碰撞）。"""

    def __init__(self, pieces):
        self.pieces = tuple(pieces)
        self.extras = sorted([p for p in self.pieces if p not in ('K', 'k')],
                             key=lambda p: _TYPE_ORDER.get(p, 99))
        self.sig = 'Kk' + ''.join(self.extras)
        self.k = len(self.extras)
        if self.k == 1:
            self.placements = 88
            self.identical = False
        elif self.k == 2 and self.extras[0] == self.extras[1]:
            self.placements = 88 * 87 // 2
            self.identical = True
        elif self.k == 2:
            self.placements = 88 * 87
            self.identical = False
        else:
            raise ValueError(f'不支持的棋子数: {self.pieces}')
        self.num_positions = 81 * self.placements
        self.num_states = self.num_positions * 2
        self.dtm = bytearray()
        self.loser = bytearray()
        self.loaded = False
        self._sub_tables: Dict[str, 'DtmTable'] = {}
        self._sub_sigs = None

    @property
    def sub_tables(self) -> Dict[str, 'DtmTable']:
        """吃子解析用子表 {sig: table}（赋值时失效 _sub_sigs 缓存）。"""
        return self._sub_tables

    @sub_tables.setter
    def sub_tables(self, value: Dict[str, 'DtmTable']) -> None:
        self._sub_tables = value
        self._sub_sigs = None

    @classmethod
    def from_sig(cls, sig: str) -> 'DtmTable':
        return cls(['K', 'k'] + list(sig[2:]))

    # ── 索引 ──

    def _placement_rank(self, us: List[int]) -> int:
        """其余子在 U 中的序号列表（规范类型序）→ 占位秩。"""
        if self.k == 1:
            return us[0]
        u1, u2 = us[0], us[1]
        i, j = (u1, u2) if u1 < u2 else (u2, u1)
        base = i + j * (j - 1) // 2
        if self.identical:
            return base
        return base * 2 + (0 if u1 == i else 1)

    def _state_id_from_parts(self, coords: List[Tuple[int, int]],
                             king_pos: Dict[int, Tuple[int, int]],
                             side: int) -> int:
        """增量坐标（coords 与 self.extras 对齐）→ state_id。"""
        rk = king_pos[1]
        bk = king_pos[2]
        kp = _kp_index(rk, bk)
        rk_flat = _flat(rk[0], rk[1])
        bk_flat = _flat(bk[0], bk[1])
        us = [_upos(_flat(r, c), rk_flat, bk_flat) for (r, c) in coords]
        return (kp * self.placements + self._placement_rank(us)) * 2 + side

    def _unrank_position(self, pos_rank: int):
        """pos_rank → (红帅位, 黑将位, 其余子位置列表（规范序）)。"""
        kp_idx, pr = divmod(pos_rank, self.placements)
        rpi, bpi = divmod(kp_idx, 9)
        rk = (7 + rpi // 3, 3 + rpi % 3)
        bk = (bpi // 3, 3 + bpi % 3)
        rk_flat = _flat(rk[0], rk[1])
        bk_flat = _flat(bk[0], bk[1])

        def _sq(u):
            f = _usq_flat(u, rk_flat, bk_flat)
            return divmod(f, BOARD_WIDTH)

        if self.k == 1:
            return rk, bk, [_sq(pr)]
        if self.identical:
            pair = pr
        else:
            pair = pr >> 1
        # 求最大 j ≥ 1 使 C(j,2) ≤ pair
        j = 1
        while j * (j + 1) // 2 <= pair:
            j += 1
        i = pair - j * (j - 1) // 2
        if self.identical:
            return rk, bk, [_sq(i), _sq(j)]
        o = pr & 1
        first_u, second_u = (i, j) if o == 0 else (j, i)
        return rk, bk, [_sq(first_u), _sq(second_u)]

    # ── 生成 ──

    def generate(self, sub_tables: Optional[Dict[str, 'DtmTable']] = None) -> None:
        """逆向回推生成 DTM 表。sub_tables: 已生成的子表 {sig: table}（吃子解析用）。"""
        if self.loaded:
            return
        from domain.game import ChineseChessGame

        self.sub_tables = sub_tables or {}
        N = self.num_positions
        S = self.num_states
        dtm = bytearray([DTM_ILLEGAL]) * S
        loser = bytearray(S)
        cnt = array('H', [0]) * S          # 同表后继中尚未被判"对手胜"的数量
        draw_escape = bytearray(S)         # 存在吃子走到子表和棋局面
        cap_win = array('H', [0]) * S      # 吃子胜线总 ply（0=无）
        max_sub_loss = array('H', [0]) * S  # 吃子输线最大总 ply
        legal_pos = bytearray(N)           # 1=非照面合法局面
        illegal_state = bytearray(S)       # 1=不可能局面（对方已处于将军，走子方行棋）
        edges = array('I')                 # 前向边（转置后删除）
        offsets = array('I', [0]) * (S + 1)
        l_buckets = [array('I') for _ in range(256)]
        seed_buckets = [array('I') for _ in range(256)]

        g = ChineseChessGame()
        board = [['.'] * BOARD_WIDTH for _ in range(BOARD_HEIGHT)]
        king_pos = {1: (9, 4), 2: (0, 4)}
        g.board = board
        g._king_pos = king_pos

        print(f'  生成 DTM 表: {self.sig}（{N} 局面 × 2 方）...')
        t0 = time.time()

        for pos_rank in range(N):
            rk, bk, extra_sqs = self._unrank_position(pos_rank)
            for r in range(BOARD_HEIGHT):
                row = board[r]
                for c in range(BOARD_WIDTH):
                    row[c] = '.'
            board[rk[0]][rk[1]] = 'K'
            board[bk[0]][bk[1]] = 'k'
            coords = list(extra_sqs)
            for p, (r, c) in zip(self.extras, extra_sqs):
                board[r][c] = p

            sid0 = pos_rank * 2
            offsets[sid0] = len(edges)
            if _kings_facing(board, rk, bk):
                offsets[sid0 + 1] = len(edges)
                continue
            legal_pos[pos_rank] = 1
            king_pos[1] = rk
            king_pos[2] = bk

            for side in (0, 1):
                sid = sid0 + side
                if side:
                    offsets[sid] = len(edges)
                mover = side + 1
                moves = g.get_all_legal_moves(mover)
                if any(board[tr][tc].upper() == 'K'
                       for _, _, tr, tc in moves):
                    # 对方已处于将军 = 不可能局面（对局中不可能轮到走子方
                    # 行棋）。不进种子、不建边：防止把吃王捏造成 W_1 并
                    # 污染合法状态；probe 返回 None。
                    illegal_state[sid] = 1
                    continue
                if not moves:
                    # 将死 或 困毙：无合法走法 = 输棋（与对局层语义一致）
                    dtm[sid] = 0
                    loser[sid] = mover
                    l_buckets[0].append(sid)
                    continue
                for fr, fc, tr, tc in moves:
                    target = board[tr][tc]
                    if target == '.':
                        # 非吃子 → 同表后继
                        piece = board[fr][fc]
                        board[tr][tc] = piece
                        board[fr][fc] = '.'
                        if piece == 'K':
                            king_pos[1] = (tr, tc)
                        elif piece == 'k':
                            king_pos[2] = (tr, tc)
                        else:
                            for i, (r, c) in enumerate(coords):
                                if (r, c) == (fr, fc):
                                    coords[i] = (tr, tc)
                                    break
                        edges.append(self._state_id_from_parts(
                            coords, king_pos, 1 - side))
                        cnt[sid] += 1
                        board[fr][fc] = piece
                        board[tr][tc] = '.'
                        if piece == 'K':
                            king_pos[1] = (fr, fc)
                        elif piece == 'k':
                            king_pos[2] = (fr, fc)
                        else:
                            coords[i] = (fr, fc)
                    else:
                        # 吃非王子 → 子表解析（吃王已被上面的非法状态检查拦截）
                        piece = board[fr][fc]
                        board[tr][tc] = piece
                        board[fr][fc] = '.'
                        if piece == 'K':
                            king_pos[1] = (tr, tc)
                        elif piece == 'k':
                            king_pos[2] = (tr, tc)
                        else:
                            for i, (r, c) in enumerate(coords):
                                if (r, c) == (fr, fc):
                                    coords[i] = (tr, tc)
                                    break
                        outcome, plies = self._resolve_substate(board, mover)
                        board[fr][fc] = piece
                        board[tr][tc] = target
                        if piece == 'K':
                            king_pos[1] = (fr, fc)
                        elif piece == 'k':
                            king_pos[2] = (fr, fc)
                        else:
                            coords[i] = (fr, fc)
                        if outcome == 'win':
                            if plies >= DTM_ILLEGAL:
                                raise ValueError(f'{self.sig}: 吃子胜线 {plies} 超出范围')
                            if cap_win[sid] == 0 or plies < cap_win[sid]:
                                cap_win[sid] = plies
                        elif outcome == 'lose':
                            if plies >= DTM_ILLEGAL:
                                raise ValueError(f'{self.sig}: 吃子输线 {plies} 超出范围')
                            if plies > max_sub_loss[sid]:
                                max_sub_loss[sid] = plies
                        else:
                            draw_escape[sid] = 1
                # 结算：种子 / 全吃子状态
                if cap_win[sid]:
                    seed_buckets[cap_win[sid]].append(sid)
                elif cnt[sid] == 0 and not draw_escape[sid] and max_sub_loss[sid] >= 1:
                    # 全部走法都是吃子且都输 → 直接判 L
                    d = max_sub_loss[sid]
                    dtm[sid] = d
                    loser[sid] = mover
                    l_buckets[d].append(sid)

            if (pos_rank + 1) % 50000 == 0:
                print(f'    pass1 {pos_rank + 1}/{N}（{time.time() - t0:.0f}s）')

        offsets[S] = len(edges)
        E = len(edges)
        print(f'    pass1 完成（{time.time() - t0:.0f}s，边数 {E}），转置反向边...')

        # ── 转置前向边 → 反向边（两遍 E，不二次走法生成）──
        counts = array('I', [0]) * S
        for t in edges:
            counts[t] += 1
        rev_off = array('I', [0]) * (S + 1)
        acc = 0
        for s in range(S):
            rev_off[s] = acc
            acc += counts[s]
        rev_off[S] = acc
        rev = array('I', [0]) * E
        cursor = array('I', rev_off[:S])
        for s in range(S):
            for e in range(offsets[s], offsets[s + 1]):
                t = edges[e]
                rev[cursor[t]] = s
                cursor[t] += 1
        del edges, counts, cursor, offsets

        # ── Pass 2：按 ply 桶固定点 ──
        def cascade(u: int, trigger_ply: int) -> None:
            """W 状态 u 被赋值（ply=trigger_ply）后，递减其前驱计数并判 L。"""
            for e in range(rev_off[u], rev_off[u + 1]):
                v = rev[e]
                c = cnt[v] - 1
                cnt[v] = c
                if (c == 0 and dtm[v] == DTM_ILLEGAL
                        and not illegal_state[v]
                        and not draw_escape[v] and cap_win[v] == 0):
                    d = trigger_ply + 1
                    if max_sub_loss[v] > d:
                        d = max_sub_loss[v]
                    if d >= DTM_ILLEGAL:
                        raise ValueError(f'{self.sig}: DTM {d} 超出 253（状态 {v}）')
                    dtm[v] = d
                    loser[v] = (v & 1) + 1
                    l_buckets[d].append(v)

        for ply in range(1, DTM_ILLEGAL + 1):
            # 处理完 l_buckets[0..253]；253 桶的前驱需要 W_254，超出
            # 表示范围（254=非法槽），必须报错而非静默截断成和棋
            bucket = l_buckets[ply - 1]
            if bucket and ply >= DTM_ILLEGAL:
                raise ValueError(f'{self.sig}: DTM 达到 {ply}（超出 253 可表示范围）')
            # (a) L_{ply-1} 的前驱 → W_ply（首触即最小 DTM）
            for q in bucket:
                for e in range(rev_off[q], rev_off[q + 1]):
                    u = rev[e]
                    if dtm[u] != DTM_ILLEGAL or illegal_state[u]:
                        continue
                    dtm[u] = ply
                    loser[u] = loser[q]
                    cascade(u, ply)
            # (b) 种子（吃子子表胜）— 与 L 发现同序处理保证最小性
            for u in seed_buckets[ply]:
                if dtm[u] != DTM_ILLEGAL:
                    continue
                dtm[u] = ply
                loser[u] = 2 - (u & 1)
                cascade(u, ply)

        # ── 结算：未赋值 = 和棋；统计 ──
        red_loses = black_loses = draws = 0
        max_real = 0
        for pos_rank in range(N):
            if not legal_pos[pos_rank]:
                continue
            sid0 = pos_rank * 2
            for side in (0, 1):
                sid = sid0 + side
                d = dtm[sid]
                if d == DTM_ILLEGAL:
                    if illegal_state[sid]:
                        continue          # 不可能局面保持 254（probe 返回 None）
                    dtm[sid] = DTM_DRAW
                    loser[sid] = 0
                    draws += 1
                else:
                    if loser[sid] == 1:
                        red_loses += 1
                    else:
                        black_loses += 1
                    if d > max_real:
                        max_real = d
        if max_real > MAX_REAL_DTM:
            raise ValueError(f'{self.sig}: 最大 DTM {max_real} 超出 {MAX_REAL_DTM}')

        self.dtm = dtm
        self.loser = loser
        self.loaded = True
        print(f'  完成 {self.sig}: 红负 {red_loses}，黑负 {black_loses}，和棋 {draws}，'
              f'最大 DTM {max_real}（{time.time() - t0:.0f}s）')

    def _resolve_substate(self, board: List[List[str]],
                          mover: int) -> Tuple[str, int]:
        """吃子后的子表解析。board 已应用吃子走法，mover = 吃子方。

        Returns:
            (outcome, plies)：outcome ∈ {'win','lose','draw'}（吃子方视角），
            plies 为含吃子这一手的总 ply。
        """
        # 3 - mover = 吃子后的走子方；_pick_frame 在旋转帧中自动翻转
        sub_sigs = self._sub_sigs
        if sub_sigs is None:
            # 生成期每手吃子都要解析一次，缓存签名集合（sub_tables
            # 赋值时经 setter 自动失效重建）
            sub_sigs = self._sub_sigs = set(self.sub_tables.keys())
        frame = _pick_frame(board, 3 - mover, sub_sigs)
        if frame is None:
            # 双方无攻击子力 → 平凡和棋（不可能成杀/困毙）
            return ('draw', 0)
        canon_board, canon_mover, _, sig = frame
        table = self.sub_tables.get(sig)
        if table is None:
            return ('draw', 0)           # 未知组合：保守视为和棋
        res = table.probe(canon_board, canon_mover)
        if res is None or res[0] == DTM_DRAW:
            return ('draw', 0)
        d, sub_loser = res
        if sub_loser == canon_mover:
            # 非吃子方负 → 吃子方胜
            return ('win', d + 1)
        return ('lose', d + 1)

    # ── 查询 / 存取 ──

    def probe(self, board: List[List[str]],
              current_player: int) -> Optional[Tuple[int, int]]:
        """查表。board 必须是规范帧（红方为攻方）。返回 (dtm, loser) 或 None。"""
        if current_player not in (1, 2):
            raise ValueError(f'非法走子方: {current_player}（须为 1=红 或 2=黑）')
        if not self.loaded:
            return None
        rk = bk = None
        extra_pos: Dict[str, List[Tuple[int, int]]] = {}
        for r in range(BOARD_HEIGHT):
            for c in range(BOARD_WIDTH):
                p = board[r][c]
                if p == 'K':
                    rk = (r, c)
                elif p == 'k':
                    bk = (r, c)
                elif p != '.':
                    extra_pos.setdefault(p, []).append((r, c))
        if rk is None or bk is None or sum(len(v) for v in extra_pos.values()) != self.k:
            return None
        kp = _kp_index(rk, bk)
        if kp < 0:
            return None
        rk_flat = _flat(rk[0], rk[1])
        bk_flat = _flat(bk[0], bk[1])
        us = []
        for p in self.extras:
            lst = extra_pos.get(p)
            if not lst:
                return None
            us.append(_upos(_flat(*lst[0]), rk_flat, bk_flat))
            del lst[0]
        sid = (kp * self.placements + self._placement_rank(us)) * 2 \
            + (0 if current_player == 1 else 1)
        d = self.dtm[sid]
        if d == DTM_ILLEGAL:
            return None
        return (d, self.loser[sid])

    def save(self, filepath: str) -> None:
        """保存 v4 格式（tmp + 原子替换，防半截文件；头含 CRC32 内容校验）。"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        tmp = filepath + '.tmp'
        sig_b = self.sig.encode('ascii')
        body = bytes(self.dtm) + bytes(self.loser)
        with open(tmp, 'wb') as f:
            f.write(DTM_MAGIC)
            f.write(bytes([len(sig_b)]))
            f.write(sig_b)
            f.write(struct.pack('<I', self.num_positions))
            f.write(struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF))
            f.write(body)
        os.replace(tmp, filepath)

    def load(self, filepath: str) -> bool:
        """加载 v4 格式（魔数 + 签名 + 局面数 + 体长 + CRC32 内容校验）。

        内容级校验保证损坏文件（如个别槽位被改写）不会以权威值被使用。
        """
        data = _read_v4_body(filepath, self.sig, self.num_positions)
        if data is None:
            return False
        self.dtm = bytearray(data[:self.num_positions * 2])
        self.loser = bytearray(data[self.num_positions * 2:])
        self.loaded = True
        return True


# 全部表签名（_canonicalize 的方向选择用）
_TABLE_SIGS = frozenset(DtmTable(p).sig for p in TABLE_SETS)


def _sig_filename(sig: str) -> str:
    """签名 → 文件名（大小写不敏感文件系统安全）。

    小写防守子前插 '_'：KkRA → KkRA；KkRa → KkR_a。
    否则 KkRA/KkRa、KkRB/KkRb 等对在 Windows 上互相同名互相覆盖。
    """
    extras = sig[2:]
    for i, ch in enumerate(extras):
        if ch.islower():
            return 'Kk' + extras[:i] + '_' + extras[i:]
    return sig


def _dtm_filepath(sig: str) -> str:
    """签名 → .dtm 文件路径。"""
    return os.path.join(DTM_DIR, _sig_filename(sig) + '.dtm')


# ── 规范化与查询 ──

def _frame_sig(board: List[List[str]]) -> str:
    """棋盘 → 规范签名（'Kk' + 按类型序排序的其余子）。"""
    extras = []
    for row in board:
        for p in row:
            if p not in ('.', 'K', 'k'):
                extras.append(p)
    return 'Kk' + ''.join(sorted(extras, key=lambda p: _TYPE_ORDER.get(p, 99)))


def _pick_frame(board: List[List[str]], mover: int,
                known: set) -> Optional[Tuple[List[List[str]], int, bool, str]]:
    """选择规范帧方向（红方为攻方）。

    红攻子 → 不旋转；仅黑攻子 → 旋转；双方都有攻子 → 优先选签名在
    known 中的方向（都在/都不在 → 红攻子优先不旋转）。
    返回 (canon_board, canon_mover, rotated, sig)；双方无攻击子力返回 None。
    """
    red_att = black_att = 0
    extras = []
    for row in board:
        for p in row:
            if p == '.' or p == 'K' or p == 'k':
                continue
            extras.append(p)
            if p.isupper():
                if p in ATTACKER_TYPES:
                    red_att += 1
            elif p in 'rncp':
                black_att += 1
    if red_att == 0 and black_att == 0:
        return None
    sig = 'Kk' + ''.join(sorted(extras, key=lambda p: _TYPE_ORDER.get(p, 99)))
    if black_att > 0 and (red_att == 0 or sig not in known):
        rot = _rotate_board(board)
        rot_sig = _frame_sig(rot)
        if red_att == 0 or rot_sig in known:
            return rot, 3 - mover, True, rot_sig
    return board, mover, False, sig


def _canonicalize(board: List[List[str]],
                  current_player: int) -> Optional[Tuple[List[List[str]], int, bool, str]]:
    """规范化：返回 (canon_board, canon_mover, rotated, sig) 或 None（双方无攻击子力）。"""
    return _pick_frame(board, current_player, _TABLE_SIGS)


# ── 全局缓存 ──
_tables: Dict[str, DtmTable] = {}
_missing_sigs: set = set()       # 已确认磁盘无文件的签名（负缓存，防每查询打盘）


def probe_local(board: List[List[str]], piece_count: int,
                current_player: int) -> Optional[Tuple[float, int]]:
    """查询本地 DTM 残局库。

    Args:
        board: 10×9 棋盘
        piece_count: 棋盘上的棋子总数
        current_player: 当前走子方 (1=红, 2=黑)

    Returns:
        None: 无匹配的本地表
        (score, dtm): score 是 current_player 视角的 centipawn；
        dtm 是杀棋距离（ply），DTM_DRAW(255)=和棋，0=已杀（L_0）
        —— 和棋不用 dtm=0，避免与"杀在 0 手"混淆
    """
    if piece_count > 4:
        return None

    canon = _canonicalize(board, current_player)
    if canon is None:
        return None
    canon_board, canon_mover, rotated, sig = canon

    table = _tables.get(sig)
    if table is None:
        if sig not in _TABLE_SIGS or sig in _missing_sigs:
            return None                # 无此表 / 负缓存：避免每次查询都打盘
        filepath = _dtm_filepath(sig)
        t = DtmTable.from_sig(sig)
        if not t.load(filepath):
            _missing_sigs.add(sig)
            return None
        _tables[sig] = t
        table = t

    res = table.probe(canon_board, canon_mover)
    if res is None:
        return None
    d, loser_canon = res
    if d == DTM_DRAW:
        return (0.0, DTM_DRAW)
    loser = 3 - loser_canon if rotated else loser_canon
    score = 99999 - d * 10       # 与 search.JIANGSHA_SCORE / egtb._dtm_to_score 一致
    if current_player == loser:
        score = -score
    return (float(score), d)


def _read_v4_body(filepath: str, sig: str, num_positions: int) -> Optional[bytes]:
    """读取并校验 v4 文件体（魔数 + 签名 + 局面数 + 体长 + CRC32）。

    任何一步不符返回 None（含内容损坏——保证损坏文件不会以权威值被使用）。
    """
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, 'rb') as f:
            if f.read(5) != DTM_MAGIC:
                return None
            sl = f.read(1)
            if not sl:
                return None
            if f.read(sl[0]).decode('ascii') != sig:
                return None
            if struct.unpack('<I', f.read(4))[0] != num_positions:
                return None
            crc = struct.unpack('<I', f.read(4))[0]
            data = f.read()
        if len(data) != num_positions * 4:
            return None
        if zlib.crc32(data) & 0xFFFFFFFF != crc:
            return None
        return data
    except (OSError, struct.error, UnicodeDecodeError):
        return None


def _file_matches(filepath: str, sig: str, num_positions: int) -> bool:
    """文件是否完整匹配（魔数 + 签名 + 局面数 + 体长 + CRC32 内容校验）。"""
    return _read_v4_body(filepath, sig, num_positions) is not None


def generate_all_4piece(force: bool = False, only: Optional[str] = None) -> None:
    """生成全部 ≤4 子残局 DTM 表（依赖序；已有完整文件则跳过）。

    Args:
        force: 忽略已有文件全部重新生成
        only: 只生成指定表（如 'KkR'）；依赖表必须已完整存在于磁盘，
            缺失/损坏即报错终止（防止目标表带着缺失依赖生成，把所有
            吃子线误标成和棋并永久保存）
    """
    os.makedirs(DTM_DIR, exist_ok=True)
    found_target = False
    for pieces in TABLE_SETS:
        t = DtmTable(pieces)
        filepath = _dtm_filepath(t.sig)
        if only is not None and t.sig != only:
            # 依赖表：只需装载，缺失/损坏 → 硬失败
            if not t.load(filepath):
                raise ValueError(
                    f'依赖表 {t.sig} 缺失或内容不完整（{filepath}），'
                    f'请先完整运行生成（不带 --only）')
            _tables[t.sig] = t
            continue
        if not force and _file_matches(filepath, t.sig, t.num_positions):
            if not t.load(filepath):
                raise ValueError(f'表 {t.sig} 加载失败: {filepath}')
            print(f'跳过（已存在且完整）: {t.sig}')
        else:
            t.generate(_tables)    # 生成失败直接抛出，绝不带着缺失依赖继续
            t.save(filepath)
            print(f'  保存: {filepath}')
        _tables[t.sig] = t
        if only is not None:
            found_target = True
            break                  # --only：目标表之后不再处理
    if only is not None and not found_target:
        raise ValueError(f'未知表签名: {only}（可选: {", ".join(sorted(_TABLE_SIGS))}）')
    print('全部完成')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='生成本地 DTM 残局表（逆向回推）')
    parser.add_argument('--only', help='只生成指定表（如 KkR）')
    parser.add_argument('--force', action='store_true', help='忽略已有文件，全部重新生成')
    args = parser.parse_args()
    generate_all_4piece(force=args.force, only=args.only)
