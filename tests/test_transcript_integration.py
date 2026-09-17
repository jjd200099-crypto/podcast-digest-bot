import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.agent import AgentIntent
from news_officer.feishu import delivery_parts, file_delivery_part
from news_officer.models import (
    DailyItem,
    Episode,
    IncomingMessage,
    Transcript,
    TranscriptAttachment,
)
from news_officer.router import (
    PluginResponse,
    TranscriptInteractionPlugin,
    conversation_key,
)
from news_officer.runtime import NewsOfficerRuntime
from news_officer.store import Store
from news_officer.transcript_view import RENDERER_VERSION, render_readable_transcript


class RecordingQA:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def answer(self, record, question):
        self.calls.append((record.episode.id, question))
        if self.error:
            raise self.error
        return f"ANSWER:{record.episode.id}:{question}"


class RecordingIntentResolver:
    def __init__(self, *intents):
        self.intents = list(intents)
        self.calls = []

    def resolve(self, text, catalog, history=()):
        self.calls.append((text, catalog, history))
        return self.intents.pop(0)


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

    @staticmethod
    def attachment(record, digest_markdown=""):
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


class TranscriptStoreTests(TranscriptFixture):
    def test_archive_returns_the_revision_written_in_its_own_transaction(self):
        episode = Episode(
            "ep-write-snapshot",
            "Writer A",
            "https://example.test/writer-a",
            "Show A",
        )
        transcript = Transcript(
            "Writer A evidence.",
            "official",
            "https://example.test/writer-a/transcript",
            True,
        )
        with patch.object(
            self.store,
            "get_verified_transcript",
            side_effect=AssertionError("must not re-read after committing"),
        ):
            stored = self.store.save_verified_transcript(episode, transcript)

        self.assertEqual(stored.episode, episode)
        self.assertEqual(stored.transcript, transcript)

    def test_legacy_digest_schema_migrates_and_old_rows_fail_closed(self):
        record = self.archive("ep-old-schema", "Old schema", "Evidence.")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("DROP TABLE episode_digests")
            connection.execute(
                """
                CREATE TABLE episode_digests (
                    episode_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    digest_markdown TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO episode_digests(
                    episode_id, source_sha256, digest_markdown,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.episode.id,
                    record.content_sha256,
                    "### 旧摘要\n1. 不可复用。",
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        self.store.initialize()

        connection = sqlite3.connect(self.db_path)
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(episode_digests)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertIn("record_revision_sha256", columns)
        self.assertEqual(
            self.store.get_transcript_digest(record.episode.id), ""
        )

    def test_legacy_context_schema_migrates_bounded_dialogue_history(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("DROP TABLE conversation_contexts")
            connection.execute(
                """
                CREATE TABLE conversation_contexts (
                    context_key TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL DEFAULT '',
                    pending_episode_ids_json TEXT NOT NULL DEFAULT '[]',
                    recent_episode_ids_json TEXT NOT NULL DEFAULT '[]',
                    pending_question TEXT NOT NULL DEFAULT '',
                    pending_action TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.store.initialize()
        for index in range(6):
            self.store.append_conversation_turn(
                "p2p:history",
                user_text=f"问题 {index}",
                assistant_text=f"回答 {index}",
                episode_id="ep-history",
            )

        context = self.store.get_conversation_context("p2p:history")
        self.assertEqual(len(context["history"]), 4)
        self.assertEqual(context["history"][0]["user"], "问题 2")
        self.assertEqual(context["history"][-1]["assistant"], "回答 5")
        self.assertEqual(context["episode_id"], "ep-history")

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
        digest = "### 核心判断\n1. 已核验全文支持这一判断。"
        self.store.save_transcript_digest(
            older.episode.id,
            digest,
            older.content_sha256,
            older.record_revision_sha256,
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
            reopened.get_transcript_digest(older.episode.id), digest
        )
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
        self.assertEqual(context["pending_episode_ids"], ())
        self.assertEqual(
            context["recent_episode_ids"],
            (newer.episode.id, older.episode.id),
        )
        self.assertEqual(context["history"], ())

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

    def test_episode_search_never_uses_a_transcript_body_mention(self):
        self.archive(
            "ep-body-only",
            "AI Roundup",
            "Sam Altman was mentioned briefly.",
            show="Technology Briefing",
        )

        self.assertEqual(self.store.search_verified_transcripts("Sam Altman"), [])

    def test_recent_transcripts_put_unknown_publication_dates_last(self):
        dated = self.archive(
            "ep-dated",
            "Dated",
            "Evidence.",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.archive("ep-undated", "Undated", "Newer stored evidence.")

        self.assertEqual(
            self.store.list_recent_transcripts(2)[0].episode.id,
            dated.episode.id,
        )

    def test_digest_navigation_invalidates_when_source_text_changes(self):
        archived = self.archive("ep-digest", "Digest", "Original complete text.")
        self.store.save_transcript_digest(
            archived.episode.id,
            "### 主题\n1. 原始摘要。",
            archived.content_sha256,
            archived.record_revision_sha256,
        )
        canonical = self.store.save_transcript_digest(
            archived.episode.id,
            "### 主题\n1. 竞争写入不应覆盖。",
            archived.content_sha256,
            archived.record_revision_sha256,
        )
        self.assertIn("原始摘要", canonical)
        self.assertIn(
            "原始摘要", self.store.get_transcript_digest(archived.episode.id)
        )

        updated = self.store.save_verified_transcript(
            archived.episode,
            Transcript(
                "Updated complete text.",
                "official",
                "https://example.test/updated",
                True,
            ),
        )

        self.assertEqual(self.store.get_transcript_digest(archived.episode.id), "")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.save_transcript_digest(
                archived.episode.id,
                "### 主题\n1. 错误绑定。",
                archived.content_sha256,
                archived.record_revision_sha256,
            )
        replacement = self.store.save_transcript_digest(
            archived.episode.id,
            "### 主题\n1. 新版本摘要。",
            updated.content_sha256,
            updated.record_revision_sha256,
        )
        self.assertIn("新版本摘要", replacement)


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
            "黄仁勋 interview A",
            "黄仁勋认为需求仍会增长。",
            published_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        second = self.archive(
            "ep-b",
            "黄仁勋 interview B",
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

    def test_agent_greeting_is_conversational_not_a_command_menu(self):
        resolver = RecordingIntentResolver()
        plugin = TranscriptInteractionPlugin(
            self.store, RecordingQA(), resolver
        )

        response = plugin.handle("你好啊", self.message("你好啊"))

        self.assertIn("你好，我在", response.messages[0])
        self.assertIn("直接问", response.messages[0])
        self.assertNotIn("可用指令", response.messages[0])
        self.assertEqual(resolver.calls, [])
        self.assertTrue(plugin.matches("Naval 在那期节目里怎么看 Agent？"))

    def test_agent_natural_question_and_followup_use_catalog_and_history(self):
        record = self.archive(
            "ep-agent",
            "Sam Altman on agents",
            "Sam: Agents need reliable tools. Host: Why? Sam: Reliability compounds.",
            show="Dwarkesh Podcast",
        )
        first_question = "Sam Altman 为什么认为 Agent 需要可靠工具？"
        followup_question = "Sam 认为工具可靠性为什么会形成复利？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (record.reference,), "", first_question, ""),
            AgentIntent("qa", (record.reference,), "", followup_question, ""),
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        first = plugin.handle(
            "Sam Altman 在 Dwarkesh 那期怎么看 Agent 工具？",
            self.message("Sam Altman 在 Dwarkesh 那期怎么看 Agent 工具？"),
        )
        second = plugin.handle(
            "那他为什么这么判断？",
            self.message("那他为什么这么判断？"),
        )

        self.assertEqual(first.context_episode_id, record.episode.id)
        self.assertEqual(second.context_episode_id, record.episode.id)
        self.assertEqual(
            qa.calls,
            [
                (record.episode.id, first_question),
                (record.episode.id, followup_question),
            ],
        )
        first_catalog = resolver.calls[0][1]
        self.assertEqual(first_catalog[0].reference, record.reference)
        self.assertFalse(resolver.calls[0][2])
        second_history = resolver.calls[1][2]
        self.assertEqual(len(second_history), 1)
        self.assertIn("Sam Altman 在 Dwarkesh", second_history[0]["user"])
        self.assertIn("ANSWER:ep-agent", second_history[0]["assistant"])

    def test_agent_can_resolve_a_natural_pending_choice(self):
        first = self.archive(
            "ep-choice-a",
            "NVIDIA A",
            "First evidence.",
            show="Acquired",
            published_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        second = self.archive(
            "ep-choice-b",
            "NVIDIA B",
            "Second evidence.",
            show="Acquired",
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        question = "黄仁勋如何判断推理需求？"
        resolver = RecordingIntentResolver(
            AgentIntent(
                "qa",
                (first.reference, second.reference),
                "",
                question,
                "",
            )
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        ambiguous = plugin.handle(
            "黄仁勋在 Acquired 里如何看推理需求？",
            self.message("黄仁勋在 Acquired 里如何看推理需求？"),
        )
        selected = plugin.handle("第二个", self.message("第二个"))

        self.assertIn("我找到多期", ambiguous.messages[0])
        self.assertEqual(selected.context_episode_id, second.episode.id)
        self.assertEqual(qa.calls, [(second.episode.id, question)])
        self.assertEqual(len(resolver.calls), 1)

    def test_agent_cannot_override_an_explicit_episode_with_a_wrong_valid_ref(self):
        first = self.archive("ep-anchor-a", "节目 A", "A evidence.")
        second = self.archive("ep-anchor-b", "节目 B", "B evidence.")
        question = "节目 B 的嘉宾为什么看好推理？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (first.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertEqual(response.context_episode_id, second.episode.id)
        self.assertEqual(qa.calls, [(second.episode.id, question)])

    def test_agent_reply_binding_overrides_a_wrong_current_ref(self):
        reply_record = self.archive("ep-agent-reply", "Reply episode", "Reply evidence.")
        current = self.archive("ep-agent-current", "Current episode", "Current evidence.")
        self.store.save_conversation_context(
            "p2p:ou", episode_id=current.episode.id
        )
        self.store.enqueue("daily:agent-reply", "daily", {})
        job = self.store.claim_next("daily")
        self.store.ensure_outbox(
            job_key=job.key,
            group_key=f"episode:{reply_record.episode.id}",
            delivery_key="agent-reply",
            operation="send",
            target_id="ou",
            target_type="open_id",
            reply_in_thread=False,
            parts=delivery_parts("summary", "agent-reply"),
        )
        item = self.store.outbox_items(job.key)[0]
        self.store.mark_outbox_sent(item.id, "om_agent_reply")
        question = "他为什么这么说？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (current.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(
            question,
            self.message(question, parent_message_id="om_agent_reply"),
        )

        self.assertEqual(response.context_episode_id, reply_record.episode.id)
        self.assertEqual(qa.calls, [(reply_record.episode.id, question)])

    def test_topic_words_do_not_match_arbitrary_chinese_title_fragments(self):
        reply_record = self.archive("ep-fragment-a", "AI Roundup", "A evidence.")
        self.archive("ep-fragment-b", "为什么模型失败", "B evidence.")
        question = "他为什么这么说？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (reply_record.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)
        self.store.save_conversation_context(
            "p2p:ou", episode_id=reply_record.episode.id
        )

        response = plugin.handle(question, self.message(question))

        self.assertEqual(response.context_episode_id, reply_record.episode.id)
        self.assertEqual(qa.calls, [(reply_record.episode.id, question)])

    def test_reply_topic_phrase_that_equals_another_title_is_clarified(self):
        reply_record = self.archive("ep-title-a", "AI Roundup", "A evidence.")
        other = self.archive("ep-title-b", "为什么模型失败", "B evidence.")
        qa = RecordingQA()
        self.store.save_conversation_context(
            "p2p:ou", episode_id=reply_record.episode.id
        )

        for question in (
            "这期为什么模型失败？",
            "请问这期为什么模型失败？",
            "那这期为什么模型失败？",
            "所以这期为什么模型失败？",
            "我想问这期为什么模型失败？",
            "麻烦问下这期为什么模型失败？",
            "想请问这期为什么模型失败？",
            "顺便问一下这期为什么模型失败？",
            "本期为什么模型失败？",
            "这集为什么模型失败？",
            "这一集为什么模型失败？",
            "本节目为什么模型失败？",
            "该播客为什么模型失败？",
            "当前节目为什么模型失败？",
            "这档节目为什么模型失败？",
            "本次节目为什么模型失败？",
            "此节目为什么模型失败？",
            "这一档节目为什么模型失败？",
            "这一个节目为什么模型失败？",
            "这一次节目为什么模型失败？",
            "这场访谈为什么模型失败？",
            "这次访谈为什么模型失败？",
            "这一场访谈为什么模型失败？",
            "本场访谈为什么模型失败？",
            "这段访谈为什么模型失败？",
            "我改问一个问题：为什么模型失败？",
            "另问一个问题，为什么模型失败？",
            "换到另一个问题，为什么模型失败？",
            "切到下一个问题：为什么模型失败？",
            "那期为什么模型失败？",
            "那一期为什么模型失败？",
            "那个节目为什么模型失败？",
            "那个访谈为什么模型失败？",
        ):
            with self.subTest(question=question):
                resolver = RecordingIntentResolver(
                    AgentIntent("qa", (other.reference,), "", question, "")
                )
                plugin = TranscriptInteractionPlugin(self.store, qa, resolver)
                response = plugin.handle(question, self.message(question))
                self.assertIn("不确定", response.messages[0])
        self.assertEqual(qa.calls, [])

        english = self.archive(
            "ep-title-english", "Why Models Fail", "English evidence."
        )
        english_question = "In this episode, why models fail?"
        english_resolver = RecordingIntentResolver(
            AgentIntent("qa", (english.reference,), "", english_question, "")
        )
        english_plugin = TranscriptInteractionPlugin(
            self.store, qa, english_resolver
        )
        english_response = english_plugin.handle(
            english_question, self.message(english_question)
        )
        self.assertIn("不确定", english_response.messages[0])
        self.assertEqual(qa.calls, [])

        switch_question = "换到 Why Models Fail 那期，嘉宾为什么这么判断？"
        switch_resolver = RecordingIntentResolver(
            AgentIntent("qa", (english.reference,), "", switch_question, "")
        )
        switch_plugin = TranscriptInteractionPlugin(
            self.store, qa, switch_resolver
        )
        switch_response = switch_plugin.handle(
            switch_question, self.message(switch_question)
        )
        self.assertEqual(switch_response.context_episode_id, english.episode.id)
        self.assertEqual(qa.calls[-1][0], english.episode.id)

    def test_negated_title_does_not_beat_the_requested_title(self):
        requested = self.archive("ep-correction-a", "节目 A", "A evidence.")
        rejected = self.archive(
            "ep-correction-b", "为什么模型失败", "B evidence."
        )
        question = "不要回答为什么模型失败，回答节目 A"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (rejected.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertEqual(response.context_episode_id, requested.episode.id)
        self.assertEqual(qa.calls, [(requested.episode.id, question)])

    def test_unique_name_token_can_select_a_catalog_episode(self):
        record = self.archive(
            "ep-naval",
            "Naval Ravikant on Startups",
            "Naval discusses leverage.",
        )
        question = "Naval 在那期节目里怎么看 Agent？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (record.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertEqual(response.context_episode_id, record.episode.id)
        self.assertEqual(qa.calls, [(record.episode.id, question)])

    def test_unique_chinese_guest_name_can_select_a_catalog_episode(self):
        record = self.archive(
            "ep-chinese-guest",
            "黄仁勋：AI 工厂与推理经济",
            "黄仁勋讨论推理需求。",
        )
        question = "黄仁勋怎么看推理？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (record.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertEqual(response.context_episode_id, record.episode.id)
        self.assertEqual(qa.calls, [(record.episode.id, question)])

    def test_chinese_topic_overlap_is_not_an_episode_selector(self):
        distractor = self.archive(
            "ep-cjk-topic",
            "如何构建 AI 应用的护城河",
            "Distractor evidence.",
        )
        question = "如何构建销售团队？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (distractor.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertIn("还没有找到", response.messages[0])
        self.assertEqual(qa.calls, [])

    def test_extended_current_deictics_never_switch_on_a_topic_overlap(self):
        current = self.archive("ep-cjk-current", "AI Roundup", "A evidence.")
        distractor = self.archive(
            "ep-cjk-other", "黄仁勋：AI 工厂与推理经济", "B evidence."
        )
        qa = RecordingQA()
        self.store.save_conversation_context(
            "p2p:ou", episode_id=current.episode.id
        )
        for question in (
            "这一期为什么推理经济重要？",
            "这个节目为什么推理经济重要？",
            "这个播客为什么推理经济重要？",
        ):
            with self.subTest(question=question):
                resolver = RecordingIntentResolver(
                    AgentIntent("qa", (distractor.reference,), "", question, "")
                )
                plugin = TranscriptInteractionPlugin(self.store, qa, resolver)
                response = plugin.handle(question, self.message(question))
                self.assertEqual(response.context_episode_id, current.episode.id)
        self.assertEqual(
            [episode_id for episode_id, _ in qa.calls],
            [current.episode.id, current.episode.id, current.episode.id],
        )

    def test_unique_topic_token_cannot_silently_override_current_episode(self):
        current = self.archive("ep-topic-current", "AI Roundup", "A evidence.")
        distractor = self.archive(
            "ep-topic-distractor", "Scaling Infrastructure", "B evidence."
        )
        question = "请问 scaling 为什么重要？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (distractor.reference,), "", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)
        self.store.save_conversation_context(
            "p2p:ou", episode_id=current.episode.id
        )

        response = plugin.handle(question, self.message(question))

        self.assertIn("不确定", response.messages[0])
        self.assertEqual(qa.calls, [])

    def test_lookup_does_not_select_an_episode_from_a_body_only_mention(self):
        self.archive(
            "ep-body-routing",
            "AI Roundup",
            "Sam Altman was mentioned briefly.",
            show="Technology Briefing",
        )
        question = "Sam Altman 那期怎么看 Agent？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (), "Sam Altman", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertIn("还没有找到", response.messages[0])
        self.assertEqual(qa.calls, [])

    def test_model_only_lookup_cannot_select_an_unmentioned_real_episode(self):
        self.archive("ep-secret", "Secret Episode", "Secret evidence.")
        question = "他怎么看？"
        resolver = RecordingIntentResolver(
            AgentIntent("qa", (), "Secret Episode", question, "")
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertIn("还没有找到", response.messages[0])
        self.assertEqual(qa.calls, [])

    def test_history_switch_beats_a_lookup_or_title_phrase(self):
        earlier = self.archive(
            "ep-history-earlier", "Earlier conversation", "Earlier evidence."
        )
        current = self.archive(
            "ep-history-current", "Current conversation", "Current evidence."
        )
        self.archive("ep-history-distractor", "Sam Altman Special", "Other.")
        self.store.append_conversation_turn(
            "p2p:ou",
            user_text="Earlier question",
            assistant_text="Earlier answer",
            episode_id=earlier.episode.id,
        )
        self.store.append_conversation_turn(
            "p2p:ou",
            user_text="Current question",
            assistant_text="Current answer",
            episode_id=current.episode.id,
        )
        self.store.save_conversation_context(
            "p2p:ou", episode_id=current.episode.id
        )
        question = "回到之前那期，Sam Altman 为什么这么说？"
        resolver = RecordingIntentResolver(
            AgentIntent(
                "qa", (earlier.reference,), "Sam Altman", question, ""
            )
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(self.store, qa, resolver)

        response = plugin.handle(question, self.message(question))

        self.assertEqual(response.context_episode_id, earlier.episode.id)
        self.assertEqual(qa.calls, [(earlier.episode.id, question)])

    def test_recent_snapshot_supports_embedded_ordinal_and_latest(self):
        oldest = self.archive(
            "ep-list-old",
            "Old episode",
            "Old evidence.",
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        middle = self.archive(
            "ep-list-middle",
            "Middle episode",
            "Middle evidence.",
            published_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        newest = self.archive(
            "ep-list-new",
            "New episode",
            "New evidence.",
            published_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
        qa = RecordingQA()
        plugin = TranscriptInteractionPlugin(
            self.store, qa, RecordingIntentResolver()
        )
        plugin.handle("最近播客", self.message("最近播客"))

        ordinal = plugin.handle(
            "第二期里嘉宾为什么这么判断？",
            self.message("第二期里嘉宾为什么这么判断？"),
        )
        self.assertEqual(ordinal.context_episode_id, middle.episode.id)
        self.assertEqual(qa.calls[-1][0], middle.episode.id)

        plugin.handle("最近播客", self.message("最近播客"))
        latest = plugin.handle(
            "最新那期为什么重要？", self.message("最新那期为什么重要？")
        )
        self.assertEqual(latest.context_episode_id, newest.episode.id)
        self.assertEqual(qa.calls[-1][0], newest.episode.id)
        self.assertNotEqual(latest.context_episode_id, oldest.episode.id)

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

        explicit_other = f"问 {other.reference} 为什么资本开支重要？"
        switched = plugin.handle(
            explicit_other,
            self.message(
                explicit_other,
                parent_message_id="om_parent",
            ),
        )
        self.assertEqual(switched.context_episode_id, other.episode.id)
        self.assertEqual(qa.calls[-1], (other.episode.id, explicit_other))

        invalid = plugin.handle("问 99 为什么", self.message("问 99 为什么"))
        self.assertIn("没有找到", invalid.messages[0])
        self.assertEqual(len(qa.calls), 2)

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
    def __init__(self, response, *, name="static"):
        self.response = response
        self.name = name

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
        self.store.save_transcript_digest(
            record.episode.id,
            "### 核心判断\n1. 这条要点来自完整文字稿。",
            record.content_sha256,
            record.record_revision_sha256,
        )
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:runtime", "daily", {})
        job = self.store.claim_next("daily")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        attachment = self.attachment(
            record,
            "### 核心判断\n1. 这条要点来自完整文字稿。",
        )

        first_key = instance._uploaded_transcript_file(job, attachment)
        second_key = instance._uploaded_transcript_file(job, attachment)
        self.assertEqual((first_key, second_key), ("file_uploaded", "file_uploaded"))
        self.assertEqual(len(messenger.uploads), 1)
        self.assertIn("## 核心论点", messenger.uploads[0][0].decode("utf-8"))
        self.assertIn("1. 这条要点来自完整文字稿。", messenger.uploads[0][0].decode("utf-8"))

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
            second_instance._uploaded_transcript_file(job, attachment),
            "file_uploaded",
        )
        self.assertEqual(second_messenger.uploads, [])

    async def test_upload_cache_tracks_transcript_and_digest_revisions(self):
        original = self.archive("ep-revision", "Revision", "Guest: Original evidence.")
        original_digest = "### 核心判断\n1. 原始版本要点。"
        self.store.save_transcript_digest(
            original.episode.id,
            original_digest,
            original.content_sha256,
            original.record_revision_sha256,
        )
        self.store.enqueue("message:revision", "message", {})
        job = self.store.claim_next("message")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        original_attachment = self.attachment(original, original_digest)

        self.assertEqual(
            instance._uploaded_transcript_file(job, original_attachment),
            "file_uploaded",
        )
        self.assertEqual(len(messenger.uploads), 1)

        updated = self.store.save_verified_transcript(
            original.episode,
            Transcript(
                "Guest: Updated evidence.",
                "official",
                "https://example.test/ep-revision/transcript-v2",
                True,
            ),
        )
        updated_digest = "### 核心判断\n1. 更新版本要点。"
        self.store.save_transcript_digest(
            updated.episode.id,
            updated_digest,
            updated.content_sha256,
            updated.record_revision_sha256,
        )
        updated_attachment = self.attachment(updated, updated_digest)

        self.assertEqual(
            instance._uploaded_transcript_file(job, updated_attachment),
            "file_uploaded",
        )
        self.assertEqual(len(messenger.uploads), 2)
        self.assertEqual(
            instance._uploaded_transcript_file(job, original_attachment),
            "file_uploaded",
        )
        self.assertEqual(
            len(messenger.uploads),
            2,
            "an already-uploaded immutable old artifact remains safe to retry",
        )
        self.assertIn(
            "Updated evidence", messenger.uploads[-1][0].decode("utf-8")
        )
        self.assertIn(
            "1. 更新版本要点。", messenger.uploads[-1][0].decode("utf-8")
        )
        self.assertEqual(
            instance._uploaded_transcript_file(job, updated_attachment),
            "file_uploaded",
        )
        self.assertEqual(len(messenger.uploads), 2)

    async def test_unknown_supplied_digest_is_never_bound(self):
        record = self.archive("ep-mismatch", "Mismatch", "Guest: Evidence.")

        with self.assertRaisesRegex(ValueError, "record revision"):
            self.store.save_transcript_digest(
                record.episode.id,
                "### 核心判断\n1. 未存储的要点。",
                record.content_sha256,
                "",
            )
        self.assertIsNone(
            self.store.get_transcript_digest_revision(record.episode.id)
        )

    async def test_metadata_correction_changes_render_fingerprint_and_cache_key(self):
        original = self.archive(
            "ep-metadata",
            "Original title",
            "Guest: Stable evidence.",
        )
        digest = "### 核心判断\n1. 稳定版本要点。"
        self.store.save_transcript_digest(
            original.episode.id,
            digest,
            original.content_sha256,
            original.record_revision_sha256,
        )
        self.store.enqueue("message:metadata", "message", {})
        job = self.store.claim_next("message")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        first = self.attachment(original, digest)
        self.assertEqual(
            instance._uploaded_transcript_file(job, first), "file_uploaded"
        )

        corrected_episode = Episode(
            original.episode.id,
            "Corrected title",
            original.episode.url,
            original.episode.show,
            original.episode.duration_seconds,
            original.episode.duration_string,
            original.episode.published_at,
        )
        corrected = self.store.save_verified_transcript(
            corrected_episode,
            original.transcript,
        )
        self.assertEqual(corrected.content_sha256, original.content_sha256)
        self.assertNotEqual(
            corrected.record_revision_sha256,
            original.record_revision_sha256,
        )
        self.assertEqual(
            self.store.get_transcript_digest(original.episode.id), ""
        )
        corrected_digest = "### 核心判断\n1. 修正元信息后的要点。"
        self.store.save_transcript_digest(
            corrected.episode.id,
            corrected_digest,
            corrected.content_sha256,
            corrected.record_revision_sha256,
        )
        second = self.attachment(corrected, corrected_digest)
        self.assertNotEqual(first.filename, second.filename)
        self.assertNotEqual(first.rendered_sha256, second.rendered_sha256)
        self.assertEqual(
            instance._uploaded_transcript_file(job, second), "file_uploaded"
        )
        self.assertEqual(len(messenger.uploads), 2)

    async def test_retry_never_pairs_old_reply_with_new_attachment(self):
        original = self.archive(
            "ep-retry",
            "Original revision",
            "Guest: Original evidence.",
        )
        original_digest = "### 核心判断\n1. 原始版本要点。"
        self.store.save_transcript_digest(
            original.episode.id,
            original_digest,
            original.content_sha256,
            original.record_revision_sha256,
        )
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        descriptor = self.attachment(original, original_digest)
        self.store.enqueue(
            "message:retry-revision",
            "message",
            {
                "message_id": "om_retry_revision",
                "chat_id": "oc",
                "text": "分析链接",
                "chat_type": "p2p",
                "sender_open_id": "ou",
            },
        )
        job = self.store.claim_next("message")
        self.store.save_job_result(
            job.key,
            "message:analysis",
            "message",
            {
                "messages": ["已附上精编可读版文字稿：Original revision"],
                "attachment_episode_ids": [original.episode.id],
                "attachments": [descriptor.to_persisted_dict()],
                "context_episode_id": original.episode.id,
            },
        )

        updated = self.store.save_verified_transcript(
            original.episode,
            Transcript(
                "Guest: New evidence.",
                "official",
                "https://example.test/ep-retry/transcript-v2",
                True,
            ),
        )
        self.store.save_transcript_digest(
            updated.episode.id,
            "### 核心判断\n1. 新版本要点。",
            updated.content_sha256,
            updated.record_revision_sha256,
        )

        await instance._handle_message_job(job)

        self.assertEqual(messenger.uploads, [])
        items = self.store.outbox_items(job.key)
        self.assertEqual([item.msg_type for item in items], ["post"])
        visible = json.dumps(json.loads(items[0].content), ensure_ascii=False)
        self.assertIn("未能附上精编可读版文字稿", visible)
        self.assertNotIn("New evidence", visible)

    async def test_handle_to_persist_race_keeps_plugin_attachment_revision(self):
        original = self.archive(
            "ep-handle-race",
            "Before race",
            "Guest: Before-race evidence.",
        )
        old_digest = "### 核心判断\n1. 竞态前要点。"
        self.store.save_transcript_digest(
            original.episode.id,
            old_digest,
            original.content_sha256,
            original.record_revision_sha256,
        )
        old_attachment = self.attachment(original, old_digest)
        fixture = self

        class RacingPlugin:
            name = "podcast"

            @staticmethod
            def acknowledgement(text):
                return None

            @staticmethod
            def handle(text, message):
                updated_episode = Episode(
                    original.episode.id,
                    "After race",
                    original.episode.url,
                    original.episode.show,
                )
                updated = fixture.store.save_verified_transcript(
                    updated_episode,
                    Transcript(
                        "Guest: After-race evidence.",
                        "official",
                        "https://example.test/after-race",
                        True,
                    ),
                )
                fixture.store.save_transcript_digest(
                    updated.episode.id,
                    "### 核心判断\n1. 竞态后要点。",
                    updated.content_sha256,
                    updated.record_revision_sha256,
                )
                return PluginResponse(
                    ("已附上精编可读版文字稿：Before race",),
                    attachment_episode_ids=(original.episode.id,),
                    context_episode_id=original.episode.id,
                    attachments=(old_attachment,),
                )

        messenger = RuntimeMessenger()
        instance = self.runtime(messenger, RacingPlugin())
        self.store.enqueue(
            "message:handle-race",
            "message",
            {
                "message_id": "om_handle_race",
                "chat_id": "oc",
                "text": "https://example.test/race",
                "chat_type": "p2p",
                "sender_open_id": "ou",
            },
        )
        job = self.store.claim_next("message")

        await instance._handle_message_job(job)

        analysis = self.store.get_job_result(job.key, "message:analysis")
        self.assertEqual(
            analysis["attachments"][0]["rendered_sha256"],
            old_attachment.rendered_sha256,
        )
        self.assertEqual(messenger.uploads, [])
        visible = json.dumps(
            json.loads(self.store.outbox_items(job.key)[0].content),
            ensure_ascii=False,
        )
        self.assertIn("未能附上精编可读版文字稿", visible)
        self.assertNotIn("After-race evidence", visible)

    async def test_legacy_message_descriptor_without_full_revision_fails_closed(self):
        record = self.archive("ep-legacy", "Legacy", "Guest: Evidence.")
        old_descriptor = self.attachment(record).to_persisted_dict()
        old_descriptor.pop("record_revision_sha256")
        self.store.enqueue(
            "message:legacy-analysis",
            "message",
            {
                "message_id": "om_legacy",
                "chat_id": "oc",
                "text": "文字稿",
                "chat_type": "p2p",
                "sender_open_id": "ou",
            },
        )
        job = self.store.claim_next("message")
        self.store.save_job_result(
            job.key,
            "message:analysis",
            "message",
            {
                "messages": ["已附上完整文字稿：Legacy"],
                "attachment_episode_ids": [record.episode.id],
                "attachments": [old_descriptor],
                "context_episode_id": record.episode.id,
            },
        )
        messenger = RuntimeMessenger()

        await self.runtime(messenger)._handle_message_job(job)

        self.assertEqual(messenger.uploads, [])
        visible = json.dumps(
            json.loads(self.store.outbox_items(job.key)[0].content),
            ensure_ascii=False,
        )
        self.assertIn("未能附上完整文字稿", visible)

    async def test_explicit_transcript_command_can_snapshot_no_digest(self):
        record = self.archive("ep-no-digest", "No digest", "Guest: Evidence.")
        attachment = self.attachment(record)
        plugin = StaticResponsePlugin(
            PluginResponse(
                ("已附上精编可读版文字稿：No digest",),
                attachment_episode_ids=(record.episode.id,),
                context_episode_id=record.episode.id,
                attachments=(attachment,),
            ),
            name="transcript_qa",
        )
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger, plugin)
        self.store.enqueue(
            "message:no-digest",
            "message",
            {
                "message_id": "om_no_digest",
                "chat_id": "oc",
                "text": "文字稿",
                "chat_type": "p2p",
                "sender_open_id": "ou",
            },
        )
        job = self.store.claim_next("message")

        await instance._handle_message_job(job)

        self.assertEqual(len(messenger.uploads), 1)
        analysis = self.store.get_job_result(job.key, "message:analysis")
        self.assertEqual(len(analysis["attachments"]), 1)
        self.assertEqual(
            analysis["attachments"][0]["source_sha256"],
            record.content_sha256,
        )

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
        with self.assertRaisesRegex(RuntimeError, "immutable transcript revision"):
            instance._persist_daily_items(
                job, [DailyItem(episode, "summarized", "SUMMARY")]
            )

        self.assertEqual(self.store.outbox_items(job.key), [])
        self.assertEqual(messenger.deliveries, [])

    async def test_text_only_daily_preserves_archive_and_skips_upload(self):
        record = self.archive("ep-text-daily", "Text only", "Full verified evidence.")
        digest = "推荐理由：用于验证摘要独立发送。\n1. 要点。\n推荐星级：★★★★☆（4/5，编辑推荐）"
        self.store.save_transcript_digest(record.episode.id, digest,
                                          record.content_sha256, record.record_revision_sha256)
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:text-only", "daily", {})
        job = self.store.claim_next("daily")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        instance.podcast_service.build_daily.return_value = [DailyItem(
            record.episode, "summarized", digest, self.attachment(record, digest),
        )]
        await instance._handle_daily_job(job)
        self.assertEqual(messenger.uploads, [])
        self.assertEqual([i.msg_type for i in self.store.outbox_items(job.key)], ["post"])
        self.assertIsNotNone(self.store.get_verified_transcript(record.episode.id))
        self.assertTrue(self.store.has_episode(record.episode.id))

    async def test_explicit_daily_file_option_still_delivers_attachment(self):
        record = self.archive("ep-file-opt-in", "File opt in", "Full verified evidence.")
        digest = "SUMMARY"
        self.store.save_transcript_digest(record.episode.id, digest,
                                          record.content_sha256, record.record_revision_sha256)
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:file-opt-in", "daily", {})
        job = self.store.claim_next("daily")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        instance.settings = SimpleNamespace(daily_transcript_attachments=True)
        instance.podcast_service.build_daily.return_value = [DailyItem(
            record.episode, "summarized", digest, self.attachment(record, digest),
        )]
        await instance._handle_daily_job(job)
        self.assertEqual(len(messenger.uploads), 1)
        self.assertEqual([i.msg_type for i in self.store.outbox_items(job.key)], ["post", "file"])

    async def test_legacy_daily_item_without_descriptor_fails_closed(self):
        record = self.archive("ep-legacy-daily", "Legacy daily", "Evidence.")
        digest = "### 核心判断\n1. 旧版摘要。"
        self.store.save_transcript_digest(
            record.episode.id,
            digest,
            record.content_sha256,
            record.record_revision_sha256,
        )
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:legacy-item", "daily", {})
        job = self.store.claim_next("daily")
        self.store.save_job_result(
            job.key,
            f"episode:{record.episode.id}",
            "daily_item",
            {
                "episode": record.episode.to_persisted_dict(),
                "status": "summarized",
                "message": digest,
            },
        )
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        instance.podcast_service.build_daily.return_value = []

        await instance._handle_daily_job(job)

        self.assertEqual(messenger.uploads, [])
        items = self.store.outbox_items(job.key)
        self.assertEqual([item.msg_type for item in items], ["post"])
        self.assertIn("版本信息已过期", items[0].content)
        self.assertTrue(self.store.analysis_complete(job.key))

    async def test_daily_retry_never_pairs_old_summary_with_new_transcript(self):
        original = self.archive(
            "ep-daily-retry",
            "Daily retry",
            "Guest: Original daily evidence.",
        )
        old_digest = "### 核心判断\n1. 原始每日要点。"
        self.store.save_transcript_digest(
            original.episode.id,
            old_digest,
            original.content_sha256,
            original.record_revision_sha256,
        )
        self.store.add_subscription("open_id", "ou")
        self.store.enqueue("daily:revision-change", "daily", {})
        job = self.store.claim_next("daily")
        messenger = RuntimeMessenger()
        instance = self.runtime(messenger)
        instance._persist_daily_items(
            job,
            [
                DailyItem(
                    original.episode,
                    "summarized",
                    old_digest,
                    self.attachment(original, old_digest),
                )
            ],
        )
        persisted = instance._persisted_daily_items(job)
        self.assertIsNotNone(persisted[0].attachment)

        updated = self.store.save_verified_transcript(
            original.episode,
            Transcript(
                "Guest: New daily evidence.",
                "official",
                "https://example.test/ep-daily-retry/transcript-v2",
                True,
            ),
        )
        self.store.save_transcript_digest(
            updated.episode.id,
            "### 核心判断\n1. 新每日要点。",
            updated.content_sha256,
            updated.record_revision_sha256,
        )

        instance.podcast_service.build_daily.return_value = []
        await instance._handle_daily_job(job)

        self.assertEqual(messenger.uploads, [])
        items = self.store.outbox_items(job.key)
        self.assertEqual([item.msg_type for item in items], ["post"])
        self.assertIn("发送前发生变化", items[0].content)
        self.assertTrue(self.store.analysis_complete(job.key))

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
