import json
import sys
import tempfile
import time
import unittest
from datetime import UTC
from datetime import time as clock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.feishu import FeishuMessenger, delivery_parts
from news_officer.models import (
    DailyItem,
    Episode,
    IncomingMessage,
    OutboxItem,
    Transcript,
    TranscriptAttachment,
)
from news_officer.router import PluginResponse
from news_officer.runtime import NewsOfficerRuntime
from news_officer.store import Store
from news_officer.transcript_view import RENDERER_VERSION, render_readable_transcript


def attachment_for(record, digest_markdown):
    filename, content = render_readable_transcript(
        record, digest_markdown=digest_markdown
    )
    return TranscriptAttachment.from_rendered(
        record,
        digest_markdown=digest_markdown,
        renderer_version=RENDERER_VERSION,
        filename=filename,
        content=content,
    )


class FakePlugin:
    def __init__(self):
        self.calls = 0
        self.last_message = None

    def acknowledgement(self, text):
        return "ACK"

    def handle(self, text, message):
        self.calls += 1
        self.last_message = message
        return PluginResponse((f"RESULT-{self.calls}",))


class FakeRouter:
    def __init__(self, plugin):
        self.plugin = plugin

    def select(self, text):
        return self.plugin


class FakeMessenger:
    def __init__(self):
        self.attempts = []
        self.delivered = {}
        self.fail_groups_once = set()
        self.uploads = []

    def upload_file(self, content, filename):
        self.uploads.append((content, filename))
        return f"file_{len(self.uploads)}"

    def deliver(self, item):
        self.attempts.append(item)
        # Model a response being lost after Feishu accepted the immutable UUID.
        self.delivered.setdefault(item.uuid, item)
        if item.group_key in self.fail_groups_once:
            self.fail_groups_once.remove(item.group_key)
            raise ConnectionError("response lost after delivery")


