from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .config import AppConfig
from .paths import AppPaths
from .storage import Storage

LOGGER = logging.getLogger(__name__)

PROFILES: dict[str, dict[str, Any]] = {
    "high": {"label": "高质量（CRF 20）", "crf": 20},
    "balanced": {"label": "平衡（CRF 24）", "crf": 24},
    "compact": {"label": "强压缩（CRF 28）", "crf": 28},
}


class CompressionWorker(QObject):
    status = Signal(str)
    error = Signal(str)
    compression_job = Signal(object)
    compression_state = Signal(object, object, str, str)
    compression_progress = Signal(object, object, int)
    compression_metrics = Signal(object, object, object)

    def __init__(self, paths: AppPaths, config: AppConfig, upload_worker: Any | None = None) -> None:
        super().__init__()
        self.paths = paths
        self.config = config
        self.storage = Storage(paths.database_file)
        self.upload_worker = upload_worker
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue[tuple[tuple[int, int], dict]] | None = None
        self._pause_event: asyncio.Event | None = None
        self._worker_task: asyncio.Task | None = None
        self._cancelled: set[tuple[int, int]] = set()
        self._jobs: dict[tuple[int, int], dict] = {}
        self._active: set[tuple[int, int]] = set()
        self._shutting_down = False

    def start(self) -> None:
        if self.is_running():
            return
        self._thread = threading.Thread(target=self.run, name="CompressionWorker", daemon=False)
        self._thread.start()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._queue = asyncio.Queue()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._worker_task = self.loop.create_task(self._worker_loop())
        self.loop.create_task(self._restore_jobs())
        try:
            self.loop.run_forever()
        finally:
            self._shutting_down = True
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()
            self.loop = None

    def submit(self, method_name: str, *args: Any) -> None:
        future = self.submit_future(method_name, *args)
        if future is None:
            self.error.emit("压缩工作线程尚未就绪，请稍后重试。")
            return

        def report(done: Any) -> None:
            try:
                done.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                pass
            except Exception as exc:
                LOGGER.exception("Compression background operation failed")
                self.error.emit(str(exc))

        future.add_done_callback(report)

    def submit_future(self, method_name: str, *args: Any) -> concurrent.futures.Future | None:
        if not self.loop or not self.loop.is_running():
            return None
        return asyncio.run_coroutine_threadsafe(getattr(self, method_name)(*args), self.loop)

    async def _restore_jobs(self) -> None:
        await asyncio.sleep(0)
        for row in self.storage.pending_compressions():
            source = Path(row["current_path"] or row["original_path"])
            if not source.is_file():
                self.storage.update_compression(int(row["chat_id"]), int(row["message_id"]), "failed", "源文件不存在；请重新下载后重试。")
                continue
            payload = {
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
                "chat_title": "",
                "name": source.name,
                "file_path": str(source),
                "size": source.stat().st_size,
                "profile": row["profile"] if row["profile"] in PROFILES else "balanced",
            }
            self.compression_job.emit(dict(payload))
            if row["status"] in {"queued", "compressing"}:
                await self.enqueue_compressions([payload], payload["profile"], restored=True)
            else:
                self.compression_state.emit(payload["chat_id"], payload["message_id"], row["status"], row["detail"])

    async def enqueue_compressions(self, items: list[dict], profile: str = "balanced", restored: bool = False) -> None:
        if profile not in PROFILES:
            profile = "balanced"
        assert self._queue is not None
        for raw in items:
            key = (int(raw["chat_id"]), int(raw["message_id"]))
            if key in self._jobs or key in self._active:
                continue
            source = Path(str(raw.get("file_path", "")))
            if not source.is_file():
                self.compression_state.emit(*key, "failed", "本地文件不存在。")
                continue
            job = dict(raw)
            job["file_path"] = str(source)
            job["size"] = source.stat().st_size
            job["profile"] = profile
            self._jobs[key] = job
            self.storage.save_compression_job(*key, str(source), job["size"], profile)
            if not restored:
                self.compression_job.emit(dict(job))
            self.compression_state.emit(*key, "queued", "等待压缩")
            await self._queue.put((key, job))

    async def set_paused(self, paused: bool) -> None:
        assert self._pause_event is not None
        if paused:
            self._pause_event.clear()
            self.status.emit("压缩队列已暂停。")
        else:
            self._pause_event.set()
            self.status.emit("压缩队列已恢复。")

    async def cancel_compressions(self, keys: list[tuple[int, int]]) -> None:
        for raw in keys:
            key = (int(raw[0]), int(raw[1]))
            if key in self._active:
                self._cancelled.add(key)
            elif key in self._jobs:
                self._jobs.pop(key, None)
                self.storage.update_compression(*key, "cancelled", "已取消")
                self.compression_state.emit(*key, "cancelled", "已取消")

    async def _worker_loop(self) -> None:
        while True:
            assert self._queue is not None
            key, job = await self._queue.get()
            self._active.add(key)
            try:
                await self._pause_event.wait() if self._pause_event else None
                if key in self._cancelled:
                    raise asyncio.CancelledError()
                await self._compress_one(job)
            except asyncio.CancelledError:
                if self._shutting_down:
                    raise
                self.storage.update_compression(*key, "cancelled", "已取消")
                self.compression_state.emit(*key, "cancelled", "已取消")
            except Exception as exc:
                LOGGER.exception("Compression failed for %s", key)
                self.storage.update_compression(*key, "failed", str(exc), temp_path="")
                record = self.storage.get_compression(*key)
                if record and record.get("snapshot_path"):
                    asyncio.create_task(self._cleanup_snapshot_after_upload(key, Path(record["snapshot_path"])))
                self.compression_state.emit(*key, "failed", str(exc))
            finally:
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

    def ffprobe_path(self, ffmpeg: Path) -> Path | None:
        sibling = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.casefold() == ".exe" else "ffprobe")
        if sibling.is_file():
            return sibling
        located = shutil.which("ffprobe")
        return Path(located) if located else None

    async def _probe(self, ffprobe: Path, source: Path) -> tuple[float, bool]:
        args = [str(ffprobe), "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(source)]
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"FFprobe 校验失败：{stderr.decode('utf-8', errors='replace')[-300:]}")
        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            duration = float(data.get("format", {}).get("duration") or 0)
            has_video = any(item.get("codec_type") == "video" for item in data.get("streams", []))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("FFprobe 返回了无法解析的结果。") from exc
        return duration, has_video

    async def _snapshot_for_upload(self, job: dict, source: Path) -> Path | None:
        if not self.upload_worker:
            return None
        key = (int(job["chat_id"]), int(job["message_id"]))
        missing_checks = 0
        while True:
            upload = self.storage.get_upload(*key)
            if upload is None and self.config.cloud_enabled and self.config.cloud_auto_upload and missing_checks < 20:
                missing_checks += 1
                await asyncio.sleep(0.25)
                continue
            if not upload or upload["status"] in {"completed", "failed", "cancelled", "remote_missing"}:
                break
            if upload["status"] == "uploading":
                await asyncio.sleep(1.0)
                continue
            snapshot = self.paths.compression_staging_dir / "originals" / f"{key[0]}_{key[1]}{source.suffix}"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if not snapshot.exists():
                await asyncio.to_thread(shutil.copy2, source, snapshot)
            future = self.upload_worker.submit_future("replace_upload_source", key[0], key[1], str(snapshot))
            if future is None:
                raise RuntimeError("上传工作线程尚未就绪，无法保护原始上传文件。")
            replaced = await asyncio.wrap_future(future)
            if not replaced:
                await asyncio.sleep(1.0)
                continue
            self.storage.update_compression(*key, "compressing", "已暂存原始文件，正在压缩", snapshot_path=str(snapshot))
            return snapshot
        return None

    async def _compress_one(self, job: dict) -> None:
        key = (int(job["chat_id"]), int(job["message_id"]))
        source = Path(job["file_path"])
        if not source.is_file():
            raise RuntimeError("源文件不存在；本地记录已保留，可重新下载后重试。")
        ffmpeg = self.ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("未找到 FFmpeg，请在云盘设置中选择 ffmpeg.exe。")
        ffprobe = self.ffprobe_path(ffmpeg)
        if not ffprobe:
            raise RuntimeError("未找到 ffprobe.exe，无法安全校验压缩结果。")
        original_size = source.stat().st_size
        duration, has_video = await self._probe(ffprobe, source)
        if not has_video or duration <= 0:
            raise RuntimeError("源文件没有可校验的视频流。")
        snapshot = await self._snapshot_for_upload(job, source)
        self.storage.update_compression(*key, "compressing", f"正在使用{PROFILES[job['profile']]['label']}压缩")
        self.compression_state.emit(*key, "compressing", f"正在使用{PROFILES[job['profile']]['label']}压缩")
        output_ext = source.suffix.casefold()
        if output_ext in {".webm", ".avi", ".flv", ".ts"}:
            output_ext = ".mkv"
        final_path = source if output_ext == source.suffix.casefold() else source.with_suffix(output_ext)
        temp = self.paths.compression_staging_dir / f"{key[0]}_{key[1]}.part{output_ext}"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.unlink(missing_ok=True)
        self.storage.update_compression(*key, "compressing", temp_path=str(temp), snapshot_path=str(snapshot or ""))
        args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0", "-map_chapters", "0", "-c:v", "libx265", "-preset", "medium", "-crf", str(PROFILES[job["profile"]]["crf"]), "-c:a", "copy", "-c:s", "copy", "-progress", "pipe:1", "-nostats"]
        if output_ext in {".mp4", ".mov", ".m4v"}:
            args.extend(["-tag:v", "hvc1", "-movflags", "+faststart"])
        args.append(str(temp))
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        last_emit = 0.0
        async for raw_line in process.stdout:
            if key in self._cancelled:
                process.terminate()
                raise asyncio.CancelledError()
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("out_time_ms="):
                try:
                    current_seconds = int(line.split("=", 1)[1]) / 1_000_000
                    percent = min(99, max(0, int(current_seconds * 100 / duration)))
                    now = time.monotonic()
                    if now - last_emit >= 0.25 or percent >= 99:
                        self.compression_progress.emit(*key, percent)
                        self.compression_metrics.emit(*key, {"phase": "compression", "progress": percent, "speed": 0.0, "eta": 0.0})
                        last_emit = now
                except ValueError:
                    pass
        stderr = await process.stderr.read()
        await process.wait()
        if process.returncode != 0 or not temp.is_file():
            raise RuntimeError(f"FFmpeg 压缩失败：{stderr.decode('utf-8', errors='replace')[-500:]}")
        compressed_duration, compressed_has_video = await self._probe(ffprobe, temp)
        if not compressed_has_video or abs(compressed_duration - duration) > max(2.0, duration * 0.05):
            raise RuntimeError("压缩结果的视频流或时长校验失败。")
        compressed_size = temp.stat().st_size
        if compressed_size >= original_size:
            temp.unlink(missing_ok=True)
            self.storage.update_compression(*key, "not_smaller", "压缩结果未变小，已保留原文件")
            self.compression_state.emit(*key, "not_smaller", "压缩结果未变小，已保留原文件")
            if snapshot:
                asyncio.create_task(self._cleanup_snapshot_after_upload(key, snapshot))
            return
        if final_path != source and final_path.exists():
            final_path.unlink()
        temp.replace(final_path)
        if source != final_path and source.exists():
            source.unlink()
        self.storage.update_download_file(*key, str(final_path), compressed_size)
        saved = original_size - compressed_size
        percent = saved * 100 / original_size if original_size else 0.0
        self.storage.update_compression(*key, "completed", f"压缩完成，节省 {percent:.1f}%", str(final_path), compressed_size, saved, percent, "", str(snapshot or ""))
        self.compression_progress.emit(*key, 100)
        self.compression_metrics.emit(*key, {"phase": "compression", "progress": 100, "current": compressed_size, "total": original_size, "speed": 0.0, "eta": 0.0, "saved": saved, "saved_percent": percent})
        self.compression_state.emit(*key, "completed", str(final_path))
        if snapshot:
            asyncio.create_task(self._cleanup_snapshot_after_upload(key, snapshot))

    async def _cleanup_snapshot_after_upload(self, key: tuple[int, int], snapshot: Path) -> None:
        for _ in range(720):
            row = self.storage.get_upload(*key)
            if row and row["status"] == "completed":
                snapshot.unlink(missing_ok=True)
                return
            await asyncio.sleep(5)

    def stop_gracefully(self, timeout_ms: int = 8000) -> bool:
        if not self.is_running():
            return True
        self._shutting_down = True
        if self.loop and self.loop.is_running():
            def cancel_and_stop() -> None:
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                self.loop.stop()
            self.loop.call_soon_threadsafe(cancel_and_stop)
        if self._thread:
            self._thread.join(timeout_ms / 1000)
        return not self.is_running()
