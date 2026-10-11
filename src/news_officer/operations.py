"""Read-only business checks. No message bodies, identities or credentials."""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import _clock, _csv
from .daily_targets import allowlist_warnings


def record_warning(store, job_key, code, day, *, target=None, used=None, limit=None):
    """Persist safe fields without source text or recipient IDs."""
    try:
        day = datetime.strptime(str(day), '%Y-%m-%d').date().isoformat()
    except ValueError:
        day = 'unknown'
    value = {'code': code, 'day': day}
    if target is not None:
        value['target'] = hashlib.sha256(repr(tuple(target)).encode()).hexdigest()[:12]
    if used is not None:
        value.update(used=int(used), limit=int(limit))
    identity = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    store.save_job_result(job_key, 'warning:' + identity, 'operations_warning', value)


def business_snapshot(path: Path, *, now=None, daily_time=None, timezone="Asia/Shanghai") -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(timezone))
    clock = _clock(daily_time if daily_time is not None else os.environ.get("NEWS_OFFICER_DAILY_TIME", "08:30"))
    hour, minute = clock.hour, clock.minute
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    key = f"daily:{local.date().isoformat()}"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        subscribed = bool(db.execute("SELECT 1 FROM subscriptions WHERE active=1 LIMIT 1").fetchone())
        subscribed = subscribed or bool(_csv(os.environ.get("NEWS_OFFICER_DAILY_CHAT_IDS", "")))
        job = db.execute("SELECT status,analysis_complete FROM jobs WHERE job_key=?", (key,)).fetchone()
        sends = db.execute(
            "SELECT status,sent_at FROM outbox WHERE job_key=? AND group_key LIKE 'daily:bundle:%'", (key,)
        ).fetchall()
        delivered = bool(sends) and all(row["status"] == "sent" for row in sends)
        complete = delivered and bool(job and job["analysis_complete"])
        last = db.execute(
            "SELECT max(sent_at) FROM outbox WHERE status='sent' "
            "AND job_key LIKE 'daily:____-__-__' AND group_key LIKE 'daily:bundle:%'"
        ).fetchone()[0]
        pending = db.execute("SELECT count(*) FROM daily_transcript_backlog WHERE state!='delivered'").fetchone()[0]
        old = db.execute(
            "SELECT count(*) FROM daily_transcript_backlog WHERE state!='delivered' AND julianday(created_at)<julianday(?)",
            ((now - timedelta(hours=72)).isoformat(),),
        ).fetchone()[0]
        failed = db.execute("SELECT count(*) FROM jobs WHERE status='failed'").fetchone()[0]
        document_failures = db.execute(
            "SELECT count(*) FROM episode_document_attempts WHERE status='failed'").fetchone()[0]
        missing_documents = db.execute(
            "SELECT episode_id,day FROM episode_document_attempts WHERE status='failed' "
            "UNION SELECT json_extract(payload_json,'$.episode_id'),json_extract(payload_json,'$.day') "
            "FROM jobs WHERE kind='document' AND status='failed' "
            "AND json_extract(payload_json,'$.mode')='daily_episode'").fetchall()
        operational = db.execute(
            "SELECT payload_json FROM job_results WHERE kind='operations_warning'").fetchall()
        rows = db.execute(
            "SELECT payload_json FROM job_results WHERE job_key=? AND kind='daily_item'", (key,)
        ).fetchall()
    counts = {}
    for row in rows:
        value = json.loads(row["payload_json"])
        status = value.get("status", "unknown")
        # Whitelisted counters only; never let source-controlled text into logs.
        if status not in {"summarized", "no_transcript", "editorial_filtered", "discovery_filtered",
                          "summary_format_error", "discovery_status", "unverified_date", "failed", "outside_window"}:
            status = "other"
        counts[status] = counts.get(status, 0) + 1
    warnings = allowlist_warnings(
        _csv(os.environ.get("NEWS_OFFICER_DAILY_CHAT_IDS", "")),
        os.environ.get("NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS", "false").strip().lower() == "true",
        os.environ.get("NEWS_OFFICER_WATCH_CHAT_ID", "").strip())
    for row in operational:
        value = json.loads(row[0])
        warnings.append(':'.join([value['code'], 'day=' + value['day']]
            + (['target=' + value['target']] if 'target' in value else [])
            + ([f"used={value['used']}/{value['limit']}"] if 'used' in value else [])))
    if document_failures:
        warnings.append(f'episode_document_retry_stopped:{document_failures}')
    for episode, day in missing_documents:
        # Episode fingerprint identifies the source without exposing URLs or titles.
        fingerprint = hashlib.sha256(str(episode).encode()).hexdigest()[:12]
        try:
            safe_day = datetime.strptime(str(day), '%Y-%m-%d').date().isoformat()
        except ValueError:
            safe_day = 'unknown'
        warnings.append(f'document link missing:episode={fingerprint}:day={safe_day}')
    overdue = subscribed and not complete and local >= scheduled + timedelta(minutes=45)
    state = ("not_subscribed" if not subscribed else "sent" if complete else "overdue" if overdue
             else "partial" if delivered else "processing" if job else "not_due" if local < scheduled else "pending")
    return {"date": str(local.date()), "daily_state": state, "daily_overdue": overdue,
            "last_daily_send_at": last, "daily_counts": counts, "transcripts_pending": pending,
            "transcripts_pending_over_72h": old, "failed_jobs": failed,
            "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--daily-time")
    args = parser.parse_args()
    print(json.dumps(business_snapshot(args.db, daily_time=args.daily_time), ensure_ascii=False))


if __name__ == "__main__":
    main()
