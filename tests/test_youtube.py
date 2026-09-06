import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.youtube import (
    _clean_vtt,
    _merge_caption_chunks,
    _parse_json3,
    intervals_cover_episode,
    latest_videos,
    normalize_channel_videos_url,
    video_metadata,
    youtube_video_id,
)


class YouTubeTranscriptTests(unittest.TestCase):
    def test_rolling_caption_text_is_merged_without_repetition(self):
        merged = _merge_caption_chunks(
            ["AI agents are", "agents are changing software", "changing software now"]
        )
        self.assertEqual(merged, "AI agents are changing software now")

    def test_vtt_cleaning_removes_timing_and_rolling_duplicates(self):
        raw = """WEBVTT

00:00:00.000 --> 00:00:03.000
AI agents are

00:00:02.000 --> 00:00:05.000
agents are changing software
"""
        self.assertEqual(_clean_vtt(raw), "AI agents are changing software")

    def test_json3_requires_timing_coverage_and_merges_text(self):
        events = []
        for start in range(0, 100, 10):
            phrase = "alpha beta" if start == 0 else "beta gamma"
            events.append(
                {
                    "tStartMs": start * 1000,
                    "dDurationMs": 10_000,
                    "segs": [{"utf8": phrase}],
                }
            )
        text = _parse_json3(json.dumps({"events": events}), 100)
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("alpha beta gamma"))

    def test_sparse_timing_is_not_a_complete_transcript(self):
        self.assertFalse(intervals_cover_episode([(0, 10), (90, 100)], 100))

    def test_channel_root_is_normalized_to_videos_tab(self):
        self.assertEqual(
            normalize_channel_videos_url(
                "https://www.youtube.com/channel/UCf_KhBXw5TIV0A7butjgFhg"
            ),
            "https://www.youtube.com/channel/UCf_KhBXw5TIV0A7butjgFhg/videos",
        )
        self.assertEqual(
            normalize_channel_videos_url("https://www.youtube.com/@SemiAnalysis"),
            "https://www.youtube.com/@SemiAnalysis/videos",
        )
        stable_url = (
            "https://www.youtube.com/channel/UCf_KhBXw5TIV0A7butjgFhg/videos"
        )
        self.assertEqual(normalize_channel_videos_url(stable_url), stable_url)

    def test_video_and_playlist_urls_are_rejected_as_channels(self):
        for url in (
            "https://youtu.be/kG8AoExkX40",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "https://www.youtube.com/playlist?list=example",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_channel_videos_url(url)

    def test_video_id_is_extracted_only_from_supported_video_shapes(self):
        for url in (
            "https://youtu.be/kG8AoExkX40",
            "https://www.youtube.com/watch?v=kG8AoExkX40&t=3",
            "https://www.youtube.com/embed/kG8AoExkX40",
            "https://www.youtube.com/shorts/kG8AoExkX40",
            "https://www.youtube.com/live/kG8AoExkX40",
        ):
            with self.subTest(url=url):
                self.assertEqual(youtube_video_id(url), "kG8AoExkX40")
        self.assertIsNone(youtube_video_id("https://www.youtube.com/@DavidSenra"))
        self.assertIsNone(youtube_video_id("https://youtube.com.evil.test/watch?v=kG8AoExkX40"))

    @patch("news_officer.youtube.requests.get")
    @patch("news_officer.youtube.run")
    @patch("news_officer.youtube._YTDLP_DISABLED_UNTIL", 0)
    def test_video_metadata_falls_back_to_official_oembed(
        self, mock_run, mock_get
    ):
        mock_run.side_effect = subprocess.CalledProcessError(1, ["yt-dlp"])
        response = mock_get.return_value
        response.status_code = 200
        response.content = b"{}"
        response.url = "https://www.youtube.com/oembed"
        response.json.return_value = {
            "type": "video",
            "title": "Sam Altman",
            "author_name": "David Senra",
            "author_url": "https://www.youtube.com/@DavidSenra",
        }

        episode = video_metadata("https://youtu.be/kG8AoExkX40")

        self.assertEqual(episode.id, "kG8AoExkX40")
        self.assertEqual(episode.title, "Sam Altman")
        self.assertEqual(episode.show, "David Senra")
        self.assertEqual(
            episode.url, "https://www.youtube.com/watch?v=kG8AoExkX40"
        )
        self.assertIsNone(episode.duration_seconds)
        self.assertIn("www.youtube.com/oembed?", mock_get.call_args.args[0])
        self.assertFalse(mock_get.call_args.kwargs["allow_redirects"])

    @patch("news_officer.youtube.requests.get")
    @patch("news_officer.youtube.run")
    @patch("news_officer.youtube._YTDLP_DISABLED_UNTIL", 0)
    def test_metadata_circuit_breaker_skips_repeated_blocked_player_requests(
        self, mock_run, mock_get
    ):
        mock_run.side_effect = subprocess.TimeoutExpired("yt-dlp", 25)
        response = mock_get.return_value
        response.status_code = 200
        response.content = b"{}"
        response.url = "https://www.youtube.com/oembed"
        response.json.return_value = {
            "type": "video",
            "title": "Sam Altman",
            "author_name": "David Senra",
        }

        video_metadata("https://youtu.be/kG8AoExkX40")
        video_metadata("https://youtu.be/kG8AoExkX40")

        mock_run.assert_called_once()
        self.assertEqual(mock_get.call_count, 2)

    @patch("news_officer.youtube.requests.get")
    @patch("news_officer.youtube.run")
    @patch("news_officer.youtube._YTDLP_DISABLED_UNTIL", 0)
    def test_one_video_failure_does_not_open_the_global_circuit(
        self, mock_run, mock_get
    ):
        mock_run.side_effect = subprocess.CalledProcessError(1, ["yt-dlp"])
        response = mock_get.return_value
        response.status_code = 200
        response.content = b"{}"
        response.url = "https://www.youtube.com/oembed"
        response.json.return_value = {
            "type": "video",
            "title": "Unavailable video",
            "author_name": "Example",
        }

        video_metadata("https://youtu.be/kG8AoExkX40")
        video_metadata("https://youtu.be/kG8AoExkX40")

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_get.call_count, 2)

    @patch("news_officer.youtube.run")
    def test_latest_videos_ignores_channel_tabs(self, mock_run):
        mock_run.return_value = json.dumps(
            {
                "channel": "Example",
                "entries": [
                    {"id": "videos", "title": "Videos"},
                    {"id": "UCf_KhBXw5TIV0A7butjgFhg", "title": "Channel"},
                    {"id": "kG8AoExkX40", "title": "A real episode"},
                ],
            }
        )

        episodes = latest_videos("https://www.youtube.com/@SemiAnalysis")

        self.assertEqual([episode.id for episode in episodes], ["kG8AoExkX40"])
        self.assertIn(
            "https://www.youtube.com/@SemiAnalysis/videos",
            mock_run.call_args.args,
        )

    @patch("news_officer.youtube.requests.get")
    @patch("news_officer.youtube.run")
    def test_latest_videos_prefers_official_atom_feed(self, mock_run, mock_get):
        mock_run.side_effect = subprocess.TimeoutExpired("yt-dlp", 90)
        response = mock_get.return_value
        response.status_code = 200
        response.url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCf_KhBXw5TIV0A7butjgFhg"
        response.content = b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<feed xmlns=\"http://www.w3.org/2005/Atom\"
      xmlns:yt=\"http://www.youtube.com/xml/schemas/2015\">
  <yt:channelId>UCf_KhBXw5TIV0A7butjgFhg</yt:channelId>
  <title>David Senra</title>
  <entry>
    <yt:videoId>kG8AoExkX40</yt:videoId>
    <title>Sam Altman</title>
    <published>2026-08-23T12:00:00+00:00</published>
    <author><name>David Senra</name></author>
  </entry>
