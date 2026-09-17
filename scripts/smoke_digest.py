"""Read-only cloud digest test: live model, archived full text, no Feishu send."""

import json

from news_officer.config import Settings
from news_officer.podcast import PodcastService
from news_officer.podwise import PodwiseTranscriptProvider
from news_officer.store import Store
from news_officer.summarizer import (
    NUMBERED_TAKEAWAY_RE,
    TranscriptSummarizer,
    _valid_editorial_summary,
)


def main():
    settings = Settings.from_env()
    store = Store(settings.db_path)
    records = store.list_recent_transcripts(1)
    assert records, "Need an already-verified archived transcript"
    record = records[0]
    summarizer = TranscriptSummarizer(settings.openai_api_key, settings.openai_model)
    summary = summarizer.summarize(record.episode, record.transcript)
    assert _valid_editorial_summary(summary)
    print(
        json.dumps(
            {
                "test": "live editorial summary",
                "episode": record.episode.title,
                "takeaways": len(NUMBERED_TAKEAWAY_RE.findall(summary)),
                "rating": summary.strip().splitlines()[-1],
                "production_writes": False,
                "feishu_sends": False,
                "podwise_configured": bool(settings.podwise_api_token),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    podcast = PodcastService(
        store,
        settings.feeds_path,
        summarizer,
        podwise_api_token=settings.podwise_api_token,
    )
    providers = podcast.transcript_resolver.providers
    assert any(
        isinstance(provider, PodwiseTranscriptProvider) for provider in providers
    ) == bool(settings.podwise_api_token)
    print("PASS: full-text model summary and provider wiring", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - credential-safe diagnostic
        print("FAIL TYPE:", type(error).__name__, flush=True)
        raise SystemExit(1) from None