class SequencePodcast:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls = 0

    def build_daily(self):
        index = min(self.calls, len(self.batches) - 1)
        self.calls += 1
        return list(self.batches[index])


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"code": 0}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def runtime(store, messenger, podcast, plugin=None):
    store.add_subscription("open_id", "ou_test", source="test")
    instance = object.__new__(NewsOfficerRuntime)
    instance.store = store
    instance.messenger = messenger
    instance.podcast_service = podcast
    instance.router = FakeRouter(plugin or FakePlugin())
    instance.settings = SimpleNamespace(
        user_open_ids=("ou_test",),
        group_chat_ids=(),
    )
    return instance


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "news-officer.sqlite3"
        self.store = Store(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_catchup_does_not_send_repeated_empty_or_pending_notices(self):
        messenger = FakeMessenger()
        podcast = MagicMock()
        podcast.build_pending.return_value = [DailyItem(Episode('later', 'Pending', 'https://example.org', 'Show'),
                                                      'no_transcript', '正在转写')]
        instance = runtime(self.store, messenger, podcast)
        self.store.enqueue('daily:catchup:test', 'daily', {'transcript_catchup': True})
        job = self.store.claim_next('daily')
        await instance._handle_daily_job(job)
        podcast.build_pending.assert_called_once()
        podcast.build_daily.assert_not_called()
        self.assertEqual(messenger.attempts, [])
        self.assertTrue(self.store.analysis_complete(job.key))

    async def test_persisted_rating_order_and_catchup_delivery_remain_idempotent(self):
        from datetime import datetime, timedelta

        items = []
        for identity, stars in [('low', '★☆☆☆☆'), ('high', '★★★★★')]:
            episode = Episode(identity, identity, 'https://example.org/'+identity, 'Show',
                              published_at=datetime.now(UTC)-timedelta(days=2))
            record = self.store.save_verified_transcript(episode, Transcript('full '+identity, 'official', 'https://example.org/t', True))
            summary = f'推荐理由：这是可核验的节目内容。\n\n1. 来自完整文字稿的要点。\n\n推荐星级：{stars}'
            self.store.save_transcript_digest(identity, summary, record.content_sha256, record.record_revision_sha256)
            items.append(DailyItem(episode, 'summarized', summary, attachment_for(record, summary)))
            self.store.defer_daily_transcript(episode, 'waiting')
        podcast = MagicMock()
        podcast.build_pending.return_value = items
        messenger = FakeMessenger()
        instance = runtime(self.store, messenger, podcast)
        self.store.enqueue('daily:catchup:ordered', 'daily', {'transcript_catchup': True})
        job = self.store.claim_next('daily')
        await instance._handle_daily_job(job)
        self.assertEqual([i.group_key for i in messenger.attempts], ['episode:high', 'episode:low'])
        self.assertTrue(all(i.target_type == 'open_id' and i.target_id == 'ou_test' for i in messenger.attempts))
        await instance._handle_daily_job(job)
        self.assertEqual(len(messenger.attempts), 2)
        self.assertTrue(all(self.store.episode_is_delivered(i.episode) for i in items))
        with self.store._connect() as db:
            self.assertEqual({r[0] for r in db.execute('SELECT state FROM daily_transcript_backlog')}, {'delivered'})

    async def test_long_takeaways_are_split_between_complete_numbered_lines(self):
        takeaways = [
            f"{number}. 洞察 {number}：" + ("高密度内容" * 40)
            for number in range(1, 11)
        ]

        parts = delivery_parts("\n".join(takeaways), "ten-takeaways")
        rendered_lines: list[str] = []
        for _msg_type, content, _uuid in parts:
            post = json.loads(content)["zh_cn"]["content"]
            rendered_lines.extend(
                "".join(element.get("text", "") for element in paragraph)
                for paragraph in post
            )
        rendered = "\n".join(rendered_lines)

        self.assertGreater(len(parts), 1)
        for takeaway in takeaways:
            self.assertEqual(rendered.count(takeaway), 1)

    async def test_message_result_and_thread_outbox_survive_restart(self):
        plugin = FakePlugin()
        messenger = FakeMessenger()
        messenger.fail_groups_once.add("message:result:1")
        self.store.enqueue(
            "message:om_1",
            "message",
            {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "thread_id": "omt_1",
                "text": "analyze",
            },
        )
        first = self.store.claim_next("message")

        with self.assertRaisesRegex(RuntimeError, "outbox delivery group"):
            await runtime(
                self.store, messenger, SequencePodcast([]), plugin
            )._handle_message_job(first)
        self.assertEqual(plugin.calls, 1)

        reopened = Store(self.db_path)
        reopened.initialize()
        self.assertEqual(reopened.recover_interrupted_jobs(), 1)
        retry = reopened.claim_next("message")
        await runtime(
            reopened, messenger, SequencePodcast([]), plugin
        )._handle_message_job(retry)
        reopened.complete(retry.key)

        self.assertEqual(plugin.calls, 1, "persisted result must not call the model twice")
        self.assertEqual(
            plugin.last_message,
            IncomingMessage(
                message_id="om_1",
                chat_id="oc_1",
                text="analyze",
                chat_type="",
                sender_open_id="",
                thread_id="omt_1",
            ),
        )
        items = reopened.outbox_items(retry.key)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item.reply_in_thread for item in items))
        self.assertTrue(all(item.msg_type == "post" for item in items))
        self.assertEqual(reopened.job_status(retry.key), "completed")
        result_attempts = [
            item for item in messenger.attempts if item.group_key == "message:result:1"
        ]
        self.assertEqual(len(result_attempts), 2)
        self.assertEqual(result_attempts[0].uuid, result_attempts[1].uuid)
        self.assertEqual(result_attempts[0].content, result_attempts[1].content)

    async def test_inbound_thread_id_is_persisted_before_main_loop_wake(self):
        instance = runtime(self.store, FakeMessenger(), SequencePodcast([]))
        instance.wake_workers = {"message": MagicMock()}
        instance._main_loop = MagicMock()
        instance._main_loop.is_closed.return_value = False
        message = SimpleNamespace(
            sender_is_bot=False,
            message_id="om_thread",
            chat_id="oc_thread",
            body_text="summarize this",
            content_text="summarize this",
            chat_type="topic",
            sender_id="ou_sender",
            conversation=SimpleNamespace(thread_id="omt_thread"),
        )

        await instance._on_message(message)

        job = self.store.claim_next("message")
        self.assertEqual(job.payload["thread_id"], "omt_thread")
        instance._main_loop.call_soon_threadsafe.assert_called_once_with(
            instance.wake_workers["message"].set
        )

    async def test_daily_crash_after_record_never_sends_false_empty(self):
        episode = Episode("ep-1", "Episode", "https://youtu.be/x", "Show")
        stored = self.store.save_verified_transcript(
            episode,
            Transcript("complete transcript", "test", "https://example.test", True),
        )
        self.store.save_transcript_digest(
            episode.id,
            "SUMMARY",
            stored.content_sha256,
            stored.record_revision_sha256,
        )
        podcast = SequencePodcast(
            [
                DailyItem(
                    episode,
                    "summarized",
                    "SUMMARY",
                    attachment_for(stored, "SUMMARY"),
                )
            ]
        )
        messenger = FakeMessenger()
        self.store.enqueue("daily:2026-09-05", "daily", {})
        first = self.store.claim_next("daily")
        await runtime(self.store, messenger, podcast)._handle_daily_job(first)
        self.assertTrue(self.store.has_episode(episode.id))
        # Crash before Store.complete().

        reopened = Store(self.db_path)
        reopened.initialize()
        self.assertEqual(reopened.recover_interrupted_jobs(), 1)
        retry = reopened.claim_next("daily")
        await runtime(reopened, messenger, podcast)._handle_daily_job(retry)
        reopened.complete(retry.key)

        self.assertEqual(podcast.calls, 1)
        groups = {item.group_key for item in messenger.delivered.values()}
        self.assertIn("episode:ep-1", groups)
        self.assertNotIn("daily:empty", groups)

    async def test_failed_daily_can_requeue_without_resending_saved_output(self):
        sent = Episode("ep-sent", "Sent", "https://youtu.be/s", "Show")
        failed = Episode("ep-failed", "Failed", "https://youtu.be/f", "Show")
        stored = self.store.save_verified_transcript(
            sent,
            Transcript("complete transcript", "test", "https://example.test", True),
        )
        self.store.save_transcript_digest(
            sent.id,
            "SUMMARY",
            stored.content_sha256,
            stored.record_revision_sha256,
        )
        podcast = SequencePodcast(
            [
                DailyItem(
                    sent,
                    "summarized",
                    "SUMMARY",
                    attachment_for(stored, "SUMMARY"),
                ),
                DailyItem(failed, "failed", "temporary outage"),
            ],
            [],
        )
        messenger = FakeMessenger()
        key = "daily:2026-09-06"
        self.store.enqueue(key, "daily", {})
        first = self.store.claim_next("daily")
        with self.assertRaises(RuntimeError):
            await runtime(self.store, messenger, podcast)._handle_daily_job(first)
        self.store.fail(
            first.key,
            "temporary outage",
            first.attempts,
            max_attempts=1,
            failed_daily_requeue_seconds=0,
        )
        self.assertEqual(self.store.job_status(key), "failed")
        self.assertTrue(self.store.enqueue(key, "daily", {"ignored": "immutable"}))

        retry = self.store.claim_next("daily")
        await runtime(self.store, messenger, podcast)._handle_daily_job(retry)
        self.store.complete(retry.key)

        summary_deliveries = [
            item for item in messenger.delivered.values() if item.group_key == "episode:ep-sent"
        ]
        self.assertEqual(
            [item.msg_type for item in summary_deliveries], ["post"]
        )
        self.assertEqual(podcast.calls, 2)
        self.assertIn(
            "daily:candidate-failure",
            {item.group_key for item in messenger.delivered.values()},
        )
        self.assertNotIn(
            "daily:empty", {item.group_key for item in messenger.delivered.values()}
        )

    async def test_unverified_date_is_a_retryable_skip_state(self):
        episode = Episode("ep-undated", "Undated", "https://youtu.be/u", "Show")
        podcast = SequencePodcast([DailyItem(episode, "unverified_date")])
        messenger = FakeMessenger()
        key = "daily:2026-09-07"
        self.store.enqueue(key, "daily", {})
        job = self.store.claim_next("daily")
        await runtime(self.store, messenger, podcast)._handle_daily_job(job)
        self.store.complete(job.key)

        self.assertTrue(
            self.store.should_review_episode(episode.id, no_transcript_retry_hours=0)
        )
        self.assertIn(
            "daily:coverage", {item.group_key for item in messenger.delivered.values()}
        )

    async def test_daily_source_failure_sends_warning_and_remains_retryable(self):
        podcast = MagicMock()
        podcast.build_daily.side_effect = TimeoutError("source unavailable")
        messenger = FakeMessenger()
        key = "daily:2026-09-08"
        self.store.enqueue(key, "daily", {})
        job = self.store.claim_next("daily")

        with self.assertRaisesRegex(TimeoutError, "source unavailable"):
            await runtime(self.store, messenger, podcast)._handle_daily_job(job)

        groups = {item.group_key for item in messenger.delivered.values()}
        self.assertIn("daily:scan-failure", groups)
        self.assertNotIn("daily:empty", groups)
        self.assertFalse(self.store.analysis_complete(key))

    async def test_daily_source_warning_is_idempotent_across_retries(self):
        podcast = MagicMock()
        podcast.build_daily.side_effect = TimeoutError("source unavailable")
        messenger = FakeMessenger()
        key = "daily:2026-09-09"
        self.store.enqueue(key, "daily", {})
        first = self.store.claim_next("daily")
        instance = runtime(self.store, messenger, podcast)

        with self.assertRaises(TimeoutError):
            await instance._handle_daily_job(first)
        self.store.fail(
            key,
            "source unavailable",
            first.attempts,
            max_attempts=1,
            failed_daily_requeue_seconds=0,
        )
        self.assertTrue(self.store.enqueue(key, "daily", {}))
        retry = self.store.claim_next("daily")
        with self.assertRaises(TimeoutError):
            await instance._handle_daily_job(retry)

        warnings = [
            item
            for item in messenger.attempts
            if item.group_key == "daily:scan-failure"
        ]
        self.assertEqual(len(warnings), 1)

    async def test_daily_job_without_subscribers_does_not_call_podcast_service(self):
        podcast = SequencePodcast([])
        instance = runtime(self.store, FakeMessenger(), podcast)
        self.store.remove_subscription("open_id", "ou_test")
        self.store.enqueue("daily:nobody", "daily", {})
        job = self.store.claim_next("daily")

        await instance._handle_daily_job(job)
        self.store.complete(job.key)

        self.assertEqual(podcast.calls, 0)
        self.assertTrue(self.store.analysis_complete(job.key))
        self.assertEqual(self.store.outbox_items(job.key), [])

    def test_outbox_is_immutable_and_persists_every_part(self):
        self.store.enqueue("message:1", "message", {"text": "x"})
        parts = delivery_parts("# Title\n\nRead [source](https://example.com)", "fixed")
        kwargs = {
            "job_key": "message:1",
            "group_key": "result",
            "delivery_key": "fixed",
            "operation": "reply",
            "target_id": "om_1",
            "target_type": "",
            "reply_in_thread": True,
            "parts": parts,
        }
        self.store.ensure_outbox(**kwargs)
        self.store.ensure_outbox(**kwargs)
        reopened = Store(self.db_path)
        reopened.initialize()
        items = reopened.outbox_items("message:1")
        self.assertEqual(len(items), len(parts))
        self.assertTrue(all(item.msg_type == "post" for item in items))
        post = json.loads(items[0].content)
        self.assertIn("zh_cn", post)

        conflicting = dict(kwargs)
        conflicting["parts"] = [("post", "different", parts[0][2])]
        with self.assertRaises(RuntimeError):
            reopened.ensure_outbox(**conflicting)

    def test_message_and_daily_workers_claim_independently(self):
        self.store.enqueue("daily:old", "daily", {})
        self.store.enqueue("message:new", "message", {"message_id": "om_1"})
        message = self.store.claim_next("message")
        daily = self.store.claim_next("daily")
        self.assertEqual(message.kind, "message")
        self.assertEqual(daily.kind, "daily")

    def test_broadcast_outbox_is_frozen_per_target_and_part(self):
        self.store.enqueue("daily:many", "daily", {})
        job = self.store.claim_next("daily")
        instance = runtime(self.store, FakeMessenger(), SequencePodcast([]))
        self.store.remove_subscription("open_id", "ou_test")
        self.store.add_subscription("open_id", "ou_1")
        self.store.add_subscription("open_id", "ou_2")
        self.store.add_subscription("chat_id", "oc_1")
        markdown = "# Long update\n\n" + ("洞察" * 5000)
        instance._ensure_broadcast(job, "episode:x", markdown, "daily:x")
        first = self.store.outbox_items(job.key)
        self.store.add_subscription("open_id", "ou_too_late")
        instance._ensure_broadcast(job, "episode:x", markdown, "daily:x")
        second = self.store.outbox_items(job.key)

        self.assertGreater(len(first), 3)
        self.assertEqual(first, second)
        self.assertEqual({item.target_id for item in first}, {"ou_1", "ou_2", "oc_1"})
        self.assertEqual(len({item.uuid for item in first}), len(first))

    async def test_bad_recipient_does_not_block_other_broadcast_targets(self):
        class OneBadRecipient(FakeMessenger):
            def deliver(self, item):
                self.attempts.append(item)
                if item.target_id == "ou_bad":
                    raise PermissionError("recipient is invalid")
                self.delivered.setdefault(item.uuid, item)

        self.store.enqueue("daily:targets", "daily", {})
        job = self.store.claim_next("daily")
        messenger = OneBadRecipient()
        instance = runtime(self.store, messenger, SequencePodcast([]))
        self.store.remove_subscription("open_id", "ou_test")
        self.store.add_subscription("open_id", "ou_bad")
        self.store.add_subscription("open_id", "ou_good")
        self.store.add_subscription("chat_id", "oc_good")
        instance._ensure_broadcast(job, "episode:x", "SUMMARY", "daily:x")

        with self.assertRaisesRegex(RuntimeError, "delivery group"):
            await instance._drain_outbox(job)

        delivered_targets = {item.target_id for item in messenger.delivered.values()}
        self.assertEqual(delivered_targets, {"ou_good", "oc_good"})
        pending_targets = {
            item.target_id for item in self.store.pending_outbox(job.key)
        }
        self.assertEqual(pending_targets, {"ou_bad"})


