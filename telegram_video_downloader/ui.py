from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
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
from .credentials import delete_api_hash, load_api_hash
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
    acceleration_requested = Signal(bool)
    notice = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("下载管理")
        self.resize(980, 520)
        self._row_by_key: dict[tuple[int, int], int] = {}
        self._states: dict[tuple[int, int], str] = {}
        self._speeds: dict[tuple[int, int], float] = {}
        self._paused = False

        layout = QVBoxLayout(self)
        self.summary_label = QLabel("暂无下载任务")
        layout.addWidget(self.summary_label)
        acceleration_row = QHBoxLayout()
        self.acceleration_checkbox = QCheckBox("安全加速（最多 3 个文件并行）")
        self.acceleration_checkbox.setToolTip(
            "仅提高多个排队视频的总下载速度，不会并行切割单个视频；"
            "遇到 Telegram 限流或网络异常时会自动关闭。"
        )
        self.acceleration_status = QLabel("普通模式：最多同时下载 2 个文件；cryptg 本地加速始终启用")
        acceleration_row.addWidget(self.acceleration_checkbox)
        acceleration_row.addWidget(self.acceleration_status, 1)
        layout.addLayout(acceleration_row)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "视频名称",
                "来源",
                "优先级",
                "状态",
                "进度",
                "速度",
                "已下载 / 总大小",
                "剩余时间",
                "保存位置",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 280)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 75)
        self.table.setColumnWidth(3, 95)
        self.table.setColumnWidth(4, 140)
        self.table.setColumnWidth(5, 100)
        self.table.setColumnWidth(6, 155)
        self.table.setColumnWidth(7, 90)
        self.table.setColumnWidth(8, 280)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.pause_button = QPushButton("暂停队列")
        self.high_button = QPushButton("优先下载")
        self.normal_button = QPushButton("普通优先级")
        self.low_button = QPushButton("低优先级")
        self.cancel_button = QPushButton("取消选中")
        self.retry_button = QPushButton("重试失败/取消")
        self.clear_button = QPushButton("清理已完成")
        self.open_button = QPushButton("打开所在目录")
        self.close_button = QPushButton("关闭")
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.high_button)
        buttons.addWidget(self.normal_button)
        buttons.addWidget(self.low_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.acceleration_checkbox.toggled.connect(self.acceleration_requested.emit)
        self.high_button.clicked.connect(lambda: self._set_selected_priority(0))
        self.normal_button.clicked.connect(lambda: self._set_selected_priority(1))
        self.low_button.clicked.connect(lambda: self._set_selected_priority(2))
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.retry_button.clicked.connect(self._retry_selected)
        self.clear_button.clicked.connect(self._clear_completed)
        self.open_button.clicked.connect(self._open_selected_directory)
        self.close_button.clicked.connect(self.hide)

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
            self.table.setCellWidget(row, 4, progress)
        self.table.item(row, 0).setText(item["name"])
        self.table.item(row, 0).setData(Qt.UserRole, payload)
        self.table.item(row, 1).setText(item.get("chat_title", ""))
        self.table.item(row, 2).setText(self._priority_label(int(payload.get("priority", 1))))
        self.table.item(row, 3).setText("等待下载")
        self.table.item(row, 5).setText("-")
        self.table.item(row, 6).setText(f"0 B / {human_size(int(item.get('size', 0)))}")
        self.table.item(row, 7).setText("-")
        self.table.item(row, 8).setText(payload["directory"])
        progress = self.table.cellWidget(row, 4)
        progress.setValue(0)
        self._states[key] = "queued"
        self._speeds[key] = 0.0
        self._update_summary()

    def update_progress(self, chat_id: int, message_id: int, progress: int) -> None:
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        if row is not None:
            self.table.cellWidget(row, 4).setValue(progress)

    def update_metrics(self, chat_id: int, message_id: int, metrics: dict) -> None:
        key = (int(chat_id), int(message_id))
        row = self._row_by_key.get(key)
        if row is None:
            return
        current = int(metrics.get("current", 0))
        total = int(metrics.get("total", 0))
        speed = float(metrics.get("speed", 0.0))
        eta = float(metrics.get("eta", 0.0))
        self._speeds[key] = speed if self._states.get(key) == "downloading" else 0.0
        self.table.item(row, 5).setText(f"{human_size(int(speed))}/s" if speed > 0 else "-")
        self.table.item(row, 6).setText(f"{human_size(current)} / {human_size(total)}")
        self.table.item(row, 7).setText(human_duration(eta) if eta > 0 else "-")
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
        }
        self._states[key] = state
        status_item = self.table.item(row, 3)
        status_item.setText(labels.get(state, state))
        status_item.setToolTip(detail)
        if state in {"downloading", "completed"} and detail:
            self.table.item(row, 8).setText(detail)
        if state == "completed":
            self.table.cellWidget(row, 4).setValue(100)
        if state != "downloading":
            self._speeds[key] = 0.0
            self.table.item(row, 5).setText("-")
            self.table.item(row, 7).setText("-")
        self._update_summary()

    def update_priority(self, chat_id: int, message_id: int, priority: int) -> None:
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        if row is not None:
            self.table.item(row, 2).setText(self._priority_label(priority))

    @staticmethod
    def _priority_label(priority: int) -> str:
        return {0: "高", 1: "普通", 2: "低"}.get(int(priority), "普通")

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("恢复队列" if paused else "暂停队列")

    def set_acceleration_state(self, enabled: bool, reason: str) -> None:
        self.acceleration_checkbox.blockSignals(True)
        self.acceleration_checkbox.setChecked(bool(enabled))
        self.acceleration_checkbox.blockSignals(False)
        self.acceleration_status.setText(reason)
        self.acceleration_status.setStyleSheet(
            "color: #168443;" if enabled else "color: #666666;"
        )

    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _cancel_selected(self) -> None:
        keys = []
        for row in self._selected_rows():
            payload = self.table.item(row, 0).data(Qt.UserRole)
            item = payload["item"]
            keys.append((int(item["chat_id"]), int(item["message_id"])))
        if keys:
            self.cancel_requested.emit(keys)

    def _set_selected_priority(self, priority: int) -> None:
        keys = []
        for row in self._selected_rows():
            payload = self.table.item(row, 0).data(Qt.UserRole)
            item = payload["item"]
            key = (int(item["chat_id"]), int(item["message_id"]))
            if self._states.get(key) == "queued":
                keys.append(key)
                payload["priority"] = priority
                self.table.item(row, 2).setText(self._priority_label(priority))
        if keys:
            self.priority_requested.emit(keys, priority)

    def _retry_selected(self) -> None:
        jobs = []
        for row in self._selected_rows():
            payload = self.table.item(row, 0).data(Qt.UserRole)
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
        payload = self.table.item(row, 0).data(Qt.UserRole)
        displayed = Path(self.table.item(row, 8).text())
        directory = displayed.parent if displayed.suffix else Path(payload["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _clear_completed(self) -> None:
        removable = []
        for key, row in self._row_by_key.items():
            if self._states.get(key) == "completed":
                removable.append((row, key))
        for row, key in sorted(removable, reverse=True):
            self.table.removeRow(row)
            self._states.pop(key, None)
            self._speeds.pop(key, None)
        self._row_by_key.clear()
        for row in range(self.table.rowCount()):
            payload = self.table.item(row, 0).data(Qt.UserRole)
            item = payload["item"]
            self._row_by_key[(int(item["chat_id"]), int(item["message_id"]))] = row
        self._update_summary()

    def _update_summary(self) -> None:
        counts = {state: 0 for state in ("queued", "downloading", "completed", "failed", "cancelled")}
        for state in self._states.values():
            if state in counts:
                counts[state] += 1
        total_speed = sum(self._speeds.values())
        self.summary_label.setText(
            "等待 {queued} ｜ 下载中 {downloading} ｜ 已完成 {completed} ｜ "
            "失败 {failed} ｜ 已取消 {cancelled} ｜ 总速度 {speed}/s".format(
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
        self.worker = TelegramWorker(paths, config)
        self.upload_worker = UploadWorker(paths, config)
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
        self._build_ui()
        self.download_manager = DownloadManagerDialog(self)
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
        self.download_manager.acceleration_requested.connect(
            lambda enabled: self.worker.submit("set_acceleration_mode", enabled)
        )
        self.download_manager.notice.connect(self.show_notice)
        self.upload_manager = UploadManagerDialog(self)
        self.cloud_settings = CloudSettingsDialog(config, self)
        self._connect_upload_ui()
        self._build_tray()
        self._connect_worker()
        self.worker.start()
        self.upload_worker.start()
        QTimer.singleShot(350, self._auto_connect)
        QTimer.singleShot(
            500, lambda: self.upload_worker.submit("refresh_cloud_state")
        )

    def _build_ui(self) -> None:
        self.setWindowTitle("Telegram 视频下载器")
        self.resize(1180, 760)
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
        self.cloud_settings_button = QPushButton("云盘设置")
        top.addWidget(self.connection_label)
        top.addStretch()
        top.addWidget(self.proxy_button)
        top.addWidget(self.log_button)
        top.addWidget(self.download_manager_button)
        top.addWidget(self.upload_manager_button)
        top.addWidget(self.cloud_settings_button)
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
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.chat_search = QLineEdit()
        self.chat_search.setPlaceholderText("搜索群聊或频道…")
        self.chat_list = QListWidget()
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

        auto_row = QHBoxLayout()
        self.auto_checkbox = QCheckBox("自动下载这个群聊之后出现的新视频")
        self.directory_edit = QLineEdit()
        self.directory_edit.setReadOnly(True)
        self.directory_button = QPushButton("选择保存目录")
        self.refresh_names_button = QPushButton("刷新已下载标记")
        auto_row.addWidget(self.auto_checkbox)
        auto_row.addWidget(self.directory_edit, 1)
        auto_row.addWidget(self.directory_button)
        auto_row.addWidget(self.refresh_names_button)
        right_layout.addLayout(auto_row)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["选择", "视频名称", "类型", "大小", "日期", "来源", "下载状态", "上传状态"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 330)
        right_layout.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.invert_button = QPushButton("反选")
        self.download_button = QPushButton("下载所选")
        self.upload_button = QPushButton("上传所选已下载")
        self.pause_button = QPushButton("暂停队列")
        self.cancel_button = QPushButton("取消所选任务")
        self.open_button = QPushButton("打开下载目录")
        self.more_button = QPushButton("加载更多")
        for button in (
            self.select_all_button,
            self.invert_button,
            self.download_button,
            self.upload_button,
            self.pause_button,
            self.cancel_button,
            self.open_button,
            self.more_button,
        ):
            action_row.addWidget(button)
        right_layout.addLayout(action_row)
        self.overall_progress = QProgressBar()
        self.overall_progress.setFormat("当前任务总体进度 %p%")
        right_layout.addWidget(self.overall_progress)
        splitter.addWidget(right)
        splitter.setSizes([260, 920])
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
        self.cloud_settings_button.clicked.connect(self.show_cloud_settings)
        self.chat_search.textChanged.connect(self.filter_chats)
        self.chat_list.currentItemChanged.connect(self.chat_changed)
        self.video_search.textChanged.connect(self.filter_videos)
        self.date_filter.toggled.connect(self.filter_videos)
        self.date_from.dateChanged.connect(self.filter_videos)
        self.date_to.dateChanged.connect(self.filter_videos)
        self.directory_button.clicked.connect(self.choose_directory)
        self.refresh_names_button.clicked.connect(self.refresh_downloaded_names)
        self.auto_checkbox.toggled.connect(self.save_auto_rule)
        self.select_all_button.clicked.connect(lambda: self.set_visible_checks(Qt.Checked))
        self.invert_button.clicked.connect(self.invert_checks)
        self.download_button.clicked.connect(self.download_selected)
        self.upload_button.clicked.connect(self.upload_selected)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.open_button.clicked.connect(self.open_directory)
        self.more_button.clicked.connect(self.load_more)

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
        self.worker.video_scan_status.connect(self.set_video_scan_status)
        self.worker.download_progress.connect(self.update_download_progress)
        self.worker.download_metrics.connect(self.download_manager.update_metrics)
        self.worker.download_state.connect(self.update_download_state)
        self.worker.download_job.connect(self.download_manager.add_job)
        self.worker.download_priority.connect(self.download_manager.update_priority)
        self.worker.acceleration_changed.connect(
            self.download_manager.set_acceleration_state
        )
        self.worker.downloaded_names_ready.connect(self.set_downloaded_names)
        self.worker.connection_changed.connect(self.set_connection)

    def _connect_upload_ui(self) -> None:
        self.upload_manager.cancel_requested.connect(
            lambda keys: self.upload_worker.submit("cancel_uploads", keys)
        )
        self.upload_manager.retry_requested.connect(self.retry_uploads)
        self.upload_manager.priority_requested.connect(
            lambda keys, priority: self.upload_worker.submit(
                "set_priority", keys, priority
            )
        )
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
        self.upload_worker.upload_priority.connect(self.upload_manager.update_priority)
        self.upload_worker.cloud_state.connect(self.cloud_settings.set_state)

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
        color = "#16803a" if connected else "#b42318"
        self.connection_label.setText(f"● {text}")
        self.connection_label.setStyleSheet(f"color: {color}; font-weight: bold")
        self.tray.setToolTip(f"Telegram 视频下载器 - {text}")
        self.tray_status_action.setText(f"状态：{text}")

    def set_chats(self, chats: list[dict]) -> None:
        self.chat_list.clear()
        for chat in chats:
            item = QListWidgetItem(f"{chat['title']}  ·  {chat['kind']}")
            item.setData(Qt.UserRole, chat)
            self.chat_list.addItem(item)
        self.show_notice(f"已载入 {len(chats)} 个群聊/频道。", "success")

    def filter_chats(self, text: str) -> None:
        needle = text.casefold().strip()
        for index in range(self.chat_list.count()):
            item = self.chat_list.item(index)
            item.setHidden(needle not in item.text().casefold())

    def chat_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        del previous
        if not current:
            return
        self.current_chat = current.data(Qt.UserRole)
        self.video_request_id += 1
        self.video_search.clear()
        self.table.setRowCount(0)
        self._row_by_key.clear()
        self._progress_by_key.clear()
        self.video_page_state = {}
        self.reached_end = False
        self._scan_in_progress = False
        rule = self.storage.get_rule(self.current_chat["chat_id"])
        default_dir = self.paths.default_download_dir / sanitize_component(self.current_chat["title"])
        self.directory_edit.setText(rule["directory"] if rule else str(default_dir))
        self.auto_checkbox.blockSignals(True)
        self.auto_checkbox.setChecked(bool(rule and rule["enabled"]))
        self.auto_checkbox.blockSignals(False)
        self.refresh_downloaded_names()
        self.load_more()

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
            values = (
                video["name"],
                video["media_kind"],
                human_size(video["size"]),
                datetime.fromisoformat(video["date"]).astimezone().strftime("%Y-%m-%d %H:%M"),
                video["chat_title"],
                self._download_label(key),
                self._upload_label(key),
            )
            for column, value in enumerate(values, start=1):
                self.table.setItem(row, column, QTableWidgetItem(value))
            self._row_by_key[key] = row
            self._apply_existing_mark(row)
        if videos:
            self.table.sortItems(4, Qt.DescendingOrder)
            self._rebuild_row_index()
        if finished:
            self.video_page_state = page_state
            self.reached_end = bool(page_state.get("reached_end", False))
            self._scan_in_progress = False
            self.more_button.setEnabled(not self.reached_end)
            self.more_button.setText("已到最早视频" if self.reached_end else "加载更多")
        self.filter_videos()

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
        directory = self.directory_edit.text()
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.worker.submit("enqueue_downloads", videos, directory)
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
                    self.table.item(row, 6).setText("本地已删除")
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
        queued = 0
        for record in self.storage.completed_downloads():
            key = (int(record["chat_id"]), int(record["message_id"]))
            uploaded = self.storage.get_upload(*key)
            if uploaded and uploaded["status"] == "completed":
                continue
            source = Path(record["file_path"])
            if not source.is_file():
                continue
            row = self._row_by_key.get(key)
            video = (
                dict(self.table.item(row, 0).data(Qt.UserRole))
                if row is not None
                else {
                    "chat_id": key[0],
                    "message_id": key[1],
                    "chat_title": record.get("chat_title") or "历史下载",
                    "name": source.name,
                }
            )
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
        self.show_notice(f"已加入 {queued} 个历史上传任务。", "success")
        if queued:
            self.show_upload_manager()

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
            self.table.item(row, 6).setText(f"下载中 {progress}%")
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
        }
        if row is not None:
            self.table.item(row, 6).setText(labels.get(state, state))
            self.table.item(row, 6).setToolTip(detail)
        self.download_manager.update_state(chat_id, message_id, state, detail)
        if state == "completed":
            completed = Path(detail)
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
        if state == "failed":
            self.show_notice(f"下载失败：{detail}", "error", 12000)

    def retry_downloads(self, jobs: list[dict]) -> None:
        for job in jobs:
            item = dict(job["item"])
            item["priority"] = int(job.get("priority", 1))
            self.worker.submit("enqueue_downloads", [item], job["directory"])

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
            payload["priority"] = int(payload.get("priority", 1))
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
        }
        if row is not None:
            self.table.item(row, 7).setText(labels.get(state, state))
            self.table.item(row, 7).setToolTip(detail)
        self.upload_manager.update_state(chat_id, message_id, state, detail)

    def _upload_label(self, key: tuple[int, int]) -> str:
        record = self.storage.get_upload(*key)
        if not record:
            return "未上传"
        return {
            "queued": "等待上传",
            "processing": "规范元数据",
            "uploading": "上传中",
            "completed": "已上传",
            "failed": "上传失败",
            "cancelled": "已取消",
            "remote_missing": "云端已删除",
        }.get(record["status"], record["status"])

    def _download_label(self, key: tuple[int, int]) -> str:
        record = self.storage.get_download(*key)
        if not record or record["status"] != "completed":
            return "未下载"
        return "本地存在" if Path(record["file_path"]).is_file() else "本地已删除"

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
        for column in range(1, self.table.columnCount()):
            item = self.table.item(row, column)
            item.setForeground(brush)
            font = item.font()
            font.setItalic(exists)
            item.setFont(font)
        name_item = self.table.item(row, 1)
        name_item.setToolTip(
            "当前保存目录中存在同名视频文件" if exists else ""
        )
        status_item = self.table.item(row, 6)
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
        self.tray.hide()
        QApplication.quit()
