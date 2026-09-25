from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import httpx
from PySide6.QtCore import QObject, Signal

from .config import AppConfig
from .cloud_catalog import classify_cloud_video
from .library import LibraryStore, cloud_name, identity_from_name
from .naming import sanitize_component
from .openlist_manager import OpenListManager
from .paths import AppPaths
from .storage import Storage


LOGGER = logging.getLogger(__name__)


class UploadRateTracker:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        minimum_sample_seconds: float = 1.0,
        window_seconds: float = 5.0,
    ) -> None:
        self.clock = clock
        self.minimum_sample_seconds = minimum_sample_seconds
        self.window_seconds = window_seconds
        self.samples: deque[tuple[float, int]] = deque([(clock(), 0)])

    def update(self, current: int, total: int) -> tuple[float, float]:
        now = self.clock()
        self.samples.append((now, current))
        cutoff = now - self.window_seconds
        while len(self.samples) > 2 and self.samples[1][0] <= cutoff:
            self.samples.popleft()

        if total > 0 and current >= total:
            return 0.0, 0.0

        first_time, first_bytes = self.samples[0]
        elapsed = now - first_time
        transferred = current - first_bytes
        if elapsed < self.minimum_sample_seconds or transferred <= 0:
            return 0.0, 0.0

        speed = transferred / elapsed
        eta = (total - current) / speed if speed > 0 and total > current else 0.0
        return speed, eta


class FileProgressStream(httpx.AsyncByteStream):
    def __init__(
        self,
        path: Path,
        pause_event: asyncio.Event,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int], None],
    ) -> None:
        self.path = path
        self.pause_event = pause_event
        self.cancelled = cancelled
        self.progress = progress

    async def __aiter__(self):
        total = self.path.stat().st_size
        current = 0
        with self.path.open("rb") as source:
            while True:
                await self.pause_event.wait()
                if self.cancelled():
                    raise asyncio.CancelledError()
                chunk = await asyncio.to_thread(source.read, 1024 * 1024)
                if not chunk:
                    break
                current += len(chunk)
                yield chunk
                self.progress(current, total)


