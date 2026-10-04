"""Read the same persisted daily results used by scheduled delivery."""

import hashlib
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .daily_report import coverage_report, ranked_daily_items, render_daily_summary
from .models import DailyItem


def read_daily_digest(store, day: str) -> dict:
    requested = date.fromisoformat(day)
    if requested.isoformat() != day:
        raise ValueError("Expected YYYY-MM-DD")
    with store._connect() as db:
        jobs = db.execute(
            "SELECT job_key,payload_json,created_at,analysis_complete FROM jobs "
            "WHERE kind='daily' ORDER BY created_at,job_key"
        ).fetchall()
    matching = []
    feature_updates = []
    items = {}
    for job in jobs:
        payload = json.loads(job["payload_json"])
        if payload.get('prepare_only'):
            continue
        scheduled = datetime.fromisoformat(payload.get("scheduled_for") or job["created_at"])
        if scheduled.tzinfo is None:
            raise ValueError("Daily job timestamp has no timezone")
        if scheduled.astimezone(ZoneInfo("Asia/Shanghai")).date() != requested:
            continue
        matching.append(job)
        for bundle in store.list_job_results(job["job_key"], "daily_bundle"):
            update = bundle.get("feature_updates", "")
            if update and update not in feature_updates:
                feature_updates.append(update)
        for value in store.list_job_results(job["job_key"], "daily_item"):
            item = DailyItem.from_persisted_dict(value)
            previous = items.get(item.episode.id)
            if previous is None or item.status == "summarized" or previous.status != "summarized":
                items[item.episode.id] = item
    if not matching:
        return {"status": "not_generated", "date": day, "count": 0,
                "message": f"{day} 的日报尚未生成或归档；这不代表没有播客更新。"}
    summaries, valid, stale = [], [], 0
    for item in ranked_daily_items(list(items.values())):
        if item.status != "summarized":
            valid.append(item)
            continue
        record = store.get_verified_transcript(item.episode.id)
        descriptor = item.attachment
        if (not record or not descriptor
                or descriptor.episode_id != item.episode.id
                or descriptor.digest_sha256 != hashlib.sha256(item.message.strip().encode()).hexdigest()
                or descriptor.source_sha256 != record.content_sha256
                or descriptor.record_revision_sha256 != record.record_revision_sha256
                or store.get_transcript_digest(item.episode.id) != item.message.strip()):
            stale += 1
            continue
        valid.append(item)
        # The raw source stays in the archive, not as an inaccessible API link.
        summaries.append(render_daily_summary(
            re.sub(r"^\*{0,2}文字稿来源：.*\n?", "", item.message, flags=re.MULTILINE),
            discovered=item.episode.id.startswith('podwise:')))
    blocks = [f"# 情报官日报｜{day}",
              *feature_updates,
              f"已归档 {len(summaries)} 期摘要。以下与每日推送共用正式资料库；不附全文。",
              *summaries]
    report = coverage_report(valid)
    if report:
        blocks.append(report)
    if stale:
        blocks.append(f"另有 {stale} 期摘要与当前文字稿版本不一致，本次未展示，需重新生成。")
    if not all(job["analysis_complete"] for job in matching):
        blocks.append("当日仍有扫描或处理任务未完成，以上是已归档结果，不代表完整更新清单。")
    return {"status": "ready", "date": day, "count": len(summaries),
            "markdown": "\n\n---\n\n".join(blocks)}
