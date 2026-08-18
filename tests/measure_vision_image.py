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

# ── 内容覆盖回归检查：超采样必须铺满画布 ──
# capture_board_image 若退化为"1:1 渲染进左上角 + 其余黑边"，
# 右下角等区域将全黑（棋盘本体只占左上 1/scale²）。采样三个角落
# （棋盘控件背景为木色 #bE9867，红通道 ≈190，远大于黑边的 ≈0）。
from PyQt6.QtGui import QImage
_img = QImage()
if _img.loadFromData(raw, 'JPEG'):
    w_, h_ = _img.width(), _img.height()
    corners = {
        '左上': _img.pixelColor(2, 2),
        '右上': _img.pixelColor(w_ - 2, 2),
        '右下': _img.pixelColor(w_ - 2, h_ - 2),
    }
    all_ok = all(c.red() > 60 for c in corners.values())
    desc = ', '.join(f'{k}=({c.red()},{c.green()},{c.blue()})'
                     for k, c in corners.items())
    print(f"[覆盖] {desc}")
    if not all_ok:
        print('[FAIL] 截图存在黑边 — 超采样未铺满画布（见 capture_board_image）')
        sys.exit(1)
    print('[PASS] 截图内容铺满画布（无黑边）')
else:
    print('[FAIL] 无法解码 JPEG')
    sys.exit(1)

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
