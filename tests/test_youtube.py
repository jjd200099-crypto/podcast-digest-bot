import json
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


if __name__ == "__main__":
    unittest.main()
