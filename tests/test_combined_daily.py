import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_reliability import (
    FakeMessenger,
    FakeResponse,
    SequencePodcast,
    attachment_for,
    runtime,
)

from news_officer.daily_report import render_daily_summary
from news_officer.feishu import (
    FeishuMessenger,
    combined_delivery_parts,
    delivery_parts,
    encoded_message_payload,
)
from news_officer.models import (
    DailyItem,
    Episode,
    IncomingMessage,
    OutboxItem,
    Transcript,
)
from news_officer.research_context import quoted_context
from news_officer.store import Store


def body(part):
    payload = json.loads(part.content)
    if part.msg_type == "text":
        return payload["text"]
    return "\n".join("".join(e.get("text", "") for e in p)
                     for p in payload["zh_cn"]["content"])


class CombinedPayloadTests(unittest.TestCase):
    def test_six_chinese_summaries_fit_one_post(self):
        text = "\n\n---\n\n".join(f"第 {i} 期\n" + "具体观点和依据。" * 65 for i in range(6))
        parts = combined_delivery_parts(text, "six")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], "post")
        self.assertGreater(len(delivery_parts(text, "old")), 1)
        for i in range(6):
            self.assertIn(f"第 {i} 期", parts[0][1])

    def test_long_post_uses_one_text_without_truncating(self):
        text = "完整观点😀\\\"\n" * 1800
        parts = combined_delivery_parts(text, "long")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], "text")
        self.assertTrue(json.loads(parts[0][1])["text"].endswith(text.strip()))

    def test_extreme_overflow_is_lossless_and_payload_safe(self):
        lines = [f"{i}. " + "观点😀" * 100 for i in range(350)]
        parts = combined_delivery_parts("\n".join(lines), "huge")
        self.assertGreater(len(parts), 1)
        rendered = "\n".join(json.loads(p[1])["text"] for p in parts)
        for line in lines:
            self.assertEqual(rendered.splitlines().count(line), 1)
        for kind, content, uid in parts:
            request = {"receive_id": "x" * 128, "msg_type": kind, "content": content, "uuid": uid}
            self.assertLess(len(encoded_message_payload(request)), 150_000)

    def test_bundle_wire_encoding_matches_capacity_check(self):
        text = "观点和依据" * 1400
        kind, content, uid = combined_delivery_parts(text, "utf8")[0]
        self.assertEqual(kind, "post")
        item = OutboxItem(0, "daily:test", "daily:bundle:wire", "wire", "send", "ou_test", "open_id",
                          False, 1, 1, kind, content, uid)
        messenger = FeishuMessenger("app", "secret")
        with (patch.object(messenger, "token", return_value="test-token"),
              patch("news_officer.feishu.requests.post", return_value=FakeResponse(body={
                  "code": 0, "data": {"message_id": "receipt"}})) as post):
            self.assertEqual(messenger.deliver(item), "receipt")
        wire = post.call_args.kwargs["data"]
        self.assertIsInstance(wire, bytes)
        self.assertLess(len(wire), 30_000)
        self.assertEqual(wire, encoded_message_payload(json.loads(wire)))
        self.assertEqual(json.loads(wire)["content"], content)


class CombinedDailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "store.sqlite3")
        self.store.initialize()
        self.messenger = FakeMessenger()

    def tearDown(self):
        self.temp.cleanup()

    def item(self, name, stars=3):
        episode = Episode(name, name, "https://example.test/" + name, "Show",
                          published_at=datetime.now(UTC))
        record = self.store.save_verified_transcript(
            episode, Transcript("Complete source " + name, "official", episode.url, True))
        summary = f"节目：{name}\n\n推荐理由：完整原文支持的观点。\n\n1. {name} 的核心判断。\n\n推荐星级：{'★' * stars}{'☆' * (5 - stars)}"
        self.store.save_transcript_digest(name, summary, record.content_sha256, record.record_revision_sha256)
        return DailyItem(episode, "summarized", summary, attachment_for(record, summary))

    def setup_job(self, items, key="daily:test", payload=None):
        podcast = SequencePodcast(items)
        instance = runtime(self.store, self.messenger, podcast)
        instance.settings.daily_combined_message = True
        self.store.enqueue(key, "daily", payload or {"scheduled_for": "2026-09-19T08:30:00+08:00"})
        return instance, self.store.claim_next("daily"), podcast

    async def test_all_six_and_pending_status_in_one_message(self):
        items = [self.item(f"episode-{i}", 1 + i % 5) for i in range(6)]
        items.append(DailyItem(Episode("pending", "Pending", "https://example.test/p", "Show"),
                               "no_transcript", "仍在转写"))
        instance, job, podcast = self.setup_job(items)
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        part = self.messenger.attempts[0]
        self.assertEqual(part.total_parts, 1)
        text = body(part)
        self.assertIn("6 期", text)
        self.assertIn("仍在转写", text)
        self.assertLess(text.index("节目：episode-4"), text.index("节目：episode-0"))
        for item in items[:6]:
            self.assertIn(render_daily_summary(item.message), text.replace("\n \n", "\n\n"))
            self.assertTrue(self.store.episode_is_delivered(item.episode))
        self.assertNotIn('★', text)
        self.assertNotIn('推荐星级', text)
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertEqual(podcast.calls, 1)

    async def test_release_note_first_morning_only_and_in_archive(self):
        from news_officer.daily_archive import read_daily_digest

        instance, job, _ = self.setup_job([], key="daily:2026-10-04", payload={
            "scheduled_for": "2026-10-04T08:30:00+08:00"})
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertIn("功能更新", body(self.messenger.attempts[0]))
        self.assertIn("新增 Podwise 扩展发现", body(self.messenger.attempts[0]))
        self.assertIn("功能更新", read_daily_digest(self.store, "2026-10-04")["markdown"])
        self.store.complete(job.key)
        reopened = Store(self.store.path)
        reopened.initialize()
        self.store = reopened
        instance, job, _ = self.setup_job([], key="daily:2026-10-05", payload={
            "scheduled_for": "2026-10-05T08:30:00+08:00"})
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 2)
        self.assertNotIn("功能更新", body(self.messenger.attempts[-1]))

    async def test_release_note_retry_retains_frozen_content(self):
        item = self.item("release-retry")
        instance, job, _ = self.setup_job([item], key="daily:2026-10-04", payload={
            "scheduled_for": "2026-10-04T08:30:00+08:00"})
        instance._persist_daily_items(job, [item])
        instance._prepare_daily_outbox_and_terminal_states(job)
        before = self.store.outbox_items(job.key)[0]
        self.messenger.fail_groups_once.add(before.group_key)
        with self.assertRaises(RuntimeError):
            await instance._handle_daily_job(job)
        with patch("news_officer.release_notes.RELEASE_NOTES", ()):
            await instance._handle_daily_job(job)
        after = self.store.outbox_items(job.key)[0]
        self.assertEqual((before.uuid, before.content), (after.uuid, after.content))
        self.assertTrue(self.store.outbox_group_sent(job.key, after.group_key))
        self.assertIn("功能更新", body(after))

    async def test_manual_and_catchup_do_not_consume_release_note(self):
        for key, catchup in (("daily:manual-test", False), ("daily:catchup-test", True)):
            instance, job, podcast = self.setup_job([self.item(key)], key=key, payload={
                "scheduled_for": "2026-10-04T08:30:00+08:00",
                "transcript_catchup": catchup})
            podcast.build_pending = podcast.build_daily
            await instance._handle_daily_job(job)
            self.assertNotIn("功能更新", body(self.messenger.attempts[-1]))
            self.store.complete(job.key)
        instance, job, _ = self.setup_job([], key="daily:2026-10-05", payload={
            "scheduled_for": "2026-10-05T08:30:00+08:00"})
        await instance._handle_daily_job(job)
        self.assertIn("功能更新", body(self.messenger.attempts[-1]))

    async def test_old_report_does_not_announce_future_feature(self):
        instance, job, _ = self.setup_job([], key="daily:2026-10-03", payload={
            "scheduled_for": "2026-10-03T08:30:00+08:00"})
        await instance._handle_daily_job(job)
        self.assertNotIn("功能更新", body(self.messenger.attempts[0]))

    async def test_lost_receipt_replays_frozen_bundle_without_regenerating(self):
        item = self.item("saved")
        instance, job, _ = self.setup_job([item])
        instance._persist_daily_items(job, [item])
        instance._prepare_daily_outbox_and_terminal_states(job)
        group = self.store.outbox_items(job.key)[0].group_key
        self.messenger.fail_groups_once.add(group)
        with self.assertRaises(RuntimeError):
            await instance._handle_daily_job(job)
        self.assertFalse(self.store.episode_is_delivered(item.episode))
        before = self.store.outbox_items(job.key)[0]
        # Changing the source after preparation cannot mutate the frozen send.
        self.store.save_verified_transcript(item.episode, Transcript("new revision", "official", item.episode.url, True))
        reopened = Store(self.store.path)
        reopened.initialize()
        instance.store = reopened
        await instance._handle_daily_job(job)
        after = reopened.outbox_items(job.key)[0]
        self.assertEqual((before.uuid, before.content), (after.uuid, after.content))
        self.assertEqual(len(self.messenger.delivered), 1)
        self.assertTrue(reopened.episode_is_delivered(item.episode))

    async def test_bundle_survives_crash_between_freeze_and_outbox(self):
        item = self.item("frozen")
        instance, job, _ = self.setup_job([item])
        instance._persist_daily_items(job, [item])
        with (patch.object(self.store, "ensure_outbox", side_effect=RuntimeError("crash")),
              self.assertRaisesRegex(RuntimeError, "crash")):
            instance._prepare_daily_outbox_and_terminal_states(job)
        self.assertEqual(len(self.store.list_job_results(job.key, "daily_bundle")), 1)
        self.assertEqual(self.store.outbox_items(job.key), [])
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.delivered), 1)

    async def test_unverified_revision_not_included_or_marked_sent(self):
        valid, stale = self.item("valid"), self.item("stale")
        self.store.save_verified_transcript(stale.episode, Transcript("changed", "official", stale.episode.url, True))
        instance, job, _ = self.setup_job([stale, valid])
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertNotIn(stale.message, body(self.messenger.attempts[0]))
        self.assertTrue(self.store.episode_is_delivered(valid.episode))
        self.assertFalse(self.store.episode_is_delivered(stale.episode))

    async def test_failed_recipient_does_not_duplicate_successful_recipient(self):
        item = self.item("shared")
        instance, job, _ = self.setup_job([item])
        self.store.add_subscription("chat_id", "team", source="test")
        deliver = self.messenger.deliver
        failed = False

        def deliver_once(part):
            nonlocal failed
            if part.target_id == "team" and not failed:
                failed = True
                raise ConnectionError("recipient unavailable")
            return deliver(part)

        with patch.object(self.messenger, "deliver", side_effect=deliver_once):
            with self.assertRaises(RuntimeError):
                await instance._handle_daily_job(job)
            self.assertFalse(self.store.episode_is_delivered(item.episode))
            await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.delivered), 2)
        self.assertEqual([p.target_id for p in self.messenger.attempts].count("ou_test"), 1)
        self.assertTrue(self.store.episode_is_delivered(item.episode))

    async def test_retry_sends_only_newly_completed_episode_as_supplement(self):
        first, later = self.item("first"), self.item("later")
        failed = DailyItem(later.episode, "failed", "temporary failure")
        instance, job, podcast = self.setup_job([first, failed])
        podcast.batches.append([later])
        with self.assertRaises(RuntimeError):
            await instance._handle_daily_job(job)
        await instance._handle_daily_job(job)
        bundled = [p for p in self.messenger.attempts if p.group_key.startswith("daily:bundle:")]
        self.assertEqual(len(bundled), 2)
        self.assertNotIn("节目：first", body(bundled[1]))
        self.assertIn("节目：later", body(bundled[1]))
        self.assertIn("补充摘要", body(bundled[1]))

    async def test_empty_daily_once_and_empty_catchup_silent(self):
        instance, job, _ = self.setup_job([])
        await instance._handle_daily_job(job)
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertIn("不代表订阅源没有更新", body(self.messenger.attempts[0]))
        self.store.complete(job.key)
        instance, job, podcast = self.setup_job([], "daily:catchup", {"transcript_catchup": True})
        podcast.build_pending = list
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_catchup_combines_ready_and_completes_backlog(self):
        items = [self.item("ready1"), self.item("ready2")]
        for item in items:
            self.store.defer_daily_transcript(item.episode, "waiting")
        instance, job, podcast = self.setup_job(items, payload={"transcript_catchup": True})
        podcast.build_pending = lambda: items
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertIn("全文已补齐", body(self.messenger.attempts[0]))
        with self.store._connect() as db:
            self.assertEqual({r[0] for r in db.execute("SELECT state FROM daily_transcript_backlog")}, {"delivered"})

    async def test_quoted_bundle_has_all_episodes_but_never_crosses_chat(self):
        items = [self.item("second", 2), self.item("first", 5)]
        instance, job, _ = self.setup_job(items)
        # Use only the explicit group subscription for this test.
        self.store.remove_subscription("open_id", "ou_test")
        self.store.add_subscription("chat_id", "team", source="test")
        await instance._handle_daily_job(job)
        part = self.store.outbox_items(job.key)[0]
        self.store.mark_outbox_sent(part.id, "remote-bundle")
        msg = IncomingMessage("new", "team", "第二期详细说说", "group", "colleague",
                              parent_message_id="remote-bundle")
        context = quoted_context(self.store, msg)
        self.assertEqual([e["title"] for e in context["episodes"]], ["first", "second"])
        self.assertNotIn("episode", context)
        other = IncomingMessage("other", "elsewhere", "第二期", "group", "colleague",
                                parent_message_id="remote-bundle")
        self.assertIsNone(quoted_context(self.store, other))

    async def test_legacy_outbox_keeps_old_format(self):
        item = self.item("legacy")
        instance, job, _ = self.setup_job([item])
        instance._persist_daily_items(job, [item])
        instance._ensure_broadcast(job, "episode:legacy", item.message, "daily:legacy")
        frozen = self.store.outbox_items(job.key)[0]
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertEqual(self.messenger.attempts[0].content, frozen.content)
        self.assertEqual(self.store.list_job_results(job.key, "daily_bundle"), [])


if __name__ == "__main__":
    unittest.main()
