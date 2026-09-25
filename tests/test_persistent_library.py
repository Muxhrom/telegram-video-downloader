import asyncio
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from telegram_video_downloader.cloud_catalog import classify_cloud_video
from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.library import LibraryStore, cloud_name, identity_from_name
from telegram_video_downloader.storage import Storage
from telegram_video_downloader.upload_service import UploadWorker
from telegram_video_downloader import upload_service
from telegram_video_downloader.paths import AppPaths
from telegram_video_downloader.ui import MainWindow

from test_upload_pipeline import make_paths


def video(chat_id: int = -1001, message_id: int = 9) -> dict:
    return {
        "chat_id": chat_id, "message_id": message_id,
        "chat_title": "测试群", "name": "片段.mp4", "size": 100,
        "date": "2026-09-25T12:00:00+00:00", "media_kind": "普通视频",
    }


def test_catalogue_restores_last_chat_and_cursor(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    first = LibraryStore(db)
    first.save_chats([{"chat_id": -1001, "title": "测试群", "kind": "群组"}])
    first.save_videos([video(), video(message_id=10)])
    first.save_scan_state(-1001, {"cursors": {"video": 8}, "reached_end": False}, checked=True)
    first.set_state("last_chat_id", "-1001")
    first.save_download_task(video(), str(tmp_path), priority=0)
    first.update_download_task(-1001, 9, "downloading")

    restored = LibraryStore(db)
    assert restored.get_state("last_chat_id") == "-1001"
    assert [item["message_id"] for item in restored.videos(-1001)] == [10, 9]
    assert restored.scan_state(-1001)["cursors"]["video"] == 8
    assert restored.checked_at(-1001)
    assert restored.download_tasks(pending_only=True)[0]["priority"] == 0
    assert restored.download_tasks(pending_only=True)[0]["status"] == "downloading"


def test_cloud_identity_and_legacy_name_cannot_false_confirm() -> None:
    item = video()
    filename = cloud_name("片段.mp4", -1001, 9)
    assert identity_from_name(filename) == (-1001, 9)
    assert cloud_name(filename, -1001, 9) == filename
    root = "/aliyun-drive/Telegram视频下载器/测试群/2026/09/"
    tagged = [{"remote_path": root + filename, "size": 100}]
    assert classify_cloud_video(item, None, tagged, verified=True) == ("confirmed", root + filename)
    assert classify_cloud_video(item, None, tagged, verified=False) == ("unverified", "")
    incomplete = {"status": "uploading", "remote_path": root + filename, "remote_size": 0}
    assert classify_cloud_video(item, incomplete, tagged, verified=True) == ("suspected", root + filename)
    wrong_size = {"status": "completed", "remote_path": root + filename, "remote_size": 200}
    assert classify_cloud_video(item, wrong_size, tagged, verified=True) == ("suspected", root + filename)
    legacy = [{"remote_path": root + "片段.mp4", "size": 100}]
    assert classify_cloud_video(item, None, legacy, verified=True) == ("suspected", root + "片段.mp4")
    assert classify_cloud_video(item, None, [], verified=True) == ("missing", "")


def test_reconcile_requeues_cloud_missing_local_file(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    library = LibraryStore(paths.database_file)
    storage = Storage(paths.database_file)
    library.save_chats([{"chat_id": -1001, "title": "测试群", "kind": "群组"}])
    library.save_videos([video()])
    source = tmp_path / "片段.mp4"
    source.write_bytes(b"video")
    storage.record_download(-1001, 9, str(source), source.stat().st_size, "completed")
    storage.save_upload_job(-1001, 9, "测试群", str(source), source.stat().st_size)
    storage.update_upload(-1001, 9, "completed", "旧记录", "片段.mp4", "/missing/片段.mp4", 5)
    worker = UploadWorker(paths, AppConfig(cloud_auto_upload=True))
    worker._fresh_cloud_files = []
    queued: list[dict] = []

    async def capture(item: dict) -> None:
        queued.append(item)

    worker.enqueue_upload = capture
    asyncio.run(worker.reconcile_local_downloads())
    assert len(queued) == 1
    assert queued[0]["message_id"] == 9
    assert storage.get_upload(-1001, 9)["status"] == "remote_missing"


def test_manual_upload_can_retry_a_completed_record_when_cloud_is_missing(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    source = tmp_path / "片段.mp4"
    source.write_bytes(b"video")
    storage = Storage(paths.database_file)
    storage.save_upload_job(-1001, 9, "测试群", str(source), 5)
    storage.update_upload(-1001, 9, "completed", "旧记录", "片段.mp4", "/missing/片段.mp4", 5)
    worker = UploadWorker(paths, AppConfig())
    worker._fresh_cloud_files = []

    async def scenario() -> None:
        worker._queue = asyncio.PriorityQueue()
        await worker.enqueue_upload({**video(), "file_path": str(source)})

    asyncio.run(scenario())
    assert (-1001, 9) in worker._jobs
    assert storage.get_upload(-1001, 9)["status"] == "queued"


def test_cloud_rename_updates_mapping_and_inventory_together(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    storage = Storage(db)
    library = LibraryStore(db)
    old = "/aliyun-drive/Telegram视频下载器/测试群/2026/09/片段.mp4"
    new = "/aliyun-drive/Telegram视频下载器/测试群/2026/09/" + cloud_name("片段.mp4", -1001, 9)
    storage.save_upload_job(-1001, 9, "测试群", "C:/片段.mp4", 100)
    storage.update_upload(-1001, 9, "completed", "ok", "片段.mp4", old, 100)
    library.replace_cloud_inventory([{"remote_path": old, "size": 100, "etag": "abc"}])
    library.record_cloud_rename(-1001, 9, old, new)
    assert storage.get_upload(-1001, 9)["remote_path"] == new
    assert library.cloud_files()[0]["remote_path"] == new


def test_cached_chat_and_cloud_status_are_visible_before_network(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    paths: AppPaths = make_paths(tmp_path)
    paths.ensure()
    library = LibraryStore(paths.database_file)
    library.save_chats([{"chat_id": -1001, "title": "测试群", "kind": "群组"}])
    library.save_videos([video()])
    library.set_state("last_chat_id", "-1001")
    storage = Storage(paths.database_file)
    storage.record_download(-1001, 9, str(tmp_path / "deleted.mp4"), 100, "completed")
    window = MainWindow(paths, AppConfig())
    try:
        assert window.current_chat["chat_id"] == -1001
        assert window.table.rowCount() == 1
        assert window.table.item(0, 7).text() == "本地已删除"
        assert window.table.item(0, 8).text() == "云端待核对"
        remote = "/aliyun-drive/Telegram视频下载器/测试群/2026/09/" + cloud_name("片段.mp4", -1001, 9)
        window.set_cloud_inventory([{"remote_path": remote, "size": 100, "etag": ""}], "")
        assert window.table.item(0, 8).text() == "云端已确认"
        window.table.item(0, 0).setCheckState(Qt.Checked)
        submitted: list[tuple] = []
        window.worker.submit = lambda *args: submitted.append(args)
        request_id = window.video_request_id
        window.set_chats([{"chat_id": -1001, "title": "测试群", "kind": "群组"}])
        assert window.video_request_id == request_id
        assert window.table.rowCount() == 1
        assert any(args[0] == "refresh_new_videos" for args in submitted)
        window.download_selected()
        assert not any(args[0] == "enqueue_downloads" for args in submitted)
        window.set_cloud_inventory([], "")
        window.download_selected()
        assert submitted[-1][0] == "enqueue_downloads"
        assert submitted[-1][3] is True
    finally:
        assert window.worker.stop_gracefully()
        assert window.upload_worker.stop_gracefully()
        assert window.compression_worker.stop_gracefully()
        window.tray.hide()
        window.hide()
        app.processEvents()


def test_rename_probe_moves_verifies_and_cleans_disposable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = UploadWorker(paths, AppConfig())
    monkeypatch.setattr(worker.openlist, "running", lambda: True)
    monkeypatch.setattr(worker.openlist, "password", lambda: "unused")
    root = "/dav/aliyun-drive/Telegram视频下载器"
    files: dict[str, bytes] = {}
    moves: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.method == "PROPFIND":
            if path not in {"/dav/aliyun-drive", root} and path not in files:
                return httpx.Response(404, request=request)
            size = len(files[path]) if path in files else 0
            body = f"<d:multistatus xmlns:d='DAV:'><d:response><d:propstat><d:prop><d:getcontentlength>{size}</d:getcontentlength></d:prop></d:propstat></d:response></d:multistatus>"
            return httpx.Response(207, content=body.encode(), request=request)
        if request.method == "PUT":
            files[path] = request.read()
            return httpx.Response(201, request=request)
        if request.method == "MOVE":
            target = unquote(urlsplit(request.headers["Destination"]).path)
            moves.append((path, target, request.headers["Overwrite"]))
            if path not in files or target in files:
                return httpx.Response(409, request=request)
            files[target] = files.pop(path)
            return httpx.Response(201, request=request)
        if request.method == "DELETE":
            files.pop(path, None)
            return httpx.Response(204, request=request)
        return httpx.Response(405, request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        upload_service.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    results: list[tuple[bool, str]] = []
    worker.rename_probe_result.connect(lambda ok, message: results.append((ok, message)))
    asyncio.run(worker.probe_cloud_rename())
    assert results and results[0][0]
    assert worker._rename_verified
    assert moves and moves[0][2] == "F"
    assert not files


def test_legacy_rename_rechecks_both_paths_and_updates_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    storage = Storage(paths.database_file)
    library = LibraryStore(paths.database_file)
    old = "/aliyun-drive/Telegram视频下载器/测试群/2026/09/片段.mp4"
    new = str(Path(old).with_name(cloud_name("片段.mp4", -1001, 9))).replace("\\", "/")
    storage.save_upload_job(-1001, 9, "测试群", "C:/片段.mp4", 5)
    storage.update_upload(-1001, 9, "completed", "ok", "片段.mp4", old, 5)
    library.replace_cloud_inventory([{"remote_path": old, "size": 5, "etag": ""}])
    worker = UploadWorker(paths, AppConfig())
    worker._fresh_cloud_files = library.cloud_files()
    worker._rename_verified = True
    monkeypatch.setattr(worker.openlist, "password", lambda: "unused")
    files = {"/dav" + old: b"video"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.method == "PROPFIND":
            if path not in files:
                return httpx.Response(404, request=request)
            body = f"<d:multistatus xmlns:d='DAV:'><d:response><d:propstat><d:prop><d:getcontentlength>{len(files[path])}</d:getcontentlength></d:prop></d:propstat></d:response></d:multistatus>"
            return httpx.Response(207, content=body.encode(), request=request)
        if request.method == "MOVE":
            target = unquote(urlsplit(request.headers["Destination"]).path)
            assert request.headers["Overwrite"] == "F"
            assert target not in files
            files[target] = files.pop(path)
            return httpx.Response(201, request=request)
        return httpx.Response(405, request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        upload_service.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    results: list[dict] = []
    worker.rename_result.connect(results.extend)
    asyncio.run(worker.execute_cloud_renames([{
        "chat_id": -1001, "message_id": 9,
        "old_path": old, "new_path": new, "size": 5,
    }]))
    assert results == [{
        "chat_id": -1001, "message_id": 9,
        "old_path": old, "new_path": new, "size": 5,
        "ok": True, "detail": "已核验",
    }]
    assert storage.get_upload(-1001, 9)["remote_path"] == new
    assert library.cloud_files()[0]["remote_path"] == new
    assert files == {"/dav" + new: b"video"}


def test_cloud_scan_walks_app_tree_and_persists_complete_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    paths.ensure()
    worker = UploadWorker(paths, AppConfig())
    monkeypatch.setattr(worker.openlist, "running", lambda: True)
    monkeypatch.setattr(worker.openlist, "password", lambda: "unused")
    mount = "/dav/aliyun-drive"
    root = mount + "/Telegram视频下载器"
    chat = root + "/测试群"
    year = chat + "/2026"
    month = year + "/09"
    target = month + "/" + cloud_name("片段.mp4", -1001, 9)
    children = {mount: [root], root: [chat], chat: [year], year: [month], month: [target]}

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path).rstrip("/")
        if request.method != "PROPFIND" or path not in children:
            return httpx.Response(404, request=request)
        entries = [path, *children[path]]
        body = ["<d:multistatus xmlns:d='DAV:'>"]
        for entry in entries:
            size = 100 if entry == target else 0
            kind = "<d:collection/>" if entry in children else ""
            body.append(
                f"<d:response><d:href>{entry}</d:href><d:propstat><d:prop>"
                f"<d:resourcetype>{kind}</d:resourcetype><d:getcontentlength>{size}</d:getcontentlength>"
                "</d:prop></d:propstat></d:response>"
            )
        body.append("</d:multistatus>")
        return httpx.Response(207, content="".join(body).encode(), request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        upload_service.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(worker.scan_cloud_inventory())
    expected = "/aliyun-drive/Telegram视频下载器/测试群/2026/09/" + cloud_name("片段.mp4", -1001, 9)
    assert worker.library.cloud_files() == [{"remote_path": expected, "size": 100, "etag": ""}]
    assert worker.library.get_state("cloud_checked_at")
