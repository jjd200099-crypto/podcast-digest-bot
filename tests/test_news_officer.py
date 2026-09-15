import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.feishu import brand_message, split_message
from news_officer.models import Episode, IncomingMessage, Transcript
from news_officer.official import DwarkeshOfficialTranscriptProvider
from news_officer.podcast import (
    PodcastService,
    TranscriptResolver,
    YouTubeFeedSource,
    _interleave,
    load_youtube_sources,
)
from news_officer.router import (
    CommandRouter,
    HelpPlugin,
    PodcastPlugin,
    SubscriptionPlugin,
)
from news_officer.store import Store


class FakeSummarizer:
    def summarize(self, episode, transcript):
        return f"SUMMARY:{episode.id}:{transcript.source}"


class StaticTranscriptProvider:
    name = "static"

    def __init__(self, transcript):
        self.transcript = transcript
        self.calls = 0

    def fetch(self, episode):
        self.calls += 1
        return self.transcript


class NewsOfficerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "state.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_store_is_a_durable_idempotent_queue(self):
        self.assertTrue(self.store.enqueue("message:1", "message", {"text": "hello"}))
        self.assertFalse(
            self.store.enqueue("message:1", "message", {"text": "duplicate"})
        )
        job = self.store.claim_next()
        self.assertEqual(job.key, "message:1")
        self.assertEqual(job.payload["text"], "hello")
        self.assertEqual(self.store.job_status("message:1"), "processing")
        self.store.complete(job.key)
        self.assertEqual(self.store.job_status("message:1"), "completed")

    def test_store_recovers_a_job_interrupted_by_restart(self):
        self.store.enqueue("daily:2026-09-05", "daily", {})
        self.store.claim_next()
        self.assertEqual(self.store.recover_interrupted_jobs(), 1)
        recovered = self.store.claim_next()
        self.assertEqual(recovered.key, "daily:2026-09-05")
        self.assertEqual(recovered.attempts, 2)

    def test_missing_transcript_is_retryable_but_sent_episode_is_final(self):
        episode = Episode("episode-1", "Title", "https://youtu.be/x", "Show")
        self.store.record_episode(episode, "no_transcript")
        self.assertFalse(
            self.store.should_review_episode(
                episode.id, no_transcript_retry_hours=6
            )
        )
        self.assertTrue(
            self.store.should_review_episode(
                episode.id, no_transcript_retry_hours=0
            )
        )
        self.store.record_episode(episode, "sent")
        self.assertFalse(
            self.store.should_review_episode(
                episode.id, no_transcript_retry_hours=0
            )
        )

    def test_unverified_date_is_retryable_after_the_same_cooldown(self):
        episode = Episode("episode-undated", "Title", "https://youtu.be/x", "Show")
        self.store.record_episode(episode, "unverified_date")

        self.assertFalse(
            self.store.should_review_episode(
                episode.id, no_transcript_retry_hours=6
            )
        )
        self.assertEqual(
            self.store.episode_review_state(
                episode.id, no_transcript_retry_hours=0
            ),
            "retry",
        )

    def test_environment_seeds_do_not_revive_an_explicit_opt_out(self):
        self.assertEqual(
            self.store.seed_subscriptions(("ou_seed",), ("oc_seed",)), 2
        )
        self.assertEqual(
            self.store.list_subscriptions(),
            [("chat_id", "oc_seed"), ("open_id", "ou_seed")],
        )
        self.assertTrue(self.store.remove_subscription("open_id", "ou_seed"))

        reopened = Store(self.store.path)
        reopened.initialize()
        self.assertEqual(
            reopened.seed_subscriptions(("ou_seed",), ("oc_seed",)), 0
        )
        self.assertEqual(reopened.list_subscriptions(), [("chat_id", "oc_seed")])

    def test_opt_out_before_first_subscription_blocks_future_environment_seed(self):
        self.assertFalse(self.store.remove_subscription("open_id", "ou_later"))
        self.assertEqual(self.store.seed_subscriptions(("ou_later",), ()), 0)
        self.assertEqual(self.store.list_subscriptions(), [])

    def test_private_and_group_subscription_commands_use_the_current_conversation(self):
        plugin = SubscriptionPlugin(self.store)
        private = IncomingMessage(
            message_id="om_private",
            chat_id="oc_private",
            text="订阅",
            chat_type="p2p",
            sender_open_id="ou_user",
        )
        group = IncomingMessage(
            message_id="om_group",
            chat_id="oc_group",
            text="<at user_id=\"bot\">情报官</at> 订阅",
            chat_type="group",
            sender_open_id="ou_member",
        )

        self.assertTrue(plugin.matches(group.text))
        self.assertIn("订阅成功", plugin.handle("订阅", private).messages[0])
        self.assertIn("订阅成功", plugin.handle(group.text, group).messages[0])
        self.assertEqual(
            self.store.list_subscriptions(),
            [("chat_id", "oc_group"), ("open_id", "ou_user")],
        )
        self.assertIn("已经订阅", plugin.handle("订阅", private).messages[0])
        self.assertIn("退订成功", plugin.handle("退订", private).messages[0])
        self.assertEqual(self.store.list_subscriptions(), [("chat_id", "oc_group")])

    def test_feed_candidates_are_interleaved(self):
        def episode(identifier):
            return Episode(
                identifier, identifier, f"https://youtu.be/{identifier}", identifier[0]
            )

        ordered = _interleave(
            [[episode("a1"), episode("a2")], [episode("b1"), episode("b2")]]
        )
        self.assertEqual([item.id for item in ordered], ["a1", "b1", "a2", "b2"])

    def test_feed_source_can_filter_a_broad_channel_by_topic(self):
        source = YouTubeFeedSource(
            "https://www.youtube.com/channel/example/videos",
            ("AI", "data center", "software"),
            20,
        )
        self.assertTrue(
            source.accepts(Episode("1", "The AI investment boom", "https://x", "x"))
        )
        self.assertTrue(
            source.accepts(
                Episode("2", "Financing a new data center", "https://x", "x")
            )
        )
        self.assertFalse(
            source.accepts(Episode("3", "Said rates may fall", "https://x", "x"))
        )

    def test_feed_source_priority_is_loaded_and_invalid_values_fall_back(self):
        feeds = Path(self.temp_dir.name) / "priorities.json"
        feeds.write_text(
            '{"sources": ['
            '{"type": "youtube", "url": "https://youtu.be/a", "priority": "a"},'
            '{"type": "rss", "rss_url": "https://example.test/rss", '
            '"priority": "urgent"}'
            ']}'
        )

        sources = load_youtube_sources(feeds)

        self.assertEqual([source.priority for source in sources], ["A", "B"])

    def test_transcript_resolver_rejects_unverified_text_and_falls_through(self):
        incomplete = StaticTranscriptProvider(
            Transcript("partial", "official", "https://example.test", False)
        )
        complete = StaticTranscriptProvider(
            Transcript("complete", "captions", "https://youtu.be/x", True)
        )
        result = TranscriptResolver([incomplete, complete]).fetch(
            Episode("x", "x", "https://youtu.be/x", "show")
        )
        self.assertEqual(result.text, "complete")
        self.assertEqual(incomplete.calls, 1)
        self.assertEqual(complete.calls, 1)

    def test_interactive_analysis_never_summarizes_without_a_complete_transcript(self):
        feeds = Path(self.temp_dir.name) / "feeds.json"
        feeds.write_text('{"youtube_channels": []}')
        provider = StaticTranscriptProvider(None)
        service = PodcastService(
            self.store,
            feeds,
            FakeSummarizer(),
            TranscriptResolver([provider]),
        )
        episode = Episode(
            "x",
            "Test episode",
            "https://www.youtube.com/watch?v=x",
            "Test show",
            600,
            "10:00",
            datetime(2026, 9, 5, tzinfo=UTC),
        )
        with patch("news_officer.podcast.video_metadata", return_value=episode):
            result = service.analyze_url(episode.url)
        self.assertEqual(result.status, "no_transcript")
        self.assertIn("未取得完整文字稿，本次不摘要", result.message)

    def test_plugins_are_selected_without_coupling_to_feishu(self):
        feeds = Path(self.temp_dir.name) / "feeds.json"
        feeds.write_text('{"youtube_channels": []}')
        service = PodcastService(self.store, feeds, FakeSummarizer())
        podcast = PodcastPlugin(service)
        subscriptions = SubscriptionPlugin(self.store)
        help_plugin = HelpPlugin()
        router = CommandRouter([subscriptions, podcast, help_plugin])
        self.assertIs(router.select("订阅"), subscriptions)
        self.assertIs(router.select("请分析 https://youtu.be/x"), podcast)
        self.assertIs(router.select("你能做什么"), help_plugin)
        help_message = IncomingMessage("om", "oc", "帮助", "p2p", "ou")
        self.assertIn("退订", help_plugin.handle("帮助", help_message).messages[0])

    def test_feishu_chunks_leave_space_for_part_suffix(self):
        chunks = split_message(brand_message("洞察" * 3000), max_bytes=500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            suffix = "\n\n（第 99/99 段）"
            self.assertLessEqual(len((chunk + suffix).encode("utf-8")), 500)

    def test_dwarkesh_adapter_requires_an_explicit_full_transcript_section(self):
        from bs4 import BeautifulSoup

        provider = DwarkeshOfficialTranscriptProvider()
        valid = BeautifulSoup(
            '<div class="body markup"><h2>Transcript</h2>'
            "<h3>00:00:00 – Start</h3><p>Host</p><p>Opening</p>"
            "<h3>00:58:00 – End</h3><p>Guest</p><p>Closing</p></div>",
            "html.parser",
        )
        extracted = provider._extract_transcript(valid)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted[1], [0, 3480])

        locked = BeautifulSoup(
            '<div class="body markup"><h2>Transcript</h2>'
            "<p>Access the full transcript — sign in</p></div>",
            "html.parser",
        )
        self.assertIsNone(provider._extract_transcript(locked))


if __name__ == "__main__":
    unittest.main()
