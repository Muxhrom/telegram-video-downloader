import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_video_downloader.parallel_download import download_document_parallel
from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.paths import AppPaths
from telegram_video_downloader.telegram_service import TelegramWorker


def test_parallel_download_writes_exact_bytes_and_uses_multiple_streams(tmp_path: Path) -> None:
    chunk_size = 512 * 1024
    original = bytes(range(256)) * (chunk_size * 8 // 256 + 13)
    active = 0
    peak_active = 0
    offsets = []
    progress = []

    class FakeStream:
        def __init__(self, offset: int):
            self.offset = offset

        def __aiter__(self):
            return self

        async def __anext__(self):
            nonlocal active, peak_active
            if self.offset >= len(original):
                raise StopAsyncIteration
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            result = original[self.offset:self.offset + chunk_size]
            self.offset += len(result)
            active -= 1
            return result

        async def close(self):
            pass

    class FakeClient:
        def iter_download(self, document, *, offset, request_size, chunk_size, file_size, limit):
            assert document is original
            assert request_size == chunk_size == 512 * 1024
            assert file_size == len(original)
            offsets.append(offset)
            return FakeStream(offset)

    async def scenario():
        ready = asyncio.Event()
        ready.set()
        await download_document_parallel(
            FakeClient(), original, tmp_path / "video.part", len(original),
            lambda current, total: progress.append((current, total)), ready,
            stream_count=4,
        )

    asyncio.run(scenario())
    assert (tmp_path / "video.part").read_bytes() == original
    assert len(offsets) == 4
    assert peak_active > 1
    assert progress[-1] == (len(original), len(original))


def test_parallel_download_rejects_incomplete_stream(tmp_path: Path) -> None:
    size = 4 * 512 * 1024

    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            pass

    class FakeClient:
        def iter_download(self, *_args, **_kwargs):
            return EmptyStream()

    async def scenario():
        ready = asyncio.Event()
        ready.set()
        await download_document_parallel(
            FakeClient(), object(), tmp_path / "incomplete.part", size,
            lambda *_args: None, ready,
        )

    with pytest.raises(RuntimeError, match="ended early"):
        asyncio.run(scenario())


def test_worker_uses_parallel_transfer_for_large_document(tmp_path: Path, monkeypatch) -> None:
    paths = AppPaths(
        data_dir=tmp_path / "data", config_file=tmp_path / "data" / "config.json",
        database_file=tmp_path / "data" / "state.sqlite3",
        session_file=tmp_path / "data" / "telegram.session",
        log_file=tmp_path / "data" / "app.log",
        default_download_dir=tmp_path / "downloads", tools_dir=tmp_path / "data" / "tools",
        openlist_dir=tmp_path / "data" / "tools" / "openlist",
        upload_staging_dir=tmp_path / "data" / "upload_staging",
        compression_staging_dir=tmp_path / "data" / "compression_staging",
    )
    paths.ensure()
    worker = TelegramWorker(paths, AppConfig())
    document = SimpleNamespace(size=33 * 1024 * 1024)
    message = SimpleNamespace(document=document)

    class FakeClient:
        async def get_entity(self, _chat_id):
            return object()

        async def get_messages(self, _entity, ids):
            assert ids == 7
            return message

    worker.client = FakeClient()
    monkeypatch.setattr(worker, "_require_authorized", lambda: None)
    monkeypatch.setattr(worker, "_video_info", lambda *_args: object())
    called = []

    async def fake_parallel(client, source, path, size, progress, pause_event):
        assert client is worker.client and source is document
        assert size == document.size and pause_event is worker._pause_event
        called.append(True)
        path.write_bytes(b"complete")
        await progress(8, 8)

    monkeypatch.setattr(
        "telegram_video_downloader.telegram_service.download_document_parallel",
        fake_parallel,
    )

    async def scenario():
        worker._pause_event = asyncio.Event()
        worker._pause_event.set()
        await worker._download_one(
            {"chat_id": -1001, "message_id": 7, "chat_title": "Test", "name": "movie.mp4"},
            tmp_path / "downloads",
        )

    asyncio.run(scenario())
    assert called
    assert (tmp_path / "downloads" / "movie.mp4").read_bytes() == b"complete"
