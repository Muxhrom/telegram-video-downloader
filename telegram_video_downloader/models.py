from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(slots=True)
class ChatInfo:
    chat_id: int
    title: str
    kind: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class VideoInfo:
    chat_id: int
    message_id: int
    chat_title: str
    name: str
    media_kind: str
    size: int
    date: datetime
    extension: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["date"] = self.date.isoformat()
        return data
