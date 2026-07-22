from pathlib import Path

from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.storage import Storage


def test_config_round_trip_without_api_hash(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(api_id=12345, phone="+8613800000000")
    config.save(path)
    text = path.read_text(encoding="utf-8")
    assert "api_hash" not in text
    assert AppConfig.load(path) == config


def test_storage_rules_and_download_deduplication(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3")
    storage.save_rule(-100123, "测试群", True, "D:/Videos")
    rule = storage.get_rule(-100123)
    assert rule is not None
    assert rule["enabled"] == 1
    assert rule["enabled_at"]
    assert -100123 in storage.enabled_rules()

    assert not storage.is_downloaded(-100123, 77)
    storage.record_download(-100123, 77, "D:/Videos/a.mp4", 10, "completed")
    assert storage.is_downloaded(-100123, 77)
    storage.record_download(-100123, 77, "", 0, "failed")
    assert not storage.is_downloaded(-100123, 77)
