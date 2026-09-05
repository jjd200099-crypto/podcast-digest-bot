import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.colossus import ColossusOfficialTranscriptProvider
from news_officer.models import Episode, Transcript

CURRENT_URL = "https://colossus.com/episode/kushner-building-thrive-capital"
LEGACY_URL = "https://joincolossus.com/episode/kushner-building-thrive-capital"


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


def transcript_page(
    words=1_200,
    *,
    speakers=("Patrick", "Josh"),
    gate=False,
    placeholder="",
    missing_toc_chapter=False,
):
    turn_words = max(1, words // 8)
    paragraphs = []
    for index in range(8):
        is_host = index % 2 == 0
        speaker = speakers[0] if is_host else speakers[-1]
        attribute = "data-transcript-host" if is_host else "data-transcript-guest"
        body = " ".join(f"word{index}x{word}" for word in range(turn_words))
        paragraphs.append(
            f"<p {attribute} data-transcript-speaker-changed>"
            f"<span class='transcript__speaker'>{speaker}</span><br>{body}</p>"
        )
    first_half = "".join(paragraphs[:4])
    second_half = "".join(paragraphs[4:])
    gate_markup = "<div class='content-gate-obscure'></div>" if gate else ""
    missing_link = (
        "<li><a href='#missing-section'>Missing section</a></li>"
        if missing_toc_chapter
        else ""
    )
    return f"""
    <html><head>
      <meta property='article:published_time' content='2026-09-01T14:55:43Z'>
      <meta property='og:audio:duration' content='600'>
    </head><body>
      <header class='single-podcast-episode-header'>
        <p class='single-podcast-episode-header__podcast-name'>
          <a href='/series/invest-like-the-best/'>Invest Like the Best</a>
        </p>
        <p class='single-podcast-episode-header__podcast-episode-number'>Episode 440</p>
        <h1 class='single-podcast-episode-header__title'>Building Thrive Capital</h1>
        <span class='single-podcast-episode-header__host--name'>Patrick O'Shaughnessy</span>
        <p class='single-podcast-episode-header__date'>09.01.2026</p>
        <p class='single-podcast-episode-header__description'>Official description.</p>
        <div class='show-notes-container'>
          <p>(00:00:00) Welcome</p><p>(00:09:20) Final question</p>
        </div>
      </header>
      <div id='outside'>OUTSIDE_TRANSCRIPT</div>
      <article class='transcript'>
        <header class='banner'><h3 class='banner__title'>Transcript</h3></header>
        <nav class='contents'><ul class='contents__links'>
          <li><a href='#introduction'>Introduction</a></li>
          <li><a href='#building-the-firm'>Building the Firm</a></li>
          {missing_link}
        </ul></nav>
        <div class='transcript__content'>
          <h2><a id='introduction'></a>Introduction</h2>{first_half}
          <h2><a id='building-the-firm'></a>Building the Firm</h2>{second_half}
          {placeholder}{gate_markup}
        </div>
      </article>
    </body></html>
    """


class ColossusOfficialTranscriptProviderTests(unittest.TestCase):
    def episode(self, **overrides):
        values = {
            "id": "youtube-id",
            "title": "Josh Kushner: Building Thrive Capital | Invest Like the Best",
            "url": "https://www.youtube.com/watch?v=youtube-id",
            "show": "Invest Like the Best",
            "duration_seconds": 600,
            "metadata": {
                "description": f"Full show notes and transcript: {LEGACY_URL}/?utm_source=youtube"
            },
        }
        values.update(overrides)
        return Episode(**values)

    def test_only_https_official_episode_urls_are_supported(self):
        provider = ColossusOfficialTranscriptProvider()
        self.assertTrue(provider.supports_url(CURRENT_URL))
        self.assertTrue(provider.supports_url(f"{CURRENT_URL}/?tab=transcript"))
        self.assertTrue(provider.supports_url(LEGACY_URL))
        self.assertTrue(
            provider.supports_url(
                "https://www.joincolossus.com/episode/kushner-building-thrive-capital/"
            )
        )
        self.assertFalse(provider.supports_url(CURRENT_URL.replace("https", "http")))
        self.assertFalse(
            provider.supports_url(
                "https://colossus.com.evil.test/episode/kushner-building-thrive-capital"
            )
        )
        self.assertFalse(
            provider.supports_url(
                "https://colossus.com@evil.test/episode/kushner-building-thrive-capital"
            )
        )
        self.assertFalse(provider.supports_url("https://colossus.com/series/foo"))
        self.assertFalse(provider.supports_url(f"{CURRENT_URL}/extra"))

    @patch("news_officer.colossus.requests.get")
    def test_fetch_prefers_description_url_and_extracts_only_transcript(self, get):
        get.return_value = FakeResponse(transcript_page(), CURRENT_URL)

        result = ColossusOfficialTranscriptProvider().fetch(self.episode())

        self.assertIsInstance(result, Transcript)
        self.assertTrue(result.verified_complete)
        self.assertEqual(result.source, "Invest Like the Best official transcript")
        self.assertEqual(result.source_url, CURRENT_URL)
        self.assertIn("Patrick word0x0", result.text)
        self.assertIn("Josh word1x0", result.text)
        self.assertNotIn("OUTSIDE_TRANSCRIPT", result.text)
        get.assert_called_once()

    @patch("news_officer.colossus.requests.get")
    def test_sitemap_fallback_matches_title(self, get):
        index_url = "https://colossus.com/sitemap_index.xml"
        episode_sitemap = "https://colossus.com/podcast_episode-sitemap2.xml"
        sitemap_index = f"""<?xml version='1.0'?>
        <sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <sitemap><loc>{episode_sitemap}</loc></sitemap>
          <sitemap><loc>https://evil.test/podcast_episode-sitemap3.xml</loc></sitemap>
        </sitemapindex>"""
        episode_urls = f"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://colossus.com/episode/unrelated-market-cycle</loc></url>
          <url><loc>{CURRENT_URL}</loc></url>
          <url><loc>https://evil.test/episode/kushner-building-thrive-capital</loc></url>
        </urlset>"""

        def response_for(url, **_kwargs):
            if url == index_url:
                return FakeResponse(sitemap_index, url)
            if url == episode_sitemap:
                return FakeResponse(episode_urls, url)
            if url == CURRENT_URL:
                return FakeResponse(transcript_page(), url)
            raise AssertionError(f"Unexpected network request: {url}")

        get.side_effect = response_for
        episode = self.episode(metadata={"description": "No official URL here"})

        result = ColossusOfficialTranscriptProvider().fetch(episode)

        self.assertIsInstance(result, Transcript)
        self.assertEqual(result.source_url, CURRENT_URL)
        self.assertEqual(get.call_count, 3)

    @patch("news_officer.colossus.requests.get")
    def test_rejects_gate_placeholder_and_incomplete_chapter_coverage(self, get):
        pages = {
            "login gate": transcript_page(gate=True),
            "placeholder": transcript_page(placeholder="<p>[insert transcript here]</p>"),
            "missing chapter": transcript_page(missing_toc_chapter=True),
            "missing transcript": "<html><body><p>Show notes only</p></body></html>",
        }
        provider = ColossusOfficialTranscriptProvider()
        for name, page in pages.items():
            with self.subTest(name=name):
                get.return_value = FakeResponse(page, CURRENT_URL)
                self.assertIsNone(provider.fetch(self.episode()))

    @patch("news_officer.colossus.requests.get")
    def test_requires_two_speakers_and_plausible_word_density(self, get):
        cases = {
            "one speaker": transcript_page(speakers=("Patrick",)),
            "too sparse": transcript_page(words=600),
            "too dense": transcript_page(words=2_400),
        }
        provider = ColossusOfficialTranscriptProvider()
        for name, page in cases.items():
            with self.subTest(name=name):
                get.return_value = FakeResponse(page, CURRENT_URL)
                self.assertIsNone(provider.fetch(self.episode()))

    @patch("news_officer.colossus.requests.get")
    def test_external_redirect_is_rejected_without_following_it(self, get):
        get.return_value = FakeResponse(
            "",
            LEGACY_URL,
            status_code=302,
            headers={"Location": "https://evil.test/transcript"},
        )

        with self.assertRaises(ValueError):
            ColossusOfficialTranscriptProvider().episode_from_url(LEGACY_URL)
        self.assertEqual(get.call_count, 1)

    @patch("news_officer.colossus.requests.get")
    def test_internal_migration_redirect_is_allowed_and_recorded(self, get):
        get.side_effect = [
            FakeResponse(
                "",
                LEGACY_URL,
                status_code=301,
                headers={"Location": CURRENT_URL},
            ),
            FakeResponse(transcript_page(), CURRENT_URL),
        ]

        episode = ColossusOfficialTranscriptProvider().episode_from_url(LEGACY_URL)

        self.assertIsInstance(episode, Episode)
        self.assertEqual(episode.url, CURRENT_URL)
        self.assertEqual(get.call_count, 2)

    @patch("news_officer.colossus.requests.get")
    def test_episode_from_url_uses_existing_episode_model(self, get):
        get.return_value = FakeResponse(transcript_page(), CURRENT_URL)

        episode = ColossusOfficialTranscriptProvider().episode_from_url(
            f"{CURRENT_URL}/?tab=transcript"
        )

        self.assertIsInstance(episode, Episode)
        self.assertEqual(episode.id, "colossus:kushner-building-thrive-capital")
        self.assertEqual(episode.title, "Building Thrive Capital")
        self.assertEqual(episode.show, "Invest Like the Best")
        self.assertEqual(episode.duration_seconds, 600)
        self.assertEqual(episode.published_at.isoformat(), "2026-09-01T14:55:43+00:00")
        self.assertEqual(episode.metadata["episode_number"], 440)
        self.assertEqual(episode.metadata["host"], "Patrick O'Shaughnessy")


if __name__ == "__main__":
    unittest.main()
