import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from datetime import time as clock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from test_reliability import FakeMessenger, runtime

from news_officer.delivery_watchdog import CheckError
from news_officer.recovery import incoming_from_history, recover_daily, recover_mentions
from news_officer.store import Store


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'db.sqlite3')
        self.store.initialize()
        self.store.add_subscription('chat_id', 'oc_group')
        self.settings = SimpleNamespace(timezone=ZoneInfo('Asia/Shanghai'), daily_time=clock(8, 30),
            research_group_chat_ids=('oc_group',), feishu_app_id='cli_bot', feishu_app_secret='secret')
        self.now = datetime(2026, 10, 8, tzinfo=UTC)
        self.start = datetime(2026, 10, 6, 18, tzinfo=UTC)
        self.end = datetime(2026, 10, 7, 20, tzinfo=UTC)
        self.message = {'message_id': 'om_request', 'msg_type': 'text',
            'sender': {'sender_type': 'user', 'id_type': 'open_id', 'id': 'ou_colleague'},
            'create_time': str(int(datetime(2026, 10, 7, 8, tzinfo=UTC).timestamp()*1000)),
            'body': {'content': json.dumps({'text': '@_user_1 总结 Noam Brown 的观点'})},
            'mentions': [{'id': 'ou_bot', 'key': '@_user_1'}],
            'thread_id': 'omt_thread', 'parent_id': 'om_parent'}

    def test_daily_preview_and_stable_key_deduplication(self):
        args = self.store, self.settings, date(2026, 10, 7)
        self.assertFalse(recover_daily(*args, now=self.now)['enqueued'])
        self.assertIsNone(self.store.job_status('daily:2026-10-07'))
        self.assertTrue(recover_daily(*args, now=self.now, execute=True)['enqueued'])
        self.assertFalse(recover_daily(*args, now=self.now, execute=True)['enqueued'])
        job = self.store.claim_next('daily')
        self.assertEqual(job.payload['recovery_window_end'], '2026-10-07T08:30:00+08:00')

    def test_current_future_and_old_days_rejected(self):
        for day in (date(2026, 10, 8), date(2026, 10, 9), date(2026, 9, 20)):
            with self.assertRaises(ValueError):
                recover_daily(self.store, self.settings, day, now=self.now, execute=True)

    async def test_worker_scans_original_window_and_labels_backfill(self):
        recover_daily(self.store, self.settings, date(2026, 10, 7), now=self.now, execute=True)
        podcast = Mock()
        podcast.build_daily.return_value = []
        messenger = FakeMessenger()
        instance = runtime(self.store, messenger, podcast)
        instance.settings.daily_combined_message = True
        instance.settings.timezone = self.settings.timezone
        instance.settings.lookback_hours = 24
        job = self.store.claim_next('daily')
        await instance._handle_daily_job(job)
        podcast.build_daily.assert_called_once_with(now=datetime(2026, 10, 7, 8, 30, tzinfo=self.settings.timezone))
        content = ''.join(item.content for item in self.store.outbox_items(job.key))
        self.assertIn('停机补发', content)
        self.assertIn('2026-10-07', content)

    def test_exact_bot_mention_and_context_preserved(self):
        value = incoming_from_history(self.message, 'oc_group', 'ou_bot', self.start, self.end)
        self.assertEqual(value.text, '总结 Noam Brown 的观点')
        self.assertEqual(value.sender_open_id, 'ou_colleague')
        self.assertEqual(value.parent_message_id, 'om_parent')
        self.assertEqual(value.thread_id, 'omt_thread')
        for changes in ({'deleted': True}, {'mentions': [{'id': 'ou_other'}]},
                        {'sender': {'sender_type': 'app'}}, {'create_time': '0'},
                        {'body': {'content': 'invalid'}}, {'msg_type': 'image'}):
            self.assertIsNone(incoming_from_history({**self.message, **changes}, 'oc_group', 'ou_bot', self.start, self.end))

    def test_replay_is_idempotent_and_preview_does_not_enqueue(self):
        def read(*args):
            return [self.message]
        with patch('news_officer.recovery.api', side_effect=lambda *args, **kwargs:
                {'tenant_access_token': 'hidden'} if args[1] == 'POST' else {'bot': {'open_id': 'ou_bot'}}), \
                patch('news_officer.recovery.history', side_effect=read):
            args = self.store, self.settings, 'oc_group', self.start, self.end
            self.assertEqual(recover_mentions(*args, now=self.now)['queued'], 0)
            self.assertEqual(recover_mentions(*args, now=self.now, execute=True)['queued'], 1)
            self.assertEqual(recover_mentions(*args, now=self.now, execute=True)['existing_jobs'], 1)
            self.assertEqual(recover_mentions(*args, now=self.now, execute=True)['queued'], 0)

    def test_permission_failure_and_wrong_chat_never_enqueue(self):
        with patch('news_officer.recovery.api', side_effect=CheckError('no permission')), self.assertRaises(CheckError):
            recover_mentions(self.store, self.settings, 'oc_group', self.start, self.end, now=self.now, execute=True)
        self.assertIsNone(self.store.claim_next('message'))
        with self.assertRaises(ValueError):
            recover_mentions(self.store, self.settings, 'other', self.start, self.end, now=self.now, execute=True)
