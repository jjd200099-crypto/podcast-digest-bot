from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.models import Episode
from news_officer.rss import (
    RSSDeclaredTranscriptProvider,
    SubstackApprovedTranscriptProvider,
    _response,
    attach_youtube_fallbacks,
    latest_rss_episodes,
)


class FakeResponse:
    def __init__(self, body: str | bytes, url: str, *, json_value=None):
        self.content = body.encode() if isinstance(body, str) else body
        self.text = self.content.decode(errors="replace")
        self.url = url
        self.status_code = 200
        self._json_value = json_value

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def json(self):
        return self._json_value if self._json_value is not None else json.loads(self.text)


def complete_plain_transcript(*, include_closing: bool = True) -> str:
    lines = []
    for turn in range(120):
        speaker = "Ben" if turn % 2 == 0 else "David"
        words = " ".join(f"word{turn}_{index}" for index in range(60))
        if turn == 0:
            words = "Welcome to the show. " + words
        if turn == 119 and include_closing:
            words += " Thanks for listening and see you next time."
        lines.append(f"{speaker}: {words}")
    return "\n\n".join(lines)


class RSSTests(unittest.TestCase):
    @patch("news_officer.rss._response")
    def test_zero_scan_limit_reads_all_feed_entries(self, response):
        url = "https://feeds.example.com/show"
        items = "".join(f"<item><guid>e-{i}</guid><title>Episode {i}</title>"
                        "<pubDate>Wed, 16 Sep 2026 07:00:00 +0000</pubDate></item>"
                        for i in range(40))
        response.return_value = FakeResponse(f"<rss><channel>{items}</channel></rss>", url)
        self.assertEqual(len(latest_rss_episodes("Show", url, limit=0)), 40)
        self.assertEqual(len(latest_rss_episodes("Show", url, limit=4)), 4)

    @patch("news_officer.rss._response")
    def test_rss_discovery_preserves_date_duration_and_transcript(self, response):
        feed_url = "https://feeds.example.com/show"
        transcript_url = "https://publisher.example.com/transcript.txt"
        response.return_value = FakeResponse(
            f"""
            <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
                 xmlns:podcast="https://podcastindex.org/namespace/1.0">
              <channel><item>
                <guid>episode-42</guid><title>Building Durable AI Companies</title>
                <link>https://publisher.example.com/episode-42</link>
                <pubDate>Mon, 14 Sep 2026 07:07:00 +0000</pubDate>
                <itunes:duration>1:02:03</itunes:duration>
                <podcast:transcript url="{transcript_url}" type="text/plain"
                                    language="en" />
              </item></channel>
            </rss>
            """,
            feed_url,
        )

        episodes = latest_rss_episodes("Example", feed_url)

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].published_at, datetime(2026, 9, 14, 7, 7, tzinfo=UTC))
        self.assertEqual(episodes[0].duration_seconds, 3723)
        self.assertEqual(episodes[0].metadata["rss_transcripts"][0]["url"], transcript_url)

    def test_rss_episode_keeps_verified_date_when_youtube_is_attached(self):
        rss_episode = Episode(
            "rss:one",
            "How We Built an AI Coding Agent",
            "https://publisher.example.com/episode",
            "Example",
            duration_seconds=3600,
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
            metadata={"rss_feed_url": "https://feeds.example.com/show"},
        )
        youtube_episode = Episode(
            "youtube-one",
            "How We Built an AI Coding Agent | Full Interview",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "Example",
            duration_seconds=3595,
        )

        merged = attach_youtube_fallbacks([rss_episode], [youtube_episode])[0]

        self.assertEqual(merged.id, "youtube-one")
        self.assertEqual(merged.published_at, rss_episode.published_at)
        self.assertEqual(merged.metadata["youtube_url"], youtube_episode.url)

    def test_ambiguous_youtube_titles_are_not_attached(self):
        rss_episode = Episode(
            "rss:one",
            "Building Durable AI Companies",
            "https://publisher.example.com/episode",
            "Example",
            duration_seconds=3600,
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        youtube_episodes = [
            Episode(
                f"youtube-{index}",
                "Building Durable AI Companies",
                f"https://www.youtube.com/watch?v=video{index}",
                "Example",
                duration_seconds=3600,
                published_at=datetime(2026, 9, 14, tzinfo=UTC),
            )
            for index in range(2)
        ]

        merged = attach_youtube_fallbacks([rss_episode], youtube_episodes)

        self.assertNotIn("youtube_url", merged[0].metadata)
        self.assertEqual([episode.id for episode in merged], ["rss:one", "youtube-0", "youtube-1"])

    def test_same_title_on_distant_dates_is_not_attached(self):
        rss_episode = Episode(
            "rss:one",
            "Building Durable AI Companies",
            "https://publisher.example.com/episode",
            "Example",
            duration_seconds=3600,
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        youtube_episode = Episode(
            "youtube-old",
            "Building Durable AI Companies",
            "https://www.youtube.com/watch?v=old",
            "Example",
            duration_seconds=3600,
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
        )

        merged = attach_youtube_fallbacks([rss_episode], [youtube_episode])

        self.assertNotIn("youtube_url", merged[0].metadata)
        self.assertEqual(len(merged), 2)

    def test_same_title_without_date_or_duration_is_not_attached(self):
        rss_episode = Episode(
            "rss:one",
            "Emergency Pod",
            "https://publisher.example.com/episode",
            "Example",
        )
        youtube_episode = Episode(
            "youtube-other",
            "Emergency Pod",
            "https://www.youtube.com/watch?v=other",
            "Example",
        )

        merged = attach_youtube_fallbacks([rss_episode], [youtube_episode])

        self.assertNotIn("youtube_url", merged[0].metadata)
        self.assertEqual(len(merged), 2)

    def test_unique_same_day_title_match_can_supply_youtube_without_duration(self):
        rss_episode = Episode(
            "rss:a16z",
            "Greg Brockman on Why OpenAI Says We're Entering the AGI Era",
            "https://publisher.example.com/greg-brockman",
            "Example",
            duration_seconds=3062,
            published_at=datetime(2026, 9, 14, 10, tzinfo=UTC),
        )
        youtube_episode = Episode(
            "youtube-a16z",
            "Greg Brockman Says AGI Has Arrived",
            "https://www.youtube.com/watch?v=example",
            "Example",
            published_at=datetime(2026, 9, 14, 14, 30, tzinfo=UTC),
        )

        merged = attach_youtube_fallbacks([rss_episode], [youtube_episode])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].id, "youtube-a16z")
        self.assertEqual(merged[0].metadata["youtube_url"], youtube_episode.url)

    @patch("news_officer.rss._response")
    def test_publisher_declared_plain_transcript_is_verified(self, response):
        transcript_url = "https://publisher.example.com/transcript.txt"
        text = complete_plain_transcript()
        response.return_value = FakeResponse(text, transcript_url)
        episode = Episode(
            "rss:one",
            "Long interview",
            "https://publisher.example.com/episode",
            "Example",
            duration_seconds=3600,
            metadata={
                "rss_transcripts": [
                    {"url": transcript_url, "type": "text/plain", "language": "en"}
                ]
            },
        )

        transcript = RSSDeclaredTranscriptProvider(
            allowed_hosts={"publisher.example.com"}
        ).fetch(episode)

        self.assertIsNotNone(transcript)
        self.assertTrue(transcript.verified_complete)
        self.assertEqual(transcript.source_url, transcript_url)

    @patch("news_officer.rss._response")
    def test_publisher_plain_transcript_requires_duration_and_closing(self, response):
        transcript_url = "https://publisher.example.com/transcript.txt"
        response.return_value = FakeResponse(
            complete_plain_transcript(include_closing=False),
            transcript_url,
        )
        without_duration = Episode(
            "rss:no-duration",
            "Interview",
            "https://publisher.example.com/episode",
            "Example",
            metadata={
                "rss_transcripts": [{"url": transcript_url, "type": "text/plain"}]
            },
        )
        truncated = Episode(
            "rss:truncated",
            "Interview",
            "https://publisher.example.com/episode",
            "Example",
            duration_seconds=3600,
            metadata=without_duration.metadata,
        )

        provider = RSSDeclaredTranscriptProvider(
            allowed_hosts={"publisher.example.com"}
        )
        self.assertIsNone(provider.fetch(without_duration))
        self.assertIsNone(provider.fetch(truncated))

    @patch("news_officer.rss._response")
    @patch.object(SubstackApprovedTranscriptProvider, "_page_data")
    def test_substack_requires_approved_transcript_with_full_timeline(
        self, page_data, response
    ):
        transcript_url = "https://substackcdn.com/transcription.json?signature=test"
        segments = [
            {
                "start": index * 10,
                "end": (index + 1) * 10,
                "speaker": "SPEAKER_0",
                "text": " ".join(f"word{index}_{word}" for word in range(18)),
            }
            for index in range(60)
        ]
        page_data.return_value = (
            {
                "post": {
                    "podcastUpload": {
                        "duration": 600,
                        "transcription": {
                            "status": "transcribed",
                            "approved_at": "2026-09-14T00:00:00Z",
                            "cdn_url": transcript_url,
                            "speaker_map": {"SPEAKER_0": "Guest"},
                        },
                    }
                }
            },
            "https://www.lennysnewsletter.com/p/example",
        )
        response.return_value = FakeResponse(
            json.dumps(segments), transcript_url, json_value=segments
        )
        episode = Episode(
            "rss:substack",
            "Example",
            "https://www.lennysnewsletter.com/p/example",
            "Lenny's Podcast",
            duration_seconds=600,
        )

        transcript = SubstackApprovedTranscriptProvider().fetch(episode)

        self.assertIsNotNone(transcript)
        self.assertTrue(transcript.verified_complete)
        self.assertIn("Guest:", transcript.text)

    @patch("news_officer.rss._response")
    @patch.object(SubstackApprovedTranscriptProvider, "_page_data")
    def test_substack_rejects_partial_timeline(self, page_data, response):
        transcript_url = "https://substackcdn.com/transcription.json?signature=test"
        segments = [
            {
                "start": 200 + index * 10,
                "end": 210 + index * 10,
                "text": " ".join(f"word{index}_{word}" for word in range(20)),
            }
            for index in range(40)
        ]
        page_data.return_value = (
            {
                "post": {
                    "podcastUpload": {
                        "duration": 600,
                        "transcription": {
                            "status": "transcribed",
                            "approved_at": "2026-09-14T00:00:00Z",
                            "cdn_url": transcript_url,
                        },
                    }
                }
            },
            "https://www.lennysnewsletter.com/p/example",
        )
        response.return_value = FakeResponse(
            json.dumps(segments), transcript_url, json_value=segments
        )
        episode = Episode(
            "rss:substack",
            "Example",
            "https://www.lennysnewsletter.com/p/example",
            "Lenny's Podcast",
            duration_seconds=600,
        )

        self.assertIsNone(SubstackApprovedTranscriptProvider().fetch(episode))

    @patch("news_officer.rss.requests.get")
    @patch("news_officer.rss.socket.getaddrinfo")
    def test_network_fetch_rejects_redirect_to_private_address(self, getaddrinfo, get):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ]
        redirect = FakeResponse("", "https://publisher.example.com/start")
        redirect.status_code = 302
        redirect.headers = {"Location": "https://127.0.0.1/transcript"}
        get.return_value = redirect

        with self.assertRaisesRegex(ValueError, "Private"):
            _response(
                "https://publisher.example.com/start",
                timeout=5,
                max_bytes=1_000,
            )

        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
