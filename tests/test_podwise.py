import copy
import json
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_officer.models import Episode
from news_officer.podwise import PodwiseAPIError, PodwiseTranscriptProvider


class PodwiseTests(unittest.TestCase):
    def setUp(self):
        self.provider = PodwiseTranscriptProvider("test-secret-not-real")
        self.episode = Episode(
            "rss:1",
            "A precise episode",
            "https://example.org/episode",
            "Show",
            duration_seconds=1200,
            published_at=datetime(2026, 9, 17, tzinfo=UTC),
        )
        self.meta = {
            "seq": 123,
            "title": self.episode.title,
            "podcastName": "Show",
            "link": self.episode.url,
            "duration": 1200,
            "publishTime": self.episode.published_at.timestamp(),
            "transcribed": True,
            "language": "en",
        }
        self.segments = [
            {
                "time": f"{i // 2:02d}:{(i % 2) * 30:02d}",
                "start": i * 30,
                "end": (i + 1) * 30,
                "content": "We discuss capital investment and model capabilities with concrete customer evidence. "
                * 5,
            }
            for i in range(40)
        ]

    def fetch(self, meta=None, segments=None, rows=None):
        with patch.object(
            self.provider,
            "_get",
            side_effect=[
                {"result": rows if rows is not None else [self.meta]},
                {
                    "episode": meta or self.meta,
                    "result": segments if segments is not None else self.segments,
                },
            ],
        ):
            return self.provider.fetch(self.episode)

    def test_full_transcript_with_verified_identity(self):
        result = self.fetch()
        self.assertTrue(result.verified_complete)
        self.assertIn("[19:30]", result.text)
        self.assertEqual(
            result.source_url,
            "https://app.podwise.ai/api/open/v1/episodes/123/transcripts",
        )

    def test_search_miss_falls_back_to_dated_podcast_catalog(self):
        with patch.object(self.provider, "_get", side_effect=[
            {"result": []}, {"result": [{"seq": 778}]}, {"result": [self.meta]},
            {"episode": self.meta, "result": self.segments},
        ]) as get:
            self.assertTrue(self.provider.fetch(self.episode).verified_complete)
        self.assertEqual(get.call_args_list[2].args[0], "/podcasts/778/episodes")
        self.assertEqual(get.call_args_list[2].args[1], {"date": "2026-09-18", "days": 3})

    def test_timestamp_only_full_transcript(self):
        segments = [
            {k: v for k, v in s.items() if k not in {"start", "end"}}
            for s in self.segments
        ]
        self.assertTrue(self.fetch(segments=segments).verified_complete)

    def test_millisecond_segments_are_validated_against_text_timestamps(self):
        segments = [
            {**s, "start": s["start"] * 1000 + 789, "end": s["end"] * 1000}
            for s in self.segments
        ]
        segments[-1]["end"] += 14000
        result = self.fetch(segments=segments)
        self.assertTrue(result.verified_complete)
        self.assertIn("[19:30]", result.text)

    def test_mixed_units_or_wrong_timestamp_are_not_silently_accepted(self):
        for changes in ({"start": 1000000}, {"time": "00:01:00"}):
            segments = copy.deepcopy(self.segments)
            segments[4].update(changes)
            self.assertIsNone(self.fetch(segments=segments))

    def test_publisher_audio_identity_and_unprocessed_duplicate(self):
        audio = "https://traffic.megaphone.fm/EXAMPLE123.mp3"
        self.episode = replace(self.episode, metadata={"audio_url": audio})
        meta = {**self.meta, "link": audio, "podcastName": "Show with the host"}
        pending = {**meta, "seq": 456, "transcribed": False}
        result = self.fetch(meta=meta, rows=[pending, meta])
        self.assertTrue(result.verified_complete)

    def test_large_timing_overrun_is_rejected(self):
        segments = copy.deepcopy(self.segments)
        segments[-1]["end"] += 120
        self.assertIsNone(self.fetch(segments=segments))

    def test_rss_audio_beats_transcribed_youtube_fallback(self):
        audio = "https://publisher.test/episode.mp3"
        video = "https://www.youtube.com/watch?v=full-episode"
        self.episode = replace(self.episode, metadata={
            "audio_url": audio, "youtube_url": video,
        })
        meta = {**self.meta, "link": audio}
        video_meta = {**self.meta, "seq": 456, "link": video}
        result = self.fetch(meta=meta, rows=[video_meta, meta])
        self.assertTrue(result.verified_complete)
        self.assertIn("/123/transcripts", result.source_url)
        # Two processed records for the *same* asset are still ambiguous.
        self.assertIsNone(self.fetch(rows=[meta, {**meta, "seq": 789}]))

    def test_small_rendition_overrun_requires_independent_done_status(self):
        segments = copy.deepcopy(self.segments)
        segments[-1]["end"] += 45
        for status, progress, accepted in [("done", 100, True), ("processing", 90, False),
                                           ("done", 99, False)]:
            with patch.object(self.provider, "_get", side_effect=[
                {"result": [self.meta]}, {"episode": self.meta, "result": segments},
                {"result": {"status": status, "progress": progress}},
            ]):
                self.assertEqual(bool(self.provider.fetch(self.episode)), accepted)

    def test_done_status_does_not_excuse_missing_middle(self):
        segments = copy.deepcopy(self.segments[:5] + self.segments[15:])
        segments[-1]["end"] += 45
        with patch.object(self.provider, "_get", side_effect=[
            {"result": [self.meta]}, {"episode": self.meta, "result": segments},
            {"result": {"status": "done", "progress": 100}},
        ]):
            self.assertIsNone(self.provider.fetch(self.episode))

    def test_truncation_gaps_snippets_and_wrong_episode_rejected(self):
        cases = [
            self.segments[:20],
            self.segments[:5] + self.segments[15:],
            [{**s, "content": "short summary"} for s in self.segments],
        ]
        for segments in cases:
            with self.subTest(length=len(segments)):
                self.assertIsNone(self.fetch(segments=segments))
        self.assertIsNone(self.fetch(meta={**self.meta, "seq": 456}))
        self.assertIsNone(self.fetch(meta={**self.meta, "duration": 2400}))

    def test_ambiguous_or_unprocessed_match_does_not_trigger_processing(self):
        self.assertIsNone(self.fetch(rows=[self.meta, {**self.meta, "seq": 456}]))
        self.assertIsNone(self.fetch(rows=[{**self.meta, "transcribed": False}]))
        self.assertIsNone(
            self.fetch(
                rows=[
                    {
                        **self.meta,
                        "link": "https://other.test/clip",
                        "title": "Similar clip",
                    }
                ]
            )
        )

    def test_exact_title_show_date_duration_can_match_changed_url(self):
        meta = {**self.meta, "link": "https://publisher.test/new-page"}
        self.assertTrue(self.fetch(rows=[meta], meta=meta).verified_complete)

    def test_no_token_no_network(self):
        with patch("news_officer.podwise.requests.get") as get:
            self.assertIsNone(PodwiseTranscriptProvider("").fetch(self.episode))
            get.assert_not_called()

    def test_http_errors_are_sanitized_and_redirects_disabled(self):
        for status in (301, 401, 402, 429, 500):
            response = MagicMock(status_code=status)
            response.__enter__.return_value = response
            with patch(
                "news_officer.podwise.requests.get", return_value=response
            ) as get:
                with self.assertRaises(PodwiseAPIError) as error:
                    self.provider._get("/episodes/search")
                self.assertNotIn("test-secret", str(error.exception))
                self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_response_limit_and_invalid_json(self):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        for body in (
            b"invalid json",
            json.dumps({"success": False}).encode(),
            b"x" * 4_000_001,
        ):
            response.iter_content.return_value = [body]
            with (
                patch("news_officer.podwise.requests.get", return_value=response),
                self.assertRaises(PodwiseAPIError),
            ):
                self.provider._get("/episodes/search")

    def test_nan_and_reversed_timing_rejected(self):
        for changes in ({"start": float("nan")}, {"start": 90}, {"end": -1}):
            segments = copy.deepcopy(self.segments)
            segments[0].update(changes)
            self.assertIsNone(self.fetch(segments=segments))
