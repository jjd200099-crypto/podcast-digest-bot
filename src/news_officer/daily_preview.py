"""Render archived daily items with a SQLite read-only connection and no runtime."""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .daily_report import render_reader_daily
from .models import DailyItem


def render_preview(db_path, day, *, compact=False, layout="compact"):
    requested = date.fromisoformat(day)
    items, documents = {}, {}
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute('PRAGMA query_only = ON')
        rows = db.execute(
            "SELECT j.payload_json,j.created_at,r.kind,r.payload_json "
            "FROM jobs j JOIN job_results r ON j.job_key=r.job_key "
            "WHERE j.kind='daily' ORDER BY j.created_at,r.created_at"
        )
        for payload_json, created_at, kind, result_json in rows:
            payload = json.loads(payload_json)
            if payload.get('prepare_only'):
                continue
            scheduled = datetime.fromisoformat(payload.get('scheduled_for') or created_at)
            if scheduled.tzinfo is None:
                raise ValueError('Daily timestamp has no timezone')
            if scheduled.astimezone(ZoneInfo('Asia/Shanghai')).date() != requested:
                continue
            result = json.loads(result_json)
            if kind == 'daily_item':
                item = DailyItem.from_persisted_dict(result)
                previous = items.get(item.episode.id)
                if previous is None or item.status == 'summarized':
                    items[item.episode.id] = item
            for document in result.get('documents', []):
                documents[document.get('episode_id') or document['title']] = document
    return render_reader_daily(list(items.values()), list(documents.values()), compact=compact, layout=layout) or f'{day} 暂无已归档摘要。'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', required=True)
    parser.add_argument('--date', default=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
    parser.add_argument('--db', default=os.environ.get('NEWS_OFFICER_DB_PATH', '/data/news-officer.sqlite3'))
    args = parser.parse_args(argv)
    compact = os.environ.get('NEWS_OFFICER_DAILY_COMPACT_PARAGRAPHS', '').lower() in {'1', 'true', 'yes', 'on'}
    print(render_preview(args.db, args.date, compact=compact,
                         layout=os.environ.get("NEWS_OFFICER_DAILY_LAYOUT", "v4")))


if __name__ == '__main__':
    main()