class FeishuDeliveryTests(unittest.TestCase):
    def item(self, *, reply_in_thread=False):
        msg_type, content, item_uuid = delivery_parts("# Update\n\nBody", "key")[0]
        return OutboxItem(
            id=1,
            job_key="message:1",
            group_key="result",
            delivery_key="key",
            operation="reply",
            target_id="om_1",
            target_type="",
            reply_in_thread=reply_in_thread,
            part=1,
            total_parts=1,
            msg_type=msg_type,
            content=content,
            uuid=item_uuid,
        )

    @patch("news_officer.feishu.requests.post")
    def test_401_refreshes_token_and_thread_reply_uses_post(self, post):
        send_count = 0

        def response(url, **kwargs):
            nonlocal send_count
            if url.endswith("tenant_access_token/internal"):
                return FakeResponse(
                    body={"code": 0, "tenant_access_token": "fresh", "expire": 7200}
                )
            send_count += 1
            return FakeResponse(status_code=401 if send_count == 1 else 200)

        post.side_effect = response
        messenger = FeishuMessenger("app", "secret")
        messenger._token = "expired"
        messenger._token_expires_at = time.monotonic() + 1000
        messenger.deliver(self.item(reply_in_thread=True))

        message_calls = [
            call for call in post.call_args_list if "/im/v1/messages/" in call.args[0]
        ]
        self.assertEqual(len(message_calls), 2)
        self.assertEqual(
            message_calls[0].kwargs["headers"]["Authorization"], "Bearer expired"
        )
        self.assertEqual(
            message_calls[1].kwargs["headers"]["Authorization"], "Bearer fresh"
        )
        payload = message_calls[1].kwargs["json"]
        self.assertEqual(payload["msg_type"], "post")
        self.assertTrue(payload["reply_in_thread"])
        self.assertIn("zh_cn", json.loads(payload["content"]))

    @patch("news_officer.feishu.time.sleep")
    @patch("news_officer.feishu.requests.post")
    def test_429_and_5xx_honor_retry_after(self, post, sleep):
        post.side_effect = [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(503),
            FakeResponse(200),
        ]
        messenger = FeishuMessenger("app", "secret")
        messenger._token = "token"
        messenger._token_expires_at = time.monotonic() + 1000
        messenger.deliver(self.item())

        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 1.0])


class RuntimeSupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_exits_if_channel_stops(self):
        class StoppedChannel:
            disconnected = False

            async def connect_until_ready(self, *, timeout):
                return None

            def connection_snapshot(self):
                return SimpleNamespace(ready=False)

            async def disconnect(self):
                self.disconnected = True

        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state.sqlite3")
            instance = object.__new__(NewsOfficerRuntime)
            instance.store = store
            instance.messenger = FakeMessenger()
            instance.router = FakeRouter(FakePlugin())
            instance.podcast_service = SequencePodcast([])
            instance.research_agent = None
            instance.wake_workers = {
                "message": __import__("asyncio").Event(),
                "daily": __import__("asyncio").Event(),
            }
            instance._main_loop = None
            instance.settings = SimpleNamespace(
                timezone=UTC,
                daily_time=clock(23, 59),
                user_open_ids=(),
                group_chat_ids=(),
            )
            instance.channel = StoppedChannel()

            with self.assertRaisesRegex(RuntimeError, "feishu-channel"):
                await instance.run()
            self.assertTrue(instance.channel.disconnected)


class RuntimeChannelSafetyTests(unittest.TestCase):
    @patch("news_officer.runtime.FeishuChannel")
    def test_sdk_batching_and_busy_chat_merging_are_disabled(self, channel):
        NewsOfficerRuntime(
            SimpleNamespace(feishu_app_id="app", feishu_app_secret="secret"),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )

        safety = channel.call_args.kwargs["safety"]
        self.assertEqual(safety.text_batch.delay_ms, 0)
        self.assertEqual(safety.text_batch.long_delay_ms, 0)
        self.assertEqual(safety.text_batch.max_messages, 1)
        self.assertFalse(safety.chat_queue.enabled)
        self.assertFalse(safety.chat_queue.merge_while_busy)
        policy = channel.call_args.kwargs["policy"]
        self.assertEqual(policy.dm_policy, "open")
        self.assertEqual(policy.group_policy, "open")
        self.assertTrue(policy.require_mention)

    def test_background_callback_wakes_worker_on_main_loop(self):
        instance = object.__new__(NewsOfficerRuntime)
        event = MagicMock()
        loop = MagicMock()
        loop.is_closed.return_value = False
        instance.wake_workers = {"message": event}
        instance._main_loop = loop

        instance._wake_worker("message")

        loop.call_soon_threadsafe.assert_called_once_with(event.set)
        event.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
