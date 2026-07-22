from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .config import AppConfig
from .paths import AppPaths
from .ui import MainWindow


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
