from __future__ import annotations

import mimetypes
import re
from datetime import datetime
from pathlib import Path


INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(value: str, *, max_length: int = 120) -> str:
    cleaned = INVALID_CHARS.sub("_", value).strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "未命名"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:max_length].rstrip(". ") or "未命名"


def extension_for(filename: str | None, mime_type: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix
        if suffix and len(suffix) <= 10:
            return suffix.lower()
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed or ".mp4"


def choose_video_name(
    original_name: str | None,
    caption: str | None,
    date: datetime,
    message_id: int,
    extension: str,
) -> str:
    if original_name:
        path = Path(original_name)
        stem = sanitize_component(path.stem)
        suffix = extension_for(original_name, None)
        return f"{stem}{suffix}"
    first_line = next((line.strip() for line in (caption or "").splitlines() if line.strip()), "")
    if first_line:
        return f"{sanitize_component(first_line)}{extension}"
    stamp = date.astimezone().strftime("%Y%m%d_%H%M%S")
    return f"video_{stamp}_{message_id}{extension}"


def unique_target(
    directory: Path,
    filename: str,
    message_id: int,
    reserved: set[Path] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    reserved = reserved if reserved is not None else set()
    target = directory / filename
    if (
        target not in reserved
        and not target.exists()
        and not target.with_name(target.name + ".part").exists()
    ):
        return target
    candidate = directory / f"{target.stem}_{message_id}{target.suffix}"
    index = 2
    while (
        candidate in reserved
        or candidate.exists()
        or candidate.with_name(candidate.name + ".part").exists()
    ):
        candidate = directory / f"{target.stem}_{message_id}_{index}{target.suffix}"
        index += 1
    return candidate
