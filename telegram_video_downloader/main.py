from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .config import AppConfig
from .paths import AppPaths
from .ui import MainWindow


APP_STYLE = """
QWidget {
    color: #17212b;
    font-size: 14px;
}
QMainWindow, QDialog {
    background: #f4f7f8;
}
QLineEdit, QDateEdit, QSpinBox, QListWidget, QTableWidget {
    background: #ffffff;
    border: 1px solid #d7e0e3;
    border-radius: 7px;
    padding: 5px;
    selection-background-color: #d9f3f2;
    selection-color: #0f4546;
}
QLineEdit:focus, QDateEdit:focus, QSpinBox:focus, QListWidget:focus, QTableWidget:focus {
    border: 1px solid #15999b;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #cbd6da;
    border-radius: 7px;
    padding: 7px 13px;
    min-height: 20px;
}
QPushButton:hover {
    background: #edf8f7;
    border-color: #63b9b7;
}
QPushButton:pressed {
    background: #d8efee;
}
QPushButton:disabled {
    color: #9aa4aa;
    background: #eef1f2;
}
QPushButton#primaryButton {
    color: #ffffff;
    background: #118c8e;
    border-color: #118c8e;
    font-weight: 600;
}
QPushButton#primaryButton:hover {
    background: #0d7779;
}
QHeaderView::section {
    background: #e8f0f1;
    color: #29434a;
    border: 0;
    border-right: 1px solid #d3dddf;
    border-bottom: 1px solid #cbd6da;
    padding: 8px 6px;
    font-weight: 600;
}
QTableWidget {
    gridline-color: #e5ebed;
    alternate-background-color: #f8fbfb;
}
QListWidget::item {
    padding: 8px;
    border-radius: 5px;
}
QListWidget::item:selected {
    background: #d9f3f2;
    color: #0f4546;
}
QProgressBar {
    background: #e6ecee;
    border: 0;
    border-radius: 6px;
    text-align: center;
    min-height: 18px;
}
QProgressBar::chunk {
    background: #19a3a5;
    border-radius: 6px;
}
QLabel#summaryLabel {
    background: #e6f5f4;
    color: #155e60;
    border: 1px solid #b9dfdd;
    border-radius: 7px;
    padding: 8px 12px;
    font-weight: 600;
}
QCheckBox {
    spacing: 7px;
}
"""


def configure_logging(paths: AppPaths) -> None:
    logging.basicConfig(
        filename=paths.log_file,
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    paths = AppPaths.discover()
    paths.ensure()
    configure_logging(paths)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    app.setApplicationName("Telegram 视频下载器")
    app.setOrganizationName("Local")
    app.setQuitOnLastWindowClosed(False)
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    icon_path = resource_root / "assets" / "app-icon.png"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    config = AppConfig.load(paths.config_file)
    window = MainWindow(paths, config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
