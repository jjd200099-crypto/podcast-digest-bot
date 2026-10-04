import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.models import Episode, Transcript
from news_officer.podcast import (
    FeedDiscoveryError,
    PodcastService,
    TranscriptLookupError,
    TranscriptResolver,
)
from news_officer.store import Store
from news_officer.summarizer import SummaryFormatError
from news_officer.youtube import is_youtube_url


class FakeSummarizer:
    def summarize(self, episode, transcript):
        return "summary"


class BadFormatSummarizer:
    def summarize(self, episode, transcript):
        raise SummaryFormatError("not ten")


class BrokenProvider:
    name = "broken official source"

    def fetch(self, episode):
        raise TimeoutError("temporary outage")


class MissingProvider:
    name = "healthy source without transcript"

    def fetch(self, episode):
        return None


class CompleteProvider:
    name = "complete"

    def fetch(self, episode):
        return Transcript(
            text="complete transcript",
            source=self.name,
            source_url=episode.url,
            verified_complete=True,
        )


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

    def test_interactive_source_outage_returns_an_explicit_safe_result(self):
        self.feeds.write_text('{"youtube_channels": []}')
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            TranscriptResolver([BrokenProvider()]),
        )
        episode = Episode(
            "kG8AoExkX40",
            "Sam Altman",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "David Senra",
        )
        with patch("news_officer.podcast.video_metadata", return_value=episode):
            result = service.analyze_url(episode.url)
        self.assertEqual(result.status, "source_error")
        self.assertIn("未取得完整文字稿，本次不摘要", result.message)

    def test_summary_format_failure_returns_a_terminal_interactive_result(self):
        self.feeds.write_text('{"youtube_channels": []}')
        service = PodcastService(
            self.store,
            self.feeds,
            BadFormatSummarizer(),
            TranscriptResolver([CompleteProvider()]),
        )
        episode = Episode(
            "kG8AoExkX40",
            "Sam Altman",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "David Senra",
            duration_seconds=3600,
        )

        with patch("news_officer.podcast.video_metadata", return_value=episode):
            result = service.analyze_url(episode.url)

        self.assertEqual(result.status, "summary_format_error")
        self.assertIn("没有发送不合格结果", result.message)
        archived = self.store.get_verified_transcript(episode.id)
        self.assertIsNotNone(archived)
        self.assertEqual(archived.transcript.text, "complete transcript")

    def test_successful_summary_is_cached_for_readable_transcript_navigation(self):
        self.feeds.write_text('{"youtube_channels": []}')
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            TranscriptResolver([CompleteProvider()]),
        )
        episode = Episode(
            "digest-cache",
            "Cached digest",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "David Senra",
            duration_seconds=3600,
        )

        with patch("news_officer.podcast.video_metadata", return_value=episode):
            result = service.analyze_url(episode.url)

        self.assertEqual(result.status, "summarized")
        self.assertEqual(
            self.store.get_transcript_digest(episode.id), result.message
        )

    def test_summary_format_failure_is_cooled_down_without_failing_daily_scan(self):
        self.feeds.write_text(
            '{"sources": [{"name": "David Senra", "type": "youtube", '
            '"url": "https://www.youtube.com/@DavidSenra"}]}'
        )
        episode = Episode(
            "format-error",
            "Title",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "David Senra",
            duration_seconds=3600,
            published_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
        service = PodcastService(
            self.store,
            self.feeds,
            BadFormatSummarizer(),
            TranscriptResolver([CompleteProvider()]),
        )

        with patch("news_officer.podcast.latest_videos", return_value=[episode]):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))

        self.assertEqual([item.status for item in items], ["summary_format_error"])
        archived = self.store.get_verified_transcript(episode.id)
        self.assertIsNotNone(archived)
        self.assertEqual(archived.transcript.text, "complete transcript")
        self.store.record_episode(episode, items[0].status)
        self.assertIsNone(self.store.episode_review_state(episode.id))

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

        def discover(channel, playlist_end=4):
            if channel.endswith("@one"):
                return [candidate]
            raise TimeoutError("temporary outage")

        with (
            patch("news_officer.podcast.latest_videos", side_effect=discover),
            self.assertRaises(FeedDiscoveryError),
        ):
            service.discover_daily_candidates()

    def test_minority_feed_outage_is_reported_with_healthy_results(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one", '
            '"https://www.youtube.com/@two", "https://www.youtube.com/@three"]}'
        )
        candidate = Episode(
            "new",
            "New episode",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "Show",
            duration_seconds=600,
            published_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            TranscriptResolver([MissingProvider()]),
        )

        def discover(channel, playlist_end=4):
            if channel.endswith("@one"):
                raise TimeoutError("temporary outage")
            return [candidate]

        with patch("news_officer.podcast.latest_videos", side_effect=discover):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))

        self.assertEqual(
            [item.status for item in items], ["failed", "no_transcript"]
        )

    def test_optional_youtube_failure_does_not_fail_a_healthy_rss_source(self):
        self.feeds.write_text(
            '{"sources": [{"name": "Show", "type": "youtube", '
            '"url": "https://www.youtube.com/@show", '
            '"rss_url": "https://example.test/show.rss", "priority": "A"}]}'
        )
        episode = Episode(
            "rss:healthy",
            "Healthy RSS episode",
            "https://example.test/episode",
            "Show",
            published_at=datetime(2026, 9, 5, tzinfo=UTC),
            metadata={"rss_feed_url": "https://example.test/show.rss"},
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())

        with (
            patch("news_officer.podcast.latest_videos", side_effect=TimeoutError),
            patch(
                "news_officer.podcast.latest_rss_episodes",
                return_value=[episode],
            ),
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual([candidate.id for candidate in candidates], ["rss:healthy"])
        self.assertEqual(service._last_failed_feeds, ())

    def test_dual_channel_source_is_hard_failure_when_both_are_empty(self):
        self.feeds.write_text(
            '{"sources": [{"name": "Show", "type": "youtube", '
            '"url": "https://www.youtube.com/@show", '
            '"rss_url": "https://example.test/show.rss"}]}'
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())

        with (
            patch("news_officer.podcast.latest_videos", return_value=[]),
            patch("news_officer.podcast.latest_rss_episodes", return_value=[]),
            self.assertRaises(FeedDiscoveryError),
        ):
            service.discover_daily_candidates(datetime(2026, 9, 6, tzinfo=UTC))

    def test_unmatched_youtube_episode_survives_rss_merge(self):
        self.feeds.write_text(
            '{"sources": [{"name": "Show", "type": "youtube", '
            '"url": "https://www.youtube.com/@show", '
            '"rss_url": "https://example.test/show.rss"}]}'
        )
        rss_episode = Episode(
            "rss:old-title",
            "A portfolio construction conversation",
            "https://example.test/rss-episode",
            "Show",
            published_at=datetime(2026, 9, 5, 10, tzinfo=UTC),
            metadata={"rss_feed_url": "https://example.test/show.rss"},
        )
        youtube_episode = Episode(
            "youtube-new",
            "An entirely different AI release",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "Show",
            published_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())

        with (
            patch(
                "news_officer.podcast.latest_videos",
                return_value=[youtube_episode],
            ),
            patch(
                "news_officer.podcast.latest_rss_episodes",
                return_value=[rss_episode],
            ),
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual(
            [candidate.id for candidate in candidates],
            ["rss:old-title", "youtube-new"],
        )

    def test_full_rss_episode_beats_newer_clip_under_small_budget(self):
        self.feeds.write_text(json.dumps({"sources": [{
            "name": "Show", "type": "youtube", "url": "https://www.youtube.com/@show",
            "rss_url": "https://example.test/show.rss",
        }]}))
        rss = Episode("rss:full", "Long interview", "https://example.test/full", "Show",
                      published_at=datetime(2026, 9, 5, 10, tzinfo=UTC),
                      metadata={"rss_feed_url": "https://example.test/show.rss"})
        clip = Episode("clip", "Short cut", "https://youtu.be/clip", "Show",
                       published_at=datetime(2026, 9, 5, 12, tzinfo=UTC))
        service = PodcastService(self.store, self.feeds, FakeSummarizer(), max_daily_candidates=1)
        with (
            patch("news_officer.podcast.latest_videos", return_value=[clip]),
            patch("news_officer.podcast.latest_rss_episodes", return_value=[rss]) as scan,
        ):
            candidates = service.discover_daily_candidates(datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual([item.id for item in candidates], ["rss:full"])
        self.assertEqual(scan.call_args.kwargs["limit"], 30)

        # A fresh unknown-date video must not starve a due full-episode retry.
        unknown_clip = Episode("unknown", "Unknown age", "https://youtu.be/unknown", "Show")
        with (
            patch("news_officer.podcast.latest_videos", return_value=[unknown_clip]),
            patch("news_officer.podcast.latest_rss_episodes", return_value=[rss]),
            patch.object(self.store, "episode_review_state", side_effect=lambda episode_id: "retry" if episode_id == "rss:full" else "new"),
        ):
            candidates = service.discover_daily_candidates(datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual([item.id for item in candidates], ["rss:full"])

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

    def test_syndicated_episode_prefers_official_transcript_and_honors_alias_history(self):
        self.feeds.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "name": "Video copy",
                            "type": "youtube",
                            "url": "https://www.youtube.com/@video-copy",
                            "priority": "A",
                        },
                        {
                            "name": "Publisher copy",
                            "type": "rss",
                            "rss_url": "https://publisher.example.com/feed",
                            "priority": "B",
                        },
                    ]
                }
            )
        )
        published_at = datetime(2026, 9, 5, 12, tzinfo=UTC)
        video = Episode(
            "video-id",
            "A Durable AI Company",
            "https://www.youtube.com/watch?v=video-id",
            "Video copy",
            duration_seconds=3600,
            published_at=published_at,
        )
        publisher = Episode(
            "rss:publisher-id",
            "A Durable AI Company",
            "https://publisher.example.com/episode",
            "Publisher copy",
            duration_seconds=3600,
            published_at=published_at,
            metadata={
                "rss_feed_url": "https://publisher.example.com/feed",
                "rss_transcripts": [
                    {
                        "url": "https://publisher.example.com/transcript.txt",
                        "type": "text/plain",
                    }
                ],
            },
        )
        service = PodcastService(self.store, self.feeds, FakeSummarizer())

        with (
            patch("news_officer.podcast.latest_videos", return_value=[video]),
            patch(
                "news_officer.podcast.latest_rss_episodes",
                return_value=[publisher],
            ),
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual([candidate.id for candidate in candidates], [publisher.id])

        self.store.record_episode(video, "sent")
        with (
            patch("news_officer.podcast.latest_videos", return_value=[video]),
            patch(
                "news_officer.podcast.latest_rss_episodes",
                return_value=[publisher],
            ),
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual(candidates, [])

    def test_shared_rss_feed_link_does_not_collapse_distinct_episodes(self):
        feed_url = "https://feeds.example.com/show.rss"
        self.feeds.write_text(
            json.dumps(
                {
                    "sources": [
                        {"name": "Show", "type": "rss", "rss_url": feed_url}
                    ]
                }
            )
        )
        episodes = [
            Episode(
                f"rss:{index}",
                f"Distinct interview number {index}",
                feed_url,
                "Show",
                duration_seconds=3600,
                published_at=datetime(2026, 9, 5, 12 + index, tzinfo=UTC),
                metadata={
                    "rss_feed_url": feed_url,
                    "audio_url": f"https://audio.example.com/{index}.mp3",
                },
            )
            for index in range(2)
        ]
        service = PodcastService(self.store, self.feeds, FakeSummarizer())
        with patch(
            "news_officer.podcast.latest_rss_episodes", return_value=episodes
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual({candidate.id for candidate in candidates}, {"rss:0", "rss:1"})

    def test_summary_quota_reserves_b_then_backfills_a_if_b_has_no_transcript(self):
        self.feeds.write_text('{"sources": []}')
        candidates = [
            Episode(
                identifier,
                identifier,
                f"https://publisher.example.com/{identifier}",
                identifier,
                duration_seconds=3600,
                published_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
                metadata={"source_priority": priority},
            )
            for identifier, priority in (
                ("a1", "A"), ("a2", "A"), ("a3", "A"), ("b1", "B")
            )
        ]
        resolver = TranscriptResolver([CompleteProvider()])
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            resolver,
            max_daily_summaries=3,
        )
        with patch.object(
            service, "discover_daily_candidates", return_value=candidates
        ):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))

        self.assertEqual(
            [item.episode.id for item in items if item.status == "summarized"],
            ["a1", "a2", "b1"],
        )

        with (
            patch.object(service, "discover_daily_candidates", return_value=candidates),
            patch.object(
                resolver,
                "fetch",
                side_effect=lambda episode: (
                    None if episode.id == "b1" else CompleteProvider().fetch(episode)
                ),
            ),
        ):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))

        self.assertEqual(
            [item.episode.id for item in items if item.status == "summarized"],
            ["a1", "a2", "a3"],
        )

    def test_zero_summary_limit_processes_every_candidate(self):
        candidates = [Episode(
            f"ep-{i}", f"Episode {i}", f"https://publisher.example.com/{i}", "Show",
            duration_seconds=3600, published_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
            metadata={"source_priority": "A"},
        ) for i in range(7)]
        service = PodcastService(self.store, self.feeds, FakeSummarizer(),
                                 TranscriptResolver([CompleteProvider()]),
                                 max_daily_summaries=0)
        with patch.object(service, "discover_daily_candidates", return_value=candidates):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual([i.episode.id for i in items], [e.id for e in candidates])
        self.assertTrue(all(i.status == "summarized" for i in items))

    def test_existing_verified_archive_precedes_all_external_providers(self):
        episode = Episode("cached", "Cached episode", "https://publisher.test/cached", "Show")
        transcript = CompleteProvider().fetch(episode)
        self.store.save_verified_transcript(episode, transcript)
        service = PodcastService(self.store, self.feeds, FakeSummarizer(),
                                 podwise_api_token="test-token")
        self.assertEqual(service.transcript_resolver.providers[0].name,
                         "previously verified archive")
        self.assertEqual(service.transcript_resolver.providers[-2].name,
                         "Podwise verified transcript")
        with patch.object(service.transcript_resolver.providers[-1], "fetch") as podwise:
            self.assertEqual(service.transcript_resolver.fetch(episode), transcript)
            podwise.assert_not_called()

    def test_zero_candidate_limit_does_not_truncate_at_sixteen(self):
        self.feeds.write_text('{"sources":[{"name":"Show","type":"rss",'
                              '"rss_url":"https://publisher.example.com/feed"}]}')
        episodes = [Episode(
            f"rss:{i}", f"Unique title for episode {i}", f"https://publisher.example.com/{i}",
            "Show", published_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
            metadata={"rss_feed_url": "https://publisher.example.com/feed"},
        ) for i in range(20)]
        service = PodcastService(self.store, self.feeds, FakeSummarizer())
        with patch("news_officer.podcast.latest_rss_episodes", return_value=episodes) as fetch:
            candidates = service.discover_daily_candidates(datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual(len(candidates), 20)
        self.assertEqual(fetch.call_args.kwargs["limit"], 0)

    def test_deferred_digest_save_failure_isolated_and_next_candidate_backfills(self):
        self.feeds.write_text('{"sources": []}')
        candidates = [
            Episode(
                identifier,
                identifier,
                f"https://publisher.example.com/{identifier}",
                identifier,
                duration_seconds=3600,
                published_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
                metadata={"source_priority": priority},
            )
            for identifier, priority in (
                ("a1", "A"),
                ("a2", "A"),
                ("a3", "A"),
                ("a4", "A"),
                ("a5", "A"),
                ("b1", "B"),
            )
        ]
        resolver = TranscriptResolver([CompleteProvider()])
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            resolver,
            max_daily_summaries=4,
        )
        save_digest = self.store.save_transcript_digest

        def save_with_one_cas_failure(
            episode_id, digest, source_sha256, record_revision_sha256
        ):
            if episode_id == "a4":
                raise ValueError("Transcript source changed during digest generation")
            return save_digest(
                episode_id,
                digest,
                source_sha256,
                record_revision_sha256,
            )

        with (
            patch.object(service, "discover_daily_candidates", return_value=candidates),
            patch.object(
                resolver,
                "fetch",
                side_effect=lambda episode: (
                    None if episode.id == "b1" else CompleteProvider().fetch(episode)
                ),
            ),
            patch.object(
                self.store,
                "save_transcript_digest",
                side_effect=save_with_one_cas_failure,
            ),
        ):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))

        self.assertEqual(
            [item.episode.id for item in items if item.status == "summarized"],
            ["a1", "a2", "a3", "a5"],
        )
        self.assertEqual(
            [item.episode.id for item in items if item.status == "failed"],
            ["a4"],
        )

    def test_new_candidates_are_selected_before_due_retries(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one"]}'
        )
        retry_one = Episode("retry-1", "Retry 1", "https://youtu.be/retry-one", "Show")
        retry_two = Episode("retry-2", "Retry 2", "https://youtu.be/retry-two", "Show")
        new_one = Episode("new-1", "New 1", "https://youtu.be/new-one", "Show")
        new_two = Episode("new-2", "New 2", "https://youtu.be/new-two", "Show")
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            max_daily_candidates=2,
        )

        states = {
            retry_one.id: "retry",
            retry_two.id: "retry",
            new_one.id: "new",
            new_two.id: "new",
        }
        with (
            patch(
                "news_officer.podcast.latest_videos",
                return_value=[retry_one, retry_two, new_one, new_two],
            ),
            patch.object(
                self.store,
                "episode_review_state",
                side_effect=lambda episode_id: states[episode_id],
            ),
        ):
            candidates = service.discover_daily_candidates()

        self.assertEqual([episode.id for episode in candidates], ["new-1", "new-2"])

    def test_priority_tiers_keep_round_robin_source_fairness(self):
        sources = [
            {
                "name": "A one",
                "type": "youtube",
                "url": "https://www.youtube.com/@a-one",
                "priority": "A",
            },
            {
                "name": "A two",
                "type": "youtube",
                "url": "https://www.youtube.com/@a-two",
                "priority": "A",
            },
            {
                "name": "B one",
                "type": "youtube",
                "url": "https://www.youtube.com/@b-one",
                "priority": "B",
            },
        ]
        self.feeds.write_text(json.dumps({"sources": sources}))
        episodes = {
            sources[0]["url"]: [
                Episode(
                    "a1",
                    "A1",
                    "https://youtu.be/a1",
                    "A one",
                    duration_seconds=600,
                    published_at=datetime(2026, 9, 5, 5, tzinfo=UTC),
                ),
                Episode(
                    "a2",
                    "A2",
                    "https://youtu.be/a2",
                    "A one",
                    duration_seconds=600,
                    published_at=datetime(2026, 9, 5, 23, tzinfo=UTC),
                ),
            ],
            sources[1]["url"]: [
                Episode(
                    "x1",
                    "X1",
                    "https://youtu.be/x1",
                    "A two",
                    duration_seconds=600,
                    published_at=datetime(2026, 9, 5, 20, tzinfo=UTC),
                ),
                Episode(
                    "x2",
                    "X2",
                    "https://youtu.be/x2",
                    "A two",
                    duration_seconds=600,
                    published_at=datetime(2026, 9, 5, 21, tzinfo=UTC),
                ),
            ],
            sources[2]["url"]: [
                Episode(
                    "b1",
                    "B1",
                    "https://youtu.be/b1",
                    "B one",
                    duration_seconds=600,
                    published_at=datetime(2026, 9, 5, 22, tzinfo=UTC),
                )
            ],
        }
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            max_daily_candidates=5,
        )

        with patch(
            "news_officer.podcast.latest_videos",
            side_effect=lambda url, playlist_end=4: episodes[url],
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual(
            [candidate.id for candidate in candidates],
            ["x1", "a1", "b1", "x2", "a2"],
        )

        with patch(
            "news_officer.podcast.latest_videos",
            side_effect=lambda url, playlist_end=4: episodes[url],
        ):
            next_day = service.discover_daily_candidates(
                datetime(2026, 9, 7, tzinfo=UTC)
            )

        self.assertEqual(
            [candidate.id for candidate in next_day],
            ["a1", "x1", "b1", "a2", "x2"],
        )

        summarizing_service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            TranscriptResolver([CompleteProvider()]),
            max_daily_candidates=5,
            max_daily_summaries=3,
        )
        with patch(
            "news_officer.podcast.latest_videos",
            side_effect=lambda url, playlist_end=4: episodes[url],
        ):
            summarized = summarizing_service.build_daily(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual([item.status for item in summarized], ["summarized"] * 3)
        self.assertIn("B one", [item.episode.show for item in summarized])

    def test_old_dated_candidate_is_filtered_before_budget_and_new_beats_retry(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one"]}'
        )
        old = Episode(
            "old",
            "Old",
            "https://youtu.be/old",
            "Show",
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        retry = Episode(
            "retry",
            "Retry",
            "https://youtu.be/retry",
            "Show",
            published_at=datetime(2026, 9, 5, 20, tzinfo=UTC),
        )
        fresh = Episode(
            "fresh",
            "Fresh",
            "https://youtu.be/fresh",
            "Show",
            published_at=datetime(2026, 9, 5, 10, tzinfo=UTC),
        )
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            max_daily_candidates=1,
        )
        states = {"old": "new", "retry": "retry", "fresh": "new"}

        with (
            patch(
                "news_officer.podcast.latest_videos",
                return_value=[old, retry, fresh],
            ),
            patch.object(
                self.store,
                "episode_review_state",
                side_effect=lambda episode_id: states[episode_id],
            ),
        ):
            candidates = service.discover_daily_candidates(
                datetime(2026, 9, 6, tzinfo=UTC)
            )

        self.assertEqual([candidate.id for candidate in candidates], ["fresh"])

    def test_partial_metadata_enrichment_preserves_verified_feed_date(self):
        self.feeds.write_text(
            '{"youtube_channels": ["https://www.youtube.com/@one"]}'
        )
        published_at = datetime(2026, 9, 5, tzinfo=UTC)
        discovered = Episode(
            "kG8AoExkX40",
            "Sam Altman",
            "https://www.youtube.com/watch?v=kG8AoExkX40",
            "David Senra",
            published_at=published_at,
        )
        partial = Episode(
            discovered.id,
            discovered.title,
            discovered.url,
            discovered.show,
            published_at=None,
        )
        service = PodcastService(
            self.store,
            self.feeds,
            FakeSummarizer(),
            TranscriptResolver([MissingProvider()]),
        )
        with (
            patch.object(
                service, "discover_daily_candidates", return_value=[discovered]
            ),
            patch("news_officer.podcast.video_metadata", return_value=partial),
        ):
            items = service.build_daily(datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual(items[0].status, "no_transcript")
        self.assertEqual(items[0].episode.published_at, published_at)

    def test_youtube_source_requires_https(self):
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=x"))
        self.assertFalse(is_youtube_url("ftp://www.youtube.com/watch?v=x"))
        self.assertFalse(is_youtube_url("https://youtube.com.evil.test/watch?v=x"))


if __name__ == "__main__":
    unittest.main()
