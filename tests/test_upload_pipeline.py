import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.openlist_manager import OpenListManager
from telegram_video_downloader.paths import AppPaths
from telegram_video_downloader.storage import Storage
from telegram_video_downloader.upload_service import UploadRateTracker, UploadWorker


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
    )


def test_upload_record_survives_local_file_deletion(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    storage = Storage(paths.database_file)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    storage.record_download(-1001, 9, str(source), source.stat().st_size, "completed")
    storage.save_upload_job(-1001, 9, "测试群", str(source), 5)
    storage.update_upload(
        -1001,
        9,
        "completed",
        "上传并校验完成",
        "video.mp4",
        "/aliyun-drive/video.mp4",
        5,
        "etag",
    )
    source.unlink()

    download = storage.get_download(-1001, 9)
    upload = storage.get_upload(-1001, 9)
    assert download and download["status"] == "completed"
    assert not Path(download["file_path"]).exists()
    assert upload and upload["status"] == "completed"
    assert upload["remote_path"] == "/aliyun-drive/video.mp4"


def test_propfind_parses_remote_size_and_etag(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = UploadWorker(paths, AppConfig())
    body = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>
    <d:getcontentlength>12345</d:getcontentlength><d:getetag>"abc"</d:getetag>
    </d:prop></d:propstat></d:response></d:multistatus>"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(207, content=body, request=request)
    )

    async def scenario() -> dict | None:
        async with httpx.AsyncClient(transport=transport) as client:
            return await worker._propfind(client, ["aliyun-drive", "video.mp4"])

    assert asyncio.run(scenario()) == {"size": 12345, "etag": "abc"}


def test_upload_rate_tracker_ignores_short_initial_spike() -> None:
    now = [0.0]
    tracker = UploadRateTracker(clock=lambda: now[0])

    now[0] = 0.01
    assert tracker.update(1024 * 1024, 100 * 1024 * 1024) == (0.0, 0.0)

    now[0] = 1.01
    speed, eta = tracker.update(11 * 1024 * 1024, 100 * 1024 * 1024)
    assert speed == pytest.approx(11 * 1024 * 1024 / 1.01)
    assert eta == pytest.approx(89 * 1024 * 1024 / speed)


def test_upload_rate_tracker_hides_speed_during_cloud_commit() -> None:
    now = [0.0]
    tracker = UploadRateTracker(clock=lambda: now[0])

    now[0] = 2.0
    assert tracker.update(100, 100) == (0.0, 0.0)


def test_ensure_directories_does_not_recreate_existing_mount(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = UploadWorker(paths, AppConfig())
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "PROPFIND":
            if request.url.path in {"/dav/aliyun-drive", "/dav/aliyun-drive/Telegram"}:
                return httpx.Response(
                    207,
                    content=b"<d:multistatus xmlns:d='DAV:' />",
                    request=request,
                )
            return httpx.Response(404, request=request)
        if request.method == "MKCOL":
            return httpx.Response(201, request=request)
        return httpx.Response(405, request=request)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await worker._ensure_directories(
                client,
                ["aliyun-drive", "Telegram", "test-chat", "2026", "07"],
            )

    asyncio.run(scenario())

    assert ("MKCOL", "/dav/aliyun-drive") not in requests
    assert ("MKCOL", "/dav/aliyun-drive/Telegram") not in requests
    assert [item for item in requests if item[0] == "MKCOL"] == [
        ("MKCOL", "/dav/aliyun-drive/Telegram/test-chat"),
        ("MKCOL", "/dav/aliyun-drive/Telegram/test-chat/2026"),
        ("MKCOL", "/dav/aliyun-drive/Telegram/test-chat/2026/07"),
    ]


def test_openlist_process_does_not_inherit_proxy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    paths.openlist_dir.mkdir(parents=True, exist_ok=True)
    (paths.openlist_dir / "openlist.exe").write_bytes(b"placeholder")
    manager = OpenListManager(paths, AppConfig())
    captured: dict = {}

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(*args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager, "choose_port", lambda: 5244)
    monkeypatch.setattr(manager, "running", lambda: False)
    monkeypatch.setattr(manager, "_tcp_ready", lambda port: True)
    monkeypatch.setattr(manager, "password", lambda: "test-password")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")

    manager.start()
    assert "HTTP_PROXY" not in captured
    assert "ALL_PROXY" not in captured
    assert captured["NO_PROXY"] == "*"
    assert captured["no_proxy"] == "*"


def test_openlist_config_is_local_only_and_has_no_upstream_proxy(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    manager = OpenListManager(paths, AppConfig(openlist_port=5251))

    manager._ensure_local_config(5251)

    data = json.loads(
        (paths.openlist_dir / "data" / "config.json").read_text(encoding="utf-8")
    )
    assert data["scheme"]["address"] == "127.0.0.1"
    assert data["scheme"]["http_port"] == 5251
    assert data["scheme"]["https_port"] == -1
    assert data["proxy_address"] == ""
    assert data["cors"]["allow_origins"] == ["*"]
    assert data["cors"]["allow_methods"] == ["*"]
    assert data["cors"]["allow_headers"] == ["*"]
    assert data["ftp"]["listen"].startswith("127.0.0.1:")
    assert data["sftp"]["listen"].startswith("127.0.0.1:")


def test_openlist_password_is_updated_without_inheriting_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    paths.openlist_dir.mkdir(parents=True, exist_ok=True)
    (paths.openlist_dir / "openlist.exe").write_bytes(b"placeholder")
    (paths.openlist_dir / "data").mkdir(parents=True, exist_ok=True)
    (paths.openlist_dir / "data" / "data.db").write_bytes(b"placeholder")
    manager = OpenListManager(paths, AppConfig())
    captured: dict = {}
    saved: list[str] = []

    def fake_run(native_args, **kwargs):
        captured["args"] = list(native_args)
        captured["env"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(native_args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "telegram_video_downloader.openlist_manager.save_openlist_password",
        saved.append,
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")

    manager.set_password("CustomPassword123!")

    assert captured["args"][-3:] == ["admin", "set", "CustomPassword123!"]
    assert "HTTP_PROXY" not in captured["env"]
    assert captured["env"]["NO_PROXY"] == "*"
    assert saved == ["CustomPassword123!"]


def test_ffmpeg_metadata_standardization_without_reencoding(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is not available")
    paths = make_paths(tmp_path)
    paths.ensure()
    config = AppConfig(ffmpeg_path=ffmpeg)
    worker = UploadWorker(paths, config)
    source = tmp_path / "sample.mp4"
    native_args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x90:d=0.4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-y",
        str(source),
    ]
    completed = subprocess.run(native_args, check=False)
    assert completed.returncode == 0
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    worker.storage.save_upload_job(-1001, 7, "测试群", str(source), source.stat().st_size)

    output = asyncio.run(
        worker._standardize(
            {
                "chat_id": -1001,
                "message_id": 7,
                "chat_title": "测试群",
                "file_path": str(source),
            }
        )
    )
    probe_args = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags=title,artist,album:stream=codec_name",
        "-of",
        "json",
        str(output),
    ]
    probe = subprocess.run(probe_args, capture_output=True, check=False)
    assert probe.returncode == 0
    metadata = json.loads(probe.stdout.decode("utf-8"))
    tags = metadata["format"]["tags"]
    assert tags["title"] == "sample"
    assert tags["artist"] == "Telegram Video Downloader"
    assert tags["album"] == "测试群"
    assert metadata["streams"][0]["codec_name"] == "h264"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256(output.read_bytes()).hexdigest() != original_hash
