import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_reliability import FakeMessenger, FakePlugin, SequencePodcast, runtime

from news_officer.feishu import FeishuMessenger, combined_delivery_parts, delivery_parts
from news_officer.router import PluginResponse
from news_officer.store import Store


def visible(part):
    value = json.loads(part.content)
    if part.msg_type == "text":
        return value["text"]
    return "\n".join("".join(element.get("text", "") for element in line)
                     for line in value["zh_cn"]["content"])


class ReplyPlugin(FakePlugin):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    def acknowledgement(self, text):
        return None

    def handle(self, text, message):
        self.calls += 1
        self.last_message = message
        return PluginResponse(self.messages)


class CombinedReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "store.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def setup_job(self, kind, messages):
        key = "message:test-" + kind
        payload = {"message_id": "om_" + kind, "chat_id": "oc_" + kind,
                   "chat_type": kind, "sender_open_id": "ou_test", "text": "展开说说"}
        if kind == "group":
            payload["thread_id"] = "omt_test"
        self.store.enqueue(key, "message", payload)
        plugin = ReplyPlugin(messages)
        messenger = FakeMessenger()
        instance = runtime(self.store, messenger, SequencePodcast([]), plugin)
        return instance, self.store.claim_next("message"), plugin, messenger

    async def test_private_and_group_long_answers_are_one_post(self):
        text = "核心判断及完整依据。" * 600
        self.assertGreater(len(text.encode()), 3500)
        for kind in ("p2p", "group"):
            instance, job, plugin, messenger = self.setup_job(kind, (text,))
            await instance._handle_message_job(job)
            self.store.complete(job.key)
            self.assertEqual(plugin.calls, 1)
            self.assertEqual(len(messenger.attempts), 1)
            part = messenger.attempts[0]
            self.assertEqual(part.msg_type, "post")
            self.assertEqual(visible(part), text)
            self.assertEqual(part.reply_in_thread, kind == "group")
            self.assertEqual(part.target_id, "om_" + kind)

    async def test_multiple_result_blocks_merge_and_retry_without_duplicates(self):
        blocks = ("第一部分：" + "依据一。" * 500, "第二部分：" + "依据二。" * 500)
        instance, job, plugin, messenger = self.setup_job("group", blocks)
        messenger.fail_groups_once.add("message:result:1")
        with self.assertRaises(RuntimeError):
            await instance._handle_message_job(job)
        frozen = self.store.outbox_items(job.key)
        self.assertEqual(len(frozen), 1)
        reopened = Store(self.store.path)
        reopened.initialize()
        instance.store = reopened
        await instance._handle_message_job(job)
        self.assertEqual(plugin.calls, 1)
        self.assertEqual(len(messenger.delivered), 1)
        self.assertEqual([(p.content, p.uuid) for p in reopened.outbox_items(job.key)],
                         [(p.content, p.uuid) for p in frozen])
        text = visible(next(iter(messenger.delivered.values())))
        for block in blocks:
            self.assertEqual(text.count(block), 1)

    async def test_one_large_answer_uses_single_text_before_splitting(self):
        text = "论点和依据。" * 5000
        instance, job, _, messenger = self.setup_job("p2p", (text,))
        await instance._handle_message_job(job)
        self.assertEqual(len(messenger.attempts), 1)
        self.assertEqual(messenger.attempts[0].msg_type, "text")
        self.assertTrue(visible(messenger.attempts[0]).endswith(text))

    async def test_legacy_partial_reply_keeps_old_parts_and_ids(self):
        blocks = ("旧的第一部分。" * 600, "旧的第二部分。" * 200)
        instance, job, plugin, messenger = self.setup_job("group", blocks)
        self.store.save_job_result(job.key, "message:analysis", "message",
                                   {"messages": list(blocks), "attachment_episode_ids": []})
        key = job.key + ":result:1"
        parts = delivery_parts(blocks[0], key)
        self.assertGreater(len(parts), 1)
        self.store.ensure_outbox(job_key=job.key, group_key="message:result:1", delivery_key=key,
                                 operation="reply", target_id=job.payload["message_id"], target_type="",
                                 reply_in_thread=True, parts=parts)
        frozen = self.store.outbox_items(job.key)
        self.store.mark_outbox_sent(frozen[0].id, "old_receipt")
        await instance._handle_message_job(job)
        self.assertEqual(plugin.calls, 0)
        after = self.store.outbox_items(job.key)
        self.assertEqual([(p.content, p.uuid) for p in after[:len(frozen)]],
                         [(p.content, p.uuid) for p in frozen])
        self.assertNotIn(frozen[0].uuid, messenger.delivered)
        self.assertEqual(visible(after[-1]), blocks[1])

    async def test_explicit_attachment_stays_separate_and_idempotent(self):
        instance, job, _, _ = self.setup_job("p2p", ())
        text = "完整分析。" * 1000
        args = (job, "message:result:1", text, job.payload["message_id"], job.key + ":result:1", False, ("file_test",))
        instance._ensure_reply(*args)
        first = self.store.outbox_items(job.key)
        instance._ensure_reply(*args)
        self.assertEqual([p.msg_type for p in first], ["post", "file"])
        self.assertEqual(self.store.outbox_items(job.key), first)


class DirectReplyTests(unittest.TestCase):
    def test_direct_send_and_reply_use_one_message(self):
        messenger = FeishuMessenger("app", "secret")
        text = "研究结论及依据。" * 700
        with patch.object(messenger, "deliver", return_value="receipt") as send:
            messenger.reply(text, "om_test", "direct_reply")
            messenger.send(text, "ou_test", "open_id", "direct_send")
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertEqual(call.args[0].total_parts, 1)
            self.assertEqual(visible(call.args[0]), text)

    def test_extreme_answer_uses_near_capacity_not_small_chunks(self):
        text = "甲" * 90_000
        parts = combined_delivery_parts(text, "extreme")
        self.assertEqual(len(parts), 2)
        recovered = "".join(json.loads(content)["text"].split("\n\n", 1)[1]
                            for _, content, _ in parts)
        self.assertEqual(recovered, text)


if __name__ == "__main__":
    unittest.main()
