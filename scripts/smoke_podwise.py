"""Opt-in live provider test; temporary archive, no Feishu or production writes."""

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_officer.config import Settings
from news_officer.podcast import _readable_attachment, load_youtube_sources
from news_officer.podwise import PodwiseTranscriptProvider
from news_officer.qa import render_transcript_attachment
from news_officer.rss import latest_rss_episodes
from news_officer.store import Store
from news_officer.summarizer import (
    NUMBERED_TAKEAWAY_RE,
    TranscriptSummarizer,
    _valid_editorial_summary,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    assert settings.podwise_api_token, "Podwise must be configured"
    provider = PodwiseTranscriptProvider(settings.podwise_api_token)
    sources = {s.name: s for s in load_youtube_sources(settings.feeds_path)}
    cutoff = datetime.now(UTC) - timedelta(hours=72)
    records = []
    with tempfile.TemporaryDirectory(prefix="podwise-smoke-") as directory:
        store = Store(Path(directory) / "archive.sqlite3")
        store.initialize()
        for name in (
            "Invest Like the Best",
            "All-In Podcast",
            "SemiAnalysis Weekly",
            "a16z Show",
        ):
            source = sources[name]
            episodes = [
                e
                for e in latest_rss_episodes(source.name, source.rss_url, limit=5)
                if e.published_at and cutoff <= e.published_at <= datetime.now(UTC)
            ]
            if not episodes:
                continue
            episode = episodes[0]
            transcript = provider.fetch(episode)
            print(
                json.dumps(
                    {
                        "show": name,
                        "title": episode.title,
                        "verified_complete": bool(transcript),
                        "characters": len(transcript.text) if transcript else 0,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if transcript:
                records.append(store.save_verified_transcript(episode, transcript))
        assert records, "No complete transcripts verified"
        if args.summarize:
            # Use the shortest of the verified full texts to bound test cost.
            record = min(records, key=lambda r: len(r.transcript.text))
            summarizer = TranscriptSummarizer(
                settings.openai_api_key, settings.openai_model
            )
            summary = summarizer.summarize(record.episode, record.transcript)
            assert _valid_editorial_summary(summary)
            saved = store.save_transcript_digest(
                record.episode.id,
                summary,
                record.content_sha256,
                record.record_revision_sha256,
            )
            attachment = _readable_attachment(record, saved)
            filename, content = render_transcript_attachment(
                record, digest_markdown=saved
            )
            assert content and filename == attachment.filename
            assert hashlib.sha256(content).hexdigest() == attachment.rendered_sha256
            print(
                json.dumps(
                    {
                        "summary_title": record.episode.title,
                        "takeaways": len(NUMBERED_TAKEAWAY_RE.findall(summary)),
                        "rating": summary.strip().splitlines()[-1],
                        "attachment_ready": True,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(
        "PASS: real Podwise + temporary archive; no production writes or Feishu sends",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact SDK credentials and HTTP bodies
        print("FAIL TYPE:", type(error).__name__, flush=True)
        raise SystemExit(1) from None