</feed>"""

        episodes = latest_videos(
            "https://www.youtube.com/channel/UCf_KhBXw5TIV0A7butjgFhg/videos"
        )

        self.assertEqual([episode.id for episode in episodes], ["kG8AoExkX40"])
        self.assertEqual(episodes[0].show, "David Senra")
        self.assertEqual(
            episodes[0].published_at.isoformat(), "2026-08-23T12:00:00+00:00"
        )
        self.assertIn("feeds/videos.xml?channel_id=", mock_get.call_args.args[0])
        mock_run.assert_not_called()

    @patch("news_officer.youtube.requests.get")
    @patch("news_officer.youtube.run")
    def test_empty_atom_feed_falls_back_to_channel_extractor(
        self, mock_run, mock_get
    ):
        channel_id = "UCf_KhBXw5TIV0A7butjgFhg"
        response = mock_get.return_value
        response.status_code = 200
        response.url = (
            "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
        )
        response.content = f"""<?xml version=\"1.0\"?>
<feed xmlns=\"http://www.w3.org/2005/Atom\"
      xmlns:yt=\"http://www.youtube.com/xml/schemas/2015\">
  <yt:channelId>{channel_id}</yt:channelId>
  <title>David Senra</title>
</feed>""".encode()
        mock_run.return_value = json.dumps(
            {
                "channel": "David Senra",
                "entries": [{"id": "kG8AoExkX40", "title": "Sam Altman"}],
            }
        )

        episodes = latest_videos(
            f"https://www.youtube.com/channel/{channel_id}/videos"
        )

        self.assertEqual([episode.id for episode in episodes], ["kG8AoExkX40"])
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
