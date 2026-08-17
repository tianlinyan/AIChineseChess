"""中国象棋 FEN 字符串生成 — 统一的棋盘序列化

供 Pikafish UCI 引擎、EGTB 云库查询等模块共用。
消除原先 egtb.py 与 pikafish.py 中 _board_to_fen 的 99% 重复代码。
"""


def board_to_fen(board: list, current_player: int) -> str:
    """将 10×9 棋盘转为中国象棋 FEN 字符串。

    FEN 格式：rows/rows/.../rows <side>
    - 大写=红方，小写=黑方，数字=连续空格数
    - w=红方走, b=黑方走
    - row 0 = 黑方底线（棋盘顶部），与 Pikafish / chessdb.cn 标准一致
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


def fen_to_board(fen: str) -> tuple:
    """解析 board_to_fen 生成的 FEN 字符串 → (board, current_player)。

    与序列化互逆：row 0 = 黑方底线，'w' → player 1（红方走），'b' → player 2。
    """
    rows_part, side = fen.split(' ')
    board = []
    for row_str in rows_part.split('/'):
        row = []
        for ch in row_str:
            if ch.isdigit():
                row.extend(['.'] * int(ch))
            else:
                row.append(ch)
        assert len(row) == 9, f'FEN 行宽错误: {row_str}'
        board.append(row)
    assert len(board) == 10
    return board, (1 if side == 'w' else 2)
