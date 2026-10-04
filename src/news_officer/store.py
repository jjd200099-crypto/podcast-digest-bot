from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import (
    Episode,
    IncomingMessage,
    Job,
    OutboxItem,
    StoredTranscript,
    Transcript,
)

RETRY_DELAYS_SECONDS = (60, 300, 900, 1800)
MESSAGE_RETRY_DELAYS_SECONDS = (3, 10, 30, 60)
FAILED_DAILY_REQUEUE_SECONDS = 3600
MAX_CONVERSATION_TURNS = 4
MAX_HISTORY_USER_CHARS = 500
MAX_HISTORY_ASSISTANT_CHARS = 2_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Durable single-replica queue and podcast state.

    SQLite keeps the first deployment simple. All persistence is behind this class,
    so a later move to Postgres does not affect the bot, plugins, or Feishu code.
    """

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    analysis_complete INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_ready_idx
                    ON jobs(status, available_at, created_at);

                CREATE TABLE IF NOT EXISTS podwise_process_requests (
                    seq INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_transcript_backlog (
                    episode_id TEXT PRIMARY KEY,
                    episode_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    next_check_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    show_name TEXT NOT NULL,
                    published_at TEXT,
                    result TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS editorial_reviews (
                    episode_id TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(episode_id, cache_key)
                );

                CREATE TABLE IF NOT EXISTS episode_transcripts (
                    episode_id TEXT PRIMARY KEY,
                    reference TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    episode_url TEXT NOT NULL,
                    show_name TEXT NOT NULL,
                    duration_seconds REAL,
                    duration_string TEXT,
                    published_at TEXT,
                    transcript_text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    language TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(length(transcript_text) > 0)
                );
                CREATE INDEX IF NOT EXISTS episode_transcripts_recent_idx
                    ON episode_transcripts(published_at DESC, updated_at DESC);

                CREATE TABLE IF NOT EXISTS episode_digests (
                    episode_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    record_revision_sha256 TEXT NOT NULL,
                    digest_markdown TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(length(digest_markdown) > 0),
                    FOREIGN KEY(episode_id) REFERENCES episode_transcripts(episode_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS conversation_contexts (
                    context_key TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL DEFAULT '',
                    pending_episode_ids_json TEXT NOT NULL DEFAULT '[]',
                    recent_episode_ids_json TEXT NOT NULL DEFAULT '[]',
                    history_json TEXT NOT NULL DEFAULT '[]',
                    pending_question TEXT NOT NULL DEFAULT '',
                    pending_action TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(target_type, target_id),
                    CHECK(target_type IN ('open_id', 'chat_id')),
                    CHECK(active IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS subscriptions_active_idx
                    ON subscriptions(active, target_type, target_id);

                CREATE TABLE IF NOT EXISTS job_results (
                    job_key TEXT NOT NULL,
                    result_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_key, result_key),
                    FOREIGN KEY(job_key) REFERENCES jobs(job_key) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS daily_release_announcements (
                    release_id TEXT PRIMARY KEY,
                    job_key TEXT NOT NULL REFERENCES jobs(job_key),
                    note_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prepared_digest_keys (
                    episode_id TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    digest_sha256 TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS discovery_observations (
                    episode_id TEXT PRIMARY KEY,
                    podcast_seq INTEGER,
                    show_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stars INTEGER NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    delivery_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    reply_in_thread INTEGER NOT NULL DEFAULT 0,
                    part INTEGER NOT NULL,
                    total_parts INTEGER NOT NULL,
                    msg_type TEXT NOT NULL DEFAULT 'post',
                    content TEXT NOT NULL,
                    uuid TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT,
                    remote_message_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT,
                    UNIQUE(job_key, delivery_key, part),
                    FOREIGN KEY(job_key) REFERENCES jobs(job_key) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS outbox_pending_idx
                    ON outbox(job_key, status, id);
                """
            )
            # Forward-compatible migration for databases created by 0.1.x.
            transcript_columns = {str(row['name']) for row in connection.execute('PRAGMA table_info(episode_transcripts)')}
            if 'episode_metadata_json' not in transcript_columns:
                connection.execute("ALTER TABLE episode_transcripts ADD COLUMN episode_metadata_json TEXT NOT NULL DEFAULT '{}'")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "analysis_complete" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN analysis_complete INTEGER NOT NULL DEFAULT 0"
                )
            if "session_key" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN session_key TEXT NOT NULL DEFAULT ''")
                for row in connection.execute("SELECT job_key,payload_json FROM jobs WHERE kind='message'").fetchall():
                    connection.execute("UPDATE jobs SET session_key=? WHERE job_key=?",
                                       (self._message_session(json.loads(row['payload_json'])), row['job_key']))
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_session_idx ON jobs(session_key,status,created_at)")
            outbox_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(outbox)").fetchall()
            }
            if "reply_in_thread" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE outbox ADD COLUMN reply_in_thread INTEGER NOT NULL DEFAULT 0"
                )
            if "msg_type" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE outbox ADD COLUMN msg_type TEXT NOT NULL DEFAULT 'post'"
                )
            if "remote_message_id" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE outbox ADD COLUMN remote_message_id TEXT NOT NULL DEFAULT ''"
                )
            context_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(conversation_contexts)"
                ).fetchall()
            }
            if "recent_episode_ids_json" not in context_columns:
                connection.execute(
                    "ALTER TABLE conversation_contexts "
                    "ADD COLUMN recent_episode_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "history_json" not in context_columns:
                connection.execute(
                    "ALTER TABLE conversation_contexts "
                    "ADD COLUMN history_json TEXT NOT NULL DEFAULT '[]'"
                )
            digest_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(episode_digests)"
                ).fetchall()
            }
            if "record_revision_sha256" not in digest_columns:
                # Existing digest rows predate full metadata binding. The empty
                # default deliberately makes them stale until regenerated.
                connection.execute(
                    "ALTER TABLE episode_digests ADD COLUMN "
                    "record_revision_sha256 TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _validate_subscription_target(target_type: str, target_id: str) -> None:
        if target_type not in {"open_id", "chat_id"}:
            raise ValueError(f"Unsupported subscription target type: {target_type}")
        if not target_id.strip():
            raise ValueError("Subscription target id cannot be empty")

    def seed_subscriptions(
        self,
        user_open_ids: tuple[str, ...],
        group_chat_ids: tuple[str, ...],
    ) -> int:
        """Import environment recipients without reviving explicit opt-outs.

        Inactive rows are retained as tombstones. This lets operators add a new
        seed later while ensuring a recipient that sent ``退订`` stays removed
        across restarts even if its old id remains in the environment.
        """

        targets = [("open_id", item) for item in user_open_ids] + [
            ("chat_id", item) for item in group_chat_ids
        ]
        now = _now()
        inserted = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for target_type, target_id in targets:
                target_id = target_id.strip()
                self._validate_subscription_target(target_type, target_id)
                result = connection.execute(
                    """
                    INSERT OR IGNORE INTO subscriptions(
                        target_type, target_id, active, source, created_at, updated_at
                    ) VALUES (?, ?, 1, 'environment', ?, ?)
                    """,
                    (target_type, target_id, now, now),
                )
                inserted += result.rowcount
        return inserted

    def add_subscription(
        self, target_type: str, target_id: str, source: str = "command"
    ) -> bool:
        """Activate a delivery target and report whether its state changed."""

        target_id = target_id.strip()
        self._validate_subscription_target(target_type, target_id)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                INSERT INTO subscriptions(
                    target_type, target_id, active, source, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(target_type, target_id) DO UPDATE SET
                    active = 1,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                WHERE subscriptions.active = 0
                """,
                (target_type, target_id, source, now, now),
            )
            return result.rowcount == 1

    def remove_subscription(self, target_type: str, target_id: str) -> bool:
        """Deactivate a target while retaining a tombstone for seed imports."""

        target_id = target_id.strip()
        self._validate_subscription_target(target_type, target_id)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            was_active = connection.execute(
                """
                SELECT 1 FROM subscriptions
                WHERE target_type = ? AND target_id = ? AND active = 1
                """,
                (target_type, target_id),
            ).fetchone() is not None
            connection.execute(
                """
                INSERT INTO subscriptions(
                    target_type, target_id, active, source, created_at, updated_at
                ) VALUES (?, ?, 0, 'command', ?, ?)
                ON CONFLICT(target_type, target_id) DO UPDATE SET
                    active = 0,
                    source = 'command',
                    updated_at = excluded.updated_at
                WHERE subscriptions.active = 1
                """,
                (target_type, target_id, now, now),
            )
            return was_active

    def list_subscriptions(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT target_type, target_id
                FROM subscriptions
                WHERE active = 1
                ORDER BY target_type, target_id
                """
            ).fetchall()
        return [(str(row["target_type"]), str(row["target_id"])) for row in rows]

    def has_subscriptions(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM subscriptions WHERE active = 1 LIMIT 1"
            ).fetchone()
        return row is not None

    def recover_interrupted_jobs(self) -> int:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', available_at = ?, updated_at = ?,
                    last_error = 'worker restarted while processing'
                WHERE status = 'processing'
                """,
                (_now(), _now()),
            )
            return result.rowcount

    def enqueue(self, key: str, kind: str, payload: dict) -> bool:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_key, kind, payload_json, status, attempts,
                    available_at, created_at, updated_at, session_key
                ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (key, kind, json.dumps(payload, ensure_ascii=False), now, now, now,
                 self._message_session(payload) if kind == "message" else ""),
            )
            if result.rowcount == 1:
                return True
            # A daily job that exhausted its immediate retries may be revived
            # later the same day by the scheduler. Its immutable results and
            # outbox are deliberately retained and resumed.
            if kind not in {"daily", "document"}:
                return False
            revived = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', attempts = 0, updated_at = ?
                WHERE job_key = ? AND kind IN ('daily','document') AND status = 'failed'
                    AND available_at <= ?
                """,
                (now, key, now),
            )
            return revived.rowcount == 1

    @staticmethod
    def _message_session(payload: dict) -> str:
        return IncomingMessage(**{name: str(payload.get(name) or '')
                                  for name in IncomingMessage.__dataclass_fields__}).conversation_key

    def claim_next(self, kind: str | None = None) -> Job | None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Atomic session FIFO, including older turns waiting for retry.
            # Independent conversations are free to run concurrently.
            row = connection.execute("""
                SELECT j.job_key,j.kind,j.payload_json,j.attempts FROM jobs j
                WHERE j.status='pending' AND j.available_at<=?
                  AND (? IS NULL OR j.kind=?)
                  AND (j.kind!='message' OR NOT EXISTS (
                    SELECT 1 FROM jobs older WHERE older.kind='message'
                      AND older.session_key=j.session_key AND older.job_key!=j.job_key
                      AND (older.status='processing' OR
                        (older.status='pending' AND (older.created_at<j.created_at OR
                         (older.created_at=j.created_at AND older.rowid<j.rowid))))))
                ORDER BY CASE j.kind WHEN 'message' THEN 0 ELSE 1 END,j.created_at,j.rowid
                LIMIT 1""", (now, kind, kind)).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE job_key = ? AND status = 'pending'
                """,
                (now, row["job_key"]),
            )
            if updated.rowcount != 1:
                return None
            return Job(
                key=row["job_key"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]) + 1,
            )

    def complete(self, key: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', updated_at = ?, last_error = NULL
                WHERE job_key = ? AND NOT EXISTS (
                    SELECT 1 FROM outbox
                    WHERE outbox.job_key = jobs.job_key AND outbox.status != 'sent'
                )
                """,
                (_now(), key),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"Cannot complete job with pending outbox: {key}")

    def fail(
        self,
        key: str,
        error: str,
        attempts: int,
        max_attempts: int = 5,
        failed_daily_requeue_seconds: int = FAILED_DAILY_REQUEUE_SECONDS,
    ) -> None:
        now = datetime.now(UTC)
        if attempts < max_attempts:
            status = "pending"
            delays = MESSAGE_RETRY_DELAYS_SECONDS if self.job_kind(key) == 'message' else RETRY_DELAYS_SECONDS
            delay_index = min(max(0, attempts - 1), len(delays) - 1)
            available_at = now + timedelta(seconds=delays[delay_index])
        else:
            status = "failed"
            available_at = now + timedelta(
                seconds=max(0, failed_daily_requeue_seconds)
                if self.job_kind(key) in {"daily", "document"}
                else 0
            )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, available_at = ?, updated_at = ?, last_error = ?
                WHERE job_key = ?
                """,
                (status, available_at.isoformat(), now.isoformat(), error[:2000], key),
            )

    def job_kind(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT kind FROM jobs WHERE job_key = ?", (key,)
            ).fetchone()
            return str(row["kind"]) if row else None

    def save_job_result(
        self, key: str, result_key: str, kind: str, payload: dict
    ) -> dict:
        """Persist the first result for a key and always return that canonical value."""

        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO job_results(
                    job_key, result_key, kind, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (key, result_key, kind, encoded, _now()),
            )
            row = connection.execute(
                """
                SELECT kind, payload_json FROM job_results
                WHERE job_key = ? AND result_key = ?
                """,
                (key, result_key),
            ).fetchone()
        if row is None or str(row["kind"]) != kind:
            raise RuntimeError(f"Conflicting immutable job result: {key}/{result_key}")
        return json.loads(str(row["payload_json"]))

    def get_job_result(self, key: str, result_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM job_results
                WHERE job_key = ? AND result_key = ?
                """,
                (key, result_key),
            ).fetchone()
        return json.loads(str(row["payload_json"])) if row else None

    def list_job_results(self, key: str, kind: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM job_results
                WHERE job_key = ? AND kind = ?
                ORDER BY created_at, result_key
                """,
                (key, kind),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def claim_daily_release_notes(self, key: str, day: str, notes: tuple) -> list[dict]:
        """Reserve announcements for one scheduled report, including its retries.

        The frozen outbox remains responsible for delivery/receipts. Reserving
        does not mark a message sent, and a failed report retains its notes.
        """
        if key != f"daily:{day}":
            return []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for note in notes:
                if note["date"] <= day:
                    connection.execute(
                        "INSERT OR IGNORE INTO daily_release_announcements "
                        "(release_id, job_key, note_json) VALUES (?, ?, ?)",
                        (note["id"], key, json.dumps(note, ensure_ascii=False)),
                    )
            rows = connection.execute(
                "SELECT note_json FROM daily_release_announcements "
                "WHERE job_key = ? ORDER BY release_id", (key,),
            ).fetchall()
        return [json.loads(row["note_json"]) for row in rows]

    def observe_discovery(self, episode: Episode, status: str, stars: int = 0) -> None:
        if not episode.id.startswith('podwise:'):
            return
        seq = episode.metadata.get('podwise_podcast_seq')
        seq = seq if type(seq) is int and seq > 0 else None
        with self._connect() as db:
            db.execute("INSERT INTO discovery_observations VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(episode_id) DO UPDATE SET podcast_seq=excluded.podcast_seq, "
                       "status=excluded.status,stars=excluded.stars,observed_at=excluded.observed_at",
                       (episode.id, seq, episode.show, status, stars, _now()))

    def discovery_watch_catalogs(self, now: datetime) -> list[int]:
        # Two distinct high-value episodes, not two retries of the same one.
        with self._connect() as db:
            rows = db.execute("SELECT podcast_seq FROM discovery_observations "
                "WHERE podcast_seq IS NOT NULL AND status='summarized' AND stars>=4 "
                "AND julianday(observed_at)>=julianday(?) GROUP BY podcast_seq HAVING count(*)>=2 "
                "ORDER BY max(observed_at) DESC LIMIT 20", ((now - timedelta(days=30)).isoformat(),)).fetchall()
        return [row[0] for row in rows]

    def prepared_digest(self, episode_id: str, cache_key: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT digest_sha256,cache_key FROM prepared_digest_keys WHERE episode_id=?",
                             (episode_id,)).fetchone()
        digest = self.get_transcript_digest(episode_id) if row else ""
        if digest and row['cache_key'] == cache_key and hashlib.sha256(digest.encode()).hexdigest() != row[0]:
            raise ValueError('Prepared digest integrity mismatch')
        return digest if digest and row['cache_key'] == cache_key else ""

    def mark_digest_prepared(self, episode_id: str, cache_key: str, digest: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO prepared_digest_keys VALUES (?,?,?) ON CONFLICT(episode_id) "
                       "DO UPDATE SET cache_key=excluded.cache_key,digest_sha256=excluded.digest_sha256",
                       (episode_id, cache_key, hashlib.sha256(digest.encode()).hexdigest()))

    def mark_analysis_complete(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET analysis_complete = 1, updated_at = ? WHERE job_key = ?",
                (_now(), key),
            )

    def analysis_complete(self, key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT analysis_complete FROM jobs WHERE job_key = ?", (key,)
            ).fetchone()
        return bool(row and row["analysis_complete"])

    def ensure_outbox(
        self,
        *,
        job_key: str,
        group_key: str,
        delivery_key: str,
        operation: str,
        target_id: str,
        target_type: str,
        reply_in_thread: bool,
        parts: list[tuple[str, str, str]],
    ) -> None:
        """Create immutable delivery parts, rejecting any key/content mismatch."""

        if operation not in {"reply", "send"}:
            raise ValueError(f"Unsupported outbox operation: {operation}")
        if not parts:
            raise ValueError("Outbox delivery must contain at least one part")
        now = _now()
        total_parts = len(parts)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for part, (msg_type, content, item_uuid) in enumerate(parts, start=1):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox(
                        job_key, group_key, delivery_key, operation,
                        target_id, target_type, reply_in_thread, part,
                        total_parts, msg_type, content, uuid, status, attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (
                        job_key,
                        group_key,
                        delivery_key,
                        operation,
                        target_id,
                        target_type,
                        int(reply_in_thread),
                        part,
                        total_parts,
                        msg_type,
                        content,
                        item_uuid,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT group_key, operation, target_id, target_type,
                           reply_in_thread, total_parts, msg_type, content, uuid
                    FROM outbox
                    WHERE job_key = ? AND delivery_key = ? AND part = ?
                    """,
                    (job_key, delivery_key, part),
                ).fetchone()
                expected = (
                    group_key,
                    operation,
                    target_id,
                    target_type,
                    int(reply_in_thread),
                    total_parts,
                    msg_type,
                    content,
                    item_uuid,
                )
                actual = tuple(row) if row else None
                if actual != expected:
                    raise RuntimeError(
                        f"Conflicting immutable outbox item: {job_key}/{delivery_key}/{part}"
                    )

    def pending_outbox(self, key: str) -> list[OutboxItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_key, group_key, delivery_key, operation,
                       target_id, target_type, reply_in_thread, part,
                       total_parts, msg_type, content, uuid, attempts
                FROM outbox
                WHERE job_key = ? AND status = 'pending'
                ORDER BY id
                """,
                (key,),
            ).fetchall()
        return [
            OutboxItem(
                id=int(row["id"]),
                job_key=str(row["job_key"]),
                group_key=str(row["group_key"]),
                delivery_key=str(row["delivery_key"]),
                operation=str(row["operation"]),
                target_id=str(row["target_id"]),
                target_type=str(row["target_type"]),
                reply_in_thread=bool(row["reply_in_thread"]),
                part=int(row["part"]),
                total_parts=int(row["total_parts"]),
                msg_type=str(row["msg_type"]),
                content=str(row["content"]),
                uuid=str(row["uuid"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def mark_outbox_attempt(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET attempts = attempts + 1, updated_at = ?, last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (_now(), item_id),
            )

    def mark_outbox_sent(self, item_id: int, remote_message_id: str = "") -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET status = 'sent', sent_at = ?, updated_at = ?,
                    remote_message_id = CASE
                        WHEN ? != '' THEN ? ELSE remote_message_id
                    END,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, now, remote_message_id, remote_message_id, item_id),
            )

    def mark_outbox_error(self, item_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outbox SET updated_at = ?, last_error = ?
                WHERE id = ? AND status = 'pending'
                """,
                (_now(), error[:2000], item_id),
            )

    def outbox_group_sent(self, key: str, group_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent
                FROM outbox WHERE job_key = ? AND group_key = ?
                """,
                (key, group_key),
            ).fetchone()
        return bool(row and int(row["total"]) > 0 and int(row["sent"] or 0) == int(row["total"]))

    def outbox_items(self, key: str) -> list[OutboxItem]:
        """Return all parts for diagnostics and restart-focused tests."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_key, group_key, delivery_key, operation,
                       target_id, target_type, reply_in_thread, part,
                       total_parts, msg_type, content, uuid, attempts
                FROM outbox WHERE job_key = ? ORDER BY id
                """,
                (key,),
            ).fetchall()
        return [
            OutboxItem(
                id=int(row["id"]),
                job_key=str(row["job_key"]),
                group_key=str(row["group_key"]),
                delivery_key=str(row["delivery_key"]),
                operation=str(row["operation"]),
                target_id=str(row["target_id"]),
                target_type=str(row["target_type"]),
                reply_in_thread=bool(row["reply_in_thread"]),
                part=int(row["part"]),
                total_parts=int(row["total_parts"]),
                msg_type=str(row["msg_type"]),
                content=str(row["content"]),
                uuid=str(row["uuid"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    @staticmethod
    def _stored_transcript(row: sqlite3.Row) -> StoredTranscript:
        published_at = str(row["published_at"] or "")
        return StoredTranscript(
            episode=Episode(
                id=str(row["episode_id"]),
                title=str(row["title"]),
                url=str(row["episode_url"]),
                show=str(row["show_name"]),
                metadata=json.loads(row['episode_metadata_json'] or '{}'),
                duration_seconds=(
                    float(row["duration_seconds"])
                    if row["duration_seconds"] is not None
                    else None
                ),
                duration_string=(
                    str(row["duration_string"])
                    if row["duration_string"] is not None
                    else None
                ),
                published_at=(
                    datetime.fromisoformat(published_at) if published_at else None
                ),
            ),
            transcript=Transcript(
                text=str(row["transcript_text"]),
                source=str(row["source"]),
                source_url=str(row["source_url"]),
                verified_complete=True,
                language=str(row["language"]),
            ),
            content_sha256=str(row["content_sha256"]),
            stored_at=datetime.fromisoformat(str(row["stored_at"])),
        )

    def save_verified_transcript(
        self, episode: Episode, transcript: Transcript
    ) -> StoredTranscript:
        """Archive one complete transcript before any model or delivery work."""

        if not transcript.verified_complete:
            raise ValueError("Only verified complete transcripts may be archived")
        if not transcript.text.strip():
            raise ValueError("A verified transcript cannot be empty")
        now = _now()
        content_sha256 = hashlib.sha256(
            transcript.text.encode("utf-8")
        ).hexdigest()
        reference = hashlib.sha256(episode.id.encode("utf-8")).hexdigest()[:8]
        published_at = (
            episode.published_at.isoformat() if episode.published_at else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO episode_transcripts(
                    episode_id, reference, title, episode_url, show_name,
                    duration_seconds, duration_string, published_at,
                    transcript_text, source, source_url, language,
                    content_sha256, stored_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    reference = excluded.reference,
                    title = excluded.title,
                    episode_url = excluded.episode_url,
                    show_name = excluded.show_name,
                    duration_seconds = excluded.duration_seconds,
                    duration_string = excluded.duration_string,
                    published_at = excluded.published_at,
                    transcript_text = excluded.transcript_text,
                    source = excluded.source,
                    source_url = excluded.source_url,
                    language = excluded.language,
                    content_sha256 = excluded.content_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    episode.id,
                    reference,
                    episode.title,
                    episode.url,
                    episode.show,
                    episode.duration_seconds,
                    episode.duration_string,
                    published_at,
                    transcript.text,
                    transcript.source,
                    transcript.source_url,
                    transcript.language,
                    content_sha256,
                    now,
                    now,
                ),
            )
            # Read back through the same write transaction. A second worker
            metadata = {k: episode.metadata[k] for k in ('description', 'chapters', 'rss_feed_url', 'audio_url', 'youtube_url')
                        if k in episode.metadata}
            if metadata:
                connection.execute('UPDATE episode_transcripts SET episode_metadata_json=? WHERE episode_id=?',
                                   (json.dumps(metadata, ensure_ascii=False), episode.id))
            # cannot replace this episode between the upsert and snapshot;
            # callers therefore summarize exactly the revision they saved.
            row = connection.execute(
                "SELECT * FROM episode_transcripts WHERE episode_id = ?",
                (episode.id,),
            ).fetchone()
            if row is None:  # pragma: no cover - the upsert above must create it
                raise RuntimeError(f"Transcript archive failed for {episode.id}")
            stored = self._stored_transcript(row)
        return stored

    def save_transcript_digest(
        self,
        episode_id: str,
        digest_markdown: str,
        expected_source_sha256: str,
        expected_record_revision_sha256: str,
    ) -> str:
        """Bind the first validated digest to exact text and metadata inputs."""

        digest = str(digest_markdown or "").strip()
        if not digest:
            raise ValueError("A transcript digest cannot be empty")
        expected_hash = str(expected_source_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("A transcript digest requires an expected source hash")
        expected_revision = str(
            expected_record_revision_sha256 or ""
        ).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise ValueError(
                "A transcript digest requires an expected record revision"
            )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM episode_transcripts WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if row is None:
                raise ValueError("A transcript digest requires an archived transcript")
            current_hash = str(row["content_sha256"])
            if current_hash != expected_hash:
                raise ValueError(
                    "The transcript changed before its digest could be stored"
                )
            current_revision = self._stored_transcript(
                row
            ).record_revision_sha256
            if current_revision != expected_revision:
                raise ValueError(
                    "The transcript metadata changed before its digest could be stored"
                )
            connection.execute(
                """
                INSERT INTO episode_digests(
                    episode_id, source_sha256, record_revision_sha256,
                    digest_markdown, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    source_sha256 = excluded.source_sha256,
                    record_revision_sha256 = excluded.record_revision_sha256,
                    digest_markdown = excluded.digest_markdown,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                WHERE episode_digests.source_sha256 != excluded.source_sha256
                   OR episode_digests.record_revision_sha256
                      != excluded.record_revision_sha256
                """,
                (
                    episode_id,
                    expected_hash,
                    expected_revision,
                    digest,
                    now,
                    now,
                ),
            )
            canonical = connection.execute(
                """
                SELECT source_sha256, record_revision_sha256, digest_markdown
                FROM episode_digests WHERE episode_id = ?
                """,
                (episode_id,),
            ).fetchone()
            if (
                canonical is None
                or str(canonical["source_sha256"]) != expected_hash
                or str(canonical["record_revision_sha256"])
                != expected_revision
            ):
                raise RuntimeError("Transcript digest revision was not stored")
            return str(canonical["digest_markdown"])

    def get_transcript_digest_revision(
        self, episode_id: str
    ) -> tuple[str, str, str] | None:
        """Return source hash, full record revision, and digest, even if stale."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_sha256, record_revision_sha256, digest_markdown
                FROM episode_digests WHERE episode_id = ?
                """,
                (episode_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            str(row["source_sha256"]),
            str(row["record_revision_sha256"]),
            str(row["digest_markdown"]),
        )

    def get_transcript_digest(self, episode_id: str) -> str:
        """Return a digest only when it still matches the current source text."""

        record = self.get_verified_transcript(episode_id)
        revision = self.get_transcript_digest_revision(episode_id)
        if record is None or revision is None:
            return ""
        source_sha256, record_revision_sha256, digest = revision
        if (
            source_sha256 != record.content_sha256
            or record_revision_sha256 != record.record_revision_sha256
        ):
            return ""
        return digest

    def get_verified_transcript(self, episode_id_or_reference: str) -> StoredTranscript | None:
        value = episode_id_or_reference.strip()
        if not value:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM episode_transcripts
                WHERE episode_id = ? OR reference = ?
                LIMIT 1
                """,
                (value, value.lower()),
            ).fetchone()
        return self._stored_transcript(row) if row else None

    def list_recent_transcripts(self, limit: int = 10) -> list[StoredTranscript]:
        limit = max(1, min(50, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM episode_transcripts
                ORDER BY
                    CASE
                        WHEN published_at IS NULL OR trim(published_at) = '' THEN 1
                        ELSE 0
                    END,
                    published_at DESC,
                    stored_at DESC,
                    episode_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._stored_transcript(row) for row in rows]

    def search_verified_transcripts(
        self, term: str, limit: int = 5
    ) -> list[StoredTranscript]:
        """Search episode metadata only; transcript text never selects an episode.

        Full transcript text is evidence after an episode has been selected. It
        is deliberately excluded here so a passing mention of a guest or show
        cannot silently bind a question to the wrong episode.
        """

        value = term.strip()
        if not value:
            return []
        limit = max(1, min(20, int(limit)))
        escaped = (
            value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM episode_transcripts
                WHERE title LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR show_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY
                    CASE
                        WHEN title = ? COLLATE NOCASE THEN 0
                        WHEN title LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1
                        WHEN show_name LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 2
                        ELSE 3
                    END,
                    CASE
                        WHEN published_at IS NULL OR trim(published_at) = '' THEN 1
                        ELSE 0
                    END,
                    published_at DESC,
                    stored_at DESC,
                    episode_id ASC
                LIMIT ?
                """,
                (pattern, pattern, value, pattern, pattern, limit),
            ).fetchall()
        return [self._stored_transcript(row) for row in rows]

    def save_conversation_context(
        self,
        context_key: str,
        *,
        episode_id: str = "",
        pending_episode_ids: tuple[str, ...] = (),
        pending_question: str = "",
        pending_action: str = "",
    ) -> None:
        if pending_action not in {"", "qa", "transcript"}:
            raise ValueError(f"Unsupported pending action: {pending_action}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_contexts(
                    context_key, episode_id, pending_episode_ids_json,
                    pending_question, pending_action, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(context_key) DO UPDATE SET
                    episode_id = excluded.episode_id,
                    pending_episode_ids_json = excluded.pending_episode_ids_json,
                    pending_question = excluded.pending_question,
                    pending_action = excluded.pending_action,
                    updated_at = excluded.updated_at
                """,
                (
                    context_key,
                    episode_id,
                    json.dumps(list(pending_episode_ids), ensure_ascii=False),
                    pending_question,
                    pending_action,
                    _now(),
                ),
            )

    def get_conversation_context(
        self, context_key: str, max_age_hours: int = 168
    ) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_contexts WHERE context_key = ?",
                (context_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
        except ValueError:
            return None
        if datetime.now(UTC) - updated_at > timedelta(hours=max(1, max_age_hours)):
            return None
        try:
            raw_history = json.loads(str(row["history_json"]))
        except (json.JSONDecodeError, TypeError):
            raw_history = []
        history = tuple(
            {
                "user": str(item.get("user") or "")[:MAX_HISTORY_USER_CHARS],
                "assistant": str(item.get("assistant") or "")[
                    :MAX_HISTORY_ASSISTANT_CHARS
                ],
                "episode_id": str(item.get("episode_id") or ""),
            }
            for item in raw_history[-MAX_CONVERSATION_TURNS:]
            if isinstance(item, dict)
            and str(item.get("user") or "").strip()
            and str(item.get("assistant") or "").strip()
        )
        return {
            "episode_id": str(row["episode_id"]),
            "pending_episode_ids": tuple(
                str(item)
                for item in json.loads(str(row["pending_episode_ids_json"]))
            ),
            "recent_episode_ids": tuple(
                str(item)
                for item in json.loads(str(row["recent_episode_ids_json"]))
            ),
            "history": history,
            "pending_question": str(row["pending_question"]),
            "pending_action": str(row["pending_action"]),
            "updated_at": updated_at.isoformat(),
        }

    def append_conversation_turn(
        self,
        context_key: str,
        *,
        user_text: str,
        assistant_text: str,
        episode_id: str = "",
    ) -> None:
        """Persist a small dialogue window for pronoun and topic follow-ups."""

        key = str(context_key or "").strip()
        user = re.sub(r"\s+", " ", str(user_text or "")).strip()[
            :MAX_HISTORY_USER_CHARS
        ]
        assistant = str(assistant_text or "").strip()[:MAX_HISTORY_ASSISTANT_CHARS]
        if not key or not user or not assistant:
            return
        entry = {
            "user": user,
            "assistant": assistant,
            "episode_id": str(episode_id or ""),
        }
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT history_json FROM conversation_contexts WHERE context_key = ?",
                (key,),
            ).fetchone()
            try:
                history = json.loads(str(row["history_json"])) if row else []
            except (json.JSONDecodeError, TypeError):
                history = []
            if not isinstance(history, list):
                history = []
            history = [item for item in history if isinstance(item, dict)]
            history = (history + [entry])[-MAX_CONVERSATION_TURNS:]
            encoded = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO conversation_contexts(
                    context_key, episode_id, history_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(context_key) DO UPDATE SET
                    history_json = excluded.history_json,
                    updated_at = excluded.updated_at
                """,
                (key, str(episode_id or ""), encoded, now),
            )

    def save_recent_transcript_snapshot(
        self, context_key: str, episode_ids: tuple[str, ...]
    ) -> None:
        """Freeze the numbering shown by ``最近播客`` for one conversation."""

        encoded = json.dumps(list(episode_ids), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_contexts(
                    context_key, recent_episode_ids_json,
                    pending_episode_ids_json, pending_question,
                    pending_action, updated_at
                ) VALUES (?, ?, '[]', '', '', ?)
                ON CONFLICT(context_key) DO UPDATE SET
                    recent_episode_ids_json = excluded.recent_episode_ids_json,
                    pending_episode_ids_json = '[]',
                    pending_question = '',
                    pending_action = '',
                    updated_at = excluded.updated_at
                """,
                (context_key, encoded, _now()),
            )

    def episode_for_remote_message(self, remote_message_id: str) -> str | None:
        value = remote_message_id.strip()
        if not value:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT group_key FROM outbox
                WHERE remote_message_id = ? AND group_key LIKE 'episode:%'
                ORDER BY id DESC LIMIT 1
                """,
                (value,),
            ).fetchone()
        if row is None:
            return None
        return str(row["group_key"])[len("episode:") :]

    def has_episode(self, episode_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM episodes WHERE episode_id = ?", (episode_id,)
                ).fetchone()
                is not None
            )

    def reserve_podwise_processing(self, seq: int) -> bool:
        """At most one billable POST per asset, even after a crash/timeout."""
        if type(seq) is not int or seq <= 0:
            raise ValueError('Invalid Podwise asset')
        with self._connect() as connection:
            return connection.execute(
                'INSERT OR IGNORE INTO podwise_process_requests VALUES (?, ?, ?, ?)',
                (seq, 'requesting', _now(), _now()),
            ).rowcount == 1

    def set_podwise_processing_state(self, seq: int, state: str) -> None:
        with self._connect() as connection:
            connection.execute('UPDATE podwise_process_requests SET state=?, updated_at=? WHERE seq=?',
                               (state, _now(), seq))

    def defer_daily_transcript(self, episode: Episode, state: str) -> None:
        """Keep original publisher identity after the daily 24h window closes."""
        value = episode.to_persisted_dict()
        value['metadata'] = episode.metadata
        now = datetime.now(UTC)
        with self._connect() as connection:
            prior = connection.execute('SELECT attempts FROM daily_transcript_backlog WHERE episode_id=?',
                                       (episode.id,)).fetchone()
            # Pending transcription is checked promptly, persistently unavailable
            # material backs off after a day instead of hammering paid APIs forever.
            delay = timedelta(hours=6) if prior and prior['attempts'] >= 48 else timedelta(minutes=30)
            connection.execute(
                'INSERT INTO daily_transcript_backlog VALUES (?, ?, ?, ?, ?, 1) '
                'ON CONFLICT(episode_id) DO UPDATE SET state=excluded.state, '
                'next_check_at=excluded.next_check_at, attempts=attempts+1',
                (episode.id, json.dumps(value, ensure_ascii=False), state, now.isoformat(),
                 (now + delay).isoformat()),
            )

    def due_daily_transcripts(self, limit: int = 50) -> list[Episode]:
        from dataclasses import replace

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT episode_json FROM daily_transcript_backlog "
                "WHERE state != 'delivered' AND next_check_at<=? "
                "ORDER BY CASE WHEN episode_id LIKE 'podwise:%' THEN 1 ELSE 0 END, next_check_at, created_at LIMIT ?",
                (_now(), limit),
            ).fetchall()
        result = []
        for row in rows:
            value = json.loads(row['episode_json'])
            result.append(replace(Episode.from_persisted_dict(value), metadata=value.get('metadata', {})))
        return result

    def complete_daily_transcript(self, episode_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE daily_transcript_backlog SET state='delivered' WHERE episode_id=?",
                               (episode_id,))

    def publisher_episode_aliases(self, episode: Episode) -> set[str]:
        """Find historical merged IDs by exact publisher metadata, not fuzzy titles."""
        if not episode.published_at or not (episode.metadata.get('rss_feed_url') or episode.id.startswith('podwise:')):
            return set()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT episode_id FROM episodes WHERE title=? AND show_name=? AND julianday(published_at)=julianday(?) "
                "AND result!='discovery_filtered'",
                (episode.title, episode.show, episode.published_at.isoformat()),
            ).fetchall()
        return {str(row['episode_id']) for row in rows}

    def episode_is_delivered(self, episode: Episode) -> bool:
        aliases = self.publisher_episode_aliases(episode) | {episode.id}
        with self._connect() as connection:
            return any(connection.execute(
                "SELECT 1 FROM episodes WHERE episode_id=? AND result IN ('sent', 'summarized')", (alias,)
            ).fetchone() is not None for alias in aliases)

    def should_review_episode(
        self, episode_id: str, no_transcript_retry_hours: int = 6
    ) -> bool:
        """Return whether an unseen or transiently incomplete episode is due."""
        return (
            self.episode_review_state(
                episode_id,
                no_transcript_retry_hours=no_transcript_retry_hours,
            )
            is not None
        )

    def episode_review_state(
        self, episode_id: str, no_transcript_retry_hours: int = 6
    ) -> str | None:
        """Classify an episode as new, retryable now, or not currently due.

        ``unverified_date`` is a transient metadata failure just like a missing
        transcript. Treating it as final permanently hid episodes when YouTube
        returned only partial metadata during one scan.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result, checked_at FROM episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
        if row is None:
            return "new"
        if row['result'] == 'not_recommended':
            # Policy v2 ranks low-star episodes instead of permanently excluding
            # them. Discovery still enforces the publisher's daily date window.
            return 'retry'
        if row["result"] not in {
            "no_transcript",
            "summary_format_error",
            "unverified_date",
        }:
            return None
        try:
            checked_at = datetime.fromisoformat(str(row["checked_at"]))
        except ValueError:
            return "retry"
        if datetime.now(UTC) - checked_at >= timedelta(
            hours=no_transcript_retry_hours
        ):
            return "retry"
        return None

    def record_episode(self, episode: Episode, result: str) -> None:
        published_at = (
            episode.published_at.isoformat() if episode.published_at else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO episodes(
                    episode_id, title, url, show_name, published_at, result, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    show_name = excluded.show_name,
                    published_at = excluded.published_at,
                    result = excluded.result,
                    checked_at = excluded.checked_at
                """,
                (
                    episode.id,
                    episode.title,
                    episode.url,
                    episode.show,
                    published_at,
                    result,
                    _now(),
                ),
            )

    def job_status(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_key = ?", (key,)
            ).fetchone()
            return str(row["status"]) if row else None

    def get_editorial_review(self, episode_id: str, cache_key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute('SELECT decision_json FROM editorial_reviews WHERE episode_id=? AND cache_key=?',
                             (episode_id, cache_key)).fetchone()
            return json.loads(row[0]) if row else None

    def save_editorial_review(self, episode_id: str, cache_key: str, decision: dict) -> None:
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO editorial_reviews VALUES (?,?,?,?)',
                       (episode_id, cache_key, json.dumps(decision, ensure_ascii=False), _now()))
