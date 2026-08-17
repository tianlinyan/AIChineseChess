from html import escape

from PyQt6.QtCore import QDateTime
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QTextEdit

from domain.constants import LOG_MAX_BLOCKS

LOG_COLORS = {
    'red': '#e06c75',       # 红方日志
    'black': '#61afef',     # 黑方日志
    'ERROR': '#e06c75',
    'WARNING': '#d19a66',
    # 'INFO' 与 'DEFAULT' 同色，冗余键已删（查找链回退 DEFAULT 仍同色）
    'DEFAULT': '#abb2bf',
}


class LogManager:
    """HTML 彩色日志管理器"""

    def __init__(self) -> None:
        self._widget: QTextEdit | None = None

    def set_widget(self, widget: QTextEdit) -> None:
        self._widget = widget
        # QTextEdit 本身没有 setMaximumBlockCount，需设置在 document 上（Qt6）
        widget.document().setMaximumBlockCount(LOG_MAX_BLOCKS)

    def clear(self) -> None:
        if self._widget:
            self._widget.clear()

    def log(self, message: str, level: str = 'INFO') -> None:
        if not self._widget:
            return
        # 先按原始 level 查（'red'/'black'），再按大写查标准级别，
        # 避免 'red'.upper()=='RED' 查不到小写键而恒回退 DEFAULT
        color = (LOG_COLORS.get(level)
                 or LOG_COLORS.get(level.upper())
                 or LOG_COLORS['DEFAULT'])
        timestamp = QDateTime.currentDateTime().toString('hh:mm:ss')
        # message 可能含 LLM 原始响应等任意文本，必须 HTML 转义；
        # 时间戳 span 是程序自己生成的 HTML，不受影响。
        # 换行转 <br>：insertHtml 会把 \n 折叠为空格，导致 LLM 多行思考
        # 文本变成一长行；在 escape 之后替换，不引入 HTML 注入面。
        # 先归一化 CRLF/CR → LF（LLM/引擎输出可能带 \r\n），否则 \r
        # 会原样进入 HTML 造成渲染异常。
        normalized = message.replace('\r\n', '\n').replace('\r', '\n')
        escaped = escape(normalized).replace('\n', '<br>')
        html = f'<span style="color:#888">{timestamp}</span> '
        html += f'<span style="color:{color}">{escaped}</span>'
        # 用光标副本在文末插入，不移动视口；insertBlock 保证每条日志独占一个块，
        # maximumBlockCount 才能按条裁剪（<br> 只是块内软换行，不产生新块）
        cursor = self._widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock()
        cursor.insertHtml(html)
        # 始终将光标置于最后，确保最新日志立即可见
        self._widget.moveCursor(QTextCursor.MoveOperation.End)
        # 确保滚动条跟随（moveCursor 有时不触发滚动，显式设置）
        scrollbar = self._widget.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
