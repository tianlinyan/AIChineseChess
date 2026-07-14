"""UI 主题常量"""

WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 900
LEFT_PANEL_MIN_WIDTH = 40
MIDDLE_PANEL_MIN_WIDTH = 600
SPLITTER_SIZES = [280, 600, 280]

DARK_THEME_QSS = """
    QMainWindow { background-color: #1a1a1a; color: #e0e0e0; }
    QWidget { background-color: #2d2d2d; color: #e0e0e0; }
    QGroupBox { border: 1px solid #444; margin-top: 0.5em; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
    QTextEdit, QComboBox { background-color: #333; border: 1px solid #555; color: #ffffff; }
    QPushButton { background-color: #4a6fa5; color: white; border: none; padding: 5px; }
    QPushButton:hover { background-color: #3a5a8a; }
    QPushButton:disabled { background-color: #555; }
    QLabel { color: #e0e0e0; }
"""
