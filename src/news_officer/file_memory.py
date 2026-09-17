"""Immutable, integrity-checked public podcast files on the persistent volume.

SQLite is the ingestion/verification ledger. These files are the durable reading
layer for Agent evidence, never a place to persist private dialogue or secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from .library import LibraryDocument


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _slug(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value).strip("._-")[:60] or "podcast"


def public_source_url(url: str) -> str:
    return re.sub(
        r"^https://app\.podwise\.ai/api/open/v1/episodes/(\d+)/transcripts$",
        r"https://podwise.ai/episodes/\1", url,
    )


class PodcastFileMemory:
    def __init__(self, store, root: Path):
        self.store = store
        root = Path(root).expanduser().absolute()
        if root.is_symlink():
            raise ValueError("Podcast memory root cannot be a symlink")
        self.root = root.resolve()
        if self.root == Path(self.root.anchor) or self.root == store.path.parent.resolve():
            raise ValueError("Podcast memory requires a dedicated subdirectory")
        self._lock = threading.RLock()
        self._entries = {}
        self.warnings = []

    def _path(self, relative: str) -> Path:
        path = self.root / relative
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError("Podcast memory root changed")
        path.resolve().relative_to(self.root)
        for parent in [path, *path.parents]:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("Podcast memory does not follow symlinks")
        return path

    def _write(self, relative: str, data: bytes, *, immutable=True) -> bool:
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if path.read_bytes() == data:
                return False
            if immutable:
                raise ValueError("Archived file differs; do not overwrite or trust it")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return True

    def sync(self) -> list[str]:
        """Backfill every verified episode; publish index only after complete files."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            index_path = self._path("index.json")
            if index_path.exists():
                old_index = json.loads(index_path.read_bytes())
                if old_index.get("format") != "news-officer-podcast-memory":
                    raise ValueError("Refusing to overwrite an unrelated memory index")
            with self.store._connect() as db:
                rows = db.execute(
                    "SELECT t.*, d.source_sha256 AS digest_source, "
                    "d.record_revision_sha256 AS digest_revision, d.digest_markdown "
                    "FROM episode_transcripts t LEFT JOIN episode_digests d "
                    "USING(episode_id) ORDER BY t.stored_at DESC,t.episode_id"
                ).fetchall()
            entries, warnings, changed = {}, [], []
            for row in rows:
                record = self.store._stored_transcript(row)
                try:
                    raw = record.transcript.text.encode()
                    if not raw or sha256(raw) != record.content_sha256:
                        raise ValueError("Source ledger hash mismatch")
                    digest = row["digest_markdown"] if (
                        row["digest_source"] == record.content_sha256
                        and row["digest_revision"] == record.record_revision_sha256
                    ) else ""
                    digest = digest or ""
                    summary_hash = sha256(digest.encode()) if digest else None
                    episode = record.episode
                    day = episode.published_at.date().isoformat() if episode.published_at else "undated"
                    folder = (
                        f"shows/{_slug(episode.show)}-{sha256(episode.show.encode())[:8]}/"
                        f"{day}-{_slug(episode.title)}-{sha256(episode.id.encode())[:16]}/"
                        f"revisions/{record.record_revision_sha256}/{summary_hash or 'no-summary'}"
                    )
                    metadata = {
                        "schema_version": 1, "reference": record.reference,
                        "episode": episode.to_persisted_dict(), "verified_complete": True,
                        "source": record.transcript.source,
                        "source_url": record.transcript.source_url,
                        "public_source_url": public_source_url(record.transcript.source_url),
                        "language": record.transcript.language,
                        "transcript_sha256": record.content_sha256,
                        "record_revision_sha256": record.record_revision_sha256,
                        "summary_sha256": summary_hash,
                        "stored_at": record.stored_at.isoformat(),
                    }
                    data = _json(metadata)
                    touched = self._write(folder + "/transcript.txt", raw)
                    if digest:
                        touched = self._write(folder + "/summary.md", digest.encode()) or touched
                    touched = self._write(folder + "/metadata.json", data) or touched
                    entries[record.reference] = {
                        "directory": folder, "metadata_sha256": sha256(data),
                        "transcript_sha256": record.content_sha256,
                        "episode_id": episode.id, "title": episode.title,
                        "show": episode.show, "published_at": metadata["episode"]["published_at"],
                    }
                    if touched:
                        changed.append(record.reference)
                except (OSError, ValueError):
                    warnings.append(f"播客 {record.reference} 文件归档或完整性校验失败，本次不作为问答依据")
            self._write("index.json", _json({"format": "news-officer-podcast-memory", "schema_version": 1, "episodes": entries}), immutable=False)
            self._entries, self.warnings = entries, warnings
            return changed

    def snapshot(self):
        with self._lock:
            self.sync()
            documents, warnings = [], list(self.warnings)
            for reference, entry in self._entries.items():
                try:
                    metadata_bytes = self._path(entry["directory"] + "/metadata.json").read_bytes()
                    raw = self._path(entry["directory"] + "/transcript.txt").read_bytes()
                    if (sha256(metadata_bytes) != entry["metadata_sha256"]
                            or sha256(raw) != entry["transcript_sha256"]):
                        raise ValueError("File hash changed")
                    metadata = json.loads(metadata_bytes)
                    documents.append(LibraryDocument(
                        reference, f"{entry['title']} | {entry['show']}",
                        metadata["public_source_url"], raw.decode(),
                    ))
                except (OSError, ValueError):
                    warnings.append(f"播客 {reference} 文件读取或完整性校验失败，本次不作为问答依据")
            return documents, warnings
