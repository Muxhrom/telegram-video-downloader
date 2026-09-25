from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QRect, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QMenu,
    QProgressBar,
)

from .config import AppConfig
from .cloud_catalog import classify_cloud_video
from .cloud_ui import CloudLibraryDialog
from .compression_service import PROFILES, CompressionWorker
from .credentials import delete_api_hash, load_api_hash
from .library import LibraryStore
from .naming import sanitize_component
from .paths import AppPaths
from .proxy import proxy_available
from .storage import Storage
from .telegram_service import TelegramWorker
from .upload_service import UploadWorker
from .upload_ui import CloudSettingsDialog, UploadManagerDialog


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return str(size)


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{seconds:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes:02d}分"


class ChatRowDelegate(QStyledItemDelegate):
    """Paint chat names on as many lines as the current sidebar needs."""

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        bounds = option.rect.adjusted(5, 3, -5, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#d9f3f2" if selected else "#edf4f5" if hovered else "#ffffff"))
        painter.drawRoundedRect(bounds, 9, 9)
        if selected:
            painter.setBrush(QColor("#118c8e"))
            painter.drawRoundedRect(QRect(bounds.left(), bounds.top() + 8, 4, bounds.height() - 16), 2, 2)
        title = str(index.data(Qt.UserRole + 1) or "")
        kind = str(index.data(Qt.UserRole + 2) or "")
        title_font = QFont(option.font)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor("#0f4546" if selected else "#1b3038"))
        text_rect = bounds.adjusted(16, 10, -13, -28)
        painter.drawText(text_rect, Qt.TextWordWrap | Qt.AlignTop, title)
        painter.setFont(option.font)
        painter.setPen(QColor("#42757b" if selected else "#73858c"))
        painter.drawText(bounds.adjusted(16, 0, -13, -10), Qt.AlignBottom, kind)
        painter.restore()


class ChatListWidget(QListWidget):
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resize_rows()

    def resize_rows(self) -> None:
        width = max(120, self.viewport().width() - 48)
        font = QFont(self.font())
        font.setWeight(QFont.DemiBold)
        metrics = QFontMetrics(font)
        for index in range(self.count()):
            item = self.item(index)
            title = str(item.data(Qt.UserRole + 1) or "")
            title_height = metrics.boundingRect(
                QRect(0, 0, width, 10000), Qt.TextWordWrap, title
            ).height()
            item.setSizeHint(QSize(0, max(66, title_height + 42)))


