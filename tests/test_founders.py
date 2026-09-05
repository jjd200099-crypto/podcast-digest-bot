import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.founders import DavidSenraOfficialTranscriptProvider
from news_officer.models import Episode, Transcript

EPISODE_URL = "https://www.davidsenra.com/episode/sam-altman"
VIDEO_ID = "kG8AoExkX40"


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
    *,
    video_id=VIDEO_ID,
    words=1_200,
    blocks=24,
    speakers=("David Senra", "Sam Altman"),
    include_heading=True,
    include_json_ld=True,
    gate=False,
    canonical_url=EPISODE_URL,
    labelled_through_end=True,
    duration="PT10M",
):
    paragraphs = []
    words_per_block = max(1, words // blocks)
    for index in range(blocks):
        speaker = speakers[index % len(speakers)]
        body = " ".join(f"word{index}x{number}" for number in range(words_per_block))
        if labelled_through_end or index < blocks // 2:
            if index % 2:
                paragraphs.append(f"<p>{speaker}: {body}</p>")
            else:
                paragraphs.append(f"<p>[{speaker}]<br>{body}</p>")
        else:
            paragraphs.append(f"<p>{body}</p>")
    record = {
        "@context": "https://schema.org",
        "@type": "PodcastEpisode",
        "name": "Sam Altman",
        "description": "Sam Altman on building OpenAI.",
        "url": canonical_url,
        "datePublished": "2026-08-23",
        "duration": duration,
        "partOfSeries": {"@type": "PodcastSeries", "name": "David Senra"},
        "associatedMedia": [
            {
                "@type": "MediaObject",
                "contentUrl": f"https://youtu.be/{video_id}?feature=shared",
            }
        ],
        "actor": {"@type": "Person", "name": "Sam Altman"},
    }
    json_ld = (
        f"<script type='application/ld+json'>{json.dumps(record)}</script>"
        if include_json_ld
        else ""
    )
    heading = "<h2>Episode transcript</h2>" if include_heading else ""
    gate_markup = "<div data-testid='paywall'>Sign in</div>" if gate else ""
    return f"""
    <html><head>{json_ld}</head><body>
      <div id='outside'>OUTSIDE_TRANSCRIPT</div>
      <div class='pc-detail_content u-mb-lg'>
        {heading}
        {gate_markup}
        <div transcript-wrapper='true' class='pc-detail_transcript'>
          <div class='w-richtext'>{"".join(paragraphs)}</div>
          <div class='pc-detail_transcript-overlay'></div>
        </div>
      </div>
    </body></html>
    """


class DavidSenraOfficialTranscriptProviderTests(unittest.TestCase):
    def episode(self, **overrides):
        values = {
            "id": VIDEO_ID,
            "title": "Sam Altman on Building OpenAI & Betting on the Impossible",
            "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
            "show": "David Senra",
            "duration_seconds": 600,
            "metadata": {"author_name": "David Senra"},
        }
        values.update(overrides)
        return Episode(**values)

    def test_only_https_official_episode_urls_are_supported(self):
        provider = DavidSenraOfficialTranscriptProvider()
        self.assertTrue(provider.supports_url(EPISODE_URL))
        self.assertTrue(provider.supports_url(f"{EPISODE_URL}/?ref=test#transcript"))
        self.assertTrue(
            provider.supports_url("https://davidsenra.com/episode/sam-altman")
        )
        self.assertFalse(provider.supports_url(EPISODE_URL.replace("https", "http")))
        self.assertFalse(
            provider.supports_url(
                "https://www.davidsenra.com.evil.test/episode/sam-altman"
            )
        )
        self.assertFalse(
            provider.supports_url(
                "https://www.davidsenra.com@evil.test/episode/sam-altman"
            )
        )
        self.assertFalse(provider.supports_url("https://www.davidsenra.com/podcast"))
        self.assertFalse(provider.supports_url(f"{EPISODE_URL}/extra"))

    @patch("news_officer.founders.requests.get")
    def test_episode_from_url_parses_official_json_ld(self, get):
        get.return_value = FakeResponse(transcript_page(), EPISODE_URL)

        episode = DavidSenraOfficialTranscriptProvider().episode_from_url(
            f"{EPISODE_URL}/?utm_source=test"
        )

        self.assertIsInstance(episode, Episode)
        self.assertEqual(episode.id, "david-senra:sam-altman")
        self.assertEqual(episode.title, "Sam Altman")
        self.assertEqual(episode.show, "David Senra")
        self.assertEqual(episode.url, EPISODE_URL)
        self.assertEqual(episode.duration_seconds, 600)
        self.assertEqual(episode.published_at.isoformat(), "2026-08-23T00:00:00+00:00")
        self.assertEqual(
            episode.metadata["youtube_url"],
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
        )
        self.assertEqual(episode.metadata["guest"], "Sam Altman")

    @patch("news_officer.founders.requests.get")
    def test_fetch_uses_official_description_url_and_extracts_only_transcript(
        self, get
    ):
        get.return_value = FakeResponse(transcript_page(), EPISODE_URL)
        episode = self.episode(
            metadata={"description": f"Official transcript: {EPISODE_URL}?ref=youtube"}
        )

        result = DavidSenraOfficialTranscriptProvider().fetch(episode)

        self.assertIsInstance(result, Transcript)
        self.assertTrue(result.verified_complete)
        self.assertEqual(result.source, "David Senra official transcript")
        self.assertEqual(result.source_url, EPISODE_URL)
        self.assertIn("David Senra", result.text)
        self.assertIn("Sam Altman", result.text)
        self.assertNotIn("OUTSIDE_TRANSCRIPT", result.text)
        get.assert_called_once()

    @patch("news_officer.founders.requests.get")
    def test_youtube_episode_is_matched_by_video_id_from_sitemap(self, get):
        wrong_url = "https://www.davidsenra.com/episode/sam-altman-archive"
        sitemap = f"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>{wrong_url}</loc></url>
          <url><loc>{EPISODE_URL}</loc></url>
          <url><loc>https://evil.test/episode/sam-altman</loc></url>
        </urlset>"""

        def response_for(url, **_kwargs):
            if url == "https://www.davidsenra.com/sitemap.xml":
                return FakeResponse(sitemap, url)
            if url == wrong_url:
                return FakeResponse(
                    transcript_page(
                        video_id="WrongVid123",
                        canonical_url=wrong_url,
                    ),
                    wrong_url,
                )
            if url == EPISODE_URL:
                return FakeResponse(transcript_page(), EPISODE_URL)
            raise AssertionError(f"Unexpected request: {url}")

        get.side_effect = response_for

        result = DavidSenraOfficialTranscriptProvider().fetch(self.episode())

        self.assertIsInstance(result, Transcript)
        self.assertEqual(result.source_url, EPISODE_URL)
        requested_urls = [call.args[0] for call in get.call_args_list]
        self.assertIn(EPISODE_URL, requested_urls)
        self.assertNotIn("https://evil.test/episode/sam-altman", requested_urls)

    @patch("news_officer.founders.requests.get")
    def test_title_match_alone_cannot_select_a_different_video(self, get):
        sitemap = f"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>{EPISODE_URL}</loc></url>
        </urlset>"""

        def response_for(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return FakeResponse(sitemap, url)
            return FakeResponse(transcript_page(video_id="WrongVid123"), EPISODE_URL)

        get.side_effect = response_for

        result = DavidSenraOfficialTranscriptProvider().fetch(self.episode())

        self.assertIsNone(result)

    @patch("news_officer.founders.requests.get")
    def test_rejects_missing_metadata_heading_gate_and_incomplete_text(self, get):
        cases = {
            "missing JSON-LD": transcript_page(include_json_ld=False),
            "missing heading": transcript_page(include_heading=False),
            "paywall": transcript_page(gate=True),
            "one speaker": transcript_page(speakers=("David Senra",)),
            "too sparse": transcript_page(words=500),
            "labels stop halfway": transcript_page(labelled_through_end=False),
            "implausibly dense": transcript_page(words=3_000),
        }
        provider = DavidSenraOfficialTranscriptProvider()
        episode = self.episode(
            url=EPISODE_URL,
            metadata={},
        )
        for name, page in cases.items():
            with self.subTest(name=name):
                get.return_value = FakeResponse(page, EPISODE_URL)
                self.assertIsNone(provider.fetch(episode))

    @patch("news_officer.founders.requests.get")
    def test_long_transcript_can_be_verified_when_duration_is_unavailable(self, get):
        get.return_value = FakeResponse(
            transcript_page(words=3_600, duration=""), EPISODE_URL
        )
        episode = Episode(
            id="official",
            title="Sam Altman",
            url=EPISODE_URL,
            show="David Senra",
        )

        result = DavidSenraOfficialTranscriptProvider().fetch(episode)

        self.assertIsInstance(result, Transcript)
        self.assertTrue(result.verified_complete)

    @patch("news_officer.founders.requests.get")
    def test_external_redirect_is_rejected_without_following_it(self, get):
        get.return_value = FakeResponse(
            "",
            EPISODE_URL,
            status_code=302,
            headers={"Location": "https://evil.test/transcript"},
        )

        with self.assertRaises(ValueError):
            DavidSenraOfficialTranscriptProvider().episode_from_url(EPISODE_URL)
        self.assertEqual(get.call_count, 1)

    @patch("news_officer.founders.requests.get")
    def test_internal_redirect_is_allowed_and_final_url_is_recorded(self, get):
        apex_url = "https://davidsenra.com/episode/sam-altman"
        get.side_effect = [
            FakeResponse(
                "",
                apex_url,
                status_code=301,
                headers={"Location": EPISODE_URL},
            ),
            FakeResponse(transcript_page(), EPISODE_URL),
        ]

        episode = DavidSenraOfficialTranscriptProvider().episode_from_url(apex_url)

        self.assertIsInstance(episode, Episode)
        self.assertEqual(episode.url, EPISODE_URL)
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
