import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import test_combined_daily as fixtures
from test_combined_daily import body

from news_officer.daily_archive import read_daily_digest
from news_officer.delivery_watchdog import is_daily_receipt
from news_officer.models import DailyItem, Episode, Job, Transcript


class ReaderDailyTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CombinedDailyTests.setUp
    tearDown = fixtures.CombinedDailyTests.tearDown
    item = fixtures.CombinedDailyTests.item

    def setup_job(self, items, **kwargs):
        instance, job, podcast = fixtures.CombinedDailyTests.setup_job(self, items, **kwargs)
        instance.settings.daily_reader_mode = True
        podcast.build_pending = Mock(return_value=[])
        return instance, job, podcast

    async def test_compact_flag_reuses_one_rewrite_in_enrichment_template(self):
        episode = Episode('flag', 'Show', 'https://example.test/flag', 'Show',
                          published_at=datetime.now(UTC))
        selected = DailyItem(episode, 'summarized', ('## Guest｜产品\n节目：Show｜Show\n\n公司认为需求可能增长。'
                            '团队强调评测。后段讨论风险。仍需验证。更多背景讨论。\n推荐理由：评测和风险。\n推荐星级：★★★★☆'))
        record = self.store.save_verified_transcript(episode,
            Transcript('Complete source', 'official', episode.url, True))
        self.store.save_transcript_digest(selected.episode.id, selected.message,
            record.content_sha256, record.record_revision_sha256)
        selected = replace(selected, attachment=fixtures.attachment_for(record, selected.message))
        instance, job, podcast = self.setup_job([selected])
        model = Mock(model='mock-model')
        model.rewrite_paragraphs.return_value = ['公司认为需求可能增长。', '需验证。']
        podcast.summarizer = model
        instance.settings.daily_compact_paragraphs = True
        await instance._handle_daily_job(job)
        model.rewrite_paragraphs.assert_called_once()
        bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
        section = bundle['enrichment']['sections'][0]
        for key in ('body', 'template'):
            self.assertNotIn('• ', section[key])
            self.assertIn('公司认为需求可能增长。', section[key])
            self.assertNotIn('Show｜Show', section[key])

    async def test_default_reader_does_not_rewrite(self):
        selected = self.item('default')
        instance, job, podcast = self.setup_job([selected])
        podcast.summarizer = Mock()
        await instance._handle_daily_job(job)
        podcast.summarizer.rewrite_paragraphs.assert_not_called()
        self.assertNotIn('• ', body(self.messenger.attempts[0]))

    async def test_flag_off_runtime_matches_pre_51_bytes_and_link_order(self):
        from news_officer.daily_enrichment import DOCUMENT_PLACEHOLDER, _content
        from news_officer.feishu import combined_delivery_parts

        # Golden prose follows runtime.py and reader_episode at 3848757 (before #51).
        # Interior trailing spaces must survive publication and the edit template.
        for fulltext in (False, True):
            with self.subTest(fulltext=fulltext):
                name = f'legacy-{fulltext}'
                selected = self.oversized_item(name,
                    '保留行尾空格。  \n下一行。\t\n链接：https://example.test/listen')
                instance, job, podcast = self.setup_job([selected], key=f'daily:{name}',
                    payload={'scheduled_for': '2026-10-09T08:30:00+08:00'})
                instance.settings.daily_compact_paragraphs = False
                instance.settings.daily_document_fulltext = fulltext
                podcast.summarizer = Mock()
                await instance._handle_daily_job(job)
                bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
                section = bundle['enrichment']['sections'][0]
                prose = (f'**节目：{name}**\n\n推荐理由：完整原文支持的观点。\n\n'
                         f'1. {name} 的核心判断。\n\n保留行尾空格。  \n下一行。\t\n'
                         '\n阅读建议：看摘要')
                expected_body = prose + '\n\n[收听节目](https://example.test/listen)'
                label = '精读和完整中文对谈' if fulltext else '精读'
                expected_template = (prose + f'\n\n[{label}]({DOCUMENT_PLACEHOLDER})'
                                     '\n\n[收听节目](https://example.test/listen)')
                self.assertEqual(section['body'].encode(), expected_body.encode())
                self.assertEqual(section['template'].encode(), expected_template.encode())
                self.assertLess(section['template'].index(f'[{label}]'),
                                section['template'].index('[收听节目]'))
                delivery = bundle['deliveries'][0]
                expected_parts = combined_delivery_parts(expected_body, delivery['key'],
                    title='🎧 播客精选 · 2026-10-09')
                self.assertEqual(self.messenger.attempts[-1].content.encode(),
                                 expected_parts[0][1].encode())
                kind, edited = _content(bundle, {name: {'url': 'https://example.test/doc'}}, delivery)
                expected_edit = combined_delivery_parts(
                    expected_template.replace(DOCUMENT_PLACEHOLDER, 'https://example.test/doc'),
                    delivery['key'], title='🎧 播客精选 · 2026-10-09')[0]
                self.assertEqual((kind, edited.encode()), (expected_edit[0], expected_edit[1].encode()))
                podcast.summarizer.rewrite_paragraphs.assert_not_called()
                self.store.complete(job.key)

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

    async def test_missing_compiler_threshold_defaults_to_four_for_real_daily_selection(self):
        instance, job, _ = self.setup_job(
            [self.item('four', 4), self.item('five', 5), self.item('three', 3)])
        instance.document_compiler = None
        await instance._handle_daily_job(job)
        bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
        self.assertEqual(set(bundle['enrichment']['eligible']), {'four', 'five'})

    async def test_reader_omits_release_notes_even_when_opted_in(self):
        from news_officer.daily_archive import read_daily_digest

        for enabled in (False, True):
            day = '2026-10-04' if not enabled else '2026-10-05'
            instance, job, _ = self.setup_job([self.item(day)], key=f'daily:{day}',
                payload={'scheduled_for': f'{day}T08:30:00+08:00'})
            instance.settings.daily_release_notes = enabled
            await instance._handle_daily_job(job)
            self.assertNotIn('功能更新', body(self.messenger.attempts[-1]))
            self.assertNotIn('功能更新', read_daily_digest(self.store, day)['markdown'])
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

    def oversized_item(self, name, extra):
        from test_reliability import attachment_for
        item = self.item(name)
        record = self.store.get_verified_transcript(name)
        message = item.message.replace('推荐星级：', extra + '\n\n推荐星级：')
        with self.store._connect() as db:
            db.execute('DELETE FROM episode_digests WHERE episode_id=?', (name,))
        self.store.save_transcript_digest(name, message, record.content_sha256, record.record_revision_sha256)
        return DailyItem(item.episode, 'summarized', message, attachment_for(record, message))

    async def test_oversized_daily_keeps_each_summary_and_link_in_one_send(self):
        from news_officer.operations import business_snapshot
        items = [self.oversized_item(name, '次要段落。' * 15000) for name in ('one', 'two')]
        instance, job, _ = self.setup_job(items)
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        content = self.messenger.attempts[0].content
        for name in ('one', 'two'):
            self.assertIn(name + ' 的核心判断', content)
            self.assertIn('https://example.test/' + name, content)
        self.assertNotIn('次要段落', content)
        bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
        self.assertTrue(all('次要段落' not in section['template'] for section in bundle['enrichment']['sections']))
        self.assertTrue(any('daily_message_degraded' in w for w in business_snapshot(self.store.path)['warnings']))

    async def test_oversized_first_paragraph_keeps_complete_first_sentence(self):
        from news_officer.daily_report import compact_reader_episode
        content = '**节目**\n\n完整的核心判断。' + '次要解释。' * 30000 + '\n\n[收听节目](https://example.test/one)'
        result = compact_reader_episode(content, 3, 'https://example.test/one')
        self.assertIn('完整的核心判断。', result)
        self.assertNotIn('次要解释', result)
        self.assertIn('https://example.test/one', result)

    async def test_plaintext_fallback_warns_without_changing_body(self):
        from news_officer.daily_report import reader_episode
        from news_officer.operations import business_snapshot
        item = self.oversized_item('one', '完整次要段落。' * 2000)
        instance, job, _ = self.setup_job([item])
        await instance._handle_daily_job(job)
        self.assertEqual(len(self.messenger.attempts), 1)
        part = self.messenger.attempts[0]
        self.assertEqual(part.msg_type, 'text')
        self.assertTrue(json.loads(part.content)['text'].endswith(reader_episode(item.message)))
        self.assertTrue(any('daily_message_plaintext_fallback' in w for w in business_snapshot(self.store.path)['warnings']))

    async def test_indivisible_summary_overflow_stops_and_warns(self):
        from news_officer.operations import business_snapshot
        instance, job, _ = self.setup_job([self.item('one')])
        # A sentence without any boundary cannot be safely shortened.
        with (patch('news_officer.daily_report.reader_episode', return_value=
                    '**节目**\n\n' + '核心判断' * 20000 + '\n\n[收听节目](https://example.test/one)'),
              self.assertRaisesRegex(ValueError, 'single-message capacity')):
            await instance._handle_daily_job(job)
        self.assertEqual(self.messenger.attempts, [])
        self.assertEqual(self.store.outbox_items(job.key), [])
        self.assertTrue(any('daily_message_capacity_exceeded' in w for w in business_snapshot(self.store.path)['warnings']))

    async def test_document_label_matches_fulltext_setting(self):
        from news_officer.daily_enrichment import _content
        for enabled in (False, True):
            instance, job, _ = self.setup_job([self.item(str(enabled), 5)], key=f'daily:fulltext-{enabled}')
            instance.settings.daily_document_fulltext = enabled
            await instance._handle_daily_job(job)
            bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
            _, content = _content(bundle, {str(enabled): {'url': 'https://example.test/doc'}}, bundle['deliveries'][0])
            self.assertIn('精读和完整中文对谈' if enabled else '精读', content)
            if not enabled:
                self.assertNotIn('完整中文对谈', content)
            self.store.complete(job.key)

    async def test_platform_overflow_never_silently_sends_multiple_messages(self):
        instance, job, _ = self.setup_job([self.item('one')])
        with (patch('news_officer.runtime.combined_delivery_parts', return_value=[
                ('text', 'first', 'uuid1'), ('text', 'second', 'uuid2')]),
              self.assertRaisesRegex(ValueError, 'single-message capacity')):
            await instance._handle_daily_job(job)
        self.assertEqual(self.messenger.attempts, [])
        self.assertEqual(self.store.outbox_items(job.key), [])
        from news_officer.operations import business_snapshot
        self.assertTrue(any('daily_message_capacity_exceeded' in w for w in business_snapshot(self.store.path)['warnings']))

    async def test_source_failure_retries_without_group_status_spam(self):
        instance, job, podcast = self.setup_job([])
        podcast.build_daily = Mock(side_effect=ConnectionError('offline'))
        with self.assertRaises(ConnectionError):
            await instance._handle_daily_job(job)
        self.assertFalse(self.store.analysis_complete(job.key))
        self.assertEqual(self.messenger.attempts, [])

    async def test_document_failure_cannot_block_original_daily_send(self):
        instance, job, podcast = self.setup_job([self.item('selected', 5)])
        compiler = Mock()
        compiler.min_stars = 4
        compiler.snapshot.return_value = ('revision', ['selected'], [])
        compiler.publish.side_effect = [TimeoutError('retry'),
            {'documents': [{'title': 'Selected', 'episode_id': 'selected', 'url': 'https://example.test/doc'}]}]
        instance.document_compiler = compiler
        await instance._handle_daily_job(job)
        self.assertEqual(podcast.calls, 1)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertNotIn('https://example.test/doc', self.messenger.attempts[0].content)
        await instance._handle_daily_job(job)
        compiler.publish.assert_not_called()
        compiler.snapshot.assert_not_called()
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

    async def test_daily_body_never_calls_document_compiler(self):
        instance, job, podcast = self.setup_job([self.item('selected', 5)])
        compiler = Mock(min_stars=5)
        compiler.publish.side_effect = ValueError('Shownotes validation failed')
        instance.document_compiler = compiler
        await instance._handle_daily_job(job)
        compiler.publish.assert_not_called()
        compiler.snapshot.assert_not_called()
        self.assertIsNone(self.store.get_job_result(job.key, 'daily:inline-documents'))
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertEqual(podcast.calls, 1)

    async def test_worker_failure_logs_stack_without_sensitive_error_message(self):
        import asyncio
        from unittest.mock import AsyncMock

        instance, job, _ = self.setup_job([])
        instance.wake_workers = {'daily': asyncio.Event()}
        error = ValueError('secret-token-in-url')
        error.__cause__ = RuntimeError('secret-token-in-chain')
        instance._handle_daily_job = AsyncMock(side_effect=error)
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
        self.assertNotIn('secret-token-in-chain', output)

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

    async def test_reader_omits_release_updates_without_claiming_them(self):
        instance, job, _ = self.setup_job([self.item('selected')], payload={
            'scheduled_for': '2026-10-11T08:30:00+08:00'})
        await instance._handle_daily_job(job)
        self.assertNotIn('功能更新', body(self.messenger.attempts[0]))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM daily_release_announcements').fetchone()[0], 0)

    async def test_mixed_allowlist_warning_reaches_operations(self):
        import os

        from news_officer.operations import business_snapshot
        instance, job, _ = self.setup_job([self.item('selected')])
        instance.settings.daily_chat_ids = ('oc_good', 'invalid')
        instance.settings.daily_require_chat_ids = True
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs:
            await instance._handle_daily_job(job)
        self.assertTrue(any('daily_allowlist_invalid:' in r.getMessage() for r in logs.records))
        self.assertEqual([r.target_id for r in self.messenger.attempts], ['oc_good'])
        with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_CHAT_IDS': 'oc_good,invalid'}, clear=True):
            self.assertTrue(any(w.startswith('daily_allowlist_invalid:')
                for w in business_snapshot(self.store.path)['warnings']))
