"""测量视觉模式截图 JPEG 大小（普通脚本，非 pytest）"""
import os
import base64
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QByteArray, QBuffer, QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt

from domain.constants import VISION_IMAGE_QUALITY
from domain.game import ChineseChessGame
from ui.board import BoardWidget

app = QApplication([])
game = ChineseChessGame()
board = BoardWidget()
board.set_game(game)
board.show()

# ── 当前（放大一倍后）──
b64 = board.capture_board_image()
raw = base64.b64decode(b64)
print(f"[当前] 缩放后尺寸未知，JPEG: {len(raw)} bytes = {len(raw)/1024:.1f} kB, base64 {len(b64)/1024:.1f} kB")

# 输出实际像素尺寸
pix = QPixmap()
pix.loadFromData(raw, 'JPEG')
print(f"[当前] 实际像素: {pix.width()}x{pix.height()}")

# ── 旧逻辑模拟：无超采样 + 300px 上限 ──
pix_old = QPixmap(board.size())
board.render(pix_old)
if pix_old.width() > 300:
    new_size = QSize(300, int(pix_old.height() * 300 / pix_old.width()))
    pix_old = pix_old.scaled(new_size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
ba = QByteArray()
buf = QBuffer(ba)
buf.open(QBuffer.OpenModeFlag.WriteOnly)
pix_old.save(buf, 'JPEG', quality=VISION_IMAGE_QUALITY)
buf.close()
print(f"[旧] 300px: {len(ba.data())} bytes = {len(ba.data())/1024:.1f} kB")
