"""传统棋谱记法验证 — 中国象棋程序竞赛规则 第二章第六节

用法：python tests/test_notation.py
覆盖：
- 四字结构（棋子名/路号/进退缩/第四字）
- 红方中文数字、黑方阿拉伯数字（竖线与步数）
- 同路同类子 前/后（士/相除外）
- 兵卒：同路 2 兵（前/后）、3 兵（中兵）、两路各 ≥2（前/中/后+路）、
  4~5 兵（前兵/二兵/三兵/四兵/后兵）
- 竞赛规则原文棋例（车三平五、士６退５、前炮平６、炮６平９、
  车八进一、炮８进１ 等）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.constants import format_chinese_notation

FAILED = []


def check(name: str, cond: bool, detail: str = ''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        FAILED.append(name)


def empty_board():
    return [['.'] * 9 for _ in range(10)]


def set_piece(board, r, c, p):
    board[r][c] = p


def game_with(board):
    g = ChineseChessGame()
    g.board = [row[:] for row in board]
    for r in range(10):
        for c in range(9):
            if g.board[r][c] == 'K':
                g._king_pos[1] = (r, c)
            elif g.board[r][c] == 'k':
                g._king_pos[2] = (r, c)
    g.recompute_hash()
    g._recompute_incremental()
    return g


# ── 1. 初始局面：红方中文、黑方阿拉伯 ──
b = ChineseChessGame().board
check('炮二平五（红炮 7,7→7,4）',
      format_chinese_notation(b, 7, 7, 7, 4) == '炮二平五',
      format_chinese_notation(b, 7, 7, 7, 4))
check('馬八进七（红馬 9,1→7,2）',
      format_chinese_notation(b, 9, 1, 7, 2) == '馬八进七',
      format_chinese_notation(b, 9, 1, 7, 2))
check('車一进一（红車 9,8→8,8，竖走步数中文）',
      format_chinese_notation(b, 9, 8, 8, 8) == '車一进一',
      format_chinese_notation(b, 9, 8, 8, 8))
check('馬2进3（黑馬 0,1→2,2）',
      format_chinese_notation(b, 0, 1, 2, 2) == '馬2进3',
      format_chinese_notation(b, 0, 1, 2, 2))
check('炮8平5（黑炮 2,7→2,4）',
      format_chinese_notation(b, 2, 7, 2, 4) == '炮8平5',
      format_chinese_notation(b, 2, 7, 2, 4))
check('卒3进1（黑卒 3,2→4,2）',
      format_chinese_notation(b, 3, 2, 4, 2) == '卒3进1',
      format_chinese_notation(b, 3, 2, 4, 2))
check('車9进1（黑車 0,8→1,8，竖走步数阿拉伯）',
      format_chinese_notation(b, 0, 8, 1, 8) == '車9进1',
      format_chinese_notation(b, 0, 8, 1, 8))
check('帥五进一（红帅 9,4→8,4）',
      format_chinese_notation(b, 9, 4, 8, 4) == '帥五进一',
      format_chinese_notation(b, 9, 4, 8, 4))
check('将5平6（黑将 0,4→0,5）',
      format_chinese_notation(b, 0, 4, 0, 5) == '将5平6',
      format_chinese_notation(b, 0, 4, 0, 5))
check('仕六进五（红仕 9,3→8,4，第四字为目标路）',
      format_chinese_notation(b, 9, 3, 8, 4) == '仕六进五',
      format_chinese_notation(b, 9, 3, 8, 4))

# ── 2. 同路同类子 前/后（规则第二条）──
b2 = empty_board()
set_piece(b2, 0, 4, 'k')
set_piece(b2, 9, 4, 'K')
set_piece(b2, 5, 0, 'R')   # 前車（接近黑方）
set_piece(b2, 8, 0, 'R')   # 后車
check('前車进二（红 5,0→3,0，竖走 2 格）',
      format_chinese_notation(b2, 5, 0, 3, 0) == '前車进二',
      format_chinese_notation(b2, 5, 0, 3, 0))
check('后車平五（红 8,0→8,4）',
      format_chinese_notation(b2, 8, 0, 8, 4) == '后車平五',
      format_chinese_notation(b2, 8, 0, 8, 4))

# 黑方：同路双炮，前=接近红方（行号大）
b3 = empty_board()
set_piece(b3, 0, 4, 'k')
set_piece(b3, 9, 4, 'K')
set_piece(b3, 0, 8, 'c')   # 后炮（行号小）
set_piece(b3, 3, 8, 'c')   # 前炮（行号大）
check('前炮平6（黑 3,8→3,5）',
      format_chinese_notation(b3, 3, 8, 3, 5) == '前炮平6',
      format_chinese_notation(b3, 3, 8, 3, 5))
check('后炮平1（黑 0,8→0,0）',
      format_chinese_notation(b3, 0, 8, 0, 0) == '后炮平1',
      format_chinese_notation(b3, 0, 8, 0, 0))

# 双仕不同路（同路同类规则不适用于士/相，此处仅验证正常记法）
b4 = empty_board()
set_piece(b4, 0, 4, 'k')
set_piece(b4, 9, 4, 'K')
set_piece(b4, 9, 3, 'A')
set_piece(b4, 7, 4, 'A')
check('仕五退六（红 7,4→8,3，无前/后前缀）',
      format_chinese_notation(b4, 7, 4, 8, 3) == '仕五退六',
      format_chinese_notation(b4, 7, 4, 8, 3))

# ── 3. 兵卒规则（规则第三条）──
# 3.1 同路 2 兵 → 前兵/后兵
b5 = empty_board()
set_piece(b5, 0, 4, 'k')
set_piece(b5, 9, 4, 'K')
set_piece(b5, 6, 3, 'P')   # 前兵
set_piece(b5, 8, 3, 'P')   # 后兵
check('前兵平五（红 6,3→6,4）',
      format_chinese_notation(b5, 6, 3, 6, 4) == '前兵平五',
      format_chinese_notation(b5, 6, 3, 6, 4))
check('后兵进一（红 8,3→7,3）',
      format_chinese_notation(b5, 8, 3, 7, 3) == '后兵进一',
      format_chinese_notation(b5, 8, 3, 7, 3))

# 3.2 同路 3 兵 → 前兵/中兵/后兵（中兵平五 示例）
b6 = empty_board()
set_piece(b6, 0, 4, 'k')
set_piece(b6, 9, 4, 'K')
set_piece(b6, 5, 3, 'P')   # 前兵
set_piece(b6, 6, 3, 'P')   # 中兵
set_piece(b6, 8, 3, 'P')   # 后兵
check('中兵平五（红 6,3→6,4，规则原文示例）',
      format_chinese_notation(b6, 6, 3, 6, 4) == '中兵平五',
      format_chinese_notation(b6, 6, 3, 6, 4))
check('前兵平五（红 5,3→5,4）',
      format_chinese_notation(b6, 5, 3, 5, 4) == '前兵平五',
      format_chinese_notation(b6, 5, 3, 5, 4))
check('后兵平五（红 8,3→8,4）',
      format_chinese_notation(b6, 8, 3, 8, 4) == '后兵平五',
      format_chinese_notation(b6, 8, 3, 8, 4))

# 3.3 两路各 ≥2 兵 → 前/中/后 + 路（前三平四、中四平五 同型）
# 红方：col2=七路，col3=六路
b7 = empty_board()
set_piece(b7, 0, 4, 'k')
set_piece(b7, 9, 3, 'K')
set_piece(b7, 5, 2, 'P')   # 前七
set_piece(b7, 6, 2, 'P')   # 中七
set_piece(b7, 8, 2, 'P')   # 后七
set_piece(b7, 5, 3, 'P')   # 前六
set_piece(b7, 8, 3, 'P')   # 后六
check('前七平六（红 5,2→5,3）',
      format_chinese_notation(b7, 5, 2, 5, 3) == '前七平六',
      format_chinese_notation(b7, 5, 2, 5, 3))
check('中七平六（红 6,2→6,3）',
      format_chinese_notation(b7, 6, 2, 6, 3) == '中七平六',
      format_chinese_notation(b7, 6, 2, 6, 3))
check('后七进一（红 8,2→7,2）',
      format_chinese_notation(b7, 8, 2, 7, 2) == '后七进一',
      format_chinese_notation(b7, 8, 2, 7, 2))
check('前六平七（红 5,3→5,2）',
      format_chinese_notation(b7, 5, 3, 5, 2) == '前六平七',
      format_chinese_notation(b7, 5, 3, 5, 2))
check('后六进一（红 8,3→7,3）',
      format_chinese_notation(b7, 8, 3, 7, 3) == '后六进一',
      format_chinese_notation(b7, 8, 3, 7, 3))

# 3.4 同路 5 兵 → 前兵/二兵/三兵/四兵/后兵（后兵平五、三兵平五 规则原文示例）
b8 = empty_board()
set_piece(b8, 0, 4, 'k')
set_piece(b8, 9, 3, 'K')
for r in (2, 4, 5, 7, 8):          # 前→后 排序（红：行号升序）
    set_piece(b8, r, 3, 'P')
check('前兵平五（红 2,3→2,4）',
      format_chinese_notation(b8, 2, 3, 2, 4) == '前兵平五',
      format_chinese_notation(b8, 2, 3, 2, 4))
check('二兵平五（红 4,3→4,4）',
      format_chinese_notation(b8, 4, 3, 4, 4) == '二兵平五',
      format_chinese_notation(b8, 4, 3, 4, 4))
check('三兵平五（红 5,3→5,4，规则原文示例）',
      format_chinese_notation(b8, 5, 3, 5, 4) == '三兵平五',
      format_chinese_notation(b8, 5, 3, 5, 4))
check('四兵平五（红 7,3→7,4）',
      format_chinese_notation(b8, 7, 3, 7, 4) == '四兵平五',
      format_chinese_notation(b8, 7, 3, 7, 4))
check('后兵平五（红 8,3→8,4，规则原文示例）',
      format_chinese_notation(b8, 8, 3, 8, 4) == '后兵平五',
      format_chinese_notation(b8, 8, 3, 8, 4))

# 3.5 同路 4 兵 → 前兵/二兵/三兵/后兵
b9 = empty_board()
set_piece(b9, 0, 4, 'k')
set_piece(b9, 9, 3, 'K')
for r in (3, 5, 6, 8):
    set_piece(b9, r, 3, 'P')
check('三兵进一（红 6,3→5,3）',
      format_chinese_notation(b9, 6, 3, 5, 3) == '三兵进一',
      format_chinese_notation(b9, 6, 3, 5, 3))
check('二兵平五（红 5,3→5,4）',
      format_chinese_notation(b9, 5, 3, 5, 4) == '二兵平五',
      format_chinese_notation(b9, 5, 3, 5, 4))

# 黑方卒同路 3 → 前卒/中卒/后卒（黑路号：col4=5 路，col5=6 路）
b10 = empty_board()
set_piece(b10, 0, 4, 'k')
set_piece(b10, 9, 4, 'K')
set_piece(b10, 4, 4, 'p')   # 后卒（行号小=接近黑方）
set_piece(b10, 6, 4, 'p')   # 中卒
set_piece(b10, 8, 4, 'p')   # 前卒（行号大=接近红方）
check('中卒平6（黑 6,4→6,5）',
      format_chinese_notation(b10, 6, 4, 6, 5) == '中卒平6',
      format_chinese_notation(b10, 6, 4, 6, 5))
check('前卒平6（黑 8,4→8,5）',
      format_chinese_notation(b10, 8, 4, 8, 5) == '前卒平6',
      format_chinese_notation(b10, 8, 4, 8, 5))
check('后卒进1（黑 4,4→5,4）',
      format_chinese_notation(b10, 4, 4, 5, 4) == '后卒进1',
      format_chinese_notation(b10, 4, 4, 5, 4))

# ── 4. 竞赛规则原文棋例（第三章 例4 / 例1 / 例3）──
# 例4：黑双炮同路 9 路（FEN 4k3c/9/4bn2n/8c/6R2/6P2/9/9/9/3K5）
b11 = empty_board()
set_piece(b11, 0, 4, 'k')
set_piece(b11, 0, 8, 'c')     # 后炮（行号小）
set_piece(b11, 2, 4, 'b')
set_piece(b11, 2, 5, 'n')
set_piece(b11, 2, 8, 'n')
set_piece(b11, 3, 8, 'c')     # 前炮（行号大）
set_piece(b11, 4, 6, 'R')
set_piece(b11, 5, 6, 'P')
set_piece(b11, 9, 3, 'K')
check('例4 前炮平6（黑 3,8→3,5）',
      format_chinese_notation(b11, 3, 8, 3, 5) == '前炮平6',
      format_chinese_notation(b11, 3, 8, 3, 5))
# 前炮走后：① 该炮回到 6 路再平回 9 路 → 炮6平9；② 后炮仍在 9 路 → 炮9平1
b11b = [row[:] for row in b11]
b11b[3][8] = '.'              # 前炮已离开 9 路
set_piece(b11b, 3, 5, 'c')    # 前炮现处 6 路
check('例4 炮6平9（前炮 3,5→3,8 回原位，单炮不再加前/后）',
      format_chinese_notation(b11b, 3, 5, 3, 8) == '炮6平9',
      format_chinese_notation(b11b, 3, 5, 3, 8))
b11c = [row[:] for row in b11]
b11c[3][8] = '.'
check('例4 炮9平1（后炮 0,8→0,0，单炮不加前/后）',
      format_chinese_notation(b11c, 0, 8, 0, 0) == '炮9平1',
      format_chinese_notation(b11c, 0, 8, 0, 0))

# 例1：车三平五 / 士６退５（FEN 4k4/9/2c2an2/4c4/6R2/9/9/4B4/4A4/3K1AB2）
b12 = empty_board()
set_piece(b12, 0, 4, 'k')
set_piece(b12, 2, 2, 'c')
set_piece(b12, 2, 5, 'a')
set_piece(b12, 2, 6, 'n')
set_piece(b12, 3, 4, 'c')
set_piece(b12, 4, 6, 'R')
set_piece(b12, 7, 4, 'B')
set_piece(b12, 8, 4, 'A')
set_piece(b12, 9, 3, 'K')
set_piece(b12, 9, 5, 'A')
set_piece(b12, 9, 6, 'B')
check('例1 車三平五（红 4,6→4,4）',
      format_chinese_notation(b12, 4, 6, 4, 4) == '車三平五',
      format_chinese_notation(b12, 4, 6, 4, 4))
check('例1 士6退5（黑 2,5→1,4）',
      format_chinese_notation(b12, 2, 5, 1, 4) == '士6退5',
      format_chinese_notation(b12, 2, 5, 1, 4))

# 例3：车八进一 / 炮８进１（FEN 2ba1k1r1/4a4/4b4/9/9/9/7c1/1R7/9/4K2R1）
b13 = empty_board()
set_piece(b13, 0, 2, 'b')
set_piece(b13, 0, 3, 'a')
set_piece(b13, 0, 4, 'k')
set_piece(b13, 0, 7, 'r')
set_piece(b13, 1, 4, 'a')
set_piece(b13, 2, 4, 'b')
set_piece(b13, 6, 7, 'c')
set_piece(b13, 7, 1, 'R')
set_piece(b13, 9, 4, 'K')
set_piece(b13, 9, 8, 'R')
check('例3 車八进一（红 7,1→6,1）',
      format_chinese_notation(b13, 7, 1, 6, 1) == '車八进一',
      format_chinese_notation(b13, 7, 1, 6, 1))
check('例3 炮8进1（黑 6,7→7,7）',
      format_chinese_notation(b13, 6, 7, 7, 7) == '炮8进1',
      format_chinese_notation(b13, 6, 7, 7, 7))

# ── 5. 走子记录存档：format_move_history 输出传统棋谱 ──
g2 = ChineseChessGame()
g2.move_piece(7, 7, 7, 4)          # 炮二平五
g2.move_piece(0, 1, 2, 2)          # 馬2进3
g2.move_piece(9, 1, 7, 2)          # 馬八进七
hist = g2.format_move_history()
check('历史含 炮二平五', '炮二平五' in hist, hist.replace('\n', ' | '))
check('历史含 馬2进3', '馬2进3' in hist, hist.replace('\n', ' | '))
check('历史含 馬八进七', '馬八进七' in hist, hist.replace('\n', ' | '))
check('历史不再显示坐标箭头', '→' not in hist, hist.replace('\n', ' | '))

# 存档着法在落子时定格：前/中/后消歧不因后续棋盘变化而改变
g3 = game_with(b7)
g3.current_player = 1
g3.move_piece(8, 2, 7, 2)          # 后七进一（红：后兵前进，合法）
check('move_notation 返回存档着法',
      g3.move_notation(g3.moves[-1]) == '后七进一',
      g3.move_notation(g3.moves[-1]))
g3.move_piece(0, 4, 0, 3)          # 将5平4（黑）
check('黑方着法同样以传统记法存档',
      g3.move_notation(g3.moves[-1]) == '将5平4',
      g3.move_notation(g3.moves[-1]))

if FAILED:
    print(f'\nFAILED {len(FAILED)} 项: {FAILED}')
    sys.exit(1)
print('\n全部通过')
