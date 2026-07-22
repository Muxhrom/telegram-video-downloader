from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .config import AppConfig


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


class UploadManagerDialog(QDialog):
    cancel_requested = Signal(object)
    retry_requested = Signal(object)
    priority_requested = Signal(object, int)
    pause_requested = Signal()
    refresh_requested = Signal()
    open_cloud_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("上传管理")
        self.resize(1180, 590)
        self._row_by_key: dict[tuple[int, int], int] = {}
        self._states: dict[tuple[int, int], str] = {}
        self._speeds: dict[tuple[int, int], float] = {}
        self._paused = False

        layout = QVBoxLayout(self)
        self.summary = QLabel("暂无上传任务")
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "视频名称",
                "来源",
                "优先级",
                "阶段/状态",
                "进度",
                "速度",
                "已上传 / 总大小",
                "剩余时间",
                "重试",
                "云端路径 / 详情",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((250, 130, 70, 110, 130, 95, 155, 85, 55, 300)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.pause_button = QPushButton("暂停上传")
        self.high_button = QPushButton("优先上传")
        self.normal_button = QPushButton("普通优先级")
        self.low_button = QPushButton("低优先级")
        self.cancel_button = QPushButton("取消选中")
        self.retry_button = QPushButton("重试失败/取消")
        self.clear_button = QPushButton("清理已完成")
        self.local_button = QPushButton("打开本地目录")
        self.cloud_button = QPushButton("打开云端")
        self.refresh_button = QPushButton("刷新云端状态")
        self.close_button = QPushButton("关闭")
        for button in (
            self.pause_button,
            self.high_button,
            self.normal_button,
            self.low_button,
            self.cancel_button,
            self.retry_button,
            self.clear_button,
            self.local_button,
            self.cloud_button,
            self.refresh_button,
        ):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(self.close_button)
        layout.addLayout(row)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.high_button.clicked.connect(lambda: self._priority(0))
        self.normal_button.clicked.connect(lambda: self._priority(1))
        self.low_button.clicked.connect(lambda: self._priority(2))
        self.cancel_button.clicked.connect(self._cancel)
        self.retry_button.clicked.connect(self._retry)
        self.clear_button.clicked.connect(self._clear)
        self.local_button.clicked.connect(self._open_local)
        self.cloud_button.clicked.connect(self.open_cloud_requested.emit)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.close_button.clicked.connect(self.hide)

    def add_job(self, payload: dict) -> None:
        key = (int(payload["chat_id"]), int(payload["message_id"]))
        row = self._row_by_key.get(key)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_by_key[key] = row
            for column in range(self.table.columnCount()):
                self.table.setItem(row, column, QTableWidgetItem())
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setFormat("%p%")
            self.table.setCellWidget(row, 4, progress)
        self.table.item(row, 0).setText(payload.get("name") or Path(payload["file_path"]).name)
        self.table.item(row, 0).setData(Qt.UserRole, dict(payload))
        self.table.item(row, 1).setText(payload.get("chat_title", ""))
        self.table.item(row, 2).setText(self._priority_label(payload.get("priority", 1)))
        self.table.item(row, 3).setText("等待处理")
        self.table.item(row, 5).setText("-")
        self.table.item(row, 6).setText(f"0 B / {human_size(int(payload.get('size', 0)))}")
        self.table.item(row, 7).setText("-")
        self.table.item(row, 8).setText(str(payload.get("retry_count", 0)))
        self.table.item(row, 9).setText(payload.get("file_path", ""))
        self._states[key] = payload.get("status", "queued")
        self._speeds[key] = 0.0
        self._update_summary()

    def update_state(self, chat_id: int, message_id: int, state: str, detail: str) -> None:
        key = (int(chat_id), int(message_id))
        row = self._row_by_key.get(key)
        if row is None:
            return
        labels = {
            "queued": "等待处理",
            "processing": "规范元数据",
            "uploading": "上传中",
            "completed": "已上传",
            "failed": "失败",
            "cancelled": "已取消",
            "remote_missing": "云端已删除",
        }
        self._states[key] = state
        self.table.item(row, 3).setText(labels.get(state, state))
        self.table.item(row, 3).setToolTip(detail)
        if detail:
            self.table.item(row, 9).setText(detail)
            self.table.item(row, 9).setToolTip(detail)
        if state == "completed":
            self.table.cellWidget(row, 4).setValue(100)
        if state not in {"uploading"}:
            self._speeds[key] = 0.0
            self.table.item(row, 5).setText("-")
            self.table.item(row, 7).setText("-")
        self._update_summary()

    def update_progress(self, chat_id: int, message_id: int, value: int) -> None:
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        if row is not None:
            self.table.cellWidget(row, 4).setValue(int(value))

    def update_metrics(self, chat_id: int, message_id: int, metrics: dict) -> None:
        key = (int(chat_id), int(message_id))
        row = self._row_by_key.get(key)
        if row is None:
            return
        current = int(metrics.get("current", 0))
        total = int(metrics.get("total", 0))
        speed = float(metrics.get("speed", 0))
        eta = float(metrics.get("eta", 0))
        phase = str(metrics.get("phase", "sending"))
        self._speeds[key] = speed
        self.table.item(row, 5).setText(f"{human_size(int(speed))}/s" if speed else "-")
        self.table.item(row, 6).setText(f"{human_size(current)} / {human_size(total)}")
        self.table.item(row, 7).setText(human_duration(eta) if eta else "-")
        if phase == "cloud_commit":
            self.table.item(row, 3).setText("\u4e91\u7aef\u5199\u5165\u4e2d")
        elif self._states.get(key) == "uploading":
            self.table.item(row, 3).setText("\u4e0a\u4f20\u4e2d")
        self._update_summary()

    def update_priority(self, chat_id: int, message_id: int, priority: int) -> None:
        row = self._row_by_key.get((int(chat_id), int(message_id)))
        if row is not None:
            self.table.item(row, 2).setText(self._priority_label(priority))

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("恢复上传" if paused else "暂停上传")

    def _selected(self) -> list[tuple[int, dict]]:
        result = []
        for index in self.table.selectionModel().selectedRows():
            payload = self.table.item(index.row(), 0).data(Qt.UserRole)
            result.append((index.row(), payload))
        return result

    def _keys(self) -> list[tuple[int, int]]:
        return [
            (int(payload["chat_id"]), int(payload["message_id"]))
            for _, payload in self._selected()
        ]

    def _cancel(self) -> None:
        keys = self._keys()
        if keys:
            self.cancel_requested.emit(keys)

    def _priority(self, priority: int) -> None:
        keys = []
        for row, payload in self._selected():
            key = (int(payload["chat_id"]), int(payload["message_id"]))
            if self._states.get(key) == "queued":
                keys.append(key)
                payload["priority"] = priority
                self.table.item(row, 0).setData(Qt.UserRole, payload)
                self.table.item(row, 2).setText(self._priority_label(priority))
        if keys:
            self.priority_requested.emit(keys, priority)

    def _retry(self) -> None:
        jobs = []
        for _, payload in self._selected():
            key = (int(payload["chat_id"]), int(payload["message_id"]))
            if self._states.get(key) in {"failed", "cancelled", "remote_missing"}:
                jobs.append(payload)
        if jobs:
            self.retry_requested.emit(jobs)

    def _clear(self) -> None:
        removable = [
            (row, key)
            for key, row in self._row_by_key.items()
            if self._states.get(key) == "completed"
        ]
        for row, key in sorted(removable, reverse=True):
            self.table.removeRow(row)
            self._states.pop(key, None)
            self._speeds.pop(key, None)
        self._row_by_key.clear()
        for row in range(self.table.rowCount()):
            payload = self.table.item(row, 0).data(Qt.UserRole)
            self._row_by_key[
                (int(payload["chat_id"]), int(payload["message_id"]))
            ] = row
        self._update_summary()

    def _open_local(self) -> None:
        selected = self._selected()
        if not selected:
            return
        path = Path(selected[0][1]["file_path"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    @staticmethod
    def _priority_label(priority: int) -> str:
        return {0: "高", 1: "普通", 2: "低"}.get(int(priority), "普通")

    def _update_summary(self) -> None:
        counts = {
            state: list(self._states.values()).count(state)
            for state in ("queued", "processing", "uploading", "completed", "failed", "cancelled")
        }
        self.summary.setText(
            "等待 {queued} ｜ 处理中 {processing} ｜ 上传中 {uploading} ｜ "
            "已上传 {completed} ｜ 失败 {failed} ｜ 已取消 {cancelled} ｜ 总速度 {speed}/s".format(
                **counts, speed=human_size(int(sum(self._speeds.values())))
            )
        )


class CloudSettingsDialog(QDialog):
    install_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    test_requested = Signal()
    refresh_requested = Signal()
    ffmpeg_install_requested = Signal()
    supplement_requested = Signal()
    password_change_requested = Signal(str)
    notice = Signal(str, str)
    saved = Signal()

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("阿里云盘与上传设置")
        self.resize(760, 430)
        self._saved_password = ""
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.openlist_status = QLabel("尚未检测")
        self.mount_status = QLabel("尚未检测")
        self.address = QLineEdit(f"http://127.0.0.1:{config.openlist_port}")
        self.address.setReadOnly(True)
        self.username = QLineEdit("admin")
        self.username.setReadOnly(True)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("自定义 8–128 位管理员密码")
        self.show_password = QCheckBox("显示")
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        password_row.addWidget(self.show_password)
        self.mount = QLineEdit(config.openlist_mount)
        self.cloud_root = QLineEdit(config.cloud_root)
        self.auto_upload = QCheckBox("下载完成后自动规范元数据并上传")
        self.auto_upload.setChecked(config.cloud_auto_upload)
        self.ffmpeg = QLineEdit(config.ffmpeg_path)
        self.ffmpeg_button = QPushButton("选择 ffmpeg.exe")
        self.ffmpeg_install_button = QPushButton("自动安装 FFmpeg")
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg, 1)
        ffmpeg_row.addWidget(self.ffmpeg_button)
        ffmpeg_row.addWidget(self.ffmpeg_install_button)
        form.addRow("OpenList 状态", self.openlist_status)
        form.addRow("阿里云盘挂载", self.mount_status)
        form.addRow("本地管理地址", self.address)
        form.addRow("管理员用户名", self.username)
        form.addRow("管理员密码", password_row)
        form.addRow("OpenList 挂载名", self.mount)
        form.addRow("云端根目录", self.cloud_root)
        form.addRow("FFmpeg", ffmpeg_row)
        form.addRow(self.auto_upload)
        layout.addLayout(form)
        note = QLabel(
            "首次使用：安装并启动 OpenList → 打开管理页 → 在“存储”中添加"
            "“阿里云盘 Open/OAuth2”，挂载路径填写 aliyun-drive → 返回本窗口测试挂载。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.install_button = QPushButton("安装/修复 OpenList")
        self.start_button = QPushButton("启动")
        self.stop_button = QPushButton("停止")
        self.admin_button = QPushButton("打开管理页")
        self.copy_button = QPushButton("复制密码")
        self.test_button = QPushButton("测试挂载")
        self.supplement_button = QPushButton("扫描并补传")
        self.save_button = QPushButton("保存设置")
        self.close_button = QPushButton("关闭")
        for button in (
            self.install_button,
            self.start_button,
            self.stop_button,
            self.admin_button,
            self.copy_button,
            self.test_button,
            self.supplement_button,
            self.save_button,
            self.close_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        self.install_button.clicked.connect(self.install_requested.emit)
        self.start_button.clicked.connect(self.start_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.test_button.clicked.connect(self.test_requested.emit)
        self.supplement_button.clicked.connect(self.supplement_requested.emit)
        self.admin_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.address.text()))
        )
        self.copy_button.clicked.connect(self._copy_password)
        self.show_password.toggled.connect(
            lambda checked: self.password.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        self.ffmpeg_button.clicked.connect(self._choose_ffmpeg)
        self.ffmpeg_install_button.clicked.connect(self.ffmpeg_install_requested.emit)
        self.save_button.clicked.connect(self._save)
        self.close_button.clicked.connect(self.hide)

    def _choose_ffmpeg(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg.exe", self.ffmpeg.text(), "ffmpeg.exe (ffmpeg.exe)"
        )
        if selected:
            self.ffmpeg.setText(selected)

    def _save(self) -> None:
        password = self.password.text().strip()
        if password != self._saved_password:
            if len(password) < 8 or len(password) > 128:
                self.notice.emit("管理员密码必须为 8–128 个字符。", "error")
                return
            if "\n" in password or "\r" in password:
                self.notice.emit("管理员密码不能包含换行符。", "error")
                return
            self.password_change_requested.emit(password)
        self.config.openlist_mount = self.mount.text().strip().strip("/") or "aliyun-drive"
        self.config.cloud_root = self.cloud_root.text().strip().strip("/") or "Telegram视频下载器"
        self.config.cloud_auto_upload = self.auto_upload.isChecked()
        self.config.ffmpeg_path = self.ffmpeg.text().strip()
        self.saved.emit()

    def _copy_password(self) -> None:
        password = self.password.text()
        if not password:
            self.notice.emit("当前没有可复制的管理员密码。", "error")
            return
        QApplication.clipboard().setText(password)
        self.notice.emit("管理员密码已复制到剪贴板。", "success")

    def set_state(self, state: dict) -> None:
        installed = bool(state.get("installed"))
        running = bool(state.get("running"))
        self.openlist_status.setText(
            "已运行" if running else ("已安装，未运行" if installed else "未安装")
        )
        self.mount_status.setText("已连接" if state.get("mount_ready") else "等待测试")
        self.address.setText(state.get("base_url", self.address.text()))
        saved_password = state.get("password", "")
        if saved_password:
            self._saved_password = saved_password
            if not self.password.hasFocus() or not self.password.text():
                self.password.setText(saved_password)
        if state.get("ffmpeg_path"):
            self.ffmpeg.setText(state["ffmpeg_path"])
