from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    text: str
    chat_type: str = ""
    sender_open_id: str = ""
    thread_id: str = ""
    parent_message_id: str = ""


@dataclass(frozen=True)
class Job:
    key: str
    kind: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class OutboxItem:
    """One immutable Feishu delivery part persisted before network I/O."""

    id: int
    job_key: str
    group_key: str
    delivery_key: str
    operation: str
    target_id: str
    target_type: str
    reply_in_thread: bool
    part: int
    total_parts: int
    msg_type: str
    content: str
    uuid: str
    attempts: int = 0


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    url: str
    show: str
    duration_seconds: float | None = None
    duration_string: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize only fields required to resume delivery after a restart."""

        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "show": self.show,
            "duration_seconds": self.duration_seconds,
            "duration_string": self.duration_string,
            "published_at": self.published_at.isoformat()
            if self.published_at
            else None,
        }

    @classmethod
    def from_persisted_dict(cls, value: dict[str, Any]) -> Episode:
        published_at = value.get("published_at")
        return cls(
            id=str(value["id"]),
            title=str(value["title"]),
            url=str(value["url"]),
            show=str(value["show"]),
            duration_seconds=float(value["duration_seconds"])
            if value.get("duration_seconds") is not None
            else None,
            duration_string=str(value["duration_string"])
            if value.get("duration_string") is not None
            else None,
            published_at=datetime.fromisoformat(str(published_at))
            if published_at
            else None,
        )


@dataclass(frozen=True)
class Transcript:
    text: str
    source: str
    source_url: str
    verified_complete: bool
    language: str = "en"


@dataclass(frozen=True)
class StoredTranscript:
    """One verified complete transcript archived for later delivery and Q&A."""

    episode: Episode
    transcript: Transcript
    content_sha256: str
    stored_at: datetime

    @property
    def reference(self) -> str:
        return hashlib.sha256(self.episode.id.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    message: str
    episode: Episode | None = None


@dataclass(frozen=True)
class DailyItem:
    episode: Episode
    status: str
    message: str = ""

    def to_persisted_dict(self) -> dict[str, Any]:
        return {
            "episode": self.episode.to_persisted_dict(),
            "status": self.status,
            "message": self.message,
        }

    @classmethod
    def from_persisted_dict(cls, value: dict[str, Any]) -> DailyItem:
        return cls(
            episode=Episode.from_persisted_dict(value["episode"]),
            status=str(value["status"]),
            message=str(value.get("message") or ""),
        )
