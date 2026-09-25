"""Persistent video catalogue and transfer state shared by the desktop workers."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .naming import sanitize_component


IDENTITY_SUFFIX = re.compile(r"__tg_(-?\d+)_([1-9]\d*)$", re.IGNORECASE)


def cloud_name(name: str, chat_id: int, message_id: int) -> str:
    """Keep the readable stem while giving every upload a stable identity."""
    path = Path(name)
    stem = IDENTITY_SUFFIX.sub("", path.stem)
    return f"{sanitize_component(stem)}__tg_{chat_id}_{message_id}{path.suffix.lower()}"


def identity_from_name(name: str) -> tuple[int, int] | None:
    match = IDENTITY_SUFFIX.search(Path(name).stem)
    return (int(match[1]), int(match[2])) if match else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LibraryStore:
    def __init__(self, database_file: Path) -> None:
        self.database_file = database_file
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_chats (
                    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    kind TEXT NOT NULL, scan_state TEXT NOT NULL DEFAULT '{}',
                    checked_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS library_videos (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS library_videos_chat_order
                    ON library_videos(chat_id, message_id DESC);
                CREATE TABLE IF NOT EXISTS library_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS download_tasks (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    payload TEXT NOT NULL, directory TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS cloud_inventory (
                    remote_path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                    etag TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS cloud_renames (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    old_path TEXT NOT NULL, new_path TEXT NOT NULL,
                    renamed_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id, old_path)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_file, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def get_state(self, name: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM library_state WHERE name=?", (name,)).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, name: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO library_state(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )

    def chats(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT chat_id,title,kind FROM library_chats ORDER BY title COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def save_chats(self, chats: list[dict]) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT INTO library_chats(chat_id,title,kind) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,kind=excluded.kind",
                [(int(c["chat_id"]), str(c["title"]), str(c["kind"])) for c in chats],
            )

    def videos(self, chat_id: int) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM library_videos WHERE chat_id=? ORDER BY message_id DESC",
                (chat_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def video(self, chat_id: int, message_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM library_videos WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def chat_title(self, chat_id: int) -> str:
        with self._connect() as db:
            row = db.execute("SELECT title FROM library_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return str(row["title"]) if row else ""

    def save_videos(self, videos: list[dict]) -> None:
        if not videos:
            return
        with self._connect() as db:
            db.executemany(
                "INSERT INTO library_videos(chat_id,message_id,payload) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,message_id) DO UPDATE SET payload=excluded.payload",
                [
                    (int(v["chat_id"]), int(v["message_id"]), json.dumps(v, ensure_ascii=False))
                    for v in videos
                ],
            )

    def replace_chat_videos(self, chat_id: int, videos: list[dict], state: dict) -> None:
        """Publish a full history recheck only after the scan finished."""
        with self._connect() as db:
            db.execute("DELETE FROM library_videos WHERE chat_id=?", (chat_id,))
            db.executemany(
                "INSERT INTO library_videos(chat_id,message_id,payload) VALUES(?,?,?)",
                [
                    (chat_id, int(v["message_id"]), json.dumps(v, ensure_ascii=False))
                    for v in videos
                ],
            )
            db.execute(
                "UPDATE library_chats SET scan_state=?,checked_at=? WHERE chat_id=?",
                (json.dumps(state, ensure_ascii=False), _now(), chat_id),
            )

    def newest_message_id(self, chat_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(message_id) AS newest FROM library_videos WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return int(row["newest"] or 0)

    def scan_state(self, chat_id: int) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT scan_state FROM library_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return json.loads(row["scan_state"]) if row else {}

    def save_scan_state(self, chat_id: int, state: dict, checked: bool = False) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO library_chats(chat_id,title,kind,scan_state,checked_at) "
                "VALUES(?,'未命名','群组',?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "scan_state=excluded.scan_state,checked_at=CASE WHEN ? THEN excluded.checked_at ELSE library_chats.checked_at END",
                (chat_id, json.dumps(state, ensure_ascii=False), _now() if checked else "", int(checked)),
            )

    def checked_at(self, chat_id: int) -> str:
        with self._connect() as db:
            row = db.execute("SELECT checked_at FROM library_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return str(row["checked_at"]) if row else ""

    def clear_chat(self, chat_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM library_videos WHERE chat_id=?", (chat_id,))
            db.execute(
                "UPDATE library_chats SET scan_state='{}',checked_at='' WHERE chat_id=?", (chat_id,)
            )

    def save_download_task(self, item: dict, directory: str, priority: int = 1) -> None:
        key = (int(item["chat_id"]), int(item["message_id"]))
        with self._connect() as db:
            db.execute(
                "INSERT INTO download_tasks(chat_id,message_id,payload,directory,priority,status,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?) ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                "payload=excluded.payload,directory=excluded.directory,priority=excluded.priority,"
                "status='queued',detail='',updated_at=excluded.updated_at",
                (*key, json.dumps(item, ensure_ascii=False), directory, priority, _now()),
            )

    def update_download_task(self, chat_id: int, message_id: int, status: str, detail: str = "") -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE download_tasks SET status=?,detail=?,updated_at=? WHERE chat_id=? AND message_id=?",
                (status, detail, _now(), chat_id, message_id),
            )

    def set_download_priority(self, chat_id: int, message_id: int, priority: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE download_tasks SET priority=? WHERE chat_id=? AND message_id=?",
                (priority, chat_id, message_id),
            )

    def download_tasks(self, *, pending_only: bool = False) -> list[dict]:
        where = "WHERE status IN ('queued','downloading')" if pending_only else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM download_tasks {where} ORDER BY priority,updated_at LIMIT 500"
            ).fetchall()
        return [{**dict(row), "item": json.loads(row["payload"])} for row in rows]

    def replace_cloud_inventory(self, files: list[dict]) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM cloud_inventory")
            db.executemany(
                "INSERT INTO cloud_inventory(remote_path,size,etag) VALUES(?,?,?)",
                [(str(f["remote_path"]), int(f["size"]), str(f.get("etag", ""))) for f in files],
            )
            db.execute(
                "INSERT INTO library_state(name,value) VALUES('cloud_checked_at',?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (_now(),),
            )

    def cloud_files(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM cloud_inventory").fetchall()
        return [dict(row) for row in rows]

    def record_cloud_rename(self, chat_id: int, message_id: int, old_path: str, new_path: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO cloud_renames(chat_id,message_id,old_path,new_path,renamed_at) "
                "VALUES(?,?,?,?,?)",
                (chat_id, message_id, old_path, new_path, _now()),
            )
            updated = db.execute(
                "UPDATE uploads SET remote_path=?,processed_name=? "
                "WHERE chat_id=? AND message_id=? AND remote_path=?",
                (new_path, Path(new_path).name, chat_id, message_id, old_path),
            )
            if updated.rowcount != 1:
                raise RuntimeError("上传记录已变化，无法登记云端改名")
            updated = db.execute(
                "UPDATE cloud_inventory SET remote_path=? WHERE remote_path=?",
                (new_path, old_path),
            )
            if updated.rowcount != 1:
                raise RuntimeError("云端清单已变化，无法登记云端改名")
