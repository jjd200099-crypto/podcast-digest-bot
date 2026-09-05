import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.models import Episode, Transcript
from news_officer.sequoia import SequoiaOfficialTranscriptProvider

EPISODE_URL = "https://sequoiacap.com/podcast/example-founder-building-ai-agents"


class FakeResponse:
    def __init__(self, text, url, status_code=200, headers=None):
        self.text = text
        self.content = text.encode()
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def transcript_page(words=200, speakers=("Host", "Guest"), extra=""):
    first = " ".join(f"alpha{index}" for index in range(words // 2))
    second = " ".join(f"beta{index}" for index in range(words - words // 2))
    paragraphs = [f"<p><strong>{speakers[0]}:</strong> {first}</p>"]
    paragraphs.append(f"<p><strong>{speakers[-1]}:</strong> {second}</p>")
    return (
        "<html><head><meta property='og:title' "
        "content='Example Founder: Building AI Agents | Sequoia Capital'>"
        "<meta name='description' content='Official episode'></head><body>"
        "<div id='show-notes'>OUTSIDE_TRANSCRIPT</div>"
        f"<div id='podcast-transcript'><h3>Main conversation</h3>{''.join(paragraphs)}"
        f"{extra}</div></body></html>"
    )


class SequoiaOfficialTranscriptProviderTests(unittest.TestCase):
    def episode(self, **overrides):
        values = {
            "id": "youtube-id",
            "title": "Example Founder: Building AI Agents",
            "url": "https://www.youtube.com/watch?v=youtube-id",
            "show": "Sequoia Capital",
            "duration_seconds": 120,
            "metadata": {
                "description": f"Read the transcript: {EPISODE_URL}?ref=youtube"
            },
        }
        values.update(overrides)
        return Episode(**values)

    @patch("news_officer.sequoia.requests.get")
    def test_fetch_uses_official_description_url_and_only_transcript_container(
        self, get
    ):
        get.return_value = FakeResponse(transcript_page(), EPISODE_URL)

        result = SequoiaOfficialTranscriptProvider().fetch(self.episode())

        self.assertIsInstance(result, Transcript)
        self.assertTrue(result.verified_complete)
        self.assertEqual(result.source, "Sequoia official transcript")
        self.assertEqual(result.source_url, EPISODE_URL)
        self.assertIn("Host: alpha0", result.text)
        self.assertIn("Guest: beta0", result.text)
        self.assertNotIn("OUTSIDE_TRANSCRIPT", result.text)
        get.assert_called_once()

    def test_only_https_sequoiacap_episode_urls_are_supported(self):
        provider = SequoiaOfficialTranscriptProvider()
        self.assertTrue(provider.supports_url(EPISODE_URL))
        self.assertTrue(
            provider.supports_url(f"{EPISODE_URL}/?campaign=test#transcript")
        )
        self.assertFalse(
            provider.supports_url(EPISODE_URL.replace("https://", "http://"))
        )
        self.assertFalse(
            provider.supports_url("https://www.sequoiacap.com/podcast/example")
        )
        self.assertFalse(
            provider.supports_url("https://sequoiacap.com.evil.test/podcast/example")
        )
        self.assertFalse(
            provider.supports_url("https://sequoiacap.com@evil.test/podcast/example")
        )
        self.assertFalse(
            provider.supports_url("https://sequoiacap.com/stories/example")
        )

    @patch("news_officer.sequoia.requests.get")
    def test_sitemap_fallback_matches_the_episode_title(self, get):
        sitemap = """<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://sequoiacap.com/podcast/unrelated-cloud-infrastructure</loc></url>
          <url><loc>https://sequoiacap.com/podcast/example-founder-building-ai-agents</loc></url>
          <url><loc>https://evil.test/podcast/example-founder-building-ai-agents</loc></url>
        </urlset>"""

        def response_for(url, **_kwargs):
            if url == "https://sequoiacap.com/sitemap.xml":
                return FakeResponse(sitemap, url)
            if url == EPISODE_URL:
                return FakeResponse(transcript_page(), url)
            raise AssertionError(f"Unexpected network request: {url}")

        get.side_effect = response_for
        episode = self.episode(metadata={"description": "No official link here"})

        result = SequoiaOfficialTranscriptProvider().fetch(episode)

        self.assertIsInstance(result, Transcript)
        self.assertEqual(result.source_url, EPISODE_URL)
        self.assertEqual(get.call_count, 2)

    @patch("news_officer.sequoia.requests.get")
    def test_rejects_placeholders_paywalls_and_missing_transcript(self, get):
        pages = {
            "placeholder": transcript_page(extra="<p>[insert intro here]</p>"),
            "paywall": transcript_page() + "<div data-testid='paywall'>Sign in</div>",
            "missing": "<html><body><div id='show-notes'>summary only</div></body></html>",
        }
        provider = SequoiaOfficialTranscriptProvider()
        for name, page in pages.items():
            with self.subTest(name=name):
                get.return_value = FakeResponse(page, EPISODE_URL)
                self.assertIsNone(provider.fetch(self.episode()))

    @patch("news_officer.sequoia.requests.get")
    def test_requires_two_speakers_and_plausible_word_density(self, get):
        cases = {
            "one speaker": (transcript_page(speakers=("Host",)), 120),
            "too sparse": (transcript_page(words=100), 120),
            "too dense": (transcript_page(words=500), 120),
        }
        provider = SequoiaOfficialTranscriptProvider()
        for name, (page, duration) in cases.items():
            with self.subTest(name=name):
                get.return_value = FakeResponse(page, EPISODE_URL)
                self.assertIsNone(
                    provider.fetch(self.episode(duration_seconds=duration))
                )

    @patch("news_officer.sequoia.requests.get")
    def test_external_redirect_is_rejected_before_following_it(self, get):
        get.return_value = FakeResponse(
            "",
            EPISODE_URL,
            status_code=302,
            headers={"Location": "https://evil.test/transcript"},
        )

        with self.assertRaises(ValueError):
            SequoiaOfficialTranscriptProvider().episode_from_url(EPISODE_URL)
        self.assertEqual(get.call_count, 1)

    @patch("news_officer.sequoia.requests.get")
    def test_episode_from_url_returns_the_existing_episode_model(self, get):
        page = (
            transcript_page()
            .replace(
                "</head>",
                "<meta property='og:video:duration' content='120'>"
                "<meta property='article:published_time' content='2026-09-04T09:00:00Z'></head>",
            )
            .replace(
                "</body>",
                "<iframe src='https://www.youtube.com/embed/AbC_123'></iframe></body>",
            )
        )
        get.return_value = FakeResponse(page, EPISODE_URL)

        episode = SequoiaOfficialTranscriptProvider().episode_from_url(
            f"{EPISODE_URL}/?utm_source=test"
        )

        self.assertIsInstance(episode, Episode)
        self.assertEqual(episode.id, "sequoia:example-founder-building-ai-agents")
        self.assertEqual(episode.title, "Example Founder: Building AI Agents")
        self.assertEqual(episode.url, EPISODE_URL)
        self.assertEqual(episode.duration_seconds, 120)
        self.assertEqual(episode.published_at.isoformat(), "2026-09-04T09:00:00+00:00")
        self.assertEqual(
            episode.metadata["youtube_url"],
            "https://www.youtube.com/watch?v=AbC_123",
        )


if __name__ == "__main__":
    unittest.main()
