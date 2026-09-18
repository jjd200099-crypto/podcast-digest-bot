import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from test_reliability import FakeMessenger, FakePlugin, SequencePodcast, runtime

from news_officer.health import health_snapshot, watch_health
from news_officer.models import IncomingMessage
from news_officer.response_quality import completeness_error
from news_officer.store import Store


class ConversationQualityTests(unittest.TestCase):
    def test_process_exits_even_when_executor_thread_cannot_be_cancelled(self):
        code = '''
import asyncio, time
from news_officer.__main__ import run_service
class Runtime:
    async def run(self):
        asyncio.create_task(asyncio.to_thread(time.sleep, 60))
        await asyncio.sleep(0.05)
        print('stuck executor started', flush=True)
        self.on_shutdown()
        raise RuntimeError('simulated fatal health failure')
run_service(Runtime(), shutdown_grace=0.1)
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=8,
            check=False, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        self.assertIn(b'stuck executor started', result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_truncated_outputs_rejected(self):
        for text in ('主要错误有：', '我可以帮你追踪和研究已订阅播客：', '## 结论', '1.', '```python\nprint(1)', '好的，我会检查一下。'):
            with self.subTest(text=text):
                self.assertIsNotNone(completeness_error(text))

    def test_complete_short_or_multilingual_replies_allowed(self):
        for text in ('你好，有什么想讨论的？', 'OpenAI Agents SDK', '结论：可以\n原因：已收到确认', '```python\nprint(1)\n```', 'https://example.com', '1. 第一条\n2. 第二条'):
            with self.subTest(text=text):
                self.assertIsNone(completeness_error(text))

    def test_health_detects_connection_loss_and_stalled_request_without_secrets(self):
        channel = SimpleNamespace(connection_snapshot=lambda: SimpleNamespace(ready=True, state='connected', last_error='secret'))
        self.assertTrue(health_snapshot(channel, {}, now=1300)['ok'])
        result = health_snapshot(channel, {'private-message-id': 0}, now=1300)
        self.assertFalse(result['ok'])
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('private-message-id', json.dumps(result))
        channel.connection_snapshot = lambda: SimpleNamespace(ready=True, state='reconnecting')
        self.assertFalse(health_snapshot(channel, {}, now=1300)['ok'])


class ConcurrentSessionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, identifier, sender='alice', chat='team'):
        message = IncomingMessage(identifier, chat, 'hello', 'group', sender)
        self.store.enqueue('message:' + identifier, 'message', message.__dict__)

    def test_same_session_fifo_but_other_member_not_blocked(self):
        self.enqueue('a1')
        self.enqueue('a2')
        self.enqueue('b1', 'bob')
        first = self.store.claim_next('message')
        self.assertEqual(first.key, 'message:a1')
        self.assertEqual(self.store.claim_next('message').key, 'message:b1')
        self.assertIsNone(self.store.claim_next('message'))
        self.store.fail(first.key, 'temporary', first.attempts)
        self.assertIsNone(self.store.claim_next('message'))
        self.store.complete(first.key)
        self.assertEqual(self.store.claim_next('message').key, 'message:a2')

    def test_claims_are_atomic_across_real_threads(self):
        for user in range(8):
            for turn in range(3):
                self.enqueue(f'{user}-{turn}', f'user-{user}')
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.store.claim_next('message'), range(16)))
        keys = [j.key for j in jobs if j]
        self.assertEqual(len(keys), 8)
        self.assertEqual(len(set(keys)), 8)
        self.assertTrue(all(k.endswith('-0') for k in keys))

    async def test_slow_member_does_not_block_fast_member_and_progress_is_durable(self):
        release = threading.Event()

        class SlowPlugin(FakePlugin):
            def acknowledgement(self, text):
                return None

            def handle(self, text, message):
                if message.sender_open_id == 'alice':
                    release.wait(3)
                return super().handle(text, message)

        plugin, messenger = SlowPlugin(), FakeMessenger()
        instance = runtime(self.store, messenger, SequencePodcast([]), plugin)
        instance.settings = SimpleNamespace(progress_delay_seconds=0.01)
        self.enqueue('slow')
        self.enqueue('fast', 'bob')
        slow = asyncio.create_task(instance._handle_message_job(self.store.claim_next('message')))
        try:
            await asyncio.wait_for(instance._handle_message_job(self.store.claim_next('message')), 1)
            self.assertFalse(slow.done())
            await asyncio.sleep(0.05)
        finally:
            release.set()
            await slow
        with self.store._connect() as db:
            progress = db.execute("SELECT content FROM outbox WHERE group_key='message:progress'").fetchall()
            self.assertEqual(len(progress), 1)
            self.assertIn('仍在处理中', json.dumps(json.loads(progress[0][0]), ensure_ascii=False))

    def test_restart_preserves_session_order(self):
        self.enqueue('a1')
        self.enqueue('a2')
        self.store.claim_next('message')
        self.assertEqual(self.store.recover_interrupted_jobs(), 1)
        self.assertEqual(self.store.claim_next('message').key, 'message:a1')
        self.assertIsNone(self.store.claim_next('message'))

    async def test_watchdog_fails_process_on_persistent_bad_health(self):
        with self.assertRaisesRegex(RuntimeError, 'health watchdog'):
            await asyncio.wait_for(watch_health(lambda: {'ok': False}, interval=0.001, grace=0.002), 1)


if __name__ == '__main__':
    unittest.main()
