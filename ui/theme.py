"""UI 主题常量"""

WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 900
MIDDLE_PANEL_MIN_WIDTH = 600
SPLITTER_SIZES = [300, 620, 280]

DARK_THEME_QSS = """
    QMainWindow { background-color: #1a1a1a; color: #e0e0e0; }
    QWidget { background-color: #2d2d2d; color: #e0e0e0; }
    QGroupBox { border: 1px solid #444; margin-top: 10px; padding-top: 12px; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #7eb8da; }
    QTextEdit { background-color: #333; border: 1px solid #555; color: #ffffff; }
    QComboBox { background-color: #383838; border: 1px solid #555; color: #ffffff;
                padding: 4px 8px; border-radius: 3px; min-height: 24px; }
    QComboBox:hover { border-color: #6a6a6a; }
    QComboBox::drop-down { border: none; padding-right: 6px; }
    QComboBox QAbstractItemView { background-color: #383838; color: #e0e0e0;
                                   selection-background-color: #4a6fa5; border: 1px solid #555; }
    QPushButton { background-color: #4a6fa5; color: white; border: none;
                  border-radius: 4px; padding: 6px 12px; font-size: 12px; }
    QPushButton:hover { background-color: #3a5a8a; }
    QPushButton:disabled { background-color: #444; color: #777; }
    QCheckBox { color: #d0d0d0; spacing: 6px; }
    QCheckBox::indicator { width: 15px; height: 15px; }
    QLabel { color: #e0e0e0; }
"""
