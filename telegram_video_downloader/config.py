from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    api_id: int | None = None
    phone: str = ""
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 7890
    proxy_type: str = "socks5"
    max_concurrent_downloads: int = 2
    cloud_enabled: bool = False
    cloud_auto_upload: bool = True
    openlist_port: int = 5244
    openlist_mount: str = "aliyun-drive"
    cloud_root: str = "Telegram视频下载器"
    ffmpeg_path: str = ""
    auto_compress: bool = False
    compression_profile: str = "balanced"

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            allowed = cls.__dataclass_fields__.keys()
            return cls(**{key: value for key, value in data.items() if key in allowed})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)
