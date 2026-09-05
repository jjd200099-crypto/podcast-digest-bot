import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.models import Episode
from news_officer.podcast import (
    FeedDiscoveryError,
    PodcastService,
    TranscriptLookupError,
    TranscriptResolver,
)
from news_officer.store import Store
from news_officer.youtube import is_youtube_url


class FakeSummarizer:
    def summarize(self, episode, transcript):
        return "summary"


class BrokenProvider:
    name = "broken official source"

    def fetch(self, episode):
        raise TimeoutError("temporary outage")


class MissingProvider:
    name = "healthy source without transcript"

    def fetch(self, episode):
        return None


class SourceFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "state.sqlite3")
        self.store.initialize()
        self.feeds = Path(self.temp_dir.name) / "feeds.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_provider_outage_is_not_reported_as_missing_transcript(self):
        resolver = TranscriptResolver([MissingProvider(), BrokenProvider()])
        with self.assertRaises(TranscriptLookupError):
            resolver.fetch(Episode("x", "Title", "https://youtu.be/x", "Show"))

    def test_all_feed_outage_is_not_reported_as_an_empty_scan(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one"]}'
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())
        with (
            patch("news_officer.podcast.latest_videos", side_effect=TimeoutError),
            self.assertRaises(FeedDiscoveryError),
        ):
                service.discover_daily_candidates()

    def test_majority_feed_outage_is_not_treated_as_healthy(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one", '
            '"https://www.youtube.com/@two", "https://www.youtube.com/@three"]}'
        )
        candidate = Episode(
            "x", "Title", "https://www.youtube.com/watch?v=x", "Show"
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())

        def discover(channel):
            if channel.endswith("@one"):
                return [candidate]
            raise TimeoutError("temporary outage")

        with (
            patch("news_officer.podcast.latest_videos", side_effect=discover),
            self.assertRaises(FeedDiscoveryError),
        ):
            service.discover_daily_candidates()

    def test_unknown_publication_date_is_never_treated_as_recent(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one"]}'
        )
        episode = Episode(
            "x",
            "Title",
            "https://www.youtube.com/watch?v=x",
            "Show",
            duration_seconds=600,
            published_at=None,
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())
        with (
            patch.object(service, "discover_daily_candidates", return_value=[episode]),
            patch("news_officer.podcast.video_metadata", return_value=episode),
        ):
            items = service.build_daily(datetime(2026, 9, 5, tzinfo=UTC))
        self.assertEqual(items[0].status, "unverified_date")

    def test_youtube_source_requires_https(self):
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=x"))
        self.assertFalse(is_youtube_url("ftp://www.youtube.com/watch?v=x"))
        self.assertFalse(is_youtube_url("https://youtube.com.evil.test/watch?v=x"))


if __name__ == "__main__":
    unittest.main()
