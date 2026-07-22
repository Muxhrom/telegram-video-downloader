import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from telegram_video_downloader.config import AppConfig
from telegram_video_downloader.paths import AppPaths
from telegram_video_downloader.ui import MainWindow
from telegram_video_downloader.upload_ui import CloudSettingsDialog


def test_cloud_password_can_be_edited_saved_and_copied() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = CloudSettingsDialog(AppConfig())
    changed: list[str] = []
    notices: list[tuple[str, str]] = []
    dialog.password_change_requested.connect(changed.append)
    dialog.notice.connect(lambda text, level: notices.append((text, level)))
    dialog.set_state({"password": "InitialPassword123!"})
    dialog.password.setText("CustomPassword123!")

    dialog._copy_password()
    dialog._save()

    assert QApplication.clipboard().text() == "CustomPassword123!"
    assert changed == ["CustomPassword123!"]
    assert notices[-1] == ("管理员密码已复制到剪贴板。", "success")
    dialog.hide()
    app.processEvents()


def test_main_window_starts_and_worker_stops(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    paths = AppPaths(
        data_dir=tmp_path / "data",
        config_file=tmp_path / "data" / "config.json",
        database_file=tmp_path / "data" / "state.sqlite3",
        session_file=tmp_path / "data" / "telegram.session",
        log_file=tmp_path / "data" / "app.log",
        default_download_dir=tmp_path / "downloads",
        tools_dir=tmp_path / "data" / "tools",
        openlist_dir=tmp_path / "data" / "tools" / "openlist",
        upload_staging_dir=tmp_path / "data" / "upload_staging",
    )
    paths.ensure()
    window = MainWindow(paths, AppConfig())
    QTest.qWait(200)
    assert window.worker.isRunning()
    assert window.windowTitle() == "Telegram 视频下载器"
    large_chat_id = -1002985557858
    window.current_chat = {"chat_id": large_chat_id, "title": "测试群"}
    window.video_request_id = 1
    video = {
        "chat_id": large_chat_id,
        "message_id": 9,
        "chat_title": "测试群",
        "name": "目标视频.mp4",
        "media_kind": "普通视频",
        "size": 1024,
        "date": "2026-07-22T12:00:00+00:00",
        "extension": ".mp4",
    }
    window.add_videos(1, large_chat_id, [video], {}, False)
    window.video_search.setText("不匹配")
    assert window.table.isRowHidden(0)
    assert "均不符合当前筛选" in window.statusBar().currentMessage()
    assert not window.notice_label.isHidden()
    assert "均不符合当前筛选" in window.notice_label.text()
    window.video_search.clear()
    assert not window.table.isRowHidden(0)
    window.set_downloaded_names(0, {"目标视频.mp4"}, "")
    assert window.table.item(0, 6).text() == "目录中已存在"
    assert window.table.item(0, 1).foreground().color().name() == "#8a8a8a"

    payload = {
        "item": video,
        "directory": str(tmp_path / "downloads"),
        "priority": 1,
    }
    window.download_manager.add_job(payload)
    window.download_manager.update_state(
        large_chat_id, video["message_id"], "downloading", str(tmp_path / "target.mp4")
    )
    window.download_manager.update_metrics(
        large_chat_id,
        video["message_id"],
        {"current": 512, "total": 1024, "speed": 256, "eta": 2},
    )
    window.download_manager.update_progress(large_chat_id, video["message_id"], 50)
    assert window.download_manager.table.rowCount() == 1
    assert window.download_manager.table.item(0, 3).text() == "下载中"
    assert window.download_manager.table.item(0, 5).text() == "256 B/s"
    assert window.download_manager.table.cellWidget(0, 4).value() == 50
    window.download_manager.set_acceleration_state(True, "安全加速已开启")
    assert window.download_manager.acceleration_checkbox.isChecked()
    assert window.download_manager.acceleration_status.text() == "安全加速已开启"
    window.download_manager.set_acceleration_state(False, "检测到限流，已自动关闭")
    assert not window.download_manager.acceleration_checkbox.isChecked()
    window.upload_manager.add_job(
        {
            **video,
            "file_path": str(tmp_path / "target.mp4"),
            "priority": 1,
        }
    )
    window.upload_manager.update_state(
        large_chat_id, video["message_id"], "uploading", "/aliyun-drive/test.mp4"
    )
    window.upload_manager.update_metrics(
        large_chat_id,
        video["message_id"],
        {"current": 512, "total": 1024, "speed": 128, "eta": 4},
    )
    assert window.upload_manager.table.item(0, 3).text() == "上传中"
    assert window.upload_manager.table.item(0, 5).text() == "128 B/s"
    assert window.worker.stop_gracefully()
    assert window.upload_worker.stop_gracefully()
    window.tray.hide()
    window.hide()
    assert not window.worker.isRunning()
    app.processEvents()
