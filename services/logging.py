from PyQt6.QtCore import QDateTime
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QTextEdit

LOG_COLORS = {
    'PLAYER': '#56b6c2',
    'DEALER': '#c678dd',
    'SYSTEM': '#98c379',
    'CHIPS': '#e5c07b',
    'ACTION': '#61afef',
    'ERROR': '#e06c75',
    'WARNING': '#d19a66',
    'INFO': '#abb2bf',
    'red': '#e06c75',       # 红方日志
    'black': '#61afef',     # 黑方日志
    'DEFAULT': '#abb2bf',
}


class LogManager:
    """HTML 彩色日志管理器"""

    def __init__(self) -> None:
        self._widget: QTextEdit | None = None

    def set_widget(self, widget: QTextEdit) -> None:
        self._widget = widget

    def clear(self) -> None:
        if self._widget:
            self._widget.clear()

    def log(self, message: str, level: str = 'INFO') -> None:
        if not self._widget:
            return
        color = LOG_COLORS.get(level.upper(), LOG_COLORS['DEFAULT'])
        timestamp = QDateTime.currentDateTime().toString('hh:mm:ss')
        html = f'<span style="color:#888">{timestamp}</span> '
        html += f'<span style="color:{color}">{message}</span><br>'
        self._widget.moveCursor(QTextCursor.MoveOperation.End)
        self._widget.textCursor().insertHtml(html)
        self._widget.moveCursor(QTextCursor.MoveOperation.End)
