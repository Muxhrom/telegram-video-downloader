from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeVideo,
    InputMessagesFilterDocument,
    InputMessagesFilterRoundVideo,
    InputMessagesFilterVideo,
)

from .config import AppConfig
from .credentials import save_api_hash
from .models import ChatInfo, VideoInfo
from .naming import choose_video_name, extension_for, sanitize_component, unique_target
from .paths import AppPaths
from .proxy import proxy_available
from .storage import Storage


LOGGER = logging.getLogger(__name__)


class TelegramWorker(QObject):
    status = Signal(str)
    error = Signal(str)
    auth_state = Signal(str, str)
    chats_ready = Signal(object)
    # Telegram peer IDs are 64-bit values (for example -1002985557858).
    # Qt's Signal(int) is a signed 32-bit C++ int and silently truncates them.
    videos_ready = Signal(int, object, object, object, bool)
    video_scan_status = Signal(int, object, str)
    download_progress = Signal(object, object, int)
    download_metrics = Signal(object, object, object)
    download_state = Signal(object, object, str, str)
    download_job = Signal(object)
    download_priority = Signal(object, object, int)
    thumbnail_ready = Signal(int, object, object, object, str)
    acceleration_changed = Signal(bool, str)
    downloaded_names_ready = Signal(int, object, str)
    connection_changed = Signal(bool, str)

    def __init__(self, paths: AppPaths, config: AppConfig) -> None:
        super().__init__()
        self.paths = paths
        self.config = config
        self.storage = Storage(paths.database_file)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: TelegramClient | None = None
        self._api_hash = ""
        self._phone = ""
        self._queue: asyncio.PriorityQueue | None = None
        self._queue_workers: list[asyncio.Task] = []
        self._acceleration_enabled = False
        self._acceleration_event: asyncio.Event | None = None
        self._queue_sequence = 0
        self._queue_versions: dict[tuple[int, int], int] = {}
        self._queued_jobs: dict[tuple[int, int], dict[str, Any]] = {}
        self._active_downloads: set[tuple[int, int]] = set()
        self._pause_event: asyncio.Event | None = None
        self._cancelled: set[tuple[int, int]] = set()
        self._queued: set[tuple[int, int]] = set()
        self._reserved_paths: set[Path] = set()
        self._chat_entities: dict[int, Any] = {}
        self._chat_titles: dict[int, str] = {}
        self._video_scan_task: asyncio.Task | None = None
        self._thumbnail_request_id = 0
        self._thumbnail_messages: dict[tuple[int, int], Any] = {}
        self._thumbnail_semaphore: asyncio.Semaphore | None = None
        self._shutting_down = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.isRunning():
            return
        self._thread = threading.Thread(
            target=self.run,
            name="TelegramAsyncWorker",
            daemon=False,
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
        self._acceleration_event = asyncio.Event()
        self._thumbnail_semaphore = asyncio.Semaphore(2)
        worker_count = self.config.max_concurrent_downloads + 1
        self._queue_workers = [
            self.loop.create_task(self._download_worker(index))
            for index in range(worker_count)
        ]
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()
            self.loop = None

    def submit(self, method_name: str, *args: Any) -> None:
        if not self.loop or not self.loop.is_running():
            self.error.emit("Telegram 工作线程尚未就绪，请稍后重试。")
            return
        coroutine = getattr(self, method_name)(*args)
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def report_failure(completed: Any) -> None:
            try:
                completed.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                pass
            except Exception as exc:  # pragma: no cover - Qt callback boundary
                LOGGER.exception("Telegram background operation failed")
                self.error.emit(str(exc))

        future.add_done_callback(report_failure)

    async def connect_account(self, api_id: int, api_hash: str, phone: str) -> None:
        if not await asyncio.to_thread(
            proxy_available, self.config.proxy_host, self.config.proxy_port
        ):
            self.connection_changed.emit(False, "代理未启动")
            self.error.emit("无法连接 127.0.0.1:7890，请先启动 Clash。程序不会尝试直连。")
            return
        if self.client:
            await self.client.disconnect()
        self._api_hash = api_hash
        self._phone = phone.strip()
        proxy = {
            "proxy_type": self.config.proxy_type,
            "addr": self.config.proxy_host,
            "port": self.config.proxy_port,
            "rdns": True,
        }
        self.client = TelegramClient(
            str(self.paths.session_file),
            api_id,
            api_hash,
            proxy=proxy,
            auto_reconnect=True,
            connection_retries=5,
            retry_delay=2,
            flood_sleep_threshold=60,
        )
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.status.emit("正在通过 SOCKS5 代理连接 Telegram…")
        await self.client.connect()
        self.connection_changed.emit(True, "已连接")
        if await self.client.is_user_authorized():
            await self._finish_authorization()
        elif self._phone:
            await self.client.send_code_request(self._phone)
            self.auth_state.emit("code_required", "验证码已发送到 Telegram。")
        else:
            self.auth_state.emit("login_required", "请输入手机号后登录。")

    async def submit_code(self, code: str) -> None:
        self._require_client()
        try:
            await self.client.sign_in(phone=self._phone, code=code.strip())
        except SessionPasswordNeededError:
            self.auth_state.emit("password_required", "账号已启用二步验证，请输入密码。")
            return
        await self._finish_authorization()

    async def submit_password(self, password: str) -> None:
        self._require_client()
        await self.client.sign_in(password=password)
        await self._finish_authorization()

    async def _finish_authorization(self) -> None:
        save_api_hash(self._api_hash)
        me = await self.client.get_me()
        display = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
        self.auth_state.emit("authorized", display or getattr(me, "username", "已登录"))
        self.status.emit("Telegram 登录成功。")
        await self.load_chats()

    async def load_chats(self) -> None:
        self._require_authorized()
        chats: list[dict] = []
        self._chat_entities.clear()
        self._chat_titles.clear()
        async for dialog in self.client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            kind = "群组" if dialog.is_group else "频道"
            chat_id = int(dialog.id)
            chat_title = dialog.name or "未命名"
            self._chat_entities[chat_id] = dialog.input_entity
            self._chat_titles[chat_id] = chat_title
            chats.append(ChatInfo(chat_id, chat_title, kind).to_dict())
        chats.sort(key=lambda item: item["title"].casefold())
        LOGGER.info("Loaded %d chats and cached their input entities", len(chats))
        self.chats_ready.emit(chats)

    async def start_video_scan(
        self,
        request_id: int,
        chat_id: int,
        page_state: dict | None = None,
        page_size: int = 100,
    ) -> None:
        """Cancel the previous history scan and start exactly one new page query."""
        LOGGER.info(
            "Video scan requested request_id=%s chat_id=%s page_size=%s has_state=%s",
            request_id,
            chat_id,
            page_size,
            bool(page_state),
        )
        if request_id != self._thumbnail_request_id:
            self._thumbnail_request_id = request_id
            self._thumbnail_messages.clear()
        previous = self._video_scan_task
        if previous and not previous.done():
            LOGGER.info("Cancelling previous video scan before request_id=%s", request_id)
            previous.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await previous
        task = asyncio.create_task(
            self._load_video_page(request_id, chat_id, page_state or {}, page_size)
        )
        self._video_scan_task = task
        try:
            await task
        except asyncio.CancelledError:
            LOGGER.info("Video scan cancelled request_id=%s chat_id=%s", request_id, chat_id)
        except Exception:
            LOGGER.exception("Video scan failed request_id=%s chat_id=%s", request_id, chat_id)
            failed_state = dict(page_state or {})
            failed_state["reached_end"] = False
            self.videos_ready.emit(request_id, chat_id, [], failed_state, True)
            raise
        finally:
            if self._video_scan_task is task:
                self._video_scan_task = None

    async def _load_video_page(
        self,
        request_id: int,
        chat_id: int,
        page_state: dict,
        page_size: int,
    ) -> None:
        self._require_authorized()
        entity = self._chat_entities.get(chat_id)
        if entity is None:
            LOGGER.info("Input entity cache miss for chat_id=%s", chat_id)
            try:
                entity = await asyncio.wait_for(
                    self.client.get_input_entity(chat_id),
                    timeout=15,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "获取群聊信息超过 15 秒。请检查 Clash 连接后点击“登录 / 重连”。"
                ) from exc
            self._chat_entities[chat_id] = entity
        else:
            LOGGER.info("Using cached input entity for chat_id=%s", chat_id)
        chat_title = self._chat_titles.get(chat_id, "未命名")
        filter_specs = (
            ("video", InputMessagesFilterVideo),
            ("round", InputMessagesFilterRoundVideo),
            ("document", InputMessagesFilterDocument),
        )
        cursors = {
            key: int((page_state.get("cursors") or {}).get(key, 0))
            for key, _ in filter_specs
        }
        exhausted = {
            key: bool((page_state.get("exhausted") or {}).get(key, False))
            for key, _ in filter_specs
        }
        pending = list(page_state.get("pending") or [])
        seen_ids = {int(value) for value in (page_state.get("seen_ids") or [])}
        emitted = 0
        scanned = 0

        pending.sort(key=lambda item: item["date"], reverse=True)
        if pending:
            chunk, pending = pending[:page_size], pending[page_size:]
            emitted += len(chunk)
            self.videos_ready.emit(request_id, chat_id, chunk, {}, False)

        for key, filter_type in filter_specs:
            if emitted >= page_size and page_state.get("pending"):
                break
            if exhausted[key]:
                continue
            self.video_scan_status.emit(
                request_id,
                chat_id,
                f"正在查询{self._filter_label(key)}；已扫描 {scanned} 条媒体，显示 {emitted} 个视频…",
            )
            batch = await self._fetch_filtered_batch(
                request_id,
                chat_id,
                entity,
                filter_type,
                cursors[key],
                page_size,
            )
            scanned += len(batch)
            if batch:
                cursors[key] = int(batch[-1].id)
            if len(batch) < page_size:
                exhausted[key] = True

            candidates: list[dict] = []
            for message in batch:
                message_id = int(message.id)
                if message_id in seen_ids:
                    continue
                info = self._video_info(message, chat_id, chat_title)
                if not info:
                    continue
                seen_ids.add(message_id)
                self._thumbnail_messages[(chat_id, message_id)] = message
                candidates.append(info.to_dict())
            LOGGER.info(
                "Video filter completed request_id=%s chat_id=%s filter=%s offset_id=%s "
                "returned=%s accepted=%s",
                request_id,
                chat_id,
                key,
                cursors[key],
                len(batch),
                len(candidates),
            )
            candidates.sort(key=lambda item: item["date"], reverse=True)
            available = max(0, page_size - emitted)
            chunk = candidates[:available]
            pending.extend(candidates[available:])
            if chunk:
                emitted += len(chunk)
                self.videos_ready.emit(request_id, chat_id, chunk, {}, False)
            self.video_scan_status.emit(
                request_id,
                chat_id,
                f"已扫描 {scanned} 条媒体，显示 {emitted} 个视频；继续查询…",
            )

        pending.sort(key=lambda item: item["date"], reverse=True)
        reached_end = all(exhausted.values()) and not pending
        state = {
            "cursors": cursors,
            "exhausted": exhausted,
            "pending": pending,
            "seen_ids": sorted(seen_ids),
            "reached_end": reached_end,
        }
        self.videos_ready.emit(request_id, chat_id, [], state, True)
        if emitted:
            message = f"扫描完成：本页显示 {emitted} 个视频，共检查 {scanned} 条媒体。"
        elif reached_end:
            message = "扫描完成：Telegram 未返回可下载的视频（GIF/动画已排除）。"
        else:
            message = f"本页检查了 {scanned} 条媒体，暂未发现视频；可继续加载更早内容。"
        self.video_scan_status.emit(request_id, chat_id, message)
        LOGGER.info(
            "Video scan completed request_id=%s chat_id=%s scanned=%s emitted=%s "
            "pending=%s reached_end=%s",
            request_id,
            chat_id,
            scanned,
            emitted,
            len(pending),
            reached_end,
        )

    async def _fetch_filtered_batch(
        self,
        request_id: int,
        chat_id: int,
        entity: Any,
        filter_type: type,
        offset_id: int,
        limit: int,
    ) -> list[Any]:
        while True:
            old_threshold = getattr(self.client, "flood_sleep_threshold", 60)
            try:
                self.client.flood_sleep_threshold = 0
                async def collect() -> list[Any]:
                    return [
                        message
                        async for message in self.client.iter_messages(
                            entity,
                            limit=limit,
                            offset_id=offset_id,
                            filter=filter_type(),
                        )
                    ]

                return await asyncio.wait_for(collect(), timeout=60)
            except FloodWaitError as exc:
                seconds = max(1, int(exc.seconds))
                LOGGER.warning(
                    "Video scan flood wait request_id=%s chat_id=%s filter=%s seconds=%s",
                    request_id,
                    chat_id,
                    filter_type.__name__,
                    seconds,
                )
                for remaining in range(seconds, 0, -1):
                    self.video_scan_status.emit(
                        request_id,
                        chat_id,
                        f"Telegram 请求限流，{remaining} 秒后自动继续；已有结果不会消失。",
                    )
                    await asyncio.sleep(1)
            except asyncio.TimeoutError as exc:
                LOGGER.error(
                    "Video filter timed out request_id=%s chat_id=%s filter=%s "
                    "offset_id=%s limit=%s",
                    request_id,
                    chat_id,
                    filter_type.__name__,
                    offset_id,
                    limit,
                )
                raise RuntimeError(
                    f"{filter_type.__name__} 查询超过 60 秒。请检查 Clash 后重连并重试。"
                ) from exc
            finally:
                self.client.flood_sleep_threshold = old_threshold

    @staticmethod
    def _filter_label(key: str) -> str:
        return {
            "video": "普通视频",
            "round": "圆形视频",
            "document": "视频文件",
        }[key]

    async def load_video_thumbnails(
        self, request_id: int, chat_id: int, items: list[dict]
    ) -> None:
        """Load Telegram-provided thumbnails without downloading full videos."""
        if request_id != self._thumbnail_request_id or not self.client:
            return
        if self._thumbnail_semaphore is None:
            self._thumbnail_semaphore = asyncio.Semaphore(2)
        cache_dir = self.paths.data_dir / "thumbnails" / str(int(chat_id))
        for item in items:
            if request_id != self._thumbnail_request_id:
                return
            message_id = int(item["message_id"])
            key = (int(chat_id), message_id)
            cache_file = cache_dir / f"{message_id}.thumb"
            try:
                if cache_file.is_file() and cache_file.stat().st_size:
                    data = await asyncio.to_thread(cache_file.read_bytes)
                else:
                    message = self._thumbnail_messages.get(key)
                    if message is None:
                        entity = self._chat_entities.get(int(chat_id))
                        if entity is None:
                            entity = await self.client.get_input_entity(int(chat_id))
                            self._chat_entities[int(chat_id)] = entity
                        message = await self.client.get_messages(entity, ids=message_id)
                    document = getattr(message, "document", None)
                    if not message or not getattr(document, "thumbs", None):
                        self.thumbnail_ready.emit(
                            request_id, chat_id, message_id, b"", "Telegram 未提供缩略图"
                        )
                        self._thumbnail_messages.pop(key, None)
                        continue
                    async with self._thumbnail_semaphore:
                        downloaded = await self.client.download_media(
                            message, file=bytes, thumb=-1
                        )
                    data = bytes(downloaded) if downloaded else b""
                    if data:
                        await asyncio.to_thread(
                            self._write_thumbnail_cache, cache_file, data
                        )
                if request_id == self._thumbnail_request_id:
                    self.thumbnail_ready.emit(
                        request_id,
                        chat_id,
                        message_id,
                        data,
                        "" if data else "缩略图为空",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Thumbnail load failed request_id=%s chat_id=%s message_id=%s: %s",
                    request_id,
                    chat_id,
                    message_id,
                    exc,
                )
                if request_id == self._thumbnail_request_id:
                    self.thumbnail_ready.emit(
                        request_id, chat_id, message_id, b"", "缩略图加载失败"
                    )
            finally:
                self._thumbnail_messages.pop(key, None)

    @staticmethod
    def _write_thumbnail_cache(cache_file: Path, data: bytes) -> None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_name(cache_file.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(cache_file)

    async def enqueue_downloads(self, items: list[dict], directory: str) -> None:
        if not self._queue:
            return
        for item in items:
            key = (int(item["chat_id"]), int(item["message_id"]))
            if key in self._queued:
                continue
            priority = max(0, min(2, int(item.get("priority", 1))))
            job = {"item": dict(item), "directory": directory, "priority": priority}
            self.download_job.emit(dict(job))
            if self.storage.is_downloaded(*key):
                self.download_state.emit(*key, "completed", "此前已下载")
                continue
            self._queued.add(key)
            self._queued_jobs[key] = job
            self._queue_versions[key] = self._queue_versions.get(key, 0) + 1
            await self._put_download_job(key)
            self.download_state.emit(*key, "queued", "等待下载")

    async def _put_download_job(self, key: tuple[int, int]) -> None:
        assert self._queue is not None
        job = self._queued_jobs[key]
        self._queue_sequence += 1
        await self._queue.put(
            (
                int(job["priority"]),
                self._queue_sequence,
                self._queue_versions[key],
                key,
            )
        )

    async def set_download_priority(
        self, keys: list[tuple[int, int]], priority: int
    ) -> None:
        priority = max(0, min(2, int(priority)))
        for raw_key in keys:
            key = (int(raw_key[0]), int(raw_key[1]))
            job = self._queued_jobs.get(key)
            if not job or key in self._active_downloads:
                continue
            job["priority"] = priority
            self._queue_versions[key] = self._queue_versions.get(key, 0) + 1
            await self._put_download_job(key)
            self.download_priority.emit(*key, priority)
        self.status.emit("已更新等待任务的下载优先级。")

    async def set_acceleration_mode(self, enabled: bool) -> None:
        """Enable one conservative extra file-transfer slot.

        The extra slot improves aggregate throughput for multiple queued files. It
        deliberately does not split one Telegram file into concurrent requests.
        """
        self._acceleration_enabled = bool(enabled)
        if self._acceleration_event is None:
            self._acceleration_event = asyncio.Event()
        if self._acceleration_enabled:
            self._acceleration_event.set()
            reason = "安全加速已开启：最多同时下载 3 个文件。"
        else:
            self._acceleration_event.clear()
            reason = "安全加速已关闭：恢复最多同时下载 2 个文件。"
        LOGGER.info("Download acceleration changed enabled=%s reason=%s", enabled, reason)
        self.acceleration_changed.emit(self._acceleration_enabled, reason)
        self.status.emit(reason)

    def _disable_acceleration_after_error(self, exc: BaseException) -> None:
        if not self._acceleration_enabled or not isinstance(
            exc, (FloodWaitError, ConnectionError, asyncio.TimeoutError)
        ):
            return
        self._acceleration_enabled = False
        if self._acceleration_event is not None:
            self._acceleration_event.clear()
        if isinstance(exc, FloodWaitError):
            seconds = int(getattr(exc, "seconds", 0))
            reason = f"检测到 Telegram 限流（{seconds} 秒），已自动关闭安全加速。"
        else:
            reason = "检测到网络连接异常，已自动关闭安全加速。"
        LOGGER.warning("Download acceleration disabled automatically: %s", reason)
        self.acceleration_changed.emit(False, reason)
        self.status.emit(reason)

    async def scan_download_directory(self, request_id: int, directory: str) -> None:
        try:
            names = await asyncio.to_thread(self._collect_video_names, Path(directory))
            LOGGER.info(
                "Scanned download directory request_id=%s directory=%s video_names=%s",
                request_id,
                directory,
                len(names),
            )
            self.downloaded_names_ready.emit(request_id, names, "")
        except Exception as exc:
            LOGGER.exception("Failed to scan download directory %s", directory)
            self.downloaded_names_ready.emit(request_id, set(), str(exc))

    @staticmethod
    def _collect_video_names(directory: Path) -> set[str]:
        extensions = {
            ".3gp",
            ".avi",
            ".flv",
            ".m2ts",
            ".m4v",
            ".mkv",
            ".mov",
            ".mp4",
            ".mpeg",
            ".mpg",
            ".mts",
            ".ts",
            ".webm",
            ".wmv",
        }
        if not directory.exists() or not directory.is_dir():
            return set()
        names: set[str] = set()
        for root, _, files in os.walk(directory):
            del root
            for filename in files:
                path = Path(filename)
                if path.suffix.casefold() in extensions and not filename.casefold().endswith(".part"):
                    names.add(filename.casefold())
        return names

    async def _download_worker(self, worker_id: int) -> None:
        is_acceleration_worker = worker_id >= self.config.max_concurrent_downloads
        while True:
            assert self._queue is not None
            if is_acceleration_worker:
                assert self._acceleration_event is not None
                await self._acceleration_event.wait()
            queue_item = await self._queue.get()
            if is_acceleration_worker and not self._acceleration_enabled:
                await self._queue.put(queue_item)
                self._queue.task_done()
                continue
            _, _, version, key = queue_item
            job = self._queued_jobs.get(key)
            if not job or self._queue_versions.get(key) != version:
                self._queue.task_done()
                continue
            item = job["item"]
            self._active_downloads.add(key)
            try:
                if key in self._cancelled:
                    self.download_state.emit(*key, "cancelled", "已取消")
                    continue
                await self._download_one(item, Path(job["directory"]))
            except asyncio.CancelledError:
                if self._shutting_down:
                    raise
                self.download_state.emit(*key, "cancelled", "已取消")
            except Exception as exc:
                LOGGER.exception("Download failed for %s", key)
                self._disable_acceleration_after_error(exc)
                self.storage.record_download(*key, "", 0, "failed")
                self.download_state.emit(*key, "failed", str(exc))
            finally:
                if self._queue_versions.get(key) == version:
                    self._queued.discard(key)
                    self._queued_jobs.pop(key, None)
                self._cancelled.discard(key)
                self._active_downloads.discard(key)
                self._queue.task_done()

    async def _download_one(self, item: dict, directory: Path) -> None:
        self._require_authorized()
        key = (int(item["chat_id"]), int(item["message_id"]))
        assert self._pause_event is not None
        await self._pause_event.wait()
        entity = await self.client.get_entity(key[0])
        message = await self.client.get_messages(entity, ids=key[1])
        if not message or not self._video_info(message, key[0], item.get("chat_title", "")):
            raise RuntimeError("消息不存在或已不再包含视频。")
        target = unique_target(directory, item["name"], key[1], self._reserved_paths)
        self._reserved_paths.add(target)
        part = target.with_name(target.name + ".part")
        self.download_state.emit(*key, "downloading", str(target))
        started_at = time.monotonic()
        last_emit_at = 0.0
        last_bytes = 0
        last_sample_at = started_at
        smoothed_speed = 0.0

        async def progress(current: int, total: int) -> None:
            nonlocal last_emit_at, last_bytes, last_sample_at, smoothed_speed
            if key in self._cancelled:
                raise asyncio.CancelledError()
            await self._pause_event.wait()
            percent = int(current * 100 / total) if total else 0
            self.download_progress.emit(*key, percent)
            now = time.monotonic()
            if now - last_emit_at < 0.25 and current < total:
                return
            elapsed = max(now - last_sample_at, 0.001)
            instant_speed = max(0.0, (current - last_bytes) / elapsed)
            smoothed_speed = (
                instant_speed
                if smoothed_speed <= 0
                else smoothed_speed * 0.7 + instant_speed * 0.3
            )
            eta = (
                max(0.0, (total - current) / smoothed_speed)
                if total and smoothed_speed > 0
                else 0.0
            )
            self.download_metrics.emit(
                *key,
                {
                    "current": int(current),
                    "total": int(total),
                    "speed": float(smoothed_speed),
                    "eta": float(eta),
                    "elapsed": float(now - started_at),
                },
            )
            last_emit_at = now
            last_bytes = int(current)
            last_sample_at = now

        try:
            result = await self.client.download_media(message, file=str(part), progress_callback=progress)
            if not result or not part.exists():
                raise RuntimeError("Telegram 未返回下载文件。")
            part.replace(target)
        except BaseException:
            with contextlib.suppress(OSError):
                part.unlink()
            raise
        finally:
            self._reserved_paths.discard(target)
        size = target.stat().st_size
        self.storage.record_download(*key, str(target), size, "completed")
        self.download_progress.emit(*key, 100)
        self.download_metrics.emit(
            *key,
            {"current": size, "total": size, "speed": 0.0, "eta": 0.0},
        )
        self.download_state.emit(*key, "completed", str(target))

    async def set_paused(self, paused: bool) -> None:
        if not self._pause_event:
            return
        if paused:
            self._pause_event.clear()
            self.status.emit("下载队列已暂停。")
        else:
            self._pause_event.set()
            self.status.emit("下载队列已恢复。")

    async def cancel_downloads(self, keys: list[tuple[int, int]]) -> None:
        for a, b in keys:
            key = (int(a), int(b))
            if key in self._active_downloads:
                self._cancelled.add(key)
                continue
            if key in self._queued_jobs:
                self._queue_versions[key] = self._queue_versions.get(key, 0) + 1
                self._queued_jobs.pop(key, None)
                self._queued.discard(key)
                self.download_state.emit(*key, "cancelled", "已取消")

    async def save_auto_rule(
        self, chat_id: int, chat_title: str, enabled: bool, directory: str
    ) -> None:
        self.storage.save_rule(chat_id, chat_title, enabled, directory)
        state = "已开启" if enabled else "已关闭"
        self.status.emit(f"{chat_title} 自动下载{state}。")

    async def _on_new_message(self, event: Any) -> None:
        if self._shutting_down:
            return
        chat_id = int(event.chat_id or 0)
        rule = self.storage.enabled_rules().get(chat_id)
        if not rule or self.storage.is_downloaded(chat_id, int(event.message.id)):
            return
        info = self._video_info(event.message, chat_id, rule["chat_title"])
        if info:
            await self.enqueue_downloads([info.to_dict()], rule["directory"])

    async def logout(self) -> None:
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.log_out()
            await self.client.disconnect()
            self.client = None
        for suffix in ("", "-journal", "-shm", "-wal"):
            session_artifact = Path(f"{self.paths.session_file}{suffix}")
            with contextlib.suppress(OSError):
                session_artifact.unlink()
        self.connection_changed.emit(False, "未登录")
        self.auth_state.emit("logged_out", "已退出 Telegram。")

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._video_scan_task and not self._video_scan_task.done():
            self._video_scan_task.cancel()
            await asyncio.gather(self._video_scan_task, return_exceptions=True)
        if self.client:
            await self.client.disconnect()
        for task in self._queue_workers:
            task.cancel()
        await asyncio.gather(*self._queue_workers, return_exceptions=True)

    def stop_gracefully(self, timeout_seconds: float = 5.0) -> bool:
        if not self.isRunning():
            return True
        deadline = time.monotonic() + timeout_seconds
        while self.loop is None and self.isRunning() and time.monotonic() < deadline:
            time.sleep(0.01)
        loop = self.loop
        if not loop or not loop.is_running():
            return not self.isRunning()
        future = asyncio.run_coroutine_threadsafe(self.shutdown(), loop)
        try:
            future.result(timeout=timeout_seconds)
        except Exception as exc:
            LOGGER.warning("Graceful shutdown did not finish cleanly: %s", exc)
        finally:
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not self.isRunning()

    def _require_client(self) -> None:
        if not self.client:
            raise RuntimeError("尚未连接 Telegram。")

    def _require_authorized(self) -> None:
        self._require_client()

    @staticmethod
    def _video_info(message: Any, chat_id: int, chat_title: str) -> VideoInfo | None:
        document = getattr(message, "document", None)
        if not document:
            return None
        attributes = list(getattr(document, "attributes", []) or [])
        if any(isinstance(attribute, DocumentAttributeAnimated) for attribute in attributes):
            return None
        video_attribute = next(
            (attribute for attribute in attributes if isinstance(attribute, DocumentAttributeVideo)),
            None,
        )
        mime_type = getattr(document, "mime_type", "") or ""
        if not video_attribute and not mime_type.startswith("video/"):
            return None
        media_kind = "圆形视频" if getattr(video_attribute, "round_message", False) else (
            "视频文件" if not video_attribute else "普通视频"
        )
        file_object = getattr(message, "file", None)
        original_name = getattr(file_object, "name", None)
        extension = extension_for(original_name, mime_type)
        date = getattr(message, "date", None) or datetime.now(timezone.utc)
        name = choose_video_name(
            original_name,
            getattr(message, "message", None),
            date,
            int(message.id),
            extension,
        )
        return VideoInfo(
            chat_id=int(chat_id),
            message_id=int(message.id),
            chat_title=chat_title,
            name=name,
            media_kind=media_kind,
            size=int(getattr(document, "size", 0) or 0),
            date=date,
            extension=extension,
        )
