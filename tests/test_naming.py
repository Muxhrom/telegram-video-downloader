from datetime import datetime, timezone
from pathlib import Path

from telegram_video_downloader.naming import (
    choose_video_name,
    extension_for,
    sanitize_component,
    unique_target,
)


def test_sanitize_windows_illegal_and_reserved_names() -> None:
    assert sanitize_component('课件<第一讲>:"测试"?.mp4') == "课件_第一讲___测试__.mp4"
    assert sanitize_component("CON") == "_CON"
    assert sanitize_component("name. ") == "name"


def test_name_priority_and_fallback() -> None:
    date = datetime(2026, 7, 22, 8, 9, 10, tzinfo=timezone.utc)
    local_stamp = date.astimezone().strftime("%Y%m%d_%H%M%S")
    assert choose_video_name("原视频.MKV", "说明", date, 42, ".mkv") == "原视频.mkv"
    assert choose_video_name(None, "第一行\n第二行", date, 42, ".mp4") == "第一行.mp4"
    assert choose_video_name(None, "", date, 42, ".mp4") == f"video_{local_stamp}_42.mp4"


def test_extension_and_unique_target(tmp_path: Path) -> None:
    assert extension_for(None, "video/mp4") == ".mp4"
    first = unique_target(tmp_path, "video.mp4", 99)
    assert first.name == "video.mp4"
    first.write_bytes(b"x")
    assert unique_target(tmp_path, "video.mp4", 99).name == "video_99.mp4"


def test_unique_target_avoids_simultaneous_reserved_name(tmp_path: Path) -> None:
    reserved = {tmp_path / "video.mp4"}
    assert unique_target(tmp_path, "video.mp4", 100, reserved).name == "video_100.mp4"
