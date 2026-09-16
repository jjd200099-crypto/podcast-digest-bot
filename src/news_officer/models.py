from __future__ import annotations

import hashlib
import json
import re
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

    @property
    def record_revision_sha256(self) -> str:
        """Hash every persisted input used by the summarizer or renderer."""

        payload = {
            "episode": self.episode.to_persisted_dict(),
            "transcript": {
                "content_sha256": self.content_sha256,
                "source": self.transcript.source,
                "source_url": self.transcript.source_url,
                "language": self.transcript.language,
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TranscriptAttachment:
    """Immutable identity of one human-readable transcript attachment.

    The source and digest hashes bind the editorial inputs. ``rendered_sha256``
    additionally binds every visible metadata field and the final bytes. This
    lets a retry reuse an already-uploaded artifact, but never silently render a
    newer transcript or corrected metadata beside an older persisted reply.
    """

    episode_id: str
    source_sha256: str
    record_revision_sha256: str
    digest_sha256: str
    renderer_version: str
    filename: str
    rendered_sha256: str

    def to_persisted_dict(self) -> dict[str, str]:
        return {
            "episode_id": self.episode_id,
            "source_sha256": self.source_sha256,
            "record_revision_sha256": self.record_revision_sha256,
            "digest_sha256": self.digest_sha256,
            "renderer_version": self.renderer_version,
            "filename": self.filename,
            "rendered_sha256": self.rendered_sha256,
        }

    @classmethod
    def from_persisted_dict(cls, value: dict[str, Any]) -> TranscriptAttachment:
        fields = {
            name: str(value.get(name) or "").strip()
            for name in (
                "episode_id",
                "source_sha256",
                "record_revision_sha256",
                "digest_sha256",
                "renderer_version",
                "filename",
                "rendered_sha256",
            )
        }
        if not all(fields.values()):
            raise ValueError("A transcript attachment descriptor is incomplete")
        for name in (
            "source_sha256",
            "record_revision_sha256",
            "digest_sha256",
            "rendered_sha256",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", fields[name]):
                raise ValueError(f"Invalid transcript attachment {name}")
        return cls(**fields)

    @classmethod
    def from_rendered(
        cls,
        record: StoredTranscript,
        *,
        digest_markdown: str,
        renderer_version: str,
        filename: str,
        content: bytes,
    ) -> TranscriptAttachment:
        return cls(
            episode_id=record.episode.id,
            source_sha256=record.content_sha256,
            record_revision_sha256=record.record_revision_sha256,
            digest_sha256=hashlib.sha256(
                digest_markdown.encode("utf-8")
            ).hexdigest(),
            renderer_version=renderer_version,
            filename=filename,
            rendered_sha256=hashlib.sha256(content).hexdigest(),
        )


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    message: str
    episode: Episode | None = None
    attachment: TranscriptAttachment | None = None


@dataclass(frozen=True)
class DailyItem:
    episode: Episode
    status: str
    message: str = ""
    attachment: TranscriptAttachment | None = None

    def to_persisted_dict(self) -> dict[str, Any]:
        value = {
            "episode": self.episode.to_persisted_dict(),
            "status": self.status,
            "message": self.message,
        }
        if self.attachment is not None:
            value["attachment"] = self.attachment.to_persisted_dict()
        return value

    @classmethod
    def from_persisted_dict(cls, value: dict[str, Any]) -> DailyItem:
        attachment = value.get("attachment")
        return cls(
            episode=Episode.from_persisted_dict(value["episode"]),
            status=str(value["status"]),
            message=str(value.get("message") or ""),
            attachment=(
                TranscriptAttachment.from_persisted_dict(attachment)
                if isinstance(attachment, dict)
                else None
            ),
        )
