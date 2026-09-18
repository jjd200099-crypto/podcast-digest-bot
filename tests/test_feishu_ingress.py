"""SDK wire-event contract tests; not a substitute for real Feishu E2E."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from lark_channel import ChatQueueConfig, TextBatchConfig
from lark_channel.channel.normalize.pipeline import (
    InboundPipeline,
    PipelineConfig,
    PipelineDeps,
)
from lark_channel.channel.safety.pipeline import SafetyPipeline

from news_officer.runtime import NewsOfficerRuntime
from news_officer.store import Store


class FeishuWireContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.store.initialize()
        self.runtime = NewsOfficerRuntime(
            SimpleNamespace(feishu_app_id='fake-app', feishu_app_secret='fake-secret'),
            self.store, None, None, None,
        )
        self.runtime._main_loop = asyncio.get_running_loop()
        self.normalizer = InboundPipeline(PipelineConfig(account_id='fake-app'), PipelineDeps())
        self.normalizer.set_bot_open_id('ou_bot')
        self.rejections = []
        self.safety = SafetyPipeline(
            loop=asyncio.get_running_loop(), on_message=self.runtime._on_message,
            on_reject=self.rejections.append, policy=self.runtime.channel.get_policy(),
            batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0, max_messages=1, max_chars=10000),
            queue_config=ChatQueueConfig(enabled=False, merge_while_busy=False),
        )
        self.safety.set_bot_open_id('ou_bot')

    async def asyncTearDown(self):
        await self.safety.dispose()
        self.temp.cleanup()

    async def send(self, identifier, *, sender='ou_colleague', chat_type='group',
                   mention='ou_bot', parent='', thread='', text='请把这期展开成详细版。', sender_type='user'):
        mentions = [{'key': '@_user_1', 'id': {'open_id': mention}, 'name': '情报官'}] if mention else []
        wire = {'message_id': identifier, 'chat_id': 'oc_test', 'chat_type': chat_type,
                'message_type': 'text', 'create_time': str(int(time.time() * 1000)),
                'parent_id': parent, 'root_id': parent, 'thread_id': thread,
                'mentions': mentions, 'content': json.dumps({'text': ('@_user_1 ' if mentions else '') + text})}
        inbound = await self.normalizer.process(event_id='event-' + identifier, message_event=wire,
            sender={'sender_type': sender_type, 'sender_id': {'open_id': sender}})
        self.assertIsNotNone(inbound)
        await self.safety.push_message(inbound)
        await asyncio.sleep(0.05)

    def jobs(self):
        with self.store._connect() as db:
            return [dict(r) for r in db.execute('SELECT job_key,session_key,payload_json FROM jobs ORDER BY created_at')]

    async def test_two_colleagues_mentions_are_admitted_without_owner_bypass(self):
        await self.send('one', sender='ou_alice')
        await self.send('two', sender='ou_bob')
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0]['session_key'], jobs[1]['session_key'])
        self.assertEqual(self.rejections, [])

    async def test_dm_without_mention_is_admitted(self):
        await self.send('dm', chat_type='p2p', mention=None)
        self.assertEqual(len(self.jobs()), 1)

    async def test_quoted_group_message_preserves_reply_reference_and_sender(self):
        await self.send('quoted', parent='om_podcast_card', thread='omt_discussion')
        payload = json.loads(self.jobs()[0]['payload_json'])
        self.assertEqual(payload['parent_message_id'], 'om_podcast_card')
        self.assertEqual(payload['thread_id'], 'omt_discussion')
        self.assertEqual(payload['sender_open_id'], 'ou_colleague')
        self.assertIn('这期', payload['text'])

    async def test_duplicate_event_is_one_durable_job(self):
        await self.send('duplicate')
        await self.send('duplicate')
        self.assertEqual(len(self.jobs()), 1)

    async def test_unmentioned_group_message_does_not_trigger_bot(self):
        await self.send('plain', mention=None)
        self.assertEqual(self.jobs(), [])
        self.assertTrue(self.rejections)

    async def test_other_bot_mention_does_not_trigger_our_bot(self):
        await self.send('other', mention='ou_other_bot')
        self.assertEqual(self.jobs(), [])

    async def test_bot_message_does_not_start_a_reply_loop(self):
        await self.send('bot', sender='ou_other_bot', sender_type='app')
        self.assertEqual(self.jobs(), [])


if __name__ == '__main__':
    unittest.main()
