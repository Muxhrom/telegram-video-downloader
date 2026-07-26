import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo

from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.paths import AppPaths
from telegram_video_downloader.telegram_service import TelegramWorker


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        data_dir=root / "data",
        config_file=root / "data" / "config.json",
        database_file=root / "data" / "state.sqlite3",
        session_file=root / "data" / "telegram.session",
        log_file=root / "data" / "app.log",
        default_download_dir=root / "downloads",
        tools_dir=root / "data" / "tools",
        openlist_dir=root / "data" / "tools" / "openlist",
        upload_staging_dir=root / "data" / "upload_staging",
        compression_staging_dir=root / "data" / "compression_staging",
    )


def media_message(
    message_id: int,
    attributes: list,
    mime: str = "video/mp4",
    name: str | None = None,
):
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 7, 22, 12, message_id % 60, tzinfo=timezone.utc),
        message=f"message {message_id}",
        document=SimpleNamespace(
            attributes=attributes, mime_type=mime, size=message_id, thumbs=[object()]
        ),
        file=SimpleNamespace(name=name or f"{message_id}.mp4"),
    )


class FakeClient:
    def __init__(self) -> None:
        self.flood_sleep_threshold = 60
        normal = media_message(300, [DocumentAttributeVideo(1, 10, 10)])
        self.messages = {
            "InputMessagesFilterVideo": [normal],
            "InputMessagesFilterRoundVideo": [
                normal,
                media_message(
                    250,
                    [DocumentAttributeVideo(1, 10, 10, round_message=True)],
                ),
            ],
            "InputMessagesFilterDocument": [
                media_message(200, [], "video/x-matroska", "clip.mkv"),
                media_message(
                    150,
                    [DocumentAttributeAnimated(), DocumentAttributeVideo(1, 10, 10)],
                ),
                media_message(100, [], "application/pdf", "file.pdf"),
            ],
        }
        self.calls: list[tuple[str, int]] = []
        self.download_calls: list[tuple[int, int]] = []

    async def get_entity(self, chat_id: int):
        return SimpleNamespace(id=chat_id, title="测试群")

    async def get_input_entity(self, chat_id: int):
        return SimpleNamespace(id=chat_id)

    async def get_messages(self, entity, ids: int):
        del entity
        for messages in self.messages.values():
            for message in messages:
                if message.id == ids:
                    return message
        return None

    async def download_media(self, message, file, thumb: int):
        assert file is bytes
        self.download_calls.append((int(message.id), int(thumb)))
        return b"fake-jpeg-thumbnail"

    async def iter_messages(self, entity, limit: int, offset_id: int, filter):
        del entity
        name = type(filter).__name__
        self.calls.append((name, offset_id))
        for item in self.messages[name]:
            if not offset_id or item.id < offset_id:
                yield item


def test_server_filters_merge_dedupe_and_exclude_animation(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = TelegramWorker(paths, AppConfig())
    client = FakeClient()
    worker.client = client
    events: list[tuple] = []
    worker.videos_ready.connect(lambda *args: events.append(args))

    large_chat_id = -1002985557858
    asyncio.run(worker._load_video_page(7, large_chat_id, {}, 100))

    chunks = [video for event in events for video in event[2]]
    assert all(event[1] == large_chat_id for event in events)
    assert {video["message_id"] for video in chunks} == {300, 250, 200}
    assert [video["message_id"] for video in chunks].count(300) == 1
    assert {name for name, _ in client.calls} == {
        "InputMessagesFilterVideo",
        "InputMessagesFilterRoundVideo",
        "InputMessagesFilterDocument",
    }
    final_state = events[-1][3]
    assert final_state["cursors"] == {"video": 300, "round": 250, "document": 100}
    assert final_state["reached_end"] is True
    assert events[-1][4] is True


def test_filter_cursors_are_resumed_without_raw_message_limit(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = TelegramWorker(paths, AppConfig())
    client = FakeClient()
    worker.client = client
    state = {
        "cursors": {"video": 5000, "round": 4000, "document": 3000},
        "exhausted": {"video": False, "round": False, "document": False},
        "pending": [],
        "seen_ids": [],
    }

    asyncio.run(worker._load_video_page(8, -1002985557858, state, 100))

    assert client.calls == [
        ("InputMessagesFilterVideo", 5000),
        ("InputMessagesFilterRoundVideo", 4000),
        ("InputMessagesFilterDocument", 3000),
    ]


def test_thumbnail_loader_uses_telegram_thumb_and_local_cache(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = TelegramWorker(paths, AppConfig())
    client = FakeClient()
    worker.client = client
    chat_id = -1002985557858
    message = client.messages["InputMessagesFilterVideo"][0]
    key = (chat_id, int(message.id))
    worker._thumbnail_request_id = 7
    worker._thumbnail_messages[key] = message
    events: list[tuple] = []
    worker.thumbnail_ready.connect(lambda *args: events.append(args))
    item = {"message_id": message.id}

    asyncio.run(worker.load_video_thumbnails(7, chat_id, [item]))

    assert events[-1][0:3] == (7, chat_id, message.id)
    assert events[-1][3] == b"fake-jpeg-thumbnail"
    assert events[-1][4] == ""
    assert client.download_calls == [(message.id, -1)]
    cache = paths.data_dir / "thumbnails" / str(chat_id) / f"{message.id}.thumb"
    assert cache.read_bytes() == b"fake-jpeg-thumbnail"

    asyncio.run(worker.load_video_thumbnails(7, chat_id, [item]))
    assert client.download_calls == [(message.id, -1)]


def test_download_directory_names_and_priority_queue(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    nested = tmp_path / "downloads" / "nested"
    nested.mkdir(parents=True)
    (nested / "Already.MP4").write_bytes(b"video")
    (nested / "unfinished.mp4.part").write_bytes(b"partial")
    (nested / "note.txt").write_text("not video", encoding="utf-8")
    assert TelegramWorker._collect_video_names(tmp_path / "downloads") == {
        "already.mp4"
    }

    worker = TelegramWorker(paths, AppConfig())
    first = {
        "chat_id": -1002985557858,
        "message_id": 1,
        "chat_title": "群",
        "name": "first.mp4",
        "size": 10,
    }
    second = dict(first, message_id=2, name="second.mp4")

    async def scenario() -> tuple:
        worker._queue = asyncio.PriorityQueue()
        await worker.enqueue_downloads([first, second], str(tmp_path / "downloads"))
        await worker.set_download_priority(
            [(second["chat_id"], second["message_id"])], 0
        )
        return await worker._queue.get()

    priority, _, version, key = asyncio.run(scenario())
    assert priority == 0
    assert key == (second["chat_id"], second["message_id"])
    assert version == worker._queue_versions[key]


def test_safe_acceleration_can_enable_and_auto_fallback(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = TelegramWorker(paths, AppConfig())
    changes: list[tuple[bool, str]] = []
    worker.acceleration_changed.connect(
        lambda enabled, reason: changes.append((enabled, reason))
    )

    async def scenario() -> None:
        worker._acceleration_event = asyncio.Event()
        await worker.set_acceleration_mode(True)
        assert worker._acceleration_event.is_set()
        worker._disable_acceleration_after_error(ConnectionError("proxy reset"))
        assert not worker._acceleration_event.is_set()

    asyncio.run(scenario())
    assert changes[0][0] is True
    assert "3 个文件" in changes[0][1]
    assert changes[-1][0] is False
    assert "自动关闭" in changes[-1][1]
