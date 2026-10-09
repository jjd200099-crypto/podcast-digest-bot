import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import test_combined_daily as fixtures
from test_combined_daily import body

from news_officer.daily_archive import read_daily_digest
from news_officer.delivery_watchdog import is_daily_receipt
from news_officer.models import DailyItem, Episode, Job


class ReaderDailyTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CombinedDailyTests.setUp
    tearDown = fixtures.CombinedDailyTests.tearDown
    item = fixtures.CombinedDailyTests.item

    def setup_job(self, items, **kwargs):
        instance, job, podcast = fixtures.CombinedDailyTests.setup_job(self, items, **kwargs)
        instance.settings.daily_reader_mode = True
        podcast.build_pending = Mock(return_value=[])
        return instance, job, podcast

    async def test_only_editorial_content_in_one_message_and_archive(self):
        selected = self.item('selected')
        other = Episode('missing', 'Missing title', 'https://example.test/missing', 'Show')
        items = [selected, DailyItem(other, 'no_transcript', 'Podwise 暂时限流'),
                 DailyItem(other, 'discovery_status', '17 个读取请求失败'),
                 DailyItem(other, 'failed', 'offline')]
        instance, job, _ = self.setup_job(items, key='daily:2026-10-09',
            payload={'scheduled_for': '2026-10-09T08:30:00+08:00'})
        with patch('news_officer.release_notes.RELEASE_NOTES', ()):
            await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        rendered = body(self.messenger.attempts[0])
        self.assertIn('selected 的核心判断', rendered)
        for forbidden in ('情报官日报｜', '统计窗口', '阅读优先级', '未经独立审计',
                          'Podwise', 'Missing title', '播客追踪状态', '---', '读取请求失败'):
            self.assertNotIn(forbidden, rendered)
        title = json.loads(self.messenger.attempts[0].content)['zh_cn']['title']
        self.assertEqual(title, '🎧 播客精选 · 2026-10-09')
        self.assertTrue(self.store.analysis_complete(job.key))
        self.assertEqual(self.store.get_job_result(job.key, 'daily:reader-scan')['counts']['failed'], 1)
        archived = read_daily_digest(self.store, '2026-10-09')['markdown']
        self.assertIn('selected 的核心判断', archived)
        self.assertNotIn('Podwise', archived)
        self.assertNotIn('播客追踪状态', archived)

    async def test_later_arrivals_wait_for_next_edition(self):
        instance, job, podcast = self.setup_job([self.item('current')])
        podcast.build_pending.return_value = [self.item('previous')]
        await instance._handle_daily_job(job)
        text = body(self.messenger.attempts[0])
        self.assertIn('current 的核心判断', text)
        self.assertIn('previous 的核心判断', text)
        self.assertEqual(len(self.messenger.attempts), 1)
        podcast.build_pending.assert_called_once_with(exclude_ids={'current'})

    async def test_queued_catchup_retired_without_sending_or_fetching(self):
        instance, job, podcast = self.setup_job([self.item('late')], payload={'transcript_catchup': True})
        await instance._handle_daily_job(job)
        self.assertEqual(self.messenger.attempts, [])
        self.assertEqual(podcast.calls, 0)
        podcast.build_pending.assert_not_called()
        self.assertTrue(self.store.analysis_complete(job.key))

    async def test_backlog_is_labelled_as_old_not_as_todays_release(self):
        instance, job, _ = self.setup_job([self.item('older')], payload={
            'scheduled_for': (datetime.now(UTC) + timedelta(days=3)).isoformat()})
        await instance._handle_daily_job(job)
        self.assertIn('补齐旧节目', body(self.messenger.attempts[0]))

    async def test_platform_overflow_never_silently_sends_multiple_messages(self):
        instance, job, _ = self.setup_job([self.item('one')])
        with (patch('news_officer.runtime.combined_delivery_parts', return_value=[
                ('text', 'first', 'uuid1'), ('text', 'second', 'uuid2')]),
              self.assertRaisesRegex(ValueError, 'single-message capacity')):
            await instance._handle_daily_job(job)
        self.assertEqual(self.messenger.attempts, [])
        self.assertEqual(self.store.outbox_items(job.key), [])

    async def test_source_failure_retries_without_group_status_spam(self):
        instance, job, podcast = self.setup_job([])
        podcast.build_daily = Mock(side_effect=ConnectionError('offline'))
        with self.assertRaises(ConnectionError):
            await instance._handle_daily_job(job)
        self.assertFalse(self.store.analysis_complete(job.key))
        self.assertEqual(self.messenger.attempts, [])

    async def test_document_retry_does_not_rescan_or_send_early(self):
        instance, job, podcast = self.setup_job([self.item('selected', 5)])
        compiler = Mock()
        compiler.snapshot.return_value = ('revision', ['selected'], [])
        compiler.publish.side_effect = [TimeoutError('retry'),
            {'documents': [{'title': 'Selected', 'url': 'https://example.test/doc'}]}]
        instance.document_compiler = compiler
        with self.assertRaises(TimeoutError):
            await instance._handle_daily_job(job)
        self.assertEqual(self.messenger.attempts, [])
        await instance._handle_daily_job(job)
        self.assertEqual(podcast.calls, 1)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertIn('https://example.test/doc', self.messenger.attempts[0].content)
        await instance._handle_daily_job(job)
        self.assertEqual(compiler.publish.call_count, 2)
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_send_retry_preserves_exactly_one_frozen_bundle(self):
        instance, job, podcast = self.setup_job([self.item('one')])
        deliver = self.messenger.deliver
        with (patch.object(self.messenger, 'deliver', side_effect=TimeoutError('lost')),
              self.assertRaises(RuntimeError)):
            await instance._handle_daily_job(job)
        saved = self.store.outbox_items(job.key)[0]
        with patch.object(self.messenger, 'deliver', side_effect=deliver):
            await instance._handle_daily_job(job)
        self.assertEqual(podcast.calls, 1)
        self.assertEqual(len(self.store.outbox_items(job.key)), 1)
        self.assertEqual(self.store.outbox_items(job.key)[0].content, saved.content)

    async def test_automatic_document_notification_is_silent_but_requests_still_work(self):
        instance, _, _ = self.setup_job([])
        instance.document_compiler = Mock()
        await instance._handle_document_job(Job('old', 'document', {'mode': 'selected_episodes'}, 1))
        instance.document_compiler.publish.assert_not_called()
        instance.document_compiler.publish.return_value = None
        await instance._handle_document_job(Job('request', 'document', {'mode': 'requested_episode'}, 1))
        instance.document_compiler.publish.assert_called_once()

    async def test_disabled_discovery_backlog_is_retained_but_not_polled(self):
        for identity in ('podwise:123', 'rss:abc'):
            self.store.defer_daily_transcript(Episode(identity, identity, 'https://example.test/e', 'Show'), 'pending')
        with self.store._connect() as db:
            db.execute('UPDATE daily_transcript_backlog SET next_check_at=?',
                       ((datetime.now(UTC) - timedelta(days=1)).isoformat(),))
        self.assertEqual([e.id for e in self.store.due_daily_transcripts(include_discovery=False)], ['rss:abc'])
        self.assertEqual(len(self.store.due_daily_transcripts()), 2)

    async def test_reader_title_still_verifies_independent_receipt(self):
        now = datetime(2026, 10, 9, 1, tzinfo=UTC)
        message = {'sender': {'sender_type': 'app', 'id_type': 'app_id', 'id': 'bot'},
                   'create_time': str(int(now.timestamp() * 1000)),
                   'body': {'content': json.dumps({'zh_cn': {'title': '🎧 播客精选 · 2026-10-09'}})}}
        self.assertTrue(is_daily_receipt(message, 'bot', now.date(), now.timestamp()-1, now.timestamp()+1))
        self.assertFalse(is_daily_receipt(message, 'bot', (now-timedelta(days=1)).date(), 0, now.timestamp()+1))
