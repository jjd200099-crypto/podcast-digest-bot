import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.feishu import delivery_parts, file_delivery_part
from news_officer.models import DailyItem, Episode, IncomingMessage, Transcript
from news_officer.router import (
    PluginResponse,
    TranscriptInteractionPlugin,
    conversation_key,
)
from news_officer.runtime import NewsOfficerRuntime
from news_officer.store import Store


class RecordingQA:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def answer(self, record, question):
        self.calls.append((record.episode.id, question))
        if self.error:
            raise self.error
        return f"ANSWER:{record.episode.id}:{question}"


class TranscriptFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = Store(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def archive(
        self,
        episode_id,
        title,
        text,
        *,
        published_at=None,
        show="Test Show",
    ):
        episode = Episode(
            episode_id,
            title,
            f"https://example.test/{episode_id}",
            show,
            3600,
            "1:00:00",
            published_at,
        )
        transcript = Transcript(
            text,
            "official",
            f"https://example.test/{episode_id}/transcript",
            True,
        )
        return self.store.save_verified_transcript(episode, transcript)


class TranscriptStoreTests(TranscriptFixture):
    def test_archive_roundtrip_search_context_snapshot_and_remote_mapping(self):
        older = self.archive(
            "ep-older",
            "AI_100% with Jensen",
            "黄仁勋谈到算力供给。",
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        newer = self.archive(
            "ep-newer",
            "Capital allocation",
            "黄仁勋也讨论了资本开支。",
            published_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        with self.assertRaisesRegex(ValueError, "verified complete"):
            self.store.save_verified_transcript(
                older.episode,
                Transcript("partial", "captions", "https://x", False),
            )

        reopened = Store(self.db_path)
        reopened.initialize()
        restored = reopened.get_verified_transcript(older.reference)
        self.assertEqual(restored.episode, older.episode)
        self.assertEqual(restored.transcript.text, older.transcript.text)
        self.assertEqual(restored.content_sha256, older.content_sha256)
        self.assertEqual(
            [record.episode.id for record in reopened.search_verified_transcripts("%")],
            ["ep-older"],
        )
        self.assertEqual(
            [record.episode.id for record in reopened.search_verified_transcripts("_")],
            ["ep-older"],
        )
        self.assertEqual(
            [record.episode.id for record in reopened.list_recent_transcripts(2)],
            ["ep-newer", "ep-older"],
        )

        reopened.save_conversation_context(
            "p2p:ou",
            episode_id=older.episode.id,
            pending_episode_ids=(older.episode.id, newer.episode.id),
            pending_question="问题",
            pending_action="qa",
        )
        reopened.save_recent_transcript_snapshot(
            "p2p:ou", (newer.episode.id, older.episode.id)
        )
        context = reopened.get_conversation_context("p2p:ou")
        self.assertEqual(context["episode_id"], older.episode.id)
        self.assertEqual(
            context["pending_episode_ids"],
            (older.episode.id, newer.episode.id),
        )
        self.assertEqual(
            context["recent_episode_ids"],
            (newer.episode.id, older.episode.id),
        )

        reopened.enqueue("daily:mapping", "daily", {})
        job = reopened.claim_next("daily")
        parts = delivery_parts("summary", "daily:older")
        parts.append(file_delivery_part("file-key", "daily:older", 2))
        reopened.ensure_outbox(
            job_key=job.key,
            group_key="episode:ep-older",
            delivery_key="daily:older",
            operation="send",
            target_id="ou",
            target_type="open_id",
            reply_in_thread=False,
            parts=parts,
        )
        for index, item in enumerate(reopened.outbox_items(job.key), start=1):
            reopened.mark_outbox_sent(item.id, f"om_{index}")
        self.assertEqual(reopened.episode_for_remote_message("om_1"), "ep-older")
        self.assertEqual(reopened.episode_for_remote_message("om_2"), "ep-older")


class TranscriptRouterTests(TranscriptFixture):
    def message(self, text, **overrides):
        values = {
            "message_id": "om",
            "chat_id": "oc",
            "text": text,
            "chat_type": "p2p",
            "sender_open_id": "ou",
        }
        values.update(overrides)
        return IncomingMessage(**values)

    def test_natural_query_ambiguity_choice_and_attachment(self):
        first = self.archive(
            "ep-a",
            "Jensen interview A",
            "黄仁勋认为需求仍会增长。",
            published_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        second = self.archive(
            "ep-b",
            "Jensen interview B",
            "主持人与黄仁勋讨论供给。",
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa)
        prompt = "整理黄仁勋的观点"

        ambiguous = plugin.handle(prompt, self.message(prompt))
        self.assertIn("找到多期", ambiguous.messages[0])
        self.assertEqual(qa.calls, [])
        chosen = plugin.handle("选 2", self.message("选 2"))
        self.assertEqual(chosen.context_episode_id, second.episode.id)
        self.assertEqual(qa.calls, [(second.episode.id, prompt)])
        context = self.store.get_conversation_context("p2p:ou")
        self.assertEqual(context["pending_episode_ids"], ())

        attachment = plugin.handle(
            f"文字稿 {first.reference}",
            self.message(f"文字稿 {first.reference}"),
        )
        self.assertEqual(attachment.attachment_episode_ids, (first.episode.id,))
        self.assertEqual(attachment.context_episode_id, first.episode.id)

    def test_parent_reply_is_strongest_and_invalid_selector_never_uses_old_context(self):
        parent = self.archive("ep-parent", "Parent", "OpenAI 与竞争格局。")
        other = self.archive("ep-other", "Other", "OpenAI 与资本开支。")
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa)
        self.store.save_conversation_context("p2p:ou", episode_id=other.episode.id)
        self.store.enqueue("daily:parent", "daily", {})
        job = self.store.claim_next("daily")
        self.store.ensure_outbox(
            job_key=job.key,
            group_key=f"episode:{parent.episode.id}",
            delivery_key="parent",
            operation="send",
            target_id="ou",
            target_type="open_id",
            reply_in_thread=False,
            parts=delivery_parts("summary", "parent"),
        )
        item = self.store.outbox_items(job.key)[0]
        self.store.mark_outbox_sent(item.id, "om_parent")

        question = "OpenAI 为什么会赢？"
        response = plugin.handle(
            question,
            self.message(question, parent_message_id="om_parent"),
        )
        self.assertEqual(response.context_episode_id, parent.episode.id)
        self.assertEqual(qa.calls, [(parent.episode.id, question)])

        invalid = plugin.handle("问 99 为什么", self.message("问 99 为什么"))
        self.assertIn("没有找到", invalid.messages[0])
        self.assertEqual(len(qa.calls), 1)

    def test_recent_number_snapshot_survives_new_archive_and_context_question(self):
        original_first = self.archive(
            "ep-original-first",
            "Original first",
            "主持人讨论护城河。",
            published_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        self.archive(
            "ep-original-second",
            "Original second",
            "另一段内容。",
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa)
        plugin.handle("最近播客", self.message("最近播客"))
        self.archive(
            "ep-new-arrival",
            "New arrival",
            "刚刚归档。",
            published_at=datetime(2026, 9, 16, tzinfo=UTC),
        )

        response = plugin.handle(
            "问 1 为什么护城河重要", self.message("问 1 为什么护城河重要")
        )
        self.assertEqual(response.context_episode_id, original_first.episode.id)
        self.assertEqual(qa.calls[0][0], original_first.episode.id)

        contextual = plugin.handle(
            "为什么他们认为护城河重要", self.message("为什么他们认为护城河重要")
        )
        self.assertEqual(contextual.context_episode_id, original_first.episode.id)
        self.assertEqual(qa.calls[-1][0], original_first.episode.id)

    def test_thread_context_is_isolated_by_sender(self):
        first = self.message(
            "问题",
            chat_type="topic",
            thread_id="omt",
            sender_open_id="ou_a",
        )
        second = self.message(
            "问题",
            chat_type="topic",
            thread_id="omt",
            sender_open_id="ou_b",
        )
        self.assertNotEqual(conversation_key(first), conversation_key(second))

    def test_qa_value_error_returns_a_user_facing_failure(self):
        record = self.archive("ep-long-question", "Long", "完整文字稿。")
        qa = RecordingQA(ValueError("too long"))
        plugin = TranscriptInteractionPlugin(self.store, qa)
        self.store.save_conversation_context("p2p:ou", episode_id=record.episode.id)

        response = plugin.handle("问 这个问题", self.message("问 这个问题"))

        self.assertIn("最多 500 字", response.messages[0])


class RuntimeMessenger:
    def __init__(self):
        self.uploads = []
        self.deliveries = []

    def upload_file(self, content, filename):
        self.uploads.append((content, filename))
        return "file_uploaded"

    def deliver(self, item):
        self.deliveries.append(item)
        return f"om_remote_{item.part}"


class StaticResponsePlugin:
    def __init__(self, response):
        self.response = response

    def acknowledgement(self, text):
        return None

    def handle(self, text, message):
        return self.response


class StaticRouter:
    def __init__(self, plugin):
        self.plugin = plugin

    def select(self, text):
        return self.plugin


class TranscriptRuntimeTests(TranscriptFixture, unittest.IsolatedAsyncioTestCase):
    def runtime(self, messenger, plugin=None):
        instance = object.__new__(NewsOfficerRuntime)
        instance.store = self.store
        instance.messenger = messenger
        instance.podcast_service = MagicMock()
        instance.router = StaticRouter(
            plugin or StaticResponsePlugin(PluginResponse(("ok",)))
        )
        return instance

    async def test_daily_summary_file_order_remote_mapping_and_upload_cache(self):
        record = self.archive("ep-runtime", "Runtime", "full transcript")
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:runtime", "daily", {})
        job = self.store.claim_next("daily")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)

        first_key = instance._uploaded_transcript_file(job, record.episode.id)
        second_key = instance._uploaded_transcript_file(job, record.episode.id)
        self.assertEqual((first_key, second_key), ("file_uploaded", "file_uploaded"))
        self.assertEqual(len(messenger.uploads), 1)

        instance._ensure_broadcast(
            job,
            f"episode:{record.episode.id}",
            "SUMMARY",
            "daily:runtime",
            first_key,
        )
        items = self.store.outbox_items(job.key)
        self.assertEqual([item.msg_type for item in items], ["post", "file"])
        self.assertEqual([item.part for item in items], [1, 2])
        self.assertEqual({item.delivery_key for item in items}, {"daily:runtime:open_id:ou"})

        await instance._drain_outbox(job)
        self.assertEqual(
            self.store.episode_for_remote_message("om_remote_1"),
            record.episode.id,
        )
        self.assertEqual(
            self.store.episode_for_remote_message("om_remote_2"),
            record.episode.id,
        )

        reopened = Store(self.db_path)
        reopened.initialize()
        second_messenger = RuntimeMessenger()
        second_instance = object.__new__(NewsOfficerRuntime)
        second_instance.store = reopened
        second_instance.messenger = second_messenger
        self.assertEqual(
            second_instance._uploaded_transcript_file(job, record.episode.id),
            "file_uploaded",
        )
        self.assertEqual(second_messenger.uploads, [])

    async def test_daily_summary_without_archive_fails_before_delivery(self):
        episode = Episode(
            "ep-missing-daily",
            "Missing daily archive",
            "https://example.test/missing",
            "Show",
        )
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:missing-archive", "daily", {})
        job = self.store.claim_next("daily")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        instance._persist_daily_items(
            job, [DailyItem(episode, "summarized", "SUMMARY")]
        )

        with self.assertRaisesRegex(RuntimeError, "no archived transcript"):
            instance._prepare_daily_outbox_and_terminal_states(job)

        self.assertEqual(self.store.outbox_items(job.key), [])
        self.assertEqual(messenger.deliveries, [])

    async def test_inbound_parent_comes_from_reply_then_raw_event(self):
        instance = self.runtime(RuntimeMessenger())
        instance.wake_workers = {"message": MagicMock()}
        instance._main_loop = MagicMock()
        instance._main_loop.is_closed.return_value = False
        reply_message = SimpleNamespace(
            sender_is_bot=False,
            message_id="om_reply_event",
            chat_id="oc",
            body_text="问题一",
            content_text="",
            chat_type="p2p",
            sender_id="ou",
            thread_id="",
            conversation=None,
            reply=SimpleNamespace(message_id="om_reply_target"),
            raw={"parent_id": "om_raw_parent", "root_id": "om_raw_root"},
        )
        await instance._on_message(reply_message)
        first = self.store.claim_next("message")
        self.assertEqual(first.payload["parent_message_id"], "om_reply_target")
        self.store.complete(first.key)

        raw_message = SimpleNamespace(
            sender_is_bot=False,
            message_id="om_raw_event",
            chat_id="oc",
            body_text="问题二",
            content_text="",
            chat_type="p2p",
            sender_id="ou",
            thread_id="",
            conversation=None,
            reply=None,
            raw={"parent_id": "om_raw_parent", "root_id": "om_raw_root"},
        )
        await instance._on_message(raw_message)
        second = self.store.claim_next("message")
        self.assertEqual(second.payload["parent_message_id"], "om_raw_parent")

    async def test_missing_attachment_fails_closed_in_visible_reply(self):
        plugin = StaticResponsePlugin(
            PluginResponse(
                ("已附上完整文字稿：Missing",),
                attachment_episode_ids=("missing-episode",),
                context_episode_id="missing-episode",
            )
        )
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger, plugin)
        self.store.enqueue(
            "message:missing",
            "message",
            {
                "message_id": "om_missing",
                "chat_id": "oc",
                "text": "文字稿",
                "chat_type": "p2p",
                "sender_open_id": "ou",
            },
        )
        job = self.store.claim_next("message")

        await instance._handle_message_job(job)

        items = self.store.outbox_items(job.key)
        self.assertEqual([item.msg_type for item in items], ["post"])
        visible = json.dumps(json.loads(items[0].content), ensure_ascii=False)
        self.assertIn("未能附上完整文字稿", visible)
        self.assertIn("附件暂不可用", visible)
        self.assertNotIn("已附上完整文字稿", visible)


if __name__ == "__main__":
    unittest.main()
