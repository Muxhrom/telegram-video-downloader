from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo

from telegram_video_downloader.telegram_service import TelegramWorker


def message(attributes: list, mime: str = "video/mp4", name: str | None = "demo.mp4"):
    return SimpleNamespace(
        id=8,
        date=datetime(2026, 7, 22, tzinfo=timezone.utc),
        message="演示",
        document=SimpleNamespace(attributes=attributes, mime_type=mime, size=123),
        file=SimpleNamespace(name=name),
    )


def test_normal_video_and_round_video_are_included() -> None:
    normal = TelegramWorker._video_info(
        message([DocumentAttributeVideo(1, 10, 10)]), -1, "群"
    )
    round_video = TelegramWorker._video_info(
        message([DocumentAttributeVideo(1, 10, 10, round_message=True)], name=None),
        -1,
        "群",
    )
    assert normal and normal.media_kind == "普通视频"
    assert round_video and round_video.media_kind == "圆形视频"


def test_video_document_is_included_but_animation_is_excluded() -> None:
    document = TelegramWorker._video_info(message([], "video/x-matroska", "x.mkv"), -1, "群")
    animation = TelegramWorker._video_info(
        message([DocumentAttributeAnimated(), DocumentAttributeVideo(1, 10, 10)]), -1, "群"
    )
    assert document and document.media_kind == "视频文件"
    assert animation is None
