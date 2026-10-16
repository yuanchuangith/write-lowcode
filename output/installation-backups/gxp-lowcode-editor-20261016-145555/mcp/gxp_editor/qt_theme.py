"""Shared, light Qt styling for the local approval application."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

_application: QApplication | None = None

STYLE = """
QWidget { color: #1d1d1f; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog { background: #f5f5f7; }
QLabel { background: transparent; }
QLabel#eyebrow { color: #86868b; font-size: 11px; font-weight: 600; }
QLabel#title { font-size: 27px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #6e6e73; }
QLabel#badge { background: #e8f2ff; color: #0066cc; border-radius: 12px; padding: 6px 12px; font-weight: 600; }
QLabel#metricValue { font-size: 20px; font-weight: 650; }
QFrame#card { background: white; border: 1px solid #e8e8ed; border-radius: 14px; }
QFrame#footer { background: white; border-top: 1px solid #e5e5ea; }
QPushButton { background: #ededf0; border: none; border-radius: 9px; padding: 9px 16px; font-weight: 600; }
QPushButton:hover { background: #e2e2e7; }
QPushButton:pressed { background: #d5d5dc; }
QPushButton:focus { border: 2px solid #91c3ff; padding: 7px 14px; }
QPushButton:disabled { color: #9a9aa1; background: #f0f0f3; }
QPushButton#primary { background: #007aff; color: white; }
QPushButton#primary:hover { background: #0069dd; }
QPushButton#primary:disabled { background: #dceaff; color: #87aede; }
QPushButton#approve { background: #e8f2ff; color: #0066cc; }
QPushButton#approve:hover { background: #d8eaff; }
QPushButton#approve:disabled { background: #f0f0f3; color: #9a9aa1; }
QTabWidget::pane { background: white; border: 1px solid #e5e5ea; border-radius: 12px; top: -1px; }
QTabBar::tab { background: transparent; color: #6e6e73; padding: 10px 16px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #007aff; border-bottom: 2px solid #007aff; font-weight: 600; }
QTabBar::tab:hover { color: #007aff; background: #ecf3ff; }
QTabBar QToolButton { background: #f5f5f7; border: none; }
QTreeWidget, QListWidget, QPlainTextEdit, QTextBrowser { background: white; border: none; border-radius: 8px; selection-background-color: #dcecff; selection-color: #123c6a; }
QTreeWidget::item, QListWidget::item { padding: 8px 6px; border-radius: 6px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #e8f2ff; color: #0066cc; }
QListWidget#statements::item { padding: 13px 10px; margin: 3px 4px; }
QHeaderView::section { background: #f8f8fa; border: none; border-bottom: 1px solid #e8e8ed; padding: 8px; font-weight: 600; }
QSplitter::handle { background: #f5f5f7; width: 8px; height: 8px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c7c7cc; border-radius: 4px; min-height: 28px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c7c7cc; border-radius: 4px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QProgressBar { background: #ededf0; border: none; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #007aff; border-radius: 3px; }
QCheckBox { spacing: 7px; }
QToolTip { background: #1d1d1f; color: white; border: none; padding: 8px; }
"""


def application() -> QApplication:
    global _application
    _application = QApplication.instance() or QApplication(sys.argv[:1])
    if _application.property("gxpThemeReady"):
        return _application
    # Offscreen Qt on Windows does not discover system fonts automatically.
    # Register the same installed fonts for screenshots and native windows.
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
        path = fonts / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    _application.setApplicationName("GXP 草稿审批")
    _application.setFont(QFont("Microsoft YaHei UI", 10))
    _application.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, "#f5f5f7"),
                        (QPalette.ColorRole.Base, "#ffffff"),
                        (QPalette.ColorRole.Text, "#1d1d1f"),
                        (QPalette.ColorRole.WindowText, "#1d1d1f"),
                        (QPalette.ColorRole.Highlight, "#dcecff"),
                        (QPalette.ColorRole.HighlightedText, "#123c6a")):
        palette.setColor(role, QColor(color))
    _application.setPalette(palette)
    _application.setStyleSheet(STYLE)
    _application.setProperty("gxpThemeReady", True)
    return _application


def label(text: str = "", name: str = "", *, wrap: bool = False) -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setObjectName(name)
    result.setWordWrap(wrap)
    result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return result


def text_view(content: str = "", *, code: bool = False) -> QPlainTextEdit:
    result = QPlainTextEdit()
    result.setReadOnly(True)
    result.setPlainText(content)
    result.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap if code else QPlainTextEdit.LineWrapMode.WidgetWidth)
    if code:
        result.setFont(QFont("Consolas", 10))
    result.document().setDocumentMargin(16)
    return result
