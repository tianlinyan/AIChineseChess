"""中国象棋 AI 对弈 — 入口"""

import sys
import os

# 在导入其他模块前加载 .env（使 models.json 中的 ${VAR} 可用）
try:
    from dotenv import load_dotenv
    # PyInstaller 打包后 __file__ 指向 _MEIPASS 临时目录，
    # .env 应在 exe 旁（与 services/models.py 的定位逻辑一致）
    if getattr(sys, 'frozen', False):
        _base_dir = os.path.dirname(sys.executable)
    else:
        _base_dir = os.path.dirname(os.path.abspath(__file__))
    _env_path = os.path.join(_base_dir, '.env')
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装，忽略（用户可手动设环境变量）

from PyQt6.QtWidgets import QApplication

from ui.window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
