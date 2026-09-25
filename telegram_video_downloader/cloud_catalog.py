"""Evidence-based matching between Telegram videos and cloud files."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from .library import identity_from_name
from .naming import sanitize_component


MatchKind = Literal["confirmed", "suspected", "missing", "unverified"]


def classify_cloud_video(
    video: dict,
    upload: dict | None,
    files: list[dict],
    *,
    verified: bool,
) -> tuple[MatchKind, str]:
    """A filename-only hit never becomes a confirmed backup."""
    if not verified:
        return "unverified", ""
    key = (int(video["chat_id"]), int(video["message_id"]))
    by_path = {str(file["remote_path"]): file for file in files}
    if upload and upload.get("status") == "completed" and upload.get("remote_path"):
        remote_path = str(upload["remote_path"])
        cloud_file = by_path.get(remote_path)
        if cloud_file and (
            int(upload.get("remote_size") or 0) > 0
            and int(cloud_file["size"]) == int(upload["remote_size"])
        ):
            return "confirmed", remote_path
    for file in files:
        path = str(file["remote_path"])
        if identity_from_name(PurePosixPath(path).name) == key:
            if upload and upload.get("status") != "completed":
                return "suspected", path
            if upload and int(upload.get("remote_size") or 0) != int(file["size"]):
                return "suspected", path
            if int(file["size"]) <= 0:
                return "suspected", path
            return "confirmed", path
    chat_name = sanitize_component(str(video.get("chat_title") or "未命名")).casefold()
    stem = sanitize_component(PurePosixPath(str(video.get("name") or "")).stem).casefold()
    for file in files:
        path = PurePosixPath(str(file["remote_path"]))
        parts = path.parts
        if len(parts) < 5 or parts[-4].casefold() != chat_name:
            continue
        remote_stem = path.stem.casefold()
        if remote_stem in {stem, f"{stem}_tg_{key[1]}"}:
            return "suspected", str(path)
    return "missing", ""
