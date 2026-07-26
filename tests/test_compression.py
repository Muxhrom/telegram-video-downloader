import asyncio
from pathlib import Path

import pytest

from telegram_video_downloader.compression_service import CompressionWorker
from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.paths import AppPaths
from telegram_video_downloader.storage import Storage


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


class FakeStream:
    def __init__(self, lines: list[bytes] | None = None, payload: bytes = b"") -> None:
        self.lines = lines or []
        self.payload = payload

    def __aiter__(self):
        self._iterator = iter(self.lines)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def read(self) -> bytes:
        return self.payload


class FakeProcess:
    returncode = 0

    def __init__(self, output: bytes) -> None:
        self.stdout = FakeStream([b"out_time_ms=1000000\n", b"progress=end\n"])
        self.stderr = FakeStream(payload=b"")
        self.output = output

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.parametrize(("profile", "crf"), [("high", "20"), ("balanced", "24"), ("compact", "28")])
def test_compression_profile_maps_to_x265_crf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, crf: str) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x" * 100)
    storage = Storage(paths.database_file)
    storage.record_download(-100, 1, str(source), source.stat().st_size, "completed")
    worker = CompressionWorker(paths, AppConfig(ffmpeg_path=str(tmp_path / "ffmpeg.exe")))
    (tmp_path / "ffmpeg.exe").write_bytes(b"fake")
    storage.save_compression_job(-100, 1, str(source), 100, profile)
    worker._probe = lambda probe, path: asyncio.sleep(0, result=(1.0, True))  # type: ignore[method-assign]

    async def fake_exec(*args, **kwargs):
        del kwargs
        Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(args[-1]).write_bytes(b"y" * 40)
        return FakeProcess(b"y" * 40)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(worker._compress_one({"chat_id": -100, "message_id": 1, "name": source.name, "file_path": str(source), "size": 100, "profile": profile}))
    assert source.exists()
    assert source.read_bytes() == b"y" * 40
    record = storage.get_compression(-100, 1)
    assert record and record["status"] == "completed"
    args = []

    async def capture_exec(*values, **kwargs):
        del kwargs
        args.extend(str(value) for value in values)
        Path(values[-1]).write_bytes(b"z" * 40)
        return FakeProcess(b"z" * 40)

    # The first run already proves replacement; verify the profile independently through command construction.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_exec)
    source.write_bytes(b"x" * 100)
    storage.record_download(-100, 2, str(source), 100, "completed")
    storage.save_compression_job(-100, 2, str(source), 100, profile)
    asyncio.run(worker._compress_one({"chat_id": -100, "message_id": 2, "name": source.name, "file_path": str(source), "size": 100, "profile": profile}))
    assert [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-crf"] == [crf]
    assert "libx265" in args


def test_compression_keeps_original_when_result_is_not_smaller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x" * 40)
    storage = Storage(paths.database_file)
    storage.record_download(-100, 3, str(source), source.stat().st_size, "completed")
    worker = CompressionWorker(paths, AppConfig(ffmpeg_path=str(tmp_path / "ffmpeg.exe")))
    (tmp_path / "ffmpeg.exe").write_bytes(b"fake")
    storage.save_compression_job(-100, 3, str(source), 40, "balanced")
    worker._probe = lambda probe, path: asyncio.sleep(0, result=(1.0, True))  # type: ignore[method-assign]

    async def fake_exec(*args, **kwargs):
        del kwargs
        Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(args[-1]).write_bytes(b"z" * 100)
        return FakeProcess(b"z" * 100)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(worker._compress_one({"chat_id": -100, "message_id": 3, "name": source.name, "file_path": str(source), "size": 40, "profile": "balanced"}))
    assert source.read_bytes() == b"x" * 40
    record = storage.get_compression(-100, 3)
    assert record and record["status"] == "not_smaller"