class UploadWorker(QObject):
    status = Signal(str)
    error = Signal(str)
    upload_job = Signal(object)
    upload_state = Signal(object, object, str, str)
    upload_progress = Signal(object, object, int)
    upload_metrics = Signal(object, object, object)
    upload_priority = Signal(object, object, int)
    cloud_state = Signal(object)
    cloud_inventory_ready = Signal(object, str)
    review_needed = Signal(object)
    rename_plan_ready = Signal(object)
    rename_result = Signal(object)
    rename_probe_result = Signal(bool, str)

    def __init__(self, paths: AppPaths, config: AppConfig) -> None:
        super().__init__()
        self.paths = paths
        self.config = config
        self.storage = Storage(paths.database_file)
        self.library = LibraryStore(paths.database_file)
        self.openlist = OpenListManager(paths, config)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.PriorityQueue | None = None
        self._pause_event: asyncio.Event | None = None
        self._worker_task: asyncio.Task | None = None
        self._sequence = 0
        self._versions: dict[tuple[int, int], int] = {}
        self._jobs: dict[tuple[int, int], dict[str, Any]] = {}
        self._active: set[tuple[int, int]] = set()
        self._cancelled: set[tuple[int, int]] = set()
        self._shutting_down = False
        self._rename_verified = False
        self._fresh_cloud_files: list[dict] | None = None
        self._inventory_event: asyncio.Event | None = None
        self._scan_lock: asyncio.Lock | None = None

    def start(self) -> None:
        if self.isRunning():
            return
        self._thread = threading.Thread(
            target=self.run, name="CloudUploadWorker", daemon=False
        )
        self._thread.start()

    def isRunning(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._queue = asyncio.PriorityQueue()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._inventory_event = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._worker_task = self.loop.create_task(self._upload_worker())
        self.loop.create_task(self._restore_jobs())
        if self.config.cloud_enabled:
            self.loop.create_task(self._startup_cloud_check())
        if self._shutting_down:
            self.loop.call_soon(self.loop.stop)
        try:
            self.loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self.loop.run_until_complete(
                    asyncio.gather(*tasks, return_exceptions=True)
                )
            self.openlist.stop()
            self.loop.close()
            self.loop = None

    def submit_future(self, method_name: str, *args: Any) -> concurrent.futures.Future | None:
        if not self.loop or not self.loop.is_running():
            return None
        return asyncio.run_coroutine_threadsafe(getattr(self, method_name)(*args), self.loop)
    def submit(self, method_name: str, *args: Any) -> None:
        if not self.loop or not self.loop.is_running():
            self.error.emit("上传工作线程尚未就绪，请稍后重试。")
            return
        future = asyncio.run_coroutine_threadsafe(
            getattr(self, method_name)(*args), self.loop
        )

        def report_failure(completed: Any) -> None:
            try:
                completed.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                pass
            except Exception as exc:
                LOGGER.exception("Upload background operation failed")
                self.error.emit(str(exc))

        future.add_done_callback(report_failure)

    async def _restore_jobs(self) -> None:
        await asyncio.sleep(0)
        for row in self.storage.pending_uploads():
            payload = {
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
                "chat_title": row["chat_title"],
                "name": Path(row["source_path"]).name,
                "file_path": row["source_path"],
                "size": int(row["source_size"]),
                "priority": int(row["priority"]),
                "retry_count": int(row["retry_count"]),
                "status": row["status"],
            }
            self.upload_job.emit(payload)
            if row["status"] in {"queued", "processing", "uploading"}:
                await self.enqueue_upload(payload, restored=True)
            else:
                self.upload_state.emit(
                    payload["chat_id"],
                    payload["message_id"],
                    row["status"],
                    row["detail"],
                )

    async def enqueue_upload(self, payload: dict, restored: bool = False) -> None:
        assert self._queue is not None
        key = (int(payload["chat_id"]), int(payload["message_id"]))
        previous = self.storage.get_upload(*key)
        if previous and previous["status"] == "completed":
            if self._fresh_cloud_files is None:
                self.upload_state.emit(*key, "needs_review", "请先核对云端清单")
                return
            kind, found_path = classify_cloud_video(
                payload, previous, self._fresh_cloud_files, verified=True,
            )
            if kind == "confirmed":
                self.upload_state.emit(*key, "completed", found_path)
                return
            if kind == "suspected" and not payload.get("cloud_override"):
                self.storage.update_upload(*key, "needs_review", "云端存在疑似同名文件")
                self.review_needed.emit([{**payload, "remote_path": found_path}])
                self.upload_state.emit(*key, "needs_review", found_path)
                return
            self.storage.update_upload(*key, "remote_missing", "云端文件已不存在")
        if key in self._jobs:
            return
        job = dict(payload)
        job["priority"] = max(0, min(2, int(job.get("priority", 1))))
        source = Path(job["file_path"])
        job["size"] = int(job.get("size") or (source.stat().st_size if source.exists() else 0))
        self._jobs[key] = job
        self._versions[key] = self._versions.get(key, 0) + 1
        self.storage.save_upload_job(
            *key,
            str(job.get("chat_title", "")),
            str(source),
            int(job["size"]),
            int(job["priority"]),
            "queued",
            "等待处理",
        )
        if not restored:
            self.upload_job.emit(dict(job))
        await self._put_job(key)
        self.upload_state.emit(*key, "queued", "等待处理")

    async def _put_job(self, key: tuple[int, int]) -> None:
        assert self._queue is not None
        self._sequence += 1
        job = self._jobs[key]
        await self._queue.put(
            (int(job["priority"]), self._sequence, self._versions[key], key)
        )

    async def replace_upload_source(self, chat_id: int, message_id: int, source_path: str) -> bool:
        key = (int(chat_id), int(message_id))
        if key in self._active:
            return False
        source = Path(source_path)
        if not source.is_file():
            return False
        job = self._jobs.get(key)
        if job is not None:
            job["file_path"] = str(source)
            job["size"] = source.stat().st_size
        record = self.storage.get_upload(*key)
        if not record or record["status"] not in {"queued", "processing", "cancelled", "failed"}:
            return False
        self.storage.update_upload_source(*key, str(source), source.stat().st_size)
        return True
    async def set_priority(
        self, keys: list[tuple[int, int]], priority: int
    ) -> None:
        priority = max(0, min(2, int(priority)))
        for raw in keys:
            key = (int(raw[0]), int(raw[1]))
            job = self._jobs.get(key)
            if not job or key in self._active:
                continue
            job["priority"] = priority
            self._versions[key] = self._versions.get(key, 0) + 1
            self.storage.set_upload_priority(*key, priority)
            await self._put_job(key)
            self.upload_priority.emit(*key, priority)

    async def set_paused(self, paused: bool) -> None:
        assert self._pause_event is not None
        if paused:
            self._pause_event.clear()
            self.status.emit("上传队列已暂停。")
        else:
            self._pause_event.set()
            self.status.emit("上传队列已恢复。")

    async def cancel_uploads(self, keys: list[tuple[int, int]]) -> None:
        for raw in keys:
            key = (int(raw[0]), int(raw[1]))
            self._cancelled.add(key)
            if key not in self._active and key in self._jobs:
                self._versions[key] = self._versions.get(key, 0) + 1
                self._jobs.pop(key, None)
                self.storage.update_upload(*key, "cancelled", "已取消")
                self.upload_state.emit(*key, "cancelled", "已取消")

    async def _upload_worker(self) -> None:
        while True:
            assert self._queue is not None
            _, _, version, key = await self._queue.get()
            job = self._jobs.get(key)
            if not job or self._versions.get(key) != version:
                self._queue.task_done()
                continue
            self._active.add(key)
            try:
                if key in self._cancelled:
                    raise asyncio.CancelledError()
                await self._process_job(job)
            except asyncio.CancelledError:
                if self._shutting_down:
                    raise
                self.storage.update_upload(*key, "cancelled", "已取消")
                self.upload_state.emit(*key, "cancelled", "已取消")
            except Exception as exc:
                LOGGER.exception("Upload failed for %s", key)
                self.storage.update_upload(
                    *key, "failed", str(exc), increment_retry=True
                )
                self.upload_state.emit(*key, "failed", str(exc))
            finally:
                if self._versions.get(key) == version:
                    self._jobs.pop(key, None)
                self._active.discard(key)
                self._cancelled.discard(key)
                self._queue.task_done()

    def ffmpeg_path(self) -> Path | None:
        configured = Path(self.config.ffmpeg_path) if self.config.ffmpeg_path else None
        if configured and configured.is_file():
            return configured
        located = shutil.which("ffmpeg")
        return Path(located) if located else None

    async def _standardize(self, job: dict) -> Path:
        key = (int(job["chat_id"]), int(job["message_id"]))
        source = Path(job["file_path"])
        if not source.is_file():
            raise RuntimeError("源文件不存在；本地记录已保留，可重新下载后重试。")
        ffmpeg = self.ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("未找到 FFmpeg，请在云盘设置中选择 ffmpeg.exe。")
        free = shutil.disk_usage(self.paths.upload_staging_dir).free
        if free < source.stat().st_size + 512 * 1024 * 1024:
            raise RuntimeError("临时目录剩余空间不足，无法进行无损重封装。")
        extension = source.suffix.casefold()
        if extension not in {".mp4", ".mov", ".m4v", ".mkv", ".webm"}:
            extension = ".mkv"
        job_staging = self.paths.upload_staging_dir / f"{key[0]}_{key[1]}"
        job_staging.mkdir(parents=True, exist_ok=True)
        output = job_staging / f"{sanitize_component(source.stem)}{extension}"
        output.unlink(missing_ok=True)
        self.storage.update_upload(*key, "processing", "正在规范容器元数据")
        self.upload_state.emit(*key, "processing", "正在规范容器元数据")
        arguments = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-c",
            "copy",
            "-map_metadata",
            "-1",
            "-metadata",
            f"title={source.stem}",
            "-metadata",
            "artist=Telegram Video Downloader",
            "-metadata",
            f"album={job.get('chat_title', '')}",
        ]
        if extension in {".mp4", ".mov", ".m4v"}:
            arguments.extend(["-movflags", "+faststart"])
        arguments.append(str(output))
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not output.is_file():
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg 处理失败：{message[-500:]}")
        return output

    def _remote_parts(self, job: dict, processed: Path) -> list[str]:
        now = datetime.now(timezone.utc).astimezone()
        return [
            self.config.openlist_mount.strip("/"),
            sanitize_component(self.config.cloud_root),
            sanitize_component(str(job.get("chat_title") or "未命名")),
            f"{now.year:04d}",
            f"{now.month:02d}",
            cloud_name(processed.name, int(job["chat_id"]), int(job["message_id"])),
        ]

    def _webdav_url(self, parts: list[str]) -> str:
        encoded = "/".join(quote(part, safe="") for part in parts if part)
        return self.openlist.webdav_url.rstrip("/") + "/" + encoded

    async def _propfind(
        self, client: httpx.AsyncClient, parts: list[str]
    ) -> dict | None:
        response = await client.request(
            "PROPFIND", self._webdav_url(parts), headers={"Depth": "0"}
        )
        if response.status_code == 404:
            return None
        if response.status_code not in {200, 207}:
            raise RuntimeError(
                f"云端查询失败：HTTP {response.status_code} {response.text[:200]}"
            )
        try:
            root = ET.fromstring(response.content)
            size_node = root.find(".//{DAV:}getcontentlength")
            etag_node = root.find(".//{DAV:}getetag")
            return {
                "size": int(size_node.text or 0) if size_node is not None else 0,
                "etag": (etag_node.text or "").strip('"') if etag_node is not None else "",
            }
        except (ET.ParseError, ValueError):
            return {"size": 0, "etag": ""}

    async def _ensure_directories(
        self, client: httpx.AsyncClient, parts: list[str]
    ) -> None:
        if not parts:
            return

        mount = [parts[0]]
        if await self._propfind(client, mount) is None:
            raise RuntimeError(
                f"\u672a\u627e\u5230 /{parts[0]}\uff0c\u8bf7\u5728 OpenList \u4e2d\u5b8c\u6210\u963f\u91cc\u4e91\u76d8\u6302\u8f7d\u3002"
            )

        current = mount.copy()
        for part in parts[1:]:
            current.append(part)
            if await self._propfind(client, current) is not None:
                continue
            response = await client.request("MKCOL", self._webdav_url(current))
            if response.status_code not in {200, 201, 204, 405}:
                if response.status_code == 409:
                    raise RuntimeError("云端父目录不存在或挂载尚未完成。")
                raise RuntimeError(f"创建云端目录失败：HTTP {response.status_code}")

    async def _process_job(self, job: dict) -> None:
        key = (int(job["chat_id"]), int(job["message_id"]))
        assert self._pause_event is not None
        await self._pause_event.wait()
        if self.config.cloud_enabled:
            assert self._inventory_event is not None
            await self._inventory_event.wait()
        if self._fresh_cloud_files is None:
            raise RuntimeError("云端清单尚未核实，上传已暂停；请在云盘管理中重新核对。")
        if not self.openlist.running():
            raise RuntimeError("OpenList 未运行，请先在云盘设置中启动。")
        if self._fresh_cloud_files is not None:
            kind, found_path = classify_cloud_video(
                job, self.storage.get_upload(*key), self._fresh_cloud_files, verified=True,
            )
            if kind == "confirmed":
                remote = next(f for f in self._fresh_cloud_files if f["remote_path"] == found_path)
                self.storage.update_upload(
                    *key, "completed", "云端已有，已核实", PurePosixPath(found_path).name,
                    found_path, int(remote["size"]), str(remote.get("etag", "")),
                )
                self.upload_state.emit(*key, "completed", found_path)
                return
            if kind == "suspected" and not job.get("cloud_override"):
                self.storage.update_upload(*key, "needs_review", "云端存在疑似同名文件")
                self.review_needed.emit([{**job, "remote_path": found_path}])
                self.upload_state.emit(*key, "needs_review", found_path)
                return
        processed = await self._standardize(job)
        parts = self._remote_parts(job, processed)
        password = self.openlist.password()
        timeout = httpx.Timeout(30.0, read=None, write=None, pool=30.0)
        rate_tracker: UploadRateTracker | None = None

        def report(current: int, total: int) -> None:
            assert rate_tracker is not None
            speed, eta = rate_tracker.update(current, total)
            phase = "cloud_commit" if total > 0 and current >= total else "sending"
            self.upload_progress.emit(*key, int(current * 100 / total) if total else 0)
            self.upload_metrics.emit(
                *key,
                {
                    "current": current,
                    "total": total,
                    "speed": speed,
                    "eta": eta,
                    "phase": phase,
                },
            )

        try:
            async with httpx.AsyncClient(
                auth=("admin", password),
                timeout=timeout,
                follow_redirects=True,
                trust_env=False,
            ) as client:
                await self._ensure_directories(client, parts[:-1])
                existing = await self._propfind(client, parts)
                processed_size = processed.stat().st_size
                if existing and int(existing["size"]) == processed_size:
                    remote_path = "/" + "/".join(parts)
                    self.storage.update_upload(
                        *key,
                        "completed",
                        "云端同名同大小，已完成校验",
                        parts[-1],
                        remote_path,
                        processed_size,
                        existing["etag"],
                    )
                    self.upload_progress.emit(*key, 100)
                    self.upload_state.emit(*key, "completed", remote_path)
                    self._remember_uploaded_file(remote_path, processed_size, existing["etag"])
                    return
                if existing:
                    raise RuntimeError("云端同一视频标识的文件大小不同，已暂停以避免覆盖。")
                remote_path = "/" + "/".join(parts)
                self.storage.update_upload(
                    *key, "uploading", remote_path, parts[-1], remote_path
                )
                self.upload_state.emit(*key, "uploading", remote_path)
                rate_tracker = UploadRateTracker()
                stream = FileProgressStream(
                    processed,
                    self._pause_event,
                    lambda: key in self._cancelled,
                    report,
                )
                response = await client.put(
                    self._webdav_url(parts),
                    content=stream,
                    headers={
                        "Content-Length": str(processed.stat().st_size),
                        "Content-Type": "application/octet-stream",
                    },
                )
                if response.status_code not in {200, 201, 204}:
                    raise RuntimeError(
                        f"上传失败：HTTP {response.status_code} {response.text[:200]}"
                    )
                verified = await self._propfind(client, parts)
                if not verified or int(verified["size"]) != processed.stat().st_size:
                    raise RuntimeError("上传返回成功，但云端大小校验失败。")
                size = processed.stat().st_size
                self.storage.update_upload(
                    *key,
                    "completed",
                    "上传并校验完成",
                    parts[-1],
                    remote_path,
                    size,
                    verified["etag"],
                )
                self.upload_progress.emit(*key, 100)
                self.upload_metrics.emit(
                    *key, {"current": size, "total": size, "speed": 0.0, "eta": 0.0}
                )
                self.upload_state.emit(*key, "completed", remote_path)
                self._remember_uploaded_file(remote_path, size, verified["etag"])
        finally:
            processed.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                processed.parent.rmdir()

    def _remember_uploaded_file(self, remote_path: str, size: int, etag: str) -> None:
        if self._fresh_cloud_files is None:
            return
        self._fresh_cloud_files = [
            file for file in self._fresh_cloud_files if file["remote_path"] != remote_path
        ] + [{"remote_path": remote_path, "size": size, "etag": etag}]
        self.library.replace_cloud_inventory(self._fresh_cloud_files)
        self.cloud_inventory_ready.emit(self._fresh_cloud_files, "")

    async def install_openlist(self) -> None:
        self.status.emit("正在下载并校验 OpenList…")
        await asyncio.to_thread(self.openlist.install)
        self.cloud_state.emit(self.openlist.state())
        self.status.emit("OpenList 安装完成。")

    async def install_ffmpeg(self) -> None:
        self.status.emit("正在下载并校验 FFmpeg 8.1.2 Essentials…")
        target_dir = self.paths.tools_dir / "ffmpeg"
        target_dir.mkdir(parents=True, exist_ok=True)
        archive_url = (
            "https://www.gyan.dev/ffmpeg/builds/packages/"
            "ffmpeg-8.1.2-essentials_build.zip"
        )
        checksum_url = archive_url + ".sha256"

        def download() -> Path:
            options = [
                {"trust_env": False},
                {
                    "trust_env": False,
                    "proxy": (
                        f"socks5://{self.config.proxy_host}:"
                        f"{self.config.proxy_port}"
                    ),
                },
            ]
            last_error: Exception | None = None
            for client_options in options:
                try:
                    with httpx.Client(
                        timeout=httpx.Timeout(30, read=300),
                        follow_redirects=True,
                        **client_options,
                    ) as client:
                        expected_response = client.get(checksum_url)
                        expected_response.raise_for_status()
                        expected = expected_response.text.strip().split()[0]
                        package_response = client.get(archive_url)
                        package_response.raise_for_status()
                        payload = package_response.content
                    if hashlib.sha256(payload).hexdigest().casefold() != expected.casefold():
                        raise RuntimeError("FFmpeg 安装包 SHA256 校验失败。")
                    archive = target_dir / "ffmpeg.zip"
                    archive.write_bytes(payload)
                    with zipfile.ZipFile(archive) as bundle:
                        root = target_dir.resolve()
                        for member in bundle.infolist():
                            candidate = (root / member.filename).resolve()
                            if root not in candidate.parents and candidate != root:
                                raise RuntimeError("FFmpeg 安装包包含不安全路径。")
                        bundle.extractall(root)
                    archive.unlink(missing_ok=True)
                    located = next(target_dir.rglob("ffmpeg.exe"), None)
                    if not located:
                        raise RuntimeError("FFmpeg 安装包中未找到 ffmpeg.exe。")
                    return located
                except Exception as exc:
                    last_error = exc
            raise RuntimeError(f"FFmpeg 下载失败：{last_error}")

        located = await asyncio.to_thread(download)
        self.config.ffmpeg_path = str(located)
        self.config.save(self.paths.config_file)
        state = self.openlist.state()
        state["ffmpeg_path"] = str(located)
        self.cloud_state.emit(state)
        self.status.emit("FFmpeg 8.1.2 安装完成。")

    async def start_openlist(self) -> None:
        await asyncio.to_thread(self.openlist.start)
        self.cloud_state.emit(self.openlist.state())
        self.status.emit("OpenList 已启动。")
        if self.config.cloud_enabled:
            await self.scan_cloud_inventory()

    async def set_openlist_password(self, password: str) -> None:
        await asyncio.to_thread(self.openlist.set_password, password)
        self.cloud_state.emit(self.openlist.state())
        self.status.emit("OpenList 管理员密码已更新并保存。")

    async def stop_openlist(self) -> None:
        await asyncio.to_thread(self.openlist.stop)
        self.cloud_state.emit(self.openlist.state())
        self.status.emit("OpenList 已停止。")

    async def refresh_cloud_state(self) -> None:
        state = self.openlist.state()
        ffmpeg = self.ffmpeg_path()
        state["ffmpeg_path"] = str(ffmpeg) if ffmpeg else ""
        self.cloud_state.emit(state)

    async def _startup_cloud_check(self) -> None:
        await asyncio.sleep(1)
        try:
            await asyncio.to_thread(self.openlist.start)
            await self.refresh_cloud_state()
            await self.scan_cloud_inventory()
        except Exception as exc:
            LOGGER.warning("Cloud startup check postponed: %s", exc)
            self.status.emit(f"云盘暂时不可用，已保留本地记录：{exc}")
            if self._inventory_event is not None:
                self._inventory_event.set()
            self.cloud_inventory_ready.emit([], str(exc))

    async def _list_directory(self, client: httpx.AsyncClient, parts: list[str]) -> list[dict]:
        response = await client.request(
            "PROPFIND", self._webdav_url(parts), headers={"Depth": "1"}
        )
        if response.status_code not in {200, 207}:
            raise RuntimeError(f"列出云端目录失败：HTTP {response.status_code}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError("云端目录返回了无效的 WebDAV 响应") from exc
        current = "/" + "/".join(parts)
        entries: list[dict] = []
        for node in root.findall("{DAV:}response"):
            href = node.findtext("{DAV:}href") or ""
            raw_path = unquote(urlsplit(href).path).rstrip("/")
            if not raw_path.startswith("/dav/"):
                continue
            remote_path = raw_path[4:]
            if remote_path == current:
                continue
            kind = node.find(".//{DAV:}resourcetype/{DAV:}collection")
            size_text = node.findtext(".//{DAV:}getcontentlength") or "0"
            etag = (node.findtext(".//{DAV:}getetag") or "").strip('"')
            entries.append({
                "remote_path": remote_path,
                "is_dir": kind is not None,
                "size": int(size_text) if size_text.isdecimal() else 0,
                "etag": etag,
            })
        return entries

    async def scan_cloud_inventory(self) -> None:
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        async with self._scan_lock:
            try:
                await self._scan_cloud_inventory_unlocked()
            except Exception as exc:
                self._fresh_cloud_files = None
                if self._inventory_event is not None:
                    self._inventory_event.set()
                self.cloud_inventory_ready.emit([], str(exc))
                raise

    async def _scan_cloud_inventory_unlocked(self) -> None:
        """Replace the index only after the entire app-owned tree was read."""
        if not self.openlist.running():
            raise RuntimeError("OpenList 未运行，云端状态尚未核实。")
        root = [
            self.config.openlist_mount.strip("/"),
            sanitize_component(self.config.cloud_root),
        ]
        files: list[dict] = []
        async with httpx.AsyncClient(
            auth=("admin", self.openlist.password()), timeout=30, trust_env=False
        ) as client:
            if await self._propfind(client, root[:1]) is None:
                raise RuntimeError("阿里云盘挂载不可用。")
            if await self._propfind(client, root) is not None:
                pending = [root]
                while pending:
                    parts = pending.pop()
                    for entry in await self._list_directory(client, parts):
                        if entry["is_dir"]:
                            pending.append(list(PurePosixPath(entry["remote_path"]).parts[1:]))
                        else:
                            files.append({key: entry[key] for key in ("remote_path", "size", "etag")})
        self.library.replace_cloud_inventory(files)
        self._fresh_cloud_files = files
        if self._inventory_event is not None:
            self._inventory_event.set()
        self.cloud_inventory_ready.emit(files, "")
        self.status.emit(f"云盘清单已核对：{len(files)} 个文件。")
        await self.reconcile_local_downloads()

    async def reconcile_local_downloads(self) -> None:
        """Queue missing backups without relying on the visible history page."""
        if self._fresh_cloud_files is None:
            return
        review: list[dict] = []
        queued = 0
        for row in self.storage.completed_downloads():
            key = (int(row["chat_id"]), int(row["message_id"]))
            source = Path(row["file_path"])
            if not source.is_file():
                continue
            upload = self.storage.get_upload(*key)
            cached = self.library.video(*key)
            title = self.library.chat_title(key[0]) or (upload or {}).get("chat_title", "")
            if not title:
                continue
            video = dict(cached or {})
            video.update({
                "chat_id": key[0], "message_id": key[1], "chat_title": title,
                "name": source.name, "size": source.stat().st_size,
                "file_path": str(source), "priority": 1,
            })
            kind, remote_path = classify_cloud_video(
                video, upload, self._fresh_cloud_files, verified=True,
            )
            if kind == "confirmed":
                remote = next(f for f in self._fresh_cloud_files if f["remote_path"] == remote_path)
                if not upload:
                    self.storage.save_upload_job(*key, title, str(source), source.stat().st_size)
                self.storage.update_upload(
                    *key, "completed", "云端文件已核实", PurePosixPath(remote_path).name,
                    remote_path, int(remote["size"]), str(remote.get("etag", "")),
                )
            elif kind == "suspected":
                review.append({**video, "remote_path": remote_path})
            elif kind == "missing" and self.config.cloud_auto_upload:
                if upload and upload["status"] == "completed":
                    self.storage.update_upload(*key, "remote_missing", "云端文件已不存在")
                await self.enqueue_upload(video)
                queued += 1
        self.review_needed.emit(review)
        if queued:
            self.status.emit(f"已自动补入 {queued} 个云端缺失的视频。")

    async def confirm_cloud_match(self, item: dict) -> None:
        """Adopt a legacy file only after the user accepts a specific pair."""
        if self._fresh_cloud_files is None:
            raise RuntimeError("云端清单尚未核实。")
        key = (int(item["chat_id"]), int(item["message_id"]))
        path = str(item["remote_path"])
        remote = next((f for f in self._fresh_cloud_files if f["remote_path"] == path), None)
        if remote is None:
            raise RuntimeError("选中的云端文件已不在最新清单中。")
        other = self.storage.upload_for_remote_path(path)
        if other and (int(other["chat_id"]), int(other["message_id"])) != key:
            raise RuntimeError("该云端文件已经对应另一个视频。")
        previous = self.storage.get_upload(*key)
        if identity_from_name(PurePosixPath(path).name) == key and previous and (
            int(previous.get("remote_size") or 0) != int(remote["size"])
        ):
            raise RuntimeError("同标识文件的上传大小尚未核实，不能人工确认为完整副本。")
        source = Path(str(item["file_path"]))
        self.storage.save_upload_job(
            *key, str(item["chat_title"]), str(source),
            source.stat().st_size if source.is_file() else 0,
        )
        self.storage.update_upload(
            *key, "completed", "用户确认云端旧文件", PurePosixPath(path).name,
            path, int(remote["size"]), str(remote.get("etag", "")),
        )
        self.upload_state.emit(*key, "completed", path)

    async def plan_cloud_renames(self) -> None:
        if self._fresh_cloud_files is None:
            raise RuntimeError("请先核对云端清单。")
        by_path = {f["remote_path"]: f for f in self._fresh_cloud_files}
        candidates: list[dict] = []
        skipped: list[dict] = []
        for record in self.storage.completed_uploads():
            key = (int(record["chat_id"]), int(record["message_id"]))
            old_path = str(record["remote_path"])
            remote = by_path.get(old_path)
            if remote is None or not int(record["remote_size"] or 0) or (
                int(remote["size"]) != int(record["remote_size"])
            ):
                skipped.append({"old_path": old_path, "reason": "源文件不存在或大小不符"})
                continue
            current_name = PurePosixPath(old_path).name
            new_name = cloud_name(current_name, *key)
            new_path = str(PurePosixPath(old_path).with_name(new_name))
            if new_path == old_path:
                continue
            if new_path in by_path:
                skipped.append({"old_path": old_path, "reason": "目标名称已存在"})
                continue
            candidates.append({
                "chat_id": key[0], "message_id": key[1],
                "old_path": old_path, "new_path": new_path,
                "size": int(remote["size"]),
            })
        self.rename_plan_ready.emit({"candidates": candidates, "skipped": skipped})

    async def _move_remote(self, client: httpx.AsyncClient, source: str, target: str) -> None:
        source_parts = list(PurePosixPath(source).parts[1:])
        target_parts = list(PurePosixPath(target).parts[1:])
        response = await client.request(
            "MOVE", self._webdav_url(source_parts),
            headers={"Destination": self._webdav_url(target_parts), "Overwrite": "F"},
        )
        if response.status_code not in {201, 204}:
            raise RuntimeError(f"云端改名失败：HTTP {response.status_code}")

    async def probe_cloud_rename(self) -> None:
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        async with self._scan_lock:
            await self._probe_cloud_rename_unlocked()

    async def _probe_cloud_rename_unlocked(self) -> None:
        """Prove this mounted drive can rename before enabling legacy migration."""
        self._rename_verified = False
        root = [self.config.openlist_mount.strip("/"), sanitize_component(self.config.cloud_root)]
        token = uuid4().hex
        old_parts = [*root, f".__tvd_probe_{token}.txt"]
        new_parts = [*root, f".__tvd_probe_{token}_renamed.txt"]
        old_path = "/" + "/".join(old_parts)
        new_path = "/" + "/".join(new_parts)
        cleanup_error = ""
        try:
            if not self.openlist.running():
                raise RuntimeError("OpenList 未运行。")
            async with httpx.AsyncClient(
                auth=("admin", self.openlist.password()), timeout=30, trust_env=False,
            ) as client:
                await self._ensure_directories(client, root)
                response = await client.put(self._webdav_url(old_parts), content=b"rename-probe")
                if response.status_code not in {200, 201, 204}:
                    raise RuntimeError(f"创建临时测试文件失败：HTTP {response.status_code}")
                try:
                    await self._move_remote(client, old_path, new_path)
                    new_info = await self._propfind(client, new_parts)
                    old_info = await self._propfind(client, old_parts)
                    if not new_info or int(new_info["size"]) != len(b"rename-probe") or old_info:
                        raise RuntimeError("改名后新旧路径核验失败。")
                    self._rename_verified = True
                finally:
                    for parts in (new_parts, old_parts):
                        try:
                            if await self._propfind(client, parts) is not None:
                                result = await client.delete(self._webdav_url(parts))
                                if result.status_code not in {200, 204, 404}:
                                    cleanup_error = f"临时文件清理失败：HTTP {result.status_code}"
                        except Exception as exc:
                            cleanup_error = f"临时文件清理失败：{exc}"
            message = "当前云盘挂载的小文件改名已实测通过。"
            if cleanup_error:
                self._rename_verified = False
                message = cleanup_error
            self.rename_probe_result.emit(self._rename_verified, message)
        except Exception as exc:
            self._rename_verified = False
            self.rename_probe_result.emit(False, f"改名实测未通过：{exc}")

    async def execute_cloud_renames(self, items: list[dict]) -> None:
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        async with self._scan_lock:
            await self._execute_cloud_renames_unlocked(items)

    async def _execute_cloud_renames_unlocked(self, items: list[dict]) -> None:
        if not self._rename_verified:
            raise RuntimeError("请先用临时小文件实测当前云盘挂载的改名能力。")
        if self._fresh_cloud_files is None:
            raise RuntimeError("云端清单尚未核实。")
        results: list[dict] = []
        by_path = {f["remote_path"]: f for f in self._fresh_cloud_files}
        async with httpx.AsyncClient(
            auth=("admin", self.openlist.password()), timeout=60, trust_env=False,
        ) as client:
            for item in items:
                key = (int(item["chat_id"]), int(item["message_id"]))
                old_path = str(item["old_path"])
                new_path = str(item["new_path"])
                record = self.storage.get_upload(*key)
                try:
                    if not record or record["status"] != "completed" or record["remote_path"] != old_path:
                        raise RuntimeError("上传记录已变化")
                    if int(record["remote_size"]) != int(item["size"]):
                        raise RuntimeError("上传记录与云端大小不符")
                    if new_path != str(PurePosixPath(old_path).with_name(
                        cloud_name(PurePosixPath(old_path).name, *key)
                    )):
                        raise RuntimeError("目标名称与视频标识不一致")
                    if new_path in by_path:
                        raise RuntimeError("目标文件已存在")
                    old_parts = list(PurePosixPath(old_path).parts[1:])
                    new_parts = list(PurePosixPath(new_path).parts[1:])
                    old_info = await self._propfind(client, old_parts)
                    if not old_info or int(old_info["size"]) != int(item["size"]):
                        raise RuntimeError("源文件不存在或大小变化")
                    if await self._propfind(client, new_parts) is not None:
                        raise RuntimeError("目标名称已存在")
                    await self._move_remote(client, old_path, new_path)
                    new_info = await self._propfind(client, new_parts)
                    old_after = await self._propfind(client, old_parts)
                    if not new_info or int(new_info["size"]) != int(item["size"]) or old_after:
                        raise RuntimeError("改名后核验失败，请检查两个路径")
                    self.library.record_cloud_rename(*key, old_path, new_path)
                    by_path[new_path] = {**by_path.pop(old_path), "remote_path": new_path}
                    results.append({**item, "ok": True, "detail": "已核验"})
                except Exception as exc:
                    results.append({**item, "ok": False, "detail": str(exc)})
        self._fresh_cloud_files = list(by_path.values())
        self.cloud_inventory_ready.emit(self._fresh_cloud_files, "")
        self.rename_result.emit(results)

    async def test_mount(self) -> None:
        if not self.openlist.running():
            raise RuntimeError("OpenList 未运行。")
        parts = [self.config.openlist_mount.strip("/")]
        async with httpx.AsyncClient(
            auth=("admin", self.openlist.password()), timeout=20, trust_env=False
        ) as client:
            info = await self._propfind(client, parts)
        if info is None:
            raise RuntimeError(
                f"未找到 /{self.config.openlist_mount}，请在 OpenList 中完成阿里云盘挂载。"
            )
        self.config.cloud_enabled = True
        self.config.save(self.paths.config_file)
        state = self.openlist.state()
        state["mount_ready"] = True
        self.cloud_state.emit(state)
        self.status.emit("阿里云盘挂载测试成功，自动上传已启用。")
        await self.scan_cloud_inventory()

    async def verify_uploads(self, keys: list[tuple[int, int]]) -> None:
        if not self.openlist.running():
            raise RuntimeError("OpenList 未运行，无法刷新云端状态。")
        async with httpx.AsyncClient(
            auth=("admin", self.openlist.password()),
            timeout=20,
            trust_env=False,
        ) as client:
            for raw in keys:
                key = (int(raw[0]), int(raw[1]))
                record = self.storage.get_upload(*key)
                if not record or not record["remote_path"]:
                    continue
                parts = [part for part in record["remote_path"].split("/") if part]
                info = await self._propfind(client, parts)
                if info is None:
                    self.storage.update_upload(
                        *key, "remote_missing", "云端文件已被删除"
                    )
                    self.upload_state.emit(
                        *key, "remote_missing", "云端文件已被删除"
                    )
                elif int(info["size"]) == int(record["remote_size"]):
                    self.storage.update_upload(
                        *key,
                        "completed",
                        "云端文件存在，大小校验通过",
                        remote_path=record["remote_path"],
                        remote_size=int(info["size"]),
                        remote_etag=info["etag"],
                    )
                    self.upload_state.emit(
                        *key, "completed", record["remote_path"]
                    )
                else:
                    self.storage.update_upload(
                        *key, "failed", "云端文件大小与上传记录不一致"
                    )
                    self.upload_state.emit(
                        *key, "failed", "云端文件大小与上传记录不一致"
                    )

    def stop_gracefully(self, timeout_ms: int = 8000) -> bool:
        if not self.isRunning():
            return True
        self._shutting_down = True
        self.openlist.stop()
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(timeout_ms / 1000)
        return not self.isRunning()
