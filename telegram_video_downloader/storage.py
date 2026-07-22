from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Storage:
    def __init__(self, database_file: Path) -> None:
        self.database_file = database_file
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS auto_rules (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    directory TEXT NOT NULL,
                    enabled_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    source_size INTEGER NOT NULL DEFAULT 0,
                    processed_name TEXT NOT NULL DEFAULT '',
                    remote_path TEXT NOT NULL DEFAULT '',
                    remote_size INTEGER NOT NULL DEFAULT 0,
                    remote_etag TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 1,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    detail TEXT NOT NULL DEFAULT '',
                    completed_at TEXT,
                    checked_at TEXT,
                    PRIMARY KEY (chat_id, message_id)
                );
                """
            )

    def get_rule(self, chat_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM auto_rules WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return dict(row) if row else None

    def enabled_rules(self) -> dict[int, dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM auto_rules WHERE enabled = 1"
            ).fetchall()
        return {int(row["chat_id"]): dict(row) for row in rows}

    def save_rule(self, chat_id: int, chat_title: str, enabled: bool, directory: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        previous = self.get_rule(chat_id)
        enabled_at = now if enabled and not (previous and previous["enabled"]) else (
            previous["enabled_at"] if previous else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auto_rules(chat_id, chat_title, enabled, directory, enabled_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_title=excluded.chat_title,
                    enabled=excluded.enabled,
                    directory=excluded.directory,
                    enabled_at=excluded.enabled_at,
                    updated_at=excluded.updated_at
                """,
                (chat_id, chat_title, int(enabled), directory, enabled_at, now),
            )

    def is_downloaded(self, chat_id: int, message_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM downloads WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return bool(row and row["status"] == "completed")

    def record_download(
        self,
        chat_id: int,
        message_id: int,
        file_path: str,
        file_size: int,
        status: str,
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat() if status == "completed" else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO downloads(chat_id, message_id, file_path, file_size, status, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    file_path=excluded.file_path,
                    file_size=excluded.file_size,
                    status=excluded.status,
                    completed_at=excluded.completed_at
                """,
                (chat_id, message_id, file_path, file_size, status, completed_at),
            )

    def get_download(self, chat_id: int, message_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM downloads WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def completed_downloads(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT downloads.*, COALESCE(auto_rules.chat_title, '历史下载') AS chat_title
                FROM downloads
                LEFT JOIN auto_rules ON auto_rules.chat_id = downloads.chat_id
                WHERE downloads.status = 'completed'
                ORDER BY downloads.completed_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_upload(self, chat_id: int, message_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def save_upload_job(
        self,
        chat_id: int,
        message_id: int,
        chat_title: str,
        source_path: str,
        source_size: int,
        priority: int = 1,
        status: str = "queued",
        detail: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO uploads(
                    chat_id, message_id, chat_title, source_path, source_size,
                    priority, status, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    chat_title=excluded.chat_title,
                    source_path=excluded.source_path,
                    source_size=excluded.source_size,
                    priority=excluded.priority,
                    status=excluded.status,
                    detail=excluded.detail
                """,
                (
                    chat_id,
                    message_id,
                    chat_title,
                    source_path,
                    source_size,
                    priority,
                    status,
                    detail,
                ),
            )

    def update_upload(
        self,
        chat_id: int,
        message_id: int,
        status: str,
        detail: str = "",
        processed_name: str = "",
        remote_path: str = "",
        remote_size: int = 0,
        remote_etag: str = "",
        increment_retry: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        completed_at = now if status == "completed" else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE uploads SET
                    status=?,
                    detail=?,
                    processed_name=CASE WHEN ? <> '' THEN ? ELSE processed_name END,
                    remote_path=CASE WHEN ? <> '' THEN ? ELSE remote_path END,
                    remote_size=CASE WHEN ? > 0 THEN ? ELSE remote_size END,
                    remote_etag=CASE WHEN ? <> '' THEN ? ELSE remote_etag END,
                    retry_count=retry_count + ?,
                    completed_at=CASE WHEN ? IS NOT NULL THEN ? ELSE completed_at END,
                    checked_at=?
                WHERE chat_id=? AND message_id=?
                """,
                (
                    status,
                    detail,
                    processed_name,
                    processed_name,
                    remote_path,
                    remote_path,
                    remote_size,
                    remote_size,
                    remote_etag,
                    remote_etag,
                    int(increment_retry),
                    completed_at,
                    completed_at,
                    now,
                    chat_id,
                    message_id,
                ),
            )

    def set_upload_priority(
        self, chat_id: int, message_id: int, priority: int
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE uploads SET priority=? WHERE chat_id=? AND message_id=?",
                (priority, chat_id, message_id),
            )

    def pending_uploads(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM uploads
                WHERE status IN ('queued', 'processing', 'uploading', 'failed', 'cancelled')
                ORDER BY priority, rowid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upload_statuses(self, chat_id: int, message_ids: list[int]) -> dict[int, dict]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        parameters = [chat_id, *message_ids]
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM uploads WHERE chat_id=? AND message_id IN ({placeholders})",
                parameters,
            ).fetchall()
        return {int(row["message_id"]): dict(row) for row in rows}

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM auto_rules")
            connection.execute("DELETE FROM downloads")
            connection.execute("DELETE FROM uploads")
