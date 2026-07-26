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
                CREATE TABLE IF NOT EXISTS compression_records (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    original_path TEXT NOT NULL DEFAULT '',
                    original_size INTEGER NOT NULL DEFAULT 0,
                    current_path TEXT NOT NULL DEFAULT '',
                    compressed_size INTEGER NOT NULL DEFAULT 0,
                    profile TEXT NOT NULL DEFAULT 'balanced',
                    status TEXT NOT NULL DEFAULT 'queued',
                    saved_bytes INTEGER NOT NULL DEFAULT 0,
                    saved_percent REAL NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '',
                    temp_path TEXT NOT NULL DEFAULT '',
                    snapshot_path TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
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

    def update_download_file(self, chat_id: int, message_id: int, file_path: str, file_size: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE downloads SET file_path=?, file_size=?, status='completed' WHERE chat_id=? AND message_id=?",
                (file_path, int(file_size), chat_id, message_id),
            )
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

    def update_upload_source(self, chat_id: int, message_id: int, source_path: str, source_size: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE uploads SET source_path=?, source_size=? WHERE chat_id=? AND message_id=?",
                (source_path, int(source_size), chat_id, message_id),
            )

    def get_compression(self, chat_id: int, message_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM compression_records WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def save_compression_job(self, chat_id: int, message_id: int, original_path: str, original_size: int, profile: str, status: str = 'queued', detail: str = '等待压缩', temp_path: str = '', snapshot_path: str = '') -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO compression_records(chat_id,message_id,original_path,original_size,current_path,profile,status,detail,temp_path,snapshot_path,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,message_id) DO UPDATE SET
                    original_path=excluded.original_path, original_size=excluded.original_size,
                    profile=excluded.profile, status=excluded.status, detail=excluded.detail,
                    temp_path=excluded.temp_path, snapshot_path=excluded.snapshot_path, updated_at=excluded.updated_at
                """,
                (chat_id, message_id, original_path, int(original_size), original_path, profile, status, detail, temp_path, snapshot_path, now),
            )

    def update_compression(self, chat_id: int, message_id: int, status: str, detail: str = '', current_path: str = '', compressed_size: int = 0, saved_bytes: int = 0, saved_percent: float = 0.0, temp_path: str = '', snapshot_path: str = '') -> None:
        now = datetime.now(timezone.utc).isoformat()
        started = now if status == 'compressing' else None
        completed = now if status == 'completed' else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE compression_records SET status=?, detail=?,
                    current_path=CASE WHEN ?<>'' THEN ? ELSE current_path END,
                    compressed_size=CASE WHEN ? > 0 THEN ? ELSE compressed_size END,
                    saved_bytes=CASE WHEN ? > 0 THEN ? ELSE saved_bytes END,
                    saved_percent=CASE WHEN ? > 0 THEN ? ELSE saved_percent END,
                    temp_path=CASE WHEN ?<>'' THEN ? ELSE temp_path END,
                    snapshot_path=CASE WHEN ?<>'' THEN ? ELSE snapshot_path END,
                    started_at=COALESCE(?, started_at), completed_at=COALESCE(?, completed_at), updated_at=?
                WHERE chat_id=? AND message_id=?
                """,
                (status, detail, current_path, current_path, compressed_size, compressed_size, saved_bytes, saved_bytes, saved_percent, saved_percent, temp_path, temp_path, snapshot_path, snapshot_path, started, completed, now, chat_id, message_id),
            )

    def pending_compressions(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM compression_records WHERE status IN ('queued','compressing','failed') ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]
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
