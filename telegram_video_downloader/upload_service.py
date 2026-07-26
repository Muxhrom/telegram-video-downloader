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
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from PySide6.QtCore import QObject, Signal

from .config import AppConfig
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

    def __init__(self, paths: AppPaths, config: AppConfig) -> None:
        super().__init__()
        self.paths = paths
        self.config = config
        self.storage = Storage(paths.database_file)
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
        self._worker_task = self.loop.create_task(self._upload_worker())
        self.loop.create_task(self._restore_jobs())
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
            self.upload_state.emit(*key, "completed", previous["remote_path"])
            return
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
            processed.name,
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
        if not self.openlist.running():
            raise RuntimeError("OpenList 未运行，请先在云盘设置中启动。")
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
                        processed.name,
                        remote_path,
                        processed_size,
                        existing["etag"],
                    )
                    self.upload_progress.emit(*key, 100)
                    self.upload_state.emit(*key, "completed", remote_path)
                    return
                if existing:
                    renamed = processed.with_name(
                        f"{processed.stem}_tg_{key[1]}{processed.suffix}"
                    )
                    processed.replace(renamed)
                    processed = renamed
                    parts[-1] = processed.name
                remote_path = "/" + "/".join(parts)
                self.storage.update_upload(
                    *key, "uploading", remote_path, processed.name, remote_path
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
                    processed.name,
                    remote_path,
                    size,
                    verified["etag"],
                )
                self.upload_progress.emit(*key, 100)
                self.upload_metrics.emit(
                    *key, {"current": size, "total": size, "speed": 0.0, "eta": 0.0}
                )
                self.upload_state.emit(*key, "completed", remote_path)
        finally:
            processed.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                processed.parent.rmdir()

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
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(timeout_ms / 1000)
        return not self.isRunning()
