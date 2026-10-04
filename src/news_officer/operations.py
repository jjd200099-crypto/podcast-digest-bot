"""Read-only business checks. No message bodies, identities or credentials."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def business_snapshot(path: Path, *, now=None, daily_time="08:30", timezone="Asia/Shanghai") -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(timezone))
    hour, minute = map(int, daily_time.split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    key = f"daily:{local.date().isoformat()}"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        subscribed = bool(db.execute("SELECT 1 FROM subscriptions WHERE active=1 LIMIT 1").fetchone())
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
    overdue = subscribed and not complete and local >= scheduled + timedelta(minutes=45)
    state = ("not_subscribed" if not subscribed else "sent" if complete else "overdue" if overdue
             else "partial" if delivered else "processing" if job else "not_due" if local < scheduled else "pending")
    return {"date": str(local.date()), "daily_state": state, "daily_overdue": overdue,
            "last_daily_send_at": last, "daily_counts": counts, "transcripts_pending": pending,
            "transcripts_pending_over_72h": old, "failed_jobs": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--daily-time", default="08:30")
    args = parser.parse_args()
    print(json.dumps(business_snapshot(args.db, daily_time=args.daily_time), ensure_ascii=False))


if __name__ == "__main__":
    main()