class LoginDialog(QDialog):
    def __init__(self, config: AppConfig, api_hash: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录 Telegram")
        self.setMinimumWidth(440)
        layout = QFormLayout(self)
        self.api_id = QSpinBox()
        self.api_id.setRange(1, 2_147_483_647)
        self.api_id.setValue(config.api_id or 1)
        self.api_hash = QLineEdit(api_hash or "")
        self.api_hash.setEchoMode(QLineEdit.Password)
        self.phone = QLineEdit(config.phone)
        self.phone.setPlaceholderText("例如 +8613812345678")
        note = QLabel(
            "请使用 my.telegram.org → API development tools 获取 api_id/api_hash。\n"
            "连接固定通过 SOCKS5 127.0.0.1:7890。"
        )
        note.setWordWrap(True)
        layout.addRow(note)
        layout.addRow("API ID", self.api_id)
        layout.addRow("API Hash", self.api_hash)
        layout.addRow("手机号", self.phone)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("连接并登录")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _validate(self) -> None:
        if len(self.api_hash.text().strip()) < 20 or not self.phone.text().strip():
            QMessageBox.warning(self, "信息不完整", "请填写有效的 API Hash 和手机号。")
            return
        self.accept()

    def values(self) -> tuple[int, str, str]:
        return self.api_id.value(), self.api_hash.text().strip(), self.phone.text().strip()


class DownloadManagerDialog(QDialog):
    cancel_requested = Signal(object)
    retry_requested = Signal(object)
    priority_requested = Signal(object, int)
    pause_requested = Signal()
    notice = Signal(str, str)
    compression_requested = Signal(object, str)
    compression_pause_requested = Signal()
    compression_cancel_requested = Signal(object)
    compression_retry_requested = Signal(object, str)

    NAME_COLUMN = 0
    SOURCE_COLUMN = 1
    STATUS_COLUMN = 2
    PROGRESS_COLUMN = 3
    SPEED_COLUMN = 4
    SIZE_COLUMN = 5
    ETA_COLUMN = 6
    PATH_COLUMN = 7

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("下载管理")
        self.resize(1160, 620)
        self._row_by_key: dict[tuple[int, int], int] = {}
        self._states: dict[tuple[int, int], str] = {}
        self._speeds: dict[tuple[int, int], float] = {}
        self._paused = False

        layout = QVBoxLayout(self)
        self.summary_label = QLabel("暂无下载任务")
        self.summary_label.setObjectName("summaryLabel")
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "视频名称",
                "来源",
                "状态",
                "进度",
                "速度",
                "已下载 / 总大小",
                "剩余时间",
                "保存位置",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((300, 150, 105, 145, 105, 170, 100, 300)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.pause_button = QPushButton("暂停队列")
        self.compression_profile = QComboBox()
        for key, info in PROFILES.items():
            self.compression_profile.addItem(info["label"], key)
        self.compression_button = QPushButton("压缩选中视频")
        self.compression_pause_button = QPushButton("暂停压缩")
        self.priority_button = QPushButton("优先下载选中任务")
        self.priority_button.setObjectName("primaryButton")
        self.priority_button.setToolTip("将选中的等待任务移动到下载队列前面")
        self.cancel_button = QPushButton("取消选中")
        self.retry_button = QPushButton("重试失败/取消")
        self.clear_button = QPushButton("清理已完成")
        self.open_button = QPushButton("打开所在目录")
        self.close_button = QPushButton("关闭")
        buttons.addWidget(QLabel("压缩等级"))
        buttons.addWidget(self.compression_profile)
        for button in (
            self.pause_button,
            self.compression_button,
            self.compression_pause_button,
            self.priority_button,
            self.cancel_button,
            self.retry_button,
            self.clear_button,
            self.open_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.compression_button.clicked.connect(self._compress_selected)
        self.compression_pause_button.clicked.connect(self.compression_pause_requested.emit)
        self.priority_button.clicked.connect(self._prioritize_selected)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.retry_button.clicked.connect(self._retry_selected)
        self.clear_button.clicked.connect(self._clear_completed)
        self.open_button.clicked.connect(self._open_selected_directory)
        self.close_button.clicked.connect(self.hide)

    def add_compression_job(self, payload: dict) -> None:
        item = dict(payload)
        item.setdefault("chat_title", "")
        item.setdefault("name", Path(item.get("file_path", "视频")).name)
        wrapper = {"item": item, "directory": str(Path(item["file_path"]).parent), "compression": True}
        self.add_job(wrapper)
        key = (int(item["chat_id"]), int(item["message_id"]))
        row = self._row_by_key.get(key)
        if row is not None:
            self._states[key] = "compression_queued"
            self.table.item(row, self.STATUS_COLUMN).setText("等待压缩")
    def add_job(self, payload: dict) -> None:
        item = payload["item"]
        key = (int(item["chat_id"]), int(item["message_id"]))
        row = self._row_by_key.get(key)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_by_key[key] = row
            for column in range(self.table.columnCount()):
                self.table.setItem(row, column, QTableWidgetItem())
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(0)
            progress.setFormat("%p%")
            self.table.setCellWidget(row, self.PROGRESS_COLUMN, progress)
        self.table.item(row, self.NAME_COLUMN).setText(item["name"])
        self.table.item(row, self.NAME_COLUMN).setData(Qt.UserRole, payload)
        self.table.item(row, self.SOURCE_COLUMN).setText(item.get("chat_title", ""))
        self.table.item(row, self.STATUS_COLUMN).setText("等待下载")
        self.table.item(row, self.SPEED_COLUMN).setText("-")
        self.table.item(row, self.SIZE_COLUMN).setText(
            f"0 B / {human_size(int(item.get('size', 0)))}"
        )
        self.table.item(row, self.ETA_COLUMN).setText("-")
        self.table.item(row, self.PATH_COLUMN).setText(payload["directory"])
        self.table.cellWidget(row, self.PROGRESS_COLUMN).setValue(0)
        self._states[key] = "queued"
        self._speeds[key] = 0.0
        self._update_summary()

    def update_progress(self, chat_id: int, message_id: int, progress: int) -> None:
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        if row is not None:
            self.table.cellWidget(row, self.PROGRESS_COLUMN).setValue(progress)

    def _compress_selected(self) -> None:
        items = []
        for row in self._selected_rows():
            payload = self.table.item(row, self.NAME_COLUMN).data(Qt.UserRole)
            item = payload["item"]
            key = (int(item["chat_id"]), int(item["message_id"]))
            if self._states.get(key) not in {"completed", "not_smaller", "compression_failed"}:
                continue
            path = Path(self.table.item(row, self.PATH_COLUMN).text())
            if path.is_file():
                items.append({**item, "file_path": str(path), "size": path.stat().st_size})
        if not items:
            self.notice.emit("请选中本地存在且已下载完成的视频。", "warning")
            return
        self.compression_requested.emit(items, str(self.compression_profile.currentData()))
    def update_metrics(self, chat_id: int, message_id: int, metrics: dict) -> None:
        key = (int(chat_id), int(message_id))
        row = self._row_by_key.get(key)
        if row is None:
            return
        if metrics.get("phase") == "compression":
            progress = int(metrics.get("progress", 0))
            self.table.cellWidget(row, self.PROGRESS_COLUMN).setValue(progress)
            self.table.item(row, self.SIZE_COLUMN).setText(f"压缩中 {progress}%")
            self.table.item(row, self.SPEED_COLUMN).setText("-")
            self.table.item(row, self.ETA_COLUMN).setText("-")
            return
        current = int(metrics.get("current", 0))
        total = int(metrics.get("total", 0))
        speed = float(metrics.get("speed", 0.0))
        eta = float(metrics.get("eta", 0.0))
        self._speeds[key] = speed if self._states.get(key) == "downloading" else 0.0
        self.table.item(row, self.SPEED_COLUMN).setText(
            f"{human_size(int(speed))}/s" if speed > 0 else "-"
        )
        self.table.item(row, self.SIZE_COLUMN).setText(
            f"{human_size(current)} / {human_size(total)}"
        )
        self.table.item(row, self.ETA_COLUMN).setText(
            human_duration(eta) if eta > 0 else "-"
        )
        self._update_summary()

    def update_state(
        self, chat_id: int, message_id: int, state: str, detail: str
    ) -> None:
        key = (int(chat_id), int(message_id))
        row = self._row_by_key.get(key)
        if row is None:
            return
        labels = {
            "queued": "等待下载",
            "downloading": "下载中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "compression_queued": "等待压缩",
            "compressing": "压缩中",
            "completed_compression": "已压缩",
            "compression_failed": "压缩失败",
            "not_smaller": "未变小",
        }
        self._states[key] = state
        status_item = self.table.item(row, self.STATUS_COLUMN)
        status_item.setText(labels.get(state, state))
        status_item.setToolTip(detail)
        if state in {"downloading", "completed", "completed_compression"} and detail:
            self.table.item(row, self.PATH_COLUMN).setText(detail)
        if state in {"completed", "completed_compression"}:
            self.table.cellWidget(row, self.PROGRESS_COLUMN).setValue(100)
        if state != "downloading":
            self._speeds[key] = 0.0
            self.table.item(row, self.SPEED_COLUMN).setText("-")
            self.table.item(row, self.ETA_COLUMN).setText("-")
        self._update_summary()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("恢复队列" if paused else "暂停队列")

    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _cancel_selected(self) -> None:
        keys = []
        for row in self._selected_rows():
            payload = self.table.item(row, self.NAME_COLUMN).data(Qt.UserRole)
            item = payload["item"]
            keys.append((int(item["chat_id"]), int(item["message_id"])))
        if keys:
            self.cancel_requested.emit(keys)

    def _prioritize_selected(self) -> None:
        keys = []
        for row in self._selected_rows():
            payload = self.table.item(row, self.NAME_COLUMN).data(Qt.UserRole)
            item = payload["item"]
            key = (int(item["chat_id"]), int(item["message_id"]))
            if self._states.get(key) == "queued":
                keys.append(key)
                payload["priority"] = 0
        if keys:
            self.priority_requested.emit(keys, 0)
            self.notice.emit(f"已将 {len(keys)} 个等待任务移到队列前面。", "success")
        else:
            self.notice.emit("请选中尚未开始的等待任务。", "warning")

    def _retry_selected(self) -> None:
        jobs = []
        for row in self._selected_rows():
            payload = self.table.item(row, self.NAME_COLUMN).data(Qt.UserRole)
            item = payload["item"]
            key = (int(item["chat_id"]), int(item["message_id"]))
            if self._states.get(key) in {"failed", "cancelled"}:
                jobs.append(payload)
        if jobs:
            self.retry_requested.emit(jobs)

    def _open_selected_directory(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.notice.emit("请先选择一个下载任务。", "warning")
            return
        row = rows[0]
        payload = self.table.item(row, self.NAME_COLUMN).data(Qt.UserRole)
        displayed = Path(self.table.item(row, self.PATH_COLUMN).text())
        directory = displayed.parent if displayed.suffix else Path(payload["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _clear_completed(self) -> None:
        removable = []
        for key, row in self._row_by_key.items():
            if self._states.get(key) in {"completed", "completed_compression"}:
                removable.append((row, key))
        for row, key in sorted(removable, reverse=True):
            self.table.removeRow(row)
            self._states.pop(key, None)
            self._speeds.pop(key, None)
        self._row_by_key.clear()
        for row in range(self.table.rowCount()):
            payload = self.table.item(row, self.NAME_COLUMN).data(Qt.UserRole)
            item = payload["item"]
            self._row_by_key[(int(item["chat_id"]), int(item["message_id"]))] = row
        self._update_summary()

    def _update_summary(self) -> None:
        counts = {
            state: 0
            for state in ("queued", "downloading", "completed", "failed", "cancelled", "compression_queued", "compressing", "completed_compression", "compression_failed", "not_smaller")
        }
        for state in self._states.values():
            if state in counts:
                counts[state] += 1
        total_speed = sum(self._speeds.values())
        self.summary_label.setText(
            "等待 {queued} ｜ 下载中 {downloading} ｜ 已完成 {completed} ｜ "
            "失败 {failed} ｜ 已取消 {cancelled} ｜ 压缩中 {compressing} ｜ 已压缩 {completed_compression} ｜ 总速度 {speed}/s".format(
                **counts,
                speed=human_size(int(total_speed)),
            )
        )

    def closeEvent(self, event: object) -> None:
        event.ignore()
        self.hide()


class MainWindow(QMainWindow):
    def __init__(self, paths: AppPaths, config: AppConfig) -> None:
        super().__init__()
        self.paths = paths
        self.config = config
        self.storage = Storage(paths.database_file)
        self.library = LibraryStore(paths.database_file)
        self.worker = TelegramWorker(paths, config)
        self.upload_worker = UploadWorker(paths, config)
        self.compression_worker = CompressionWorker(paths, config, self.upload_worker)
        self.current_chat: dict | None = None
        self.video_page_state: dict = {}
        self.video_request_id = 0
        self.reached_end = False
        self._scan_in_progress = False
        self.queue_paused = False
        self.upload_paused = False
        self._really_quit = False
        self._row_by_key: dict[tuple[int, int], int] = {}
        self._progress_by_key: dict[tuple[int, int], int] = {}
        self._directory_scan_id = 0
        self._existing_names: set[str] = set()
        self._telegram_ready = False
        self._refresh_in_progress = False
        self._cloud_files = self.library.cloud_files()
        self._cloud_verified = False
        self._cloud_review: dict[tuple[int, int], dict] = {}
        self._rename_probe_ok = False
        self._build_ui()
        self.download_manager = DownloadManagerDialog(self)
        self._restore_download_manager_tasks()
        self.download_manager.cancel_requested.connect(
            lambda keys: self.worker.submit("cancel_downloads", keys)
        )
        self.download_manager.retry_requested.connect(self.retry_downloads)
        self.download_manager.priority_requested.connect(
            lambda keys, priority: self.worker.submit(
                "set_download_priority", keys, priority
            )
        )
        self.download_manager.pause_requested.connect(self.toggle_pause)
        self.download_manager.notice.connect(self.show_notice)
        self.download_manager.compression_requested.connect(lambda items, profile: self.compression_worker.submit("enqueue_compressions", items, profile))
        self.download_manager.compression_pause_requested.connect(self.toggle_compression_pause)
        self.download_manager.compression_cancel_requested.connect(lambda keys: self.compression_worker.submit("cancel_compressions", keys))
        self.download_manager.compression_retry_requested.connect(lambda items, profile: self.compression_worker.submit("enqueue_compressions", items, profile))
        self.upload_manager = UploadManagerDialog(self)
        self._restore_upload_manager_tasks()
        self.cloud_library = CloudLibraryDialog(paths, self.storage, self.library, self)
        self.cloud_settings = CloudSettingsDialog(config, self)
        self._connect_upload_ui()
        self._build_tray()
        self._connect_worker()
        self.set_chats(self.library.chats(), from_cache=True)
        self.worker.start()
        self.upload_worker.start()
        self.compression_worker.start()
        QTimer.singleShot(350, self._auto_connect)
        QTimer.singleShot(
            500, lambda: self.upload_worker.submit("refresh_cloud_state")
        )

    def _build_ui(self) -> None:
        self.setWindowTitle("Telegram 视频下载器")
        self.resize(1440, 820)
        central = QWidget()
        outer = QVBoxLayout(central)

        top = QHBoxLayout()
        self.connection_label = QLabel("● 未连接")
        self.login_button = QPushButton("登录 / 重连")
        self.logout_button = QPushButton("退出账号")
        self.proxy_button = QPushButton("测试代理")
        self.log_button = QPushButton("打开日志")
        self.download_manager_button = QPushButton("下载管理")
        self.upload_manager_button = QPushButton("上传管理")
        self.compression_button = QPushButton("压缩所选已下载")
        self.cloud_settings_button = QPushButton("云盘设置")
        self.cloud_library_button = QPushButton("云盘清单")
        top.addWidget(self.connection_label)
        top.addStretch()
        top.addWidget(self.proxy_button)
        top.addWidget(self.log_button)
        top.addWidget(self.download_manager_button)
        top.addWidget(self.upload_manager_button)
        top.addWidget(self.compression_button)
        top.addWidget(self.cloud_settings_button)
        top.addWidget(self.cloud_library_button)
        top.addWidget(self.login_button)
        top.addWidget(self.logout_button)
        outer.addLayout(top)

        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setMinimumHeight(38)
        self.notice_label.setContentsMargins(12, 7, 12, 7)
        self.notice_timer = QTimer(self)
        self.notice_timer.setSingleShot(True)
        self.notice_timer.timeout.connect(self.notice_label.hide)
        outer.addWidget(self.notice_label)

        splitter = QSplitter()
        self.main_splitter = splitter
        left = QWidget()
        left.setObjectName("chatSidebar")
        left.setMinimumWidth(300)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 14, 12, 12)
        left_layout.setSpacing(10)
        sidebar_heading = QLabel("会话")
        sidebar_heading.setObjectName("chatSidebarHeading")
        left_layout.addWidget(sidebar_heading)
        self.chat_count_label = QLabel("正在读取会话…")
        self.chat_count_label.setObjectName("chatCountLabel")
        left_layout.addWidget(self.chat_count_label)
        self.chat_search = QLineEdit()
        self.chat_search.setObjectName("chatSearch")
        self.chat_search.setPlaceholderText("搜索会话名称")
        self.chat_list = ChatListWidget()
        self.chat_list.setObjectName("chatList")
        self.chat_list.setItemDelegate(ChatRowDelegate(self.chat_list))
        self.chat_list.setSpacing(3)
        self.chat_list.setMouseTracking(True)
        left_layout.addWidget(self.chat_search)
        left_layout.addWidget(self.chat_list)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        filter_row = QHBoxLayout()
        self.video_search = QLineEdit()
        self.video_search.setPlaceholderText("按视频名称筛选…")
        self.date_filter = QCheckBox("限制日期")
        self.date_from = QDateEdit(QDate.currentDate().addYears(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_from.setCalendarPopup(True)
        self.date_to.setCalendarPopup(True)
        filter_row.addWidget(self.video_search, 1)
        filter_row.addWidget(self.date_filter)
        filter_row.addWidget(QLabel("从"))
        filter_row.addWidget(self.date_from)
        filter_row.addWidget(QLabel("到"))
        filter_row.addWidget(self.date_to)
        right_layout.addLayout(filter_row)

        library_row = QHBoxLayout()
        self.refresh_videos_button = QPushButton("检查新视频")
        self.rescan_videos_button = QPushButton("重新核对历史")
        self.cache_status_label = QLabel("尚无视频缓存")
        library_row.addWidget(self.refresh_videos_button)
        library_row.addWidget(self.rescan_videos_button)
        library_row.addWidget(self.cache_status_label, 1)
        right_layout.addLayout(library_row)

        auto_row = QHBoxLayout()
        self.auto_checkbox = QCheckBox("自动下载这个群聊之后出现的新视频")
        self.auto_compress_checkbox = QCheckBox("下载完成后自动压缩")
        self.auto_compress_checkbox.setChecked(self.config.auto_compress)
        self.compression_profile = QComboBox()
        for key, info in PROFILES.items():
            self.compression_profile.addItem(info["label"], key)
        self.compression_profile.setCurrentIndex(max(0, self.compression_profile.findData(self.config.compression_profile)))
        self.directory_edit = QLineEdit()
        self.directory_edit.setReadOnly(True)
        self.directory_button = QPushButton("选择保存目录")
        self.refresh_names_button = QPushButton("刷新已下载标记")
        auto_row.addWidget(self.auto_checkbox)
        auto_row.addWidget(self.auto_compress_checkbox)
        auto_row.addWidget(self.compression_profile)
        auto_row.addWidget(self.directory_edit, 1)
        auto_row.addWidget(self.directory_button)
        auto_row.addWidget(self.refresh_names_button)
        right_layout.addLayout(auto_row)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "选择",
                "预览",
                "视频名称",
                "类型",
                "大小",
                "日期",
                "来源",
                "下载状态",
                "上传状态",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(88)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((55, 145, 330, 90, 95, 145, 150, 105, 105)):
            self.table.setColumnWidth(column, width)
        self.table.cellDoubleClicked.connect(self.show_thumbnail_preview)
        right_layout.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.invert_button = QPushButton("反选")
        self.download_button = QPushButton("下载所选")
        self.download_button.setObjectName("primaryButton")
        self.redownload_button = QPushButton("重新下载所选")
        self.upload_button = QPushButton("上传所选已下载")
        self.compress_selected_button = QPushButton("压缩所选已下载")
        self.pause_button = QPushButton("暂停队列")
        self.cancel_button = QPushButton("取消所选任务")
        self.open_button = QPushButton("打开下载目录")
        self.more_button = QPushButton("加载更多")
        for button in (
            self.select_all_button,
            self.invert_button,
            self.download_button,
            self.redownload_button,
            self.upload_button,
            self.compress_selected_button,
            self.pause_button,
            self.cancel_button,
            self.open_button,
            self.more_button,
        ):
            action_row.addWidget(button)
        right_layout.addLayout(action_row)
        bulk_row = QHBoxLayout()
        bulk_row.addWidget(QLabel("历史下载批量操作"))
        self.select_downloaded_button = QPushButton("选中已下载")
        self.compress_all_downloaded_button = QPushButton("压缩全部已下载")
        self.upload_all_downloaded_button = QPushButton("上传全部已下载")
        self.refresh_downloaded_button = QPushButton("刷新本地状态")
        for button in (self.select_downloaded_button, self.compress_all_downloaded_button, self.upload_all_downloaded_button, self.refresh_downloaded_button):
            bulk_row.addWidget(button)
        bulk_row.addStretch()
        right_layout.addLayout(bulk_row)
        self.overall_progress = QProgressBar()
        self.overall_progress.setFormat("当前任务总体进度 %p%")
        right_layout.addWidget(self.overall_progress)
        splitter.addWidget(right)
        saved_sidebar_width = self.library.get_state("chat_sidebar_width", "350")
        try:
            sidebar_width = max(300, min(520, int(saved_sidebar_width)))
        except ValueError:
            sidebar_width = 350
        splitter.setSizes([sidebar_width, 1440 - sidebar_width])
        self._sidebar_save_timer = QTimer(self)
        self._sidebar_save_timer.setSingleShot(True)
        self._sidebar_save_timer.timeout.connect(
            lambda: self.library.set_state("chat_sidebar_width", str(splitter.sizes()[0]))
        )
        splitter.splitterMoved.connect(lambda *_: self._sidebar_save_timer.start(500))
        outer.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().hide()
        self.show_notice("请先启动 Clash，然后登录 Telegram。", "info", 0)

        self.login_button.clicked.connect(self.show_login)
        self.logout_button.clicked.connect(self.logout)
        self.proxy_button.clicked.connect(self.test_proxy)
        self.log_button.clicked.connect(self.open_log_directory)
        self.download_manager_button.clicked.connect(self.show_download_manager)
        self.upload_manager_button.clicked.connect(self.show_upload_manager)
        self.compression_button.clicked.connect(self.compression_selected)
        self.compress_selected_button.clicked.connect(self.compression_selected)
        self.cloud_settings_button.clicked.connect(self.show_cloud_settings)
        self.cloud_library_button.clicked.connect(self.show_cloud_library)
        self.chat_search.textChanged.connect(self.filter_chats)
        self.chat_list.currentItemChanged.connect(self.chat_changed)
        self.video_search.textChanged.connect(self.filter_videos)
        self.date_filter.toggled.connect(self.filter_videos)
        self.date_from.dateChanged.connect(self.filter_videos)
        self.date_to.dateChanged.connect(self.filter_videos)
        self.directory_button.clicked.connect(self.choose_directory)
        self.refresh_names_button.clicked.connect(self.refresh_downloaded_names)
        self.auto_checkbox.toggled.connect(self.save_auto_rule)
        self.auto_compress_checkbox.toggled.connect(self.save_compression_settings)
        self.compression_profile.currentIndexChanged.connect(self.save_compression_settings)
        self.select_all_button.clicked.connect(lambda: self.set_visible_checks(Qt.Checked))
        self.invert_button.clicked.connect(self.invert_checks)
        self.download_button.clicked.connect(self.download_selected)
        self.redownload_button.clicked.connect(self.redownload_selected)
        self.upload_button.clicked.connect(self.upload_selected)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.open_button.clicked.connect(self.open_directory)
        self.more_button.clicked.connect(self.load_more)
        self.refresh_videos_button.clicked.connect(self.refresh_new_videos)
        self.rescan_videos_button.clicked.connect(self.rescan_history)
        self.select_downloaded_button.clicked.connect(self.select_downloaded_videos)
        self.compress_all_downloaded_button.clicked.connect(self.compress_all_downloaded)
        self.upload_all_downloaded_button.clicked.connect(self.upload_all_downloaded)
        self.refresh_downloaded_button.clicked.connect(self.refresh_all_downloaded_status)

    def _build_tray(self) -> None:
        icon = QApplication.windowIcon()
        if icon.isNull():
            icon = QIcon(sys.executable)
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_DriveNetIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Telegram 视频下载器 - 未连接")
        menu = QMenu()
        show_action = QAction("显示主窗口", self)
        self.tray_pause_action = QAction("暂停自动下载", self)
        self.tray_status_action = QAction("状态：未连接", self)
        self.tray_status_action.setEnabled(False)
        quit_action = QAction("彻底退出", self)
        show_action.triggered.connect(self.show_from_tray)
        self.tray_pause_action.triggered.connect(self.toggle_pause)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addAction(self.tray_pause_action)
        menu.addAction(self.tray_status_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_from_tray() if reason == QSystemTrayIcon.DoubleClick else None
        )
        self.tray.show()

    def _connect_worker(self) -> None:
        self.worker.status.connect(self.show_notice)
        self.worker.error.connect(self.show_error)
        self.worker.auth_state.connect(self.handle_auth_state)
        self.worker.chats_ready.connect(self.set_chats)
        self.worker.videos_ready.connect(self.add_videos)
        self.worker.new_videos_ready.connect(self.show_new_videos)
        self.worker.history_rechecked.connect(self.show_rechecked_history)
        self.worker.video_scan_status.connect(self.set_video_scan_status)
        self.worker.download_progress.connect(self.update_download_progress)
        self.worker.download_metrics.connect(self.download_manager.update_metrics)
        self.worker.download_state.connect(self.update_download_state)
        self.worker.download_job.connect(self.download_manager.add_job)
        self.worker.thumbnail_ready.connect(self.set_video_thumbnail)
        self.worker.downloaded_names_ready.connect(self.set_downloaded_names)
        self.worker.connection_changed.connect(self.set_connection)
        self.compression_worker.status.connect(self.show_notice)
        self.compression_worker.error.connect(self.show_error)
        self.compression_worker.compression_job.connect(self.download_manager.add_compression_job)
        self.compression_worker.compression_state.connect(self.update_compression_state)
        self.compression_worker.compression_progress.connect(self.download_manager.update_progress)
        self.compression_worker.compression_metrics.connect(self.download_manager.update_metrics)

    def _connect_upload_ui(self) -> None:
        self.upload_manager.cancel_requested.connect(
            lambda keys: self.upload_worker.submit("cancel_uploads", keys)
        )
        self.upload_manager.retry_requested.connect(self.retry_uploads)
        self.upload_manager.pause_requested.connect(self.toggle_upload_pause)
        self.upload_manager.refresh_requested.connect(self.refresh_cloud_upload_status)
        self.upload_manager.open_cloud_requested.connect(self.open_cloud)
        self.cloud_settings.install_requested.connect(
            lambda: self.upload_worker.submit("install_openlist")
        )
        self.cloud_settings.start_requested.connect(
            lambda: self.upload_worker.submit("start_openlist")
        )
        self.cloud_settings.stop_requested.connect(
            lambda: self.upload_worker.submit("stop_openlist")
        )
        self.cloud_settings.test_requested.connect(
            lambda: self.upload_worker.submit("test_mount")
        )
        self.cloud_settings.ffmpeg_install_requested.connect(
            lambda: self.upload_worker.submit("install_ffmpeg")
        )
        self.cloud_settings.supplement_requested.connect(
            self.supplement_completed_downloads
        )
        self.cloud_library.scan_requested.connect(self.supplement_completed_downloads)
        self.cloud_library.confirm_requested.connect(
            lambda item: self.upload_worker.submit("confirm_cloud_match", item)
        )
        self.cloud_library.override_requested.connect(
            lambda item: self.upload_worker.submit("enqueue_upload", {**item, "cloud_override": True})
        )
        self.cloud_library.probe_requested.connect(
            lambda: self.upload_worker.submit("probe_cloud_rename")
        )
        self.cloud_library.plan_requested.connect(
            lambda: self.upload_worker.submit("plan_cloud_renames")
        )
        self.cloud_settings.saved.connect(self.save_cloud_settings)
        self.cloud_settings.password_change_requested.connect(
            lambda password: self.upload_worker.submit(
                "set_openlist_password", password
            )
        )
        self.cloud_settings.notice.connect(self.show_notice)
        self.upload_worker.status.connect(self.show_notice)
        self.upload_worker.error.connect(self.show_error)
        self.upload_worker.upload_job.connect(self.upload_manager.add_job)
        self.upload_worker.upload_state.connect(self.update_upload_state)
        self.upload_worker.upload_progress.connect(self.upload_manager.update_progress)
        self.upload_worker.upload_metrics.connect(self.upload_manager.update_metrics)
        self.upload_worker.cloud_state.connect(self.cloud_settings.set_state)
        self.upload_worker.cloud_inventory_ready.connect(self.set_cloud_inventory)
        self.upload_worker.review_needed.connect(self.set_cloud_review)
        self.upload_worker.rename_plan_ready.connect(self.show_cloud_rename_plan)
        self.upload_worker.rename_probe_result.connect(self.show_cloud_rename_probe)
        self.upload_worker.rename_result.connect(self.show_cloud_rename_result)

    def _auto_connect(self) -> None:
        api_hash = load_api_hash()
        if self.config.api_id and self.config.phone and api_hash:
            self.worker.submit(
                "connect_account", self.config.api_id, api_hash, self.config.phone
            )

    def show_login(self) -> None:
        dialog = LoginDialog(self.config, load_api_hash(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        api_id, api_hash, phone = dialog.values()
        self.config.api_id = api_id
        self.config.phone = phone
        self.config.save(self.paths.config_file)
        self.worker.submit("connect_account", api_id, api_hash, phone)

    def handle_auth_state(self, state: str, message: str) -> None:
        if state == "code_required":
            code, ok = QInputDialog.getText(self, "Telegram 验证码", message)
            if ok and code.strip():
                self.worker.submit("submit_code", code)
        elif state == "password_required":
            password, ok = QInputDialog.getText(
                self, "二步验证", message, QLineEdit.Password
            )
            if ok and password:
                self.worker.submit("submit_password", password)
        elif state == "authorized":
            self.show_notice(f"已登录：{message}", "success")
        elif state == "logged_out":
            delete_api_hash()
            self.config.api_id = None
            self.config.phone = ""
            self.config.save(self.paths.config_file)
            self.chat_list.clear()
            self.table.setRowCount(0)
            self._row_by_key.clear()

    def set_connection(self, connected: bool, text: str) -> None:
        if not connected:
            self._telegram_ready = False
        color = "#16803a" if connected else "#b42318"
        self.connection_label.setText(f"● {text}")
        self.connection_label.setStyleSheet(f"color: {color}; font-weight: bold")
        self.tray.setToolTip(f"Telegram 视频下载器 - {text}")
        self.tray_status_action.setText(f"状态：{text}")

    def _restore_download_manager_tasks(self) -> None:
        for task in self.library.download_tasks():
            item = task["item"]
            self.download_manager.add_job({
                "item": item, "directory": task["directory"],
                "priority": int(task["priority"]),
            })
            state = task["status"]
            if state == "downloading":
                state = "queued"
            detail = task["detail"]
            if state == "completed":
                record = self.storage.get_download(int(item["chat_id"]), int(item["message_id"]))
                detail = str(record["file_path"]) if record else ""
            self.download_manager.update_state(
                int(item["chat_id"]), int(item["message_id"]), state, detail,
            )

    def _restore_upload_manager_tasks(self) -> None:
        for record in [*self.storage.pending_uploads(), *self.storage.completed_uploads()]:
            item = {
                **record,
                "name": Path(record["source_path"]).name,
                "file_path": record["source_path"],
                "size": int(record["source_size"]),
            }
            self.upload_manager.add_job(item)
            state = record["status"]
            if state in {"uploading", "processing"}:
                state = "queued"
            self.upload_manager.update_state(
                int(record["chat_id"]), int(record["message_id"]),
                state, record["detail"],
            )

    def set_chats(self, chats: list[dict], from_cache: bool = False) -> None:
        displayed_chat = int(self.current_chat["chat_id"]) if self.current_chat else 0
        selected = (
            int(self.current_chat["chat_id"])
            if self.current_chat else int(self.library.get_state("last_chat_id", "0"))
        )
        if not from_cache:
            self.library.save_chats(chats)
            self._telegram_ready = True
            self.upload_worker.submit("reconcile_local_downloads")
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        self.chat_count_label.setText(f"{len(chats)} 个群聊与频道")
        selected_row = -1
        for chat in chats:
            item = QListWidgetItem(chat["title"])
            item.setData(Qt.UserRole, chat)
            item.setData(Qt.UserRole + 1, chat["title"])
            item.setData(Qt.UserRole + 2, chat["kind"])
            item.setToolTip(f"{chat['title']}\n{chat['kind']}")
            self.chat_list.addItem(item)
            if int(chat["chat_id"]) == selected:
                selected_row = self.chat_list.count() - 1
        self.chat_list.resize_rows()
        if selected_row >= 0:
            self.chat_list.setCurrentRow(selected_row)
        self.chat_list.blockSignals(False)
        if selected_row >= 0:
            selected_item = self.chat_list.currentItem()
            if displayed_chat != selected:
                self.chat_changed(selected_item, None)
            else:
                self.current_chat = selected_item.data(Qt.UserRole)
                if not from_cache:
                    if self.library.newest_message_id(selected):
                        self.refresh_new_videos()
                    else:
                        self.load_more()
        if chats:
            origin = "缓存" if from_cache else "Telegram"
            self.show_notice(f"已从{origin}载入 {len(chats)} 个群聊/频道。", "success")

    def filter_chats(self, text: str) -> None:
        needle = text.casefold().strip()
        for index in range(self.chat_list.count()):
            item = self.chat_list.item(index)
            item.setHidden(needle not in str(item.data(Qt.UserRole + 1)).casefold())

    def chat_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        del previous
        if not current:
            return
        self.current_chat = current.data(Qt.UserRole)
        chat_id = int(self.current_chat["chat_id"])
        self.library.set_state("last_chat_id", str(chat_id))
        self.video_request_id += 1
        self.video_search.clear()
        self.table.setRowCount(0)
        self._row_by_key.clear()
        self._progress_by_key.clear()
        self.video_page_state = self.library.scan_state(chat_id)
        self.reached_end = bool(self.video_page_state.get("reached_end", False))
        self._scan_in_progress = False
        self._refresh_in_progress = False
        rule = self.storage.get_rule(self.current_chat["chat_id"])
        default_dir = self.paths.default_download_dir / sanitize_component(self.current_chat["title"])
        self.directory_edit.setText(rule["directory"] if rule else str(default_dir))
        self.auto_checkbox.blockSignals(True)
        self.auto_checkbox.setChecked(bool(rule and rule["enabled"]))
        self.auto_checkbox.blockSignals(False)
        cached = self.library.videos(chat_id)
        if cached:
            self.add_videos(self.video_request_id, chat_id, cached, {}, False, from_cache=True)
        checked_at = self.library.checked_at(chat_id)
        self.cache_status_label.setText(
            f"缓存 {len(cached)} 个视频 · 最近核对 {checked_at[:16].replace('T', ' ')}"
            if checked_at else f"缓存 {len(cached)} 个视频 · 尚未核对新增"
        )
        self.more_button.setEnabled(not self.reached_end)
        self.more_button.setText("已到最早视频" if self.reached_end else "加载更多")
        if self.worker.isRunning():
            self.refresh_downloaded_names()
        if self._telegram_ready:
            if cached:
                self.refresh_new_videos()
            else:
                self.load_more()

    def refresh_new_videos(self) -> None:
        if not self.current_chat or self._refresh_in_progress or self._scan_in_progress:
            return
        if not self._telegram_ready:
            self.show_notice("当前离线，已显示上次保存的视频列表。", "warning")
            return
        chat_id = int(self.current_chat["chat_id"])
        if not self.library.newest_message_id(chat_id):
            self.load_more()
            return
        self._refresh_in_progress = True
        self.refresh_videos_button.setEnabled(False)
        self.cache_status_label.setText("正在检查新视频…")
        self.worker.submit(
            "refresh_new_videos", self.video_request_id, chat_id,
            self.library.newest_message_id(chat_id),
        )

    def show_new_videos(self, request_id: int, chat_id: int, videos: list[dict], error: str) -> None:
        if not self.current_chat or request_id != self.video_request_id or chat_id != self.current_chat["chat_id"]:
            return
        self._refresh_in_progress = False
        self.refresh_videos_button.setEnabled(True)
        if error:
            self.cache_status_label.setText("新视频检查失败，缓存仍可用")
            self.show_notice(f"检查新视频失败：{error}", "warning")
            return
        self.add_videos(request_id, chat_id, videos, {}, False)
        self.cache_status_label.setText(f"已核对新增 · 当前缓存 {self.table.rowCount()} 个视频")
        self.show_notice(f"发现 {len(videos)} 个新视频。" if videos else "没有新视频。", "success")

    def rescan_history(self) -> None:
        if not self.current_chat or self._scan_in_progress or self._refresh_in_progress:
            return
        if not self._telegram_ready:
            self.show_notice("Telegram 未连接，暂时无法核对历史。", "warning")
            return
        self._scan_in_progress = True
        self.rescan_videos_button.setEnabled(False)
        self.worker.submit("recheck_history", self.video_request_id, int(self.current_chat["chat_id"]))

    def show_rechecked_history(
        self, request_id: int, chat_id: int, videos: list[dict], state: dict, error: str,
    ) -> None:
        if not self.current_chat or request_id != self.video_request_id or chat_id != self.current_chat["chat_id"]:
            return
        self._scan_in_progress = False
        self.rescan_videos_button.setEnabled(True)
        if error:
            self.show_notice(f"历史核对失败：{error}；原缓存未改变。", "warning")
            return
        self.table.setRowCount(0)
        self._row_by_key.clear()
        self.add_videos(request_id, chat_id, videos, state, True, from_cache=True)
        self.cache_status_label.setText(f"历史核对完成 · {len(videos)} 个视频")
        self.show_notice(f"历史核对完成：{len(videos)} 个视频。", "success")

    def load_more(self) -> None:
        if not self.current_chat or self.reached_end or self._scan_in_progress:
            return
        self._scan_in_progress = True
        self.more_button.setEnabled(False)
        self.more_button.setText("正在扫描…")
        self.show_notice("正在使用 Telegram 媒体筛选查询视频…")
        self.worker.submit(
            "start_video_scan",
            self.video_request_id,
            self.current_chat["chat_id"],
            self.video_page_state,
            100,
        )

    def add_videos(
        self,
        request_id: int,
        chat_id: int,
        videos: list[dict],
        page_state: dict,
        finished: bool,
        from_cache: bool = False,
    ) -> None:
        if (
            not self.current_chat
            or request_id != self.video_request_id
            or chat_id != self.current_chat["chat_id"]
        ):
            return
        for video in videos:
            key = (int(video["chat_id"]), int(video["message_id"]))
            if key in self._row_by_key:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Unchecked)
            check.setData(Qt.UserRole, video)
            self.table.setItem(row, 0, check)
            preview = QLabel("加载中…")
            preview.setAlignment(Qt.AlignCenter)
            preview.setMinimumSize(128, 72)
            preview.setStyleSheet(
                "color: #6b7280; background: #eef2f5; border-radius: 6px;"
            )
            self.table.setCellWidget(row, 1, preview)
            values = (
                video["name"],
                video["media_kind"],
                human_size(video["size"]),
                datetime.fromisoformat(video["date"]).astimezone().strftime("%Y-%m-%d %H:%M"),
                video["chat_title"],
                self._download_label(key),
                self._upload_label(key),
            )
            for column, value in enumerate(values, start=2):
                self.table.setItem(row, column, QTableWidgetItem(value))
            self._row_by_key[key] = row
            self._apply_existing_mark(row)
        if videos:
            self.table.sortItems(5, Qt.DescendingOrder)
            self._rebuild_row_index()
            if from_cache:
                for video in videos:
                    thumb = self.paths.data_dir / "thumbnails" / str(chat_id) / f"{video['message_id']}.thumb"
                    if thumb.is_file():
                        self.set_video_thumbnail(request_id, chat_id, int(video["message_id"]), thumb.read_bytes(), "")
            elif self._telegram_ready:
                self.worker.submit("load_video_thumbnails", request_id, chat_id, list(videos))
        if finished:
            self.video_page_state = page_state
            self.reached_end = bool(page_state.get("reached_end", False))
            self._scan_in_progress = False
            self.more_button.setEnabled(not self.reached_end)
            self.more_button.setText("已到最早视频" if self.reached_end else "加载更多")
        self.filter_videos()

    def set_video_thumbnail(
        self,
        request_id: int,
        chat_id: int,
        message_id: int,
        data: bytes,
        error: str,
    ) -> None:
        if (
            not self.current_chat
            or request_id != self.video_request_id
            or int(chat_id) != int(self.current_chat["chat_id"])
        ):
            return
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        if row is None:
            return
        preview = self.table.cellWidget(row, 1)
        if not isinstance(preview, QLabel):
            return
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            preview.setPixmap(
                pixmap.scaled(132, 76, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            preview.setText("")
            preview.setProperty("thumbnailData", bytes(data))
            preview.setToolTip("双击查看大图")
            preview.setStyleSheet("background: #111827; border-radius: 6px;")
        else:
            preview.setText("无预览")
            preview.setToolTip(error or "Telegram 未提供缩略图")
            preview.setStyleSheet(
                "color: #8a8f98; background: #eef2f5; border-radius: 6px;"
            )

    def show_thumbnail_preview(self, row: int, column: int) -> None:
        if column != 1:
            return
        preview = self.table.cellWidget(row, column)
        if not isinstance(preview, QLabel):
            return
        data = preview.property("thumbnailData")
        if not data:
            self.show_notice("这个视频没有可用的 Telegram 缩略图。", "warning")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("视频截图预览")
        dialog.resize(720, 440)
        layout = QVBoxLayout(dialog)
        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        image.setPixmap(
            pixmap.scaled(680, 380, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        layout.addWidget(image, 1)
        dialog.exec()

    def filter_videos(self, *_: object) -> None:
        needle = self.video_search.text().casefold().strip()
        use_date = self.date_filter.isChecked()
        from_date = self.date_from.date()
        to_date = self.date_to.date()
        visible_count = 0
        for row in range(self.table.rowCount()):
            data = self.table.item(row, 0).data(Qt.UserRole)
            date = datetime.fromisoformat(data["date"]).date()
            qdate = QDate(date.year, date.month, date.day)
            visible = needle in data["name"].casefold()
            if use_date:
                visible = visible and from_date <= qdate <= to_date
            self.table.setRowHidden(row, not visible)
            visible_count += int(visible)
        total = self.table.rowCount()
        if total and not visible_count and (needle or use_date):
            self.show_notice(
                f"已找到 {total} 个视频，但均不符合当前筛选；请清空名称或日期条件。"
            )
        elif not self._scan_in_progress and total:
            self.show_notice(f"当前显示 {visible_count} / {total} 个视频。")

    def set_video_scan_status(self, request_id: int, chat_id: int, message: str) -> None:
        if (
            not self.current_chat
            or request_id != self.video_request_id
            or chat_id != self.current_chat["chat_id"]
        ):
            return
        needle = self.video_search.text().strip()
        visible_count = sum(
            not self.table.isRowHidden(row) for row in range(self.table.rowCount())
        )
        if self.table.rowCount() and not visible_count and (needle or self.date_filter.isChecked()):
            self.show_notice(
                f"已找到 {self.table.rowCount()} 个视频，但均不符合当前筛选；扫描仍会继续。"
            )
        else:
            self.show_notice(message)

    def _rebuild_row_index(self) -> None:
        self._row_by_key.clear()
        for row in range(self.table.rowCount()):
            data = self.table.item(row, 0).data(Qt.UserRole)
            self._row_by_key[(int(data["chat_id"]), int(data["message_id"]))] = row

    def choose_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择视频保存目录", self.directory_edit.text()
        )
        if selected:
            self.directory_edit.setText(selected)
            self.save_auto_rule(self.auto_checkbox.isChecked())
            self.refresh_downloaded_names()

    def save_auto_rule(self, enabled: bool) -> None:
        if not self.current_chat:
            return
        directory = self.directory_edit.text()
        self.worker.submit(
            "save_auto_rule",
            self.current_chat["chat_id"],
            self.current_chat["title"],
            enabled,
            directory,
        )

    def selected_videos(self) -> list[dict]:
        result = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.checkState() == Qt.Checked:
                result.append(item.data(Qt.UserRole))
        return result

    def _completed_local_items(self) -> list[dict]:
        """Return every completed local download, including videos outside the loaded history page."""
        items: list[dict] = []
        for record in self.storage.completed_downloads():
            source = Path(str(record.get("file_path", "")))
            if not source.is_file():
                continue
            key = (int(record["chat_id"]), int(record["message_id"]))
            items.append({
                "chat_id": key[0], "message_id": key[1],
                "chat_title": record.get("chat_title") or "历史下载",
                "name": source.name, "media_kind": "本地视频",
                "size": source.stat().st_size, "file_path": str(source), "priority": 1,
            })
        return items

    def select_downloaded_videos(self) -> None:
        selected = 0
        missing = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            video = item.data(Qt.UserRole)
            key = (int(video["chat_id"]), int(video["message_id"]))
            record = self.storage.get_download(*key)
            local = bool(record and record["status"] == "completed" and Path(record["file_path"]).is_file())
            item.setCheckState(Qt.Checked if local else Qt.Unchecked)
            if local: selected += 1
            elif record and record["status"] == "completed": missing += 1
        message = f"已选中当前页面 {selected} 个本地已下载视频。"
        if missing: message += f"另有 {missing} 个记录对应的本地文件已不存在。"
        self.show_notice(message, "success" if selected else "warning")

    def compress_all_downloaded(self) -> None:
        items = []
        for item in self._completed_local_items():
            record = self.storage.get_compression(int(item["chat_id"]), int(item["message_id"]))
            if record and record["status"] in {"queued", "compressing", "completed"}: continue
            items.append(item)
        if not items:
            self.show_notice("没有可压缩的本地已下载视频（已压缩或文件不存在）。", "warning")
            return
        self.compression_worker.submit("enqueue_compressions", items, self.config.compression_profile)
        self.show_notice(f"已将 {len(items)} 个历史已下载视频加入压缩队列。", "success")
        self.show_download_manager()

    def upload_all_downloaded(self) -> None:
        queued = 0
        for item in self._completed_local_items():
            key = (int(item["chat_id"]), int(item["message_id"]))
            uploaded = self.storage.get_upload(*key)
            if uploaded and uploaded["status"] == "completed": continue
            self.upload_worker.submit("enqueue_upload", item)
            queued += 1
        if not queued:
            self.show_notice("没有需要上传的历史视频（已上传或本地文件不存在）。", "warning")
            return
        self.show_notice(f"已将 {queued} 个历史已下载视频加入上传队列。", "success")
        self.show_upload_manager()

    def refresh_all_downloaded_status(self) -> None:
        self.refresh_downloaded_names()
        visible = 0
        for row in range(self.table.rowCount()):
            video = self.table.item(row, 0).data(Qt.UserRole)
            key = (int(video["chat_id"]), int(video["message_id"]))
            self.table.item(row, 7).setText(self._download_label(key))
            if self.storage.get_download(*key): visible += 1
        self.show_notice(f"已刷新本地下载状态，当前页面匹配到 {visible} 条下载记录。", "success")

    def set_visible_checks(self, state: Qt.CheckState) -> None:
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.item(row, 0).setCheckState(state)

    def invert_checks(self) -> None:
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 0)
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def download_selected(self) -> None:
        videos = self.selected_videos()
        if not videos:
            self.show_notice("请先勾选要下载的视频。", "warning")
            return
        ready: list[dict] = []
        confirmed = 0
        uncertain = 0
        for video in videos:
            key = (int(video["chat_id"]), int(video["message_id"]))
            local = self.storage.get_download(*key)
            kind, _ = self._cloud_match(video)
            if local and local["status"] == "completed" and Path(local["file_path"]).is_file():
                confirmed += 1
            elif kind == "confirmed":
                confirmed += 1
            else:
                ready.append(video)
                if kind in {"suspected", "unverified"} or video["name"].casefold() in self._existing_names:
                    uncertain += 1
        if uncertain:
            answer = QMessageBox.question(
                self, "下载前核对", f"{uncertain} 个视频的本地或云端状态尚未确认。仍要下载这些视频吗？",
            )
            if answer != QMessageBox.Yes:
                ready = [
                    video for video in ready
                    if self._cloud_match(video)[0] == "missing"
                    and video["name"].casefold() not in self._existing_names
                ]
        if not ready:
            self.show_notice(f"所选视频已存在或尚待确认：{confirmed + uncertain} 个。可用“重新下载所选”明确覆盖历史判断。", "info")
            return
        directory = self.directory_edit.text()
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.worker.submit("enqueue_downloads", ready, directory, True)
        if confirmed:
            self.show_notice(f"已跳过 {confirmed} 个已下载或云端已确认的视频。", "info")
        self.show_download_manager()

    def redownload_selected(self) -> None:
        videos = self.selected_videos()
        if not videos:
            self.show_notice("请先勾选要重新下载的视频。", "warning")
            return
        if QMessageBox.question(
            self, "重新下载", f"将重新下载 {len(videos)} 个视频，历史完成记录仍会保留。继续吗？",
        ) != QMessageBox.Yes:
            return
        directory = self.directory_edit.text()
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.worker.submit("enqueue_downloads", videos, directory, True)
        self.show_download_manager()

    def upload_selected(self) -> None:
        queued = 0
        for video in self.selected_videos():
            key = (int(video["chat_id"]), int(video["message_id"]))
            record = self.storage.get_download(*key)
            if not record or record["status"] != "completed":
                continue
            source = Path(record["file_path"])
            if not source.is_file():
                row = self._row_by_key.get(key)
                if row is not None:
                    self.table.item(row, 7).setText("本地已删除")
                continue
            self.upload_worker.submit(
                "enqueue_upload",
                {
                    **video,
                    "file_path": str(source),
                    "size": source.stat().st_size,
                    "priority": 1,
                },
            )
            queued += 1
        if not queued:
            self.show_notice(
                "所选视频尚未下载，或本地文件已经被删除。", "warning"
            )
            return
        self.show_upload_manager()

    def supplement_completed_downloads(self) -> None:
        self.show_notice("正在核对云端清单与全部历史下载记录…", "info")
        self.upload_worker.submit("scan_cloud_inventory")

    def cancel_selected(self) -> None:
        keys = [(item["chat_id"], item["message_id"]) for item in self.selected_videos()]
        if keys:
            self.worker.submit("cancel_downloads", keys)

    def toggle_pause(self) -> None:
        self.queue_paused = not self.queue_paused
        self.worker.submit("set_paused", self.queue_paused)
        self.pause_button.setText("恢复队列" if self.queue_paused else "暂停队列")
        self.tray_pause_action.setText("恢复自动下载" if self.queue_paused else "暂停自动下载")
        self.download_manager.set_paused(self.queue_paused)

    def update_download_progress(self, chat_id: int, message_id: int, progress: int) -> None:
        key = (chat_id, message_id)
        self._progress_by_key[key] = progress
        self.download_manager.update_progress(chat_id, message_id, progress)
        row = self._row_by_key.get(key)
        if row is not None:
            self.table.item(row, 7).setText(f"下载中 {progress}%")
        if self._progress_by_key:
            self.overall_progress.setValue(
                sum(self._progress_by_key.values()) // len(self._progress_by_key)
            )

    def update_download_state(
        self, chat_id: int, message_id: int, state: str, detail: str
    ) -> None:
        row = self._row_by_key.get((chat_id, message_id))
        labels = {
            "queued": "等待下载",
            "downloading": "下载中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "compression_queued": "等待压缩",
            "compressing": "压缩中",
            "completed_compression": "已压缩",
            "compression_failed": "压缩失败",
            "not_smaller": "未变小",
        }
        if row is not None:
            self.table.item(row, 7).setText(labels.get(state, state))
            self.table.item(row, 7).setToolTip(detail)
        self.download_manager.update_state(chat_id, message_id, state, detail)
        if state == "completed":
            completed = Path(detail)
            video = dict(self.table.item(row, 0).data(Qt.UserRole)) if row is not None else {"chat_id": chat_id, "message_id": message_id, "name": completed.name, "chat_title": ""}
            if completed.is_file():
                self._existing_names.add(completed.name.casefold())
                self._apply_existing_marks()
                if (
                    self.config.cloud_enabled
                    and self.config.cloud_auto_upload
                    and row is not None
                ):
                    video = dict(self.table.item(row, 0).data(Qt.UserRole))
                    self.upload_worker.submit(
                        "enqueue_upload",
                        {
                            **video,
                            "file_path": str(completed),
                            "size": completed.stat().st_size,
                            "priority": 1,
                        },
                    )
                if self.config.auto_compress:
                    self.compression_worker.submit("enqueue_compressions", [{**video, "file_path": str(completed), "size": completed.stat().st_size}], self.config.compression_profile)
        if state == "failed":
            self.show_notice(f"下载失败：{detail}", "error", 12000)

    def compression_selected(self) -> None:
        items = []
        for video in self.selected_videos():
            key = (int(video["chat_id"]), int(video["message_id"]))
            record = self.storage.get_download(*key)
            if not record or record["status"] != "completed":
                continue
            source = Path(record["file_path"])
            if source.is_file():
                items.append({**video, "file_path": str(source), "size": source.stat().st_size})
        if not items:
            self.show_notice("所选视频尚未下载，或本地文件已经被删除。", "warning")
            return
        self.compression_worker.submit("enqueue_compressions", items, self.config.compression_profile)
        self.show_download_manager()

    def save_compression_settings(self) -> None:
        self.config.auto_compress = self.auto_compress_checkbox.isChecked()
        self.config.compression_profile = str(self.compression_profile.currentData() or "balanced")
        self.config.save(self.paths.config_file)

    def toggle_compression_pause(self) -> None:
        paused = getattr(self, "compression_paused", False)
        self.compression_paused = not paused
        self.compression_worker.submit("set_paused", self.compression_paused)
        self.download_manager.compression_pause_button.setText("恢复压缩" if self.compression_paused else "暂停压缩")

    def update_compression_state(self, chat_id: int, message_id: int, state: str, detail: str) -> None:
        key = (int(chat_id), int(message_id))
        row = self._row_by_key.get(key)
        labels = {
            "queued": "等待压缩",
            "compressing": "压缩中",
            "completed": "已下载 / 已压缩",
            "failed": "压缩失败",
            "cancelled": "压缩已取消",
            "not_smaller": "压缩结果未变小",
        }
        if row is not None:
            self.table.item(row, 7).setText(labels.get(state, state))
            self.table.item(row, 7).setToolTip(detail)
        manager_state = {"completed": "completed_compression", "failed": "compression_failed"}.get(state, "compression_queued" if state == "queued" else state)
        self.download_manager.update_state(chat_id, message_id, manager_state, detail)
        if state == "failed":
            self.show_notice(f"视频压缩失败：{detail}", "error", 12000)
        elif state == "completed":
            self.show_notice("视频压缩完成，原视频已删除。", "success")
    def retry_downloads(self, jobs: list[dict]) -> None:
        for job in jobs:
            item = dict(job["item"])
            item["priority"] = int(job.get("priority", 1))
            self.worker.submit("enqueue_downloads", [item], job["directory"], True)

    def show_download_manager(self) -> None:
        self.download_manager.show()
        self.download_manager.raise_()
        self.download_manager.activateWindow()

    def show_upload_manager(self) -> None:
        self.upload_manager.show()
        self.upload_manager.raise_()
        self.upload_manager.activateWindow()

    def show_cloud_settings(self) -> None:
        self.upload_worker.submit("refresh_cloud_state")
        self.cloud_settings.show()
        self.cloud_settings.raise_()
        self.cloud_settings.activateWindow()

    def show_cloud_library(self) -> None:
        self.cloud_library.set_inventory(self._cloud_files, self._cloud_verified)
        self.cloud_library.show()
        self.cloud_library.raise_()
        self.cloud_library.activateWindow()

    def save_cloud_settings(self) -> None:
        self.config.save(self.paths.config_file)
        self.show_notice("云盘与上传设置已保存。", "success")
        self.upload_worker.submit("refresh_cloud_state")

    def toggle_upload_pause(self) -> None:
        self.upload_paused = not self.upload_paused
        self.upload_worker.submit("set_paused", self.upload_paused)
        self.upload_manager.set_paused(self.upload_paused)

    def retry_uploads(self, jobs: list[dict]) -> None:
        for job in jobs:
            payload = dict(job)
            payload["priority"] = 1
            self.upload_worker.submit("enqueue_upload", payload)

    def update_upload_state(
        self, chat_id: int, message_id: int, state: str, detail: str
    ) -> None:
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        labels = {
            "queued": "等待上传",
            "processing": "规范元数据",
            "uploading": "上传中",
            "completed": "已上传",
            "failed": "上传失败",
            "cancelled": "已取消",
            "remote_missing": "云端已删除",
            "needs_review": "疑似重复·待确认",
        }
        if row is not None:
            self.table.item(row, 8).setText(labels.get(state, state))
            self.table.item(row, 8).setToolTip(detail)
        self.upload_manager.update_state(chat_id, message_id, state, detail)
        if state == "completed" and self.cloud_library.isVisible():
            self.cloud_library.refresh()

    def _upload_label(self, key: tuple[int, int]) -> str:
        record = self.storage.get_upload(*key)
        video = self.library.video(*key)
        if video:
            kind, _ = self._cloud_match(video)
            if kind == "confirmed":
                return "云端已确认"
            if kind == "suspected":
                return "云端疑似重复"
            if kind == "unverified" and (record or self._cloud_files):
                return "云端待核对"
        if not record:
            return "云端缺失" if self._cloud_verified else "云端待核对"
        return {
            "queued": "等待上传",
            "processing": "规范元数据",
            "uploading": "上传中",
            "completed": "已上传",
            "failed": "上传失败",
            "cancelled": "已取消",
            "remote_missing": "云端已删除",
            "needs_review": "疑似重复·待确认",
        }.get(record["status"], record["status"])

    def _cloud_match(self, video: dict) -> tuple[str, str]:
        key = (int(video["chat_id"]), int(video["message_id"]))
        return classify_cloud_video(
            video, self.storage.get_upload(*key), self._cloud_files,
            verified=self._cloud_verified,
        )

    def set_cloud_inventory(self, files: list[dict], error: str) -> None:
        if error:
            self._cloud_verified = False
            self.show_notice(f"云端核对失败：{error}。历史清单仍可查看。", "warning")
        else:
            self._cloud_files = files
            self._cloud_verified = True
        for key, row in self._row_by_key.items():
            self.table.item(row, 8).setText(self._upload_label(key))
        self.cloud_library.set_inventory(self._cloud_files, self._cloud_verified)

    def set_cloud_review(self, items: list[dict]) -> None:
        for item in items:
            key = (int(item["chat_id"]), int(item["message_id"]))
            self._cloud_review[key] = item
        if items:
            self.show_notice(
                f"有 {len(items)} 个云端文件疑似对应历史下载；请在“云盘清单”中逐项确认。",
                "warning", 12000,
            )

    def show_cloud_rename_probe(self, ok: bool, message: str) -> None:
        self._rename_probe_ok = ok
        QMessageBox.information(self, "云端改名实测", message)

    def show_cloud_rename_plan(self, plan: dict) -> None:
        candidates = plan["candidates"]
        skipped = plan["skipped"]
        if not candidates:
            QMessageBox.information(self, "旧文件改名预览", f"没有可安全改名的旧文件；跳过 {len(skipped)} 项。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("旧云端文件改名预览")
        dialog.resize(1000, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            f"可改名 {len(candidates)} 项，跳过 {len(skipped)} 项。仅勾选有准确上传记录的文件；目标已存在的文件会跳过。"
        ))
        table = QTableWidget(len(candidates), 3)
        table.setHorizontalHeaderLabels(["选择", "当前云端路径", "改名后路径"])
        table.setColumnWidth(0, 60)
        table.setColumnWidth(1, 430)
        table.horizontalHeader().setStretchLastSection(True)
        for row, item in enumerate(candidates):
            checkbox = QTableWidgetItem()
            checkbox.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            checkbox.setCheckState(Qt.Unchecked)
            table.setItem(row, 0, checkbox)
            table.setItem(row, 1, QTableWidgetItem(item["old_path"]))
            table.setItem(row, 2, QTableWidgetItem(item["new_path"]))
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("执行勾选的改名")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        selected = [
            item for row, item in enumerate(candidates)
            if table.item(row, 0).checkState() == Qt.Checked
        ]
        if not selected:
            return
        if not self._rename_probe_ok:
            QMessageBox.warning(self, "尚未实测", "请先点击“实测云端改名”，确认当前挂载支持改名。")
            return
        self.upload_worker.submit("execute_cloud_renames", selected)

    def show_cloud_rename_result(self, results: list[dict]) -> None:
        success = sum(1 for item in results if item["ok"])
        failed = [item for item in results if not item["ok"]]
        detail = "\n".join(f"{item['old_path']}：{item['detail']}" for item in failed[:10])
        QMessageBox.information(
            self, "云端改名结果",
            f"已核验改名 {success} 项，失败 {len(failed)} 项。" + (f"\n{detail}" if detail else ""),
        )

    def _download_label(self, key: tuple[int, int]) -> str:
        record = self.storage.get_download(*key)
        if not record or record["status"] != "completed":
            return "未下载"
        if not Path(record["file_path"]).is_file():
            return "本地已删除"
        compression = self.storage.get_compression(*key)
        if compression and compression["status"] == "completed":
            return "本地存在 / 已压缩"
        return "本地存在"

    def refresh_cloud_upload_status(self) -> None:
        keys = list(self._row_by_key.keys())
        if not keys:
            return
        self.upload_worker.submit("verify_uploads", keys)

    def open_cloud(self) -> None:
        QDesktopServices.openUrl(
            QUrl(
                f"http://127.0.0.1:{self.config.openlist_port}/"
                f"{self.config.openlist_mount.strip('/')}/"
                f"{self.config.cloud_root.strip('/')}"
            )
        )

    def refresh_downloaded_names(self) -> None:
        directory = self.directory_edit.text().strip()
        if not directory:
            return
        self._directory_scan_id += 1
        self._existing_names.clear()
        self._apply_existing_marks()
        self.worker.submit(
            "scan_download_directory", self._directory_scan_id, directory
        )

    def set_downloaded_names(
        self, request_id: int, names: set[str], error: str
    ) -> None:
        if request_id != self._directory_scan_id:
            return
        if error:
            self.show_notice(f"扫描下载目录失败：{error}", "error", 12000)
            return
        self._existing_names = {str(name).casefold() for name in names}
        self._apply_existing_marks()

    def _apply_existing_marks(self) -> None:
        for row in range(self.table.rowCount()):
            self._apply_existing_mark(row)

    def _apply_existing_mark(self, row: int) -> None:
        data = self.table.item(row, 0).data(Qt.UserRole)
        exists = data["name"].casefold() in self._existing_names
        brush = QBrush(QColor("#8a8a8a")) if exists else QBrush()
        for column in range(2, self.table.columnCount()):
            item = self.table.item(row, column)
            if item is None:
                continue
            item.setForeground(brush)
            font = item.font()
            font.setItalic(exists)
            item.setFont(font)
        name_item = self.table.item(row, 2)
        name_item.setToolTip(
            "当前保存目录中存在同名视频文件" if exists else ""
        )
        status_item = self.table.item(row, 7)
        if exists and status_item.text() == "未下载":
            status_item.setText("目录中已存在")
        elif not exists and status_item.text() == "目录中已存在":
            status_item.setText("未下载")

    def open_directory(self) -> None:
        directory = Path(self.directory_edit.text() or self.paths.default_download_dir)
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def test_proxy(self) -> None:
        if proxy_available(self.config.proxy_host, self.config.proxy_port):
            self.show_notice("127.0.0.1:7890 正在监听。", "success")
        else:
            self.show_notice(
                "127.0.0.1:7890 未监听，请先启动 Clash。", "error", 12000
            )

    def open_log_directory(self) -> None:
        self.paths.ensure()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.data_dir)))

    def logout(self) -> None:
        result = QMessageBox.question(
            self,
            "退出 Telegram",
            "确定退出账号并清除本机会话和 API Hash 吗？下载记录会保留。",
        )
        if result == QMessageBox.Yes:
            self.worker.submit("logout")

    def show_notice(
        self, text: str, level: str = "info", timeout: int = 7000
    ) -> None:
        if not text:
            return
        palette = {
            "info": ("ℹ", "#eaf3ff", "#2d6aa0", "#174f7a"),
            "success": ("✓", "#e9f7ef", "#39945e", "#17633a"),
            "warning": ("!", "#fff6df", "#d09a24", "#805a00"),
            "error": ("×", "#fdecec", "#d05a5a", "#8f2525"),
        }
        icon, background, border, foreground = palette.get(level, palette["info"])
        self.notice_label.setText(f"{icon}  {text}")
        self.notice_label.setStyleSheet(
            "QLabel {"
            f"background: {background}; color: {foreground}; "
            f"border: 1px solid {border}; border-radius: 6px; "
            "font-weight: 600;"
            "}"
        )
        self.notice_label.show()
        self.statusBar().showMessage(text)
        self.notice_timer.stop()
        if timeout > 0:
            self.notice_timer.start(timeout)

    def show_error(self, text: str) -> None:
        self.show_notice(text, "error", 12000)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()

    def closeEvent(self, event: object) -> None:
        if self._really_quit:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "Telegram 视频下载器",
            "程序仍在托盘运行，自动下载不会中断。",
            QSystemTrayIcon.Information,
            3000,
        )

    def quit_application(self) -> None:
        self._really_quit = True
        if self.worker.isRunning():
            self.worker.stop_gracefully()
        if self.upload_worker.isRunning():
            self.upload_worker.stop_gracefully()
        if self.compression_worker.is_running():
            self.compression_worker.stop_gracefully()
        self.tray.hide()
        QApplication.quit()
