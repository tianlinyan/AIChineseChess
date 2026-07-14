"""坐标文本解析器 — 当 AI 未使用工具调用时，从文本中回退提取坐标"""

import re

RE_COORD_PATTERN = re.compile(r'[A-I]\d{1,2}', re.IGNORECASE)


def parse_coordinates_from_text(text: str) -> tuple:
    """从 AI 文本回复中提取起止坐标。

    查找所有形如 A1 ~ I10 的坐标，取前两个作为 from, to。

    Returns:
        (from_coord, to_coord) 均为大写字符串；解析失败时返回 ('', '')
    """
    matches = RE_COORD_PATTERN.findall(text)
    if len(matches) >= 2:
        return matches[0].upper(), matches[1].upper()
    return '', ''
