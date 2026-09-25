"""Reviewable cloud catalogue and legacy filename migration controls."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .cloud_catalog import classify_cloud_video
from .library import LibraryStore
from .paths import AppPaths
from .storage import Storage


VIDEO_SUFFIXES = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".webm", ".wmv",
}


class CloudLibraryDialog(QDialog):
    scan_requested = Signal()
    confirm_requested = Signal(object)
    override_requested = Signal(object)
    probe_requested = Signal()
    plan_requested = Signal()

    def __init__(self, paths: AppPaths, storage: Storage, library: LibraryStore, parent=None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.storage = storage
        self.library = library
        self.files: list[dict] = library.cloud_files()
        self.verified = False
        self.setWindowTitle("云盘清单与重复视频核对")
        self.resize(1100, 640)
        layout = QVBoxLayout(self)
        self.summary = QLabel("云端清单尚未在本次运行中核对。")
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["视频或文件", "本地", "云端", "对应云端路径"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((300, 135, 155, 460)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.scan_button = QPushButton("核对云端并补传")
        self.local_button = QPushButton("查找未登记本地文件")
        self.confirm_button = QPushButton("确认所选疑似对应")
        self.override_button = QPushButton("忽略疑似并上传")
        self.probe_button = QPushButton("实测云端改名")
        self.plan_button = QPushButton("预览旧文件改名")
        for button in (
            self.scan_button, self.local_button, self.confirm_button,
            self.override_button, self.probe_button, self.plan_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        self.scan_button.clicked.connect(self.scan_requested.emit)
        self.local_button.clicked.connect(lambda: self.refresh(scan_local=True))
        self.confirm_button.clicked.connect(self._confirm_selected)
        self.override_button.clicked.connect(self._override_selected)
        self.probe_button.clicked.connect(self.probe_requested.emit)
        self.plan_button.clicked.connect(self.plan_requested.emit)
        self.refresh()

    def set_inventory(self, files: list[dict], verified: bool) -> None:
        self.files = list(files)
        self.verified = verified
        self.refresh()

    def refresh(self, *, scan_local: bool = False) -> None:
        self.table.setRowCount(0)
        counts = {"confirmed": 0, "suspected": 0, "missing": 0, "unverified": 0}
        seen: set[tuple[int, int]] = set()
        for chat in self.library.chats():
            for video in self.library.videos(int(chat["chat_id"])):
                key = (int(video["chat_id"]), int(video["message_id"]))
                seen.add(key)
                record = self.storage.get_download(*key)
                source = Path(record["file_path"]) if record and record["file_path"] else None
                local = bool(source and source.is_file())
                kind, remote_path = classify_cloud_video(
                    video, self.storage.get_upload(*key), self.files,
                    verified=self.verified,
                )
                counts[kind] += 1
                labels = {
                    "confirmed": "已确认", "suspected": "疑似重复·待确认",
                    "missing": "未找到", "unverified": "待核对",
                }
                payload = {
                    **video, "file_path": str(source) if source else "",
                    "remote_path": remote_path, "match_kind": kind,
                }
                self._add_row(
                    str(video["name"]), "存在" if local else ("已下载后删除" if record else "未下载"),
                    labels[kind], remote_path, payload,
                )
        for record in self.storage.completed_downloads():
            key = (int(record["chat_id"]), int(record["message_id"]))
            if key in seen:
                continue
            source = Path(record["file_path"])
            upload = self.storage.get_upload(*key)
            video = {
                "chat_id": key[0], "message_id": key[1],
                "chat_title": self.library.chat_title(key[0]) or (upload or {}).get("chat_title") or record["chat_title"],
                "name": source.name, "size": int(record["file_size"]),
            }
            kind, remote_path = classify_cloud_video(video, upload, self.files, verified=self.verified)
            counts[kind] += 1
            self._add_row(
                source.name, "存在" if source.is_file() else "已下载后删除",
                {"confirmed": "已确认", "suspected": "疑似重复·待确认", "missing": "未找到", "unverified": "待核对"}[kind],
                remote_path,
                {**video, "file_path": str(source), "remote_path": remote_path, "match_kind": kind},
            )
        unknown = 0
        if scan_local:
            unknown = self._add_untracked_local_files()
        checked = self.library.get_state("cloud_checked_at")[:16].replace("T", " ")
        prefix = f"本次已核对 {len(self.files)} 个云端文件" if self.verified else f"上次清单 {len(self.files)} 个文件，待本次核对"
        self.summary.setText(
            f"{prefix} · 云端确认 {counts['confirmed']} · 疑似 {counts['suspected']} · "
            f"未找到 {counts['missing']} · 未核实 {counts['unverified']}"
            + (f" · 本地未登记 {unknown}" if scan_local else "")
            + (f" · 清单时间 {checked}" if checked else "")
        )

    def _add_row(self, name: str, local: str, cloud: str, remote: str, payload: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate((name, local, cloud, remote)):
            self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.item(row, 0).setData(Qt.UserRole, payload)

    def _add_untracked_local_files(self) -> int:
        known = {
            str(Path(record["file_path"]).resolve()).casefold()
            for record in self.storage.completed_downloads() if record["file_path"]
        }
        roots = {self.paths.default_download_dir.resolve()}
        roots.update(Path(rule["directory"]).resolve() for rule in self.storage.all_rules() if rule["directory"])
        found: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for folder, _, files in os.walk(root):
                for name in files:
                    path = Path(folder) / name
                    resolved = str(path.resolve())
                    if path.suffix.casefold() not in VIDEO_SUFFIXES or resolved.casefold() in known or resolved.casefold() in found:
                        continue
                    found.add(resolved.casefold())
                    self._add_row(name, "未登记", "未自动匹配", resolved, {"untracked": True})
        return len(found)

    def _selected_payload(self) -> dict | None:
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.UserRole) if row >= 0 else None

    def _confirm_selected(self) -> None:
        item = self._selected_payload()
        if not item or item.get("match_kind") != "suspected":
            QMessageBox.information(self, "选择疑似文件", "请先选择一条“疑似重复·待确认”的视频。")
            return
        if QMessageBox.question(
            self, "确认云端对应关系",
            f"确认 Telegram 视频“{item['name']}”对应云端文件：\n{item['remote_path']}\n\n确认后会作为已上传记录。",
        ) == QMessageBox.Yes:
            self.confirm_requested.emit(item)

    def _override_selected(self) -> None:
        item = self._selected_payload()
        if not item or item.get("match_kind") != "suspected" or not item.get("file_path") or not Path(item["file_path"]).is_file():
            QMessageBox.information(self, "选择本地视频", "请选择本地存在、云端疑似重复的视频。")
            return
        if QMessageBox.question(
            self, "仍要上传", f"将为“{item['name']}”上传一个带 Telegram 标识的新文件。现有云端文件会保留。继续吗？",
        ) == QMessageBox.Yes:
            self.override_requested.emit(item)
