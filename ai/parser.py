"""坐标文本解析器 — 当 AI 未使用工具调用时，从文本中回退提取坐标"""

import re

RE_COORD_PATTERN = re.compile(r'[A-I]\d{1,2}', re.IGNORECASE)

# 文本形式的 move_piece 调用，如 move_piece(from="A1", to="B2")、move_piece("A1","B2")
RE_MOVE_PIECE_PATTERN = re.compile(
    r'move_piece\s*\(\s*(?:from\s*=\s*)?["\']?([A-I]\d{1,2})["\']?'
    r'\s*[,，]\s*(?:to\s*=\s*)?["\']?([A-I]\d{1,2})["\']?',
    re.IGNORECASE)


def parse_coordinates_from_text(text: str) -> tuple:
    """从 AI 文本回复中提取起止坐标。

    策略（按优先级）：
    1. move_piece(...) 文本调用样式；
    2. 全文【最后】两个坐标 —— LLM 的思考文本常先引用"引擎推荐 H10→G8"，
       最后才给出自己的决定，取前两个坐标会张冠李戴。

    Returns:
        (from_coord, to_coord) 均为大写字符串；解析失败时返回 ('', '')
    """
    m = RE_MOVE_PIECE_PATTERN.search(text)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    matches = RE_COORD_PATTERN.findall(text)
    if len(matches) >= 2:
        return matches[-2].upper(), matches[-1].upper()
    return '', ''
