"""中国象棋 FEN 字符串生成 — 统一的棋盘序列化

供 Pikafish UCI 引擎、EGTB 云库查询等模块共用。
消除原先 egtb.py 与 pikafish.py 中 _board_to_fen 的 99% 重复代码。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.game import ChineseChessGame


def board_to_fen(board: list, current_player: int,
                 reverse_rows: bool = False) -> str:
    """将 10×9 棋盘转为中国象棋 FEN 字符串。

    FEN 格式：rows/rows/.../rows <side>
    - 大写=红方，小写=黑方，数字=连续空格数
    - w=红方走, b=黑方走

    Args:
        board: 10×9 二维列表，row 0 = 黑方底线（棋盘顶部）
        current_player: 1=红方, 2=黑方
        reverse_rows: True 则将行序反转（Pikafish 需要 row 0 = 红方底线）
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
    if reverse_rows:
        rows.reverse()
    side = 'w' if current_player == 1 else 'b'
    return '/'.join(rows) + ' ' + side


def game_to_fen(game: 'ChineseChessGame', current_player: int) -> str:
    """从 ChineseChessGame 对象生成 FEN（EGTB 查询用）。"""
    return board_to_fen(game.board, current_player, reverse_rows=False)
