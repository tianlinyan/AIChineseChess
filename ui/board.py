"""棋盘绘制控件"""

from PyQt6.QtCore import (
    Qt, QRectF, QPointF, QBuffer, QByteArray, QSize, pyqtSignal,
)
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QRadialGradient,
    QMouseEvent,
)
from PyQt6.QtWidgets import QWidget, QSizePolicy

from domain.constants import VISION_IMAGE_QUALITY, VISION_IMAGE_MAX_WIDTH, PIECE_SYMBOLS
from domain.game import ChineseChessGame


class BoardWidget(QWidget):
    """中国象棋棋盘控件 — 绘制、点击选子、截图"""

    move_made = pyqtSignal(int, int, int, int)  # from_row, from_col, to_row, to_col

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game = None
        self.padding = 60
        self.cell_size = 0
        self.setFixedSize(590, 650)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.selected_row = -1
        self.selected_col = -1

    def set_game(self, game):
        self.game = game
        self.update()

    def paintEvent(self, event):
        if not self.game:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        self.cell_size = min((w - 2 * self.padding) / (self.game.size_cols - 1),
                             (h - 2 * self.padding) / (self.game.size_rows - 1))

        # 木色背景
        painter.fillRect(0, 0, w, h, QColor('#bE9867'))

        # 横线
        painter.setPen(QPen(QColor('#4B5A2B'), 1.5))
        for i in range(self.game.size_rows):
            y = self.padding + i * self.cell_size
            painter.drawLine(QPointF(self.padding, y), QPointF(w - self.padding, y))

        # 竖线（分上下两段，河界不画；最左最右列为贯通全盘的边线）
        top_max_y = self.padding + 4 * self.cell_size
        bottom_min_y = self.padding + 5 * self.cell_size
        bottom_max_y = self.padding + (self.game.size_rows - 1) * self.cell_size
        for j in range(self.game.size_cols):
            x = self.padding + j * self.cell_size
            if j == 0 or j == self.game.size_cols - 1:
                # 左列A和右列I是贯通全盘的边线
                painter.drawLine(QPointF(x, self.padding), QPointF(x, bottom_max_y))
            else:
                painter.drawLine(QPointF(x, self.padding), QPointF(x, top_max_y))
                painter.drawLine(QPointF(x, bottom_min_y), QPointF(x, bottom_max_y))

        # 河界文字
        painter.setPen(QPen(QColor('#000000'), 2))
        painter.setFont(QFont('KaiTi', int(self.cell_size * 0.4)))
        painter.drawText(QRectF(self.padding, self.padding + 4 * self.cell_size,
                                w - 2 * self.padding, self.cell_size),
                         Qt.AlignmentFlag.AlignCenter, "楚河    汉界")

        # 九宫斜线
        painter.setPen(QPen(QColor('#4B5A2B'), 1.5))
        # 黑方九宫（行0-2，列3-5）
        x_left = self.padding + 3 * self.cell_size
        x_right = self.padding + 5 * self.cell_size
        y_top = self.padding + 0 * self.cell_size
        y_bottom = self.padding + 2 * self.cell_size
        painter.drawLine(QPointF(x_left, y_top), QPointF(x_right, y_bottom))
        painter.drawLine(QPointF(x_right, y_top), QPointF(x_left, y_bottom))
        # 红方九宫（行7-9，列3-5）
        y_top = self.padding + 7 * self.cell_size
        y_bottom = self.padding + 9 * self.cell_size
        painter.drawLine(QPointF(x_left, y_top), QPointF(x_right, y_bottom))
        painter.drawLine(QPointF(x_right, y_top), QPointF(x_left, y_bottom))

        # 行列标签
        painter.setFont(QFont('Arial', max(9, int(self.cell_size * 0.25))))
        painter.setPen(QPen(QColor('#4B5A2B')))
        for i in range(self.game.size_rows):
            y = self.padding + i * self.cell_size
            painter.drawText(QRectF(8, y - self.cell_size / 2, self.padding - 16, self.cell_size),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(i + 1))
            painter.drawText(QRectF(w - self.padding + 8, y - self.cell_size / 2, self.padding - 16, self.cell_size),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(i + 1))
        for j in range(self.game.size_cols):
            x = self.padding + j * self.cell_size
            letter = chr(65 + j)
            painter.drawText(QRectF(x - self.cell_size / 2, 8, self.cell_size, self.padding - 16),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, letter)
            painter.drawText(QRectF(x - self.cell_size / 2, h - self.padding + 8, self.cell_size, self.padding - 16),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, letter)

        # 棋子
        for i in range(self.game.size_rows):
            for j in range(self.game.size_cols):
                piece = self.game.board[i][j]
                if piece != '.':
                    self._draw_piece(painter, i, j, piece)

        # 最后移动标记
        if self.game.last_move:
            fr, fc, tr, tc, _ = self.game.last_move
            x1 = self.padding + fc * self.cell_size
            y1 = self.padding + fr * self.cell_size
            painter.setPen(QPen(QColor('#f1c40f'), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(x1, y1), self.cell_size * 0.3, self.cell_size * 0.3)
            x2 = self.padding + tc * self.cell_size
            y2 = self.padding + tr * self.cell_size
            painter.setPen(QPen(QColor('#f1c40f'), 2))
            painter.drawEllipse(QPointF(x2, y2), self.cell_size * 0.3, self.cell_size * 0.3)

        # 选中棋子高亮
        if self.selected_row != -1 and self.selected_col != -1:
            x = self.padding + self.selected_col * self.cell_size
            y = self.padding + self.selected_row * self.cell_size
            painter.setPen(QPen(QColor('#00ff00'), 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(x, y), self.cell_size * 0.3, self.cell_size * 0.3)

    def _draw_piece(self, painter, row, col, piece):
        x = self.padding + col * self.cell_size
        y = self.padding + row * self.cell_size
        r = self.cell_size * 0.4

        # 阴影
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawEllipse(QPointF(x + 2, y + 2), r, r)

        # 渐变填充
        if piece.isupper():  # 红方
            gradient = QRadialGradient(x - r * 0.3, y - r * 0.3, r * 1.2)
            gradient.setColorAt(0, QColor('#c73030'))
            gradient.setColorAt(1, QColor('#603030'))
        else:  # 黑方
            gradient = QRadialGradient(x - r * 0.3, y - r * 0.3, r * 1.2)
            gradient.setColorAt(0, QColor('#707070'))
            gradient.setColorAt(1, QColor('#202020'))
        painter.setBrush(gradient)
        painter.drawEllipse(QPointF(x, y), r, r)

        # 文字
        painter.setPen(QPen(QColor('#ffffff')))
        painter.setFont(QFont('KaiTi', int(r * 0.8)))
        painter.drawText(QRectF(x - r, y - r, 2 * r, 2 * r),
                         Qt.AlignmentFlag.AlignCenter,
                         PIECE_SYMBOLS.get(piece, piece))

    def mousePressEvent(self, event: QMouseEvent):
        if not self.game or self.game.game_over:
            return
        x = event.position().x()
        y = event.position().y()
        if (x < self.padding or x > self.width() - self.padding or
                y < self.padding or y > self.height() - self.padding):
            return
        col = round((x - self.padding) / self.cell_size) if self.cell_size > 0 else -1
        row = round((y - self.padding) / self.cell_size) if self.cell_size > 0 else -1
        if 0 <= row < self.game.size_rows and 0 <= col < self.game.size_cols:
            if self.selected_row != -1 and self.selected_col != -1:
                if row == self.selected_row and col == self.selected_col:
                    self.selected_row = -1
                    self.selected_col = -1
                    self.update()
                else:
                    if self.move_made:
                        self.move_made.emit(self.selected_row, self.selected_col, row, col)
                    self.selected_row = -1
                    self.selected_col = -1
            else:
                piece = self.game.board[row][col]
                if piece != '.' and self.game.get_piece_owner(piece) == self.game.current_player:
                    self.selected_row = row
                    self.selected_col = col
                    self.update()
                else:
                    self.selected_row = -1
                    self.selected_col = -1
                    self.update()

    def capture_board_image(self):
        pixmap = self.grab()
        if pixmap.isNull():
            return ""

        # 缩放到最大宽度（保持宽高比）
        if VISION_IMAGE_MAX_WIDTH > 0 and pixmap.width() > VISION_IMAGE_MAX_WIDTH:
            new_size = QSize(VISION_IMAGE_MAX_WIDTH,
                             int(pixmap.height() * VISION_IMAGE_MAX_WIDTH / pixmap.width()))
            pixmap = pixmap.scaled(
                new_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        ba = QByteArray()
        buffer = QBuffer(ba)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, 'JPEG', quality=VISION_IMAGE_QUALITY)
        buffer.close()
        return ba.toBase64().data().decode()
