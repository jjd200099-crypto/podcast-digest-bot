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

    async def test_release_notes_default_off_and_opt_in(self):
        from news_officer.daily_archive import read_daily_digest

        for enabled in (False, True):
            day = '2026-10-04' if not enabled else '2026-10-05'
            instance, job, _ = self.setup_job([self.item(day)], key=f'daily:{day}',
                payload={'scheduled_for': f'{day}T08:30:00+08:00'})
            instance.settings.daily_release_notes = enabled
            await instance._handle_daily_job(job)
            self.assertEqual('功能更新' in body(self.messenger.attempts[-1]), enabled)
            self.assertEqual('功能更新' in read_daily_digest(self.store, day)['markdown'], enabled)
            self.store.complete(job.key)

    async def test_chat_allowlist_skips_invalid_ids_and_private_subscriptions(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        instance.settings.daily_chat_ids = ('oc_good', 'bad', 'ou_private', 'oc_bad space', 'oc_good')
        with self.assertLogs('news_officer.runtime', level='WARNING'):
            await instance._handle_daily_job(job)
        self.assertEqual([(i.target_type, i.target_id) for i in self.messenger.attempts],
                         [('chat_id', 'oc_good')])
        self.assertEqual(instance._daily_targets(job), [('chat_id', 'oc_good')])
        self.store.remove_subscription('open_id', 'ou_test')
        self.assertTrue(instance._has_daily_recipients())

    async def test_unconfigured_allowlist_preserves_valid_subscriptions(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        self.store.add_subscription('chat_id', 'oc_other')
        self.store.add_subscription('open_id', 'invalid')
        with self.assertLogs('news_officer.runtime', level='WARNING'):
            await instance._handle_daily_job(job)
        self.assertEqual({(i.target_type, i.target_id) for i in self.messenger.attempts},
                         {('open_id', 'ou_test'), ('chat_id', 'oc_other')})

    async def test_required_allowlist_fails_closed_without_sending(self):
        for allowlist in ((), ('bad', 'ou_private', 'oc_bad space')):
            instance, job, podcast = self.setup_job([self.item('selected')], key=f'daily:req-{len(allowlist)}')
            instance.settings.daily_chat_ids = allowlist
            instance.settings.daily_require_chat_ids = True
            with self.assertRaisesRegex(RuntimeError, 'NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS'):
                await instance._handle_daily_job(job)
            self.assertEqual(self.messenger.attempts, [])
            self.assertEqual(self.store.outbox_items(job.key), [])
            self.assertIsNone(self.store.get_job_result(job.key, 'daily:recipients'))
            self.assertEqual(podcast.calls, 0)
            with self.assertRaises(RuntimeError):
                instance._daily_targets(job)
            # The scheduler must not crash; the job itself fails loudly instead.
            self.assertTrue(instance._has_daily_recipients())
            self.assertEqual(instance._valid_daily_targets([('open_id', 'ou_test')]), [])
        # A prepare-only warm-up fails too instead of silently spending capacity.
        instance, job, _ = self.setup_job([], key='daily:prepare:req', payload={
            'prepare_only': True, 'scheduled_for': '2099-01-01T08:30:00+08:00'})
        instance.settings.daily_chat_ids = ()
        instance.settings.daily_require_chat_ids = True
        with self.assertRaises(RuntimeError):
            await instance._handle_daily_job(job)

    async def test_required_allowlist_with_valid_id_sends_only_to_it(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        instance.settings.daily_chat_ids = ('oc_good',)
        instance.settings.daily_require_chat_ids = True
        await instance._handle_daily_job(job)
        self.assertEqual([(i.target_type, i.target_id) for i in self.messenger.attempts],
                         [('chat_id', 'oc_good')])

    async def test_require_flag_defaults_off_so_unset_allowlist_still_delivers(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        self.assertFalse(getattr(instance.settings, 'daily_require_chat_ids', False))
        await instance._handle_daily_job(job)
        self.assertEqual([(i.target_type, i.target_id) for i in self.messenger.attempts],
                         [('open_id', 'ou_test')])

    async def test_subscription_fallback_warns_once_per_job(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs:
            instance._daily_candidates(job)
            instance._daily_candidates(job)
        warnings = [line for line in logs.output if 'NEWS_OFFICER_DAILY_CHAT_IDS is not set' in line]
        self.assertEqual(len(warnings), 1)
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs:
            instance._daily_candidates(Job('daily:other', 'daily', {}, 1))
        self.assertEqual(sum('is not set' in line for line in logs.output), 1)
        instance.settings.daily_chat_ids = ('oc_good',)
        with self.assertNoLogs('news_officer.runtime', level='WARNING'):
            instance._daily_candidates(Job('daily:third', 'daily', {}, 1))

    async def test_private_subscription_entries_never_receive_with_allowlist(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        # Same writes as FEISHU_USER_OPEN_IDS seeding and the private 'subscribe' command.
        self.store.seed_subscriptions(('ou_seed',), ('oc_seeded_group',))
        self.store.add_subscription('open_id', 'ou_command', source='command')
        instance.settings.daily_chat_ids = ('oc_good',)
        # A snapshot frozen before the allowlist existed still names old open_ids.
        self.store.save_job_result(job.key, 'daily:recipients', 'delivery_targets', {'targets': [
            ['open_id', 'ou_test'], ['open_id', 'ou_old'], ['chat_id', 'oc_seeded_group'],
            ['chat_id', 'oc_good']]})
        await instance._handle_daily_job(job)
        self.assertEqual([(i.target_type, i.target_id) for i in self.messenger.attempts],
                         [('chat_id', 'oc_good')])
        self.assertEqual([(i.target_type, i.target_id) for i in self.store.outbox_items(job.key)],
                         [('chat_id', 'oc_good')])
        self.assertEqual(instance._daily_targets(job), [('chat_id', 'oc_good')])

    async def test_allowlist_blocks_previously_frozen_private_outbox(self):
        instance, job, _ = self.setup_job([self.item('selected')])
        instance._persist_daily_items(job, [self.item('selected')])
        instance._prepare_daily_outbox_and_terminal_states(job)
        instance.settings.daily_chat_ids = ('oc_good',)
        await instance._drain_outbox(job)
        self.assertEqual(self.messenger.attempts, [])

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

    async def test_allowlist_blocks_frozen_document_outbox(self):
        instance, _, _ = self.setup_job([])
        job = Job('document:old', 'document', {'mode': 'selected_episodes'}, 1)
        self.store.enqueue(job.key, job.kind, job.payload)
        for kind, target in [('open_id', 'ou_old'), ('chat_id', 'oc_other'), ('chat_id', 'oc_good')]:
            self.store.ensure_outbox(job_key=job.key, group_key='document:link',
                delivery_key=target, operation='send', target_id=target, target_type=kind,
                reply_in_thread=False, parts=[('text', 'link', target)])
        instance.settings.daily_chat_ids = ('oc_good',)
        await instance._drain_outbox(job)
        self.assertEqual([(i.target_type, i.target_id) for i in self.messenger.attempts],
                         [('chat_id', 'oc_good')])

    async def test_inline_value_error_delivers_body_and_retries_without_freezing_empty(self):
        instance, job, podcast = self.setup_job([self.item('selected', 5)])
        compiler = Mock()
        compiler.snapshot.return_value = ('revision', ['selected'], [])
        compiler.publish.side_effect = [ValueError('Shownotes validation failed'),
            {'documents': [{'title': 'Selected', 'url': 'https://example.test/doc'}]}]
        instance.document_compiler = compiler
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs:
            await instance._handle_daily_job(job)
        warnings = [r for r in logs.records if 'compilation deferred' in r.getMessage()]
        self.assertTrue(warnings[0].exc_info)
        self.assertIn('Traceback', '\n'.join(logs.output))
        self.assertIsNone(self.store.get_job_result(job.key, 'daily:inline-documents'))
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertNotIn('https://example.test/doc', self.messenger.attempts[0].content)
        retry = self.store.claim_next('document')
        self.assertEqual(retry.payload['mode'], 'inline_daily_retry')
        await instance._handle_document_job(retry)
        self.assertEqual(self.store.get_job_result(job.key, 'daily:inline-documents')['documents'],
                         [{'title': 'Selected', 'url': 'https://example.test/doc'}])
        self.assertEqual(podcast.calls, 1)
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_inline_snapshot_changed_still_raises(self):
        instance, job, _ = self.setup_job([self.item('selected', 5)])
        compiler = Mock()
        compiler.snapshot.return_value = ('revision', ['selected'], [])
        compiler.publish.return_value = None
        instance.document_compiler = compiler
        with self.assertRaisesRegex(RuntimeError, 'snapshot changed'):
            instance._inline_daily_documents(job, datetime.now(UTC))
        self.assertIsNone(self.store.get_job_result(job.key, 'daily:inline-documents'))

    async def test_inline_library_error_returns_empty_then_normal_result_is_cached(self):
        from news_officer.library import LibraryError

        instance, job, _ = self.setup_job([self.item('selected', 5)])
        compiler = Mock()
        compiler.snapshot.side_effect = [LibraryError('retry'), ('revision', ['selected'], [])]
        documents = [{'title': 'Selected', 'url': 'https://example.test/doc'}]
        compiler.publish.return_value = {'documents': documents}
        instance.document_compiler = compiler
        when = datetime.now(UTC)
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs:
            self.assertEqual(instance._inline_daily_documents(job, when), [])
        self.assertTrue(logs.records[0].exc_info)
        self.assertIsNone(self.store.get_job_result(job.key, 'daily:inline-documents'))
        self.assertEqual(instance._inline_daily_documents(job, when), documents)
        self.assertEqual(instance._inline_daily_documents(job, when), documents)
        compiler.publish.assert_called_once()

    async def test_worker_failure_logs_stack_without_sensitive_error_message(self):
        import asyncio
        from unittest.mock import AsyncMock

        instance, job, _ = self.setup_job([])
        instance.wake_workers = {'daily': asyncio.Event()}
        instance._handle_daily_job = AsyncMock(side_effect=ValueError('secret-token-in-url'))
        instance._failure_notice = AsyncMock()
        with (patch.object(self.store, 'claim_next', side_effect=[job, asyncio.CancelledError()]),
              self.assertLogs('news_officer.runtime', level='ERROR') as logs,
              self.assertRaises(asyncio.CancelledError)):
            await instance._worker('daily')
        self.assertTrue(logs.records[0].exc_info)
        output = '\n'.join(logs.output)
        self.assertIn('Traceback', output)
        self.assertIn('_worker', output)
        self.assertNotIn('secret-token-in-url', output)


    async def test_invalid_optional_allowlist_logs_and_operations_warns(self):
        import os

        from news_officer.operations import business_snapshot

        instance, _, _ = self.setup_job([])
        instance.settings.daily_chat_ids = ('bad', 'ou_wrong')
        instance.settings.daily_require_chat_ids = False
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs:
            self.assertFalse(instance._has_daily_recipients())
        self.assertTrue(any('daily_allowlist_invalid' in r.getMessage() for r in logs.records))
        with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_CHAT_IDS': 'bad,ou_wrong',
                                    'NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS': 'false'}, clear=True):
            warnings = business_snapshot(self.store.path)['warnings']
        self.assertTrue(any(w.startswith('daily_allowlist_invalid:') for w in warnings))

    async def test_real_mixed_episode_validation_delivers_body_and_compiles_good_episode(self):
        from test_daily_document import notes
        from test_episode_document import SelectedDocuments

        test = SelectedDocuments()
        test.setUp()
        self.addCleanup(test.doCleanups)
        f = test.fixture
        f.add_episode('two')
        test.rate('two', 5)
        def generate(record):
            value = notes(record)
            if record.episode.id == 'one':
                value['core'][0]['evidence_ids'] = ['invented']
            return value
        f.writer.generate.side_effect = generate
        instance, job, _ = self.setup_job([self.item('one', 5), self.item('two', 5)],
            key=f.base, payload={'scheduled_for': '2026-09-21T08:30:00+08:00'})
        instance.document_compiler = test.compiler
        test.compiler.audience_filter = instance._document_audience
        with self.assertLogs(level='WARNING'):
            await instance._handle_daily_job(job)
        self.assertEqual(f.api.creates, 1)
        self.assertTrue(f.api.files[0]['name'].startswith('two｜'))
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertNotIn('https://www.feishu.cn/docx/', self.messenger.attempts[0].content)
        self.assertIsNone(self.store.get_job_result(job.key, 'daily:inline-documents'))
        self.assertIsNone(f.store.get_job_result(job.key, 'episodes:content'))
        retry = self.store.claim_next('document')
        self.assertEqual(retry.payload['mode'], 'inline_daily_retry')
        f.writer.generate.side_effect = notes
        await instance._handle_document_job(retry)
        self.assertEqual(f.api.creates, 2)
        self.assertEqual(len(self.store.get_job_result(job.key, 'daily:inline-documents')['documents']), 2)
        self.assertEqual(len(self.messenger.attempts), 1)
