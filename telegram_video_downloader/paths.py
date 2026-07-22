from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "TelegramVideoDownloader"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    config_file: Path
    database_file: Path
    session_file: Path
    log_file: Path
    default_download_dir: Path
    tools_dir: Path
    openlist_dir: Path
    upload_staging_dir: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        data_dir = local_app_data / APP_NAME
        downloads = Path.home() / "Downloads" / "Telegram Video Downloader"
        return cls(
            data_dir=data_dir,
            config_file=data_dir / "config.json",
            database_file=data_dir / "state.sqlite3",
            session_file=data_dir / "telegram.session",
            log_file=data_dir / "app.log",
            default_download_dir=downloads,
            tools_dir=data_dir / "tools",
            openlist_dir=data_dir / "tools" / "openlist",
            upload_staging_dir=data_dir / "upload_staging",
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.default_download_dir.mkdir(parents=True, exist_ok=True)
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.openlist_dir.mkdir(parents=True, exist_ok=True)
        self.upload_staging_dir.mkdir(parents=True, exist_ok=True)
