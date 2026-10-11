import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import test_reader_daily as fixtures

from news_officer.daily_enrichment import (
    _content,
    enqueue_enrichment,
    enrichment_bundle,
    normalized_content,
    update_daily_message,
)
from news_officer.library import LibraryError


class MessageAPI:
    def __init__(self, rows):
        self.messenger = SimpleNamespace(app_id='bot')
        self.messages = {r['remote_message_id']: {
            'message_id': r['remote_message_id'], 'msg_type': r['msg_type'], 'chat_id': r['target_id'],
            'sender': {'id': 'bot', 'id_type': 'app_id', 'sender_type': 'app'},
            'body': {'content': r['content']}} for r in rows}
        self.puts = []
        self.lose_response = False

    def request(self, method, path, *, data=None):
        message = self.messages[path.rsplit('/', 1)[-1]]
        if method == 'GET':
            # Platform readback adds empty styles and unwraps the locale.
            result = copy.deepcopy(message)
            value = json.loads(result['body']['content'])
            if 'zh_cn' in value:
                value = value['zh_cn']
                value['content_v2'] = 'platform metadata'
                for line in value['content']:
                    for node in line:
                        node.setdefault('style', [])
            result['body']['content'] = json.dumps(value)
            return {'items': [result]}
        assert method == 'PUT'
        self.puts.append(copy.deepcopy(data))
        message['body']['content'] = data['content']
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('lost receipt')
        return copy.deepcopy(message)


class EnrichmentTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ReaderDailyTests.setUp
    tearDown = fixtures.ReaderDailyTests.tearDown
    item = fixtures.ReaderDailyTests.item
    setup_job = fixtures.ReaderDailyTests.setup_job

    async def prepare(self):
        items = [self.item('one', 5), self.item('two', 5), self.item('low', 3)]
        instance, job, _ = self.setup_job(items, key='daily:2026-10-11',
            payload={'scheduled_for': '2026-10-11T08:30:00+08:00'})
        await instance._handle_daily_job(job)
        self.store.complete(job.key)
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET remote_message_id='om_original' WHERE job_key=?", (job.key,))
            rows = db.execute('SELECT * FROM outbox WHERE job_key=?', (job.key,)).fetchall()
        self.api = MessageAPI(rows)
        self.original = rows[0]['content']
        return instance, job

    def ready(self, base, episode):
        bundle = enrichment_bundle(self.store, base)
        targets = [(d['target_type'], d['target_id']) for d in bundle['deliveries']]
        value = {'documents': [{'episode_id': episode, 'url': 'https://www.feishu.cn/docx/' + episode,
                                'title': episode}], 'targets': targets, 'notify': True}
        self.store.save_job_result(base, 'daily:document:' + episode, 'daily_document_ready', value)
        return value

    async def test_bad_target_isolated_without_document_retry(self):
        from news_officer.operations import business_snapshot
        self.store.add_subscription('chat_id', 'oc_bad')
        instance, base = await self.prepare()
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET remote_message_id=target_id WHERE job_key=?", (base.key,))
            rows = db.execute('SELECT * FROM outbox WHERE job_key=?', (base.key,)).fetchall()
        self.api = MessageAPI(rows)
        original = self.api.request
        error = ValueError('secret oc_bad')
        error.__cause__ = RuntimeError('secret chain')
        def request(method, path, **kwargs):
            if self.api.messages[path.rsplit('/', 1)[-1]]['chat_id'] == 'oc_bad':
                raise error
            return original(method, path, **kwargs)
        self.api.request = request
        enqueue_enrichment(self.store, '2026-10-11')
        job = self.store.claim_next('document')
        compiler = Mock(api=self.api, _audience=instance._document_audience)
        compiler.publish.return_value = self.ready(base.key, job.payload['episode_id'])
        instance.document_compiler = compiler
        with self.assertLogs('news_officer.daily_enrichment', level='WARNING') as logs:
            await instance._handle_document_job(job)
        self.store.complete(job.key)
        self.assertEqual(len(self.api.puts), 1)
        self.assertIn('Traceback', '\n'.join(logs.output))
        self.assertNotIn('secret', '\n'.join(logs.output))
        warnings = business_snapshot(self.store.path)['warnings']
        self.assertTrue(any('daily_enrichment_target_failed:day=2026-10-11:target=' in w for w in warnings))
        self.assertNotIn('oc_bad', str(warnings))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE job_key=?', (job.key,)).fetchone()[0], 'completed')

    async def test_bad_followup_data_does_not_block_other_target(self):
        from news_officer.daily_enrichment import enqueue_followup
        self.store.add_subscription('chat_id', 'oc_bad')
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        self.ready(base.key, 'two')
        self.store.save_job_result(base.key, 'bad-target-data', 'daily_document_ready', {
            'targets': [['chat_id', 'oc_bad']],
            'documents': [{'episode_id': 'one', 'title': 'one'}]})
        with self.assertLogs('news_officer.daily_enrichment', level='WARNING'):
            self.assertEqual(enqueue_followup(self.store, base.key, '2026-10-11'), 1)
        job = self.store.claim_next('document')
        self.assertEqual([d['target'] for d in job.payload['deliveries']], [['open_id', 'ou_test']])

    async def test_reserved_edit_budget_warns_without_put(self):
        from news_officer.operations import business_snapshot
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        for number in range(19):
            self.store.save_job_result(base.key, f'intent:{number}', 'daily_edit_intent', {
                'message_id': 'om_original', 'msg_type': 'post', 'content': self.original})
        update_daily_message(self.store, self.api, base.key)
        update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])
        self.assertEqual(self.store.list_job_results(base.key, 'daily_edit_receipt'), [])
        warnings = business_snapshot(self.store.path)['warnings']
        self.assertEqual(sum('daily_enrichment_edit_budget_exhausted:day=2026-10-11:used=19/20' == w for w in warnings), 1)
        self.assertNotIn('om_original', str(warnings))
        # The reserved final edit remains available for the complete document set.
        self.ready(base.key, 'two')
        update_daily_message(self.store, self.api, base.key)
        self.assertEqual(len(self.api.puts), 1)

    async def test_old_frozen_template_uses_current_label_without_mutating_archive(self):
        _, base = await self.prepare()
        bundle = enrichment_bundle(self.store, base.key)
        for section in bundle['enrichment']['sections']:
            section['template'] = section['template'].replace('[精读](', '[精读与完整中文对谈](')
        original = copy.deepcopy(bundle)
        _, content = _content(bundle, {'one': {'url': 'https://www.feishu.cn/docx/one'}},
                              bundle['deliveries'][0])
        self.assertIn('精读和完整中文对谈', content)
        self.assertNotIn('精读与完整中文对谈', content)
        self.assertIn('https://www.feishu.cn/docx/one', content)
        self.assertEqual(bundle, original)

    async def test_enqueue_independent_selected_episodes_after_original_delivery(self):
        await self.prepare()
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 2)
        first, second = self.store.claim_next('document'), self.store.claim_next('document')
        self.assertEqual({first.payload['episode_id'], second.payload['episode_id']}, {'one', 'two'})
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 0)
        self.store.fail(first.key, 'validation', 5, failed_daily_requeue_seconds=0)
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 1)
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_existing_failed_document_is_stopped_after_six_days(self):
        await self.prepare()
        enqueue_enrichment(self.store, '2026-10-11')
        first = self.store.claim_next('document')
        self.store.fail(first.key, 'temporary outage', 5, failed_daily_requeue_seconds=0)
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-20'), 0)
        self.assertIsNone(self.store.claim_next('document'))
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_three_fulltext_recommendations_without_caches_all_enqueue_automatically(self):
        # A missing translation cache is ordinary work, not a new approval gate.
        instance, job, _ = self.setup_job(
            [self.item('first', 5), self.item('second', 4), self.item('third', 4)],
            key='daily:2026-10-11', payload={'scheduled_for': '2026-10-11T08:30:00+08:00'})
        instance.document_compiler = Mock(min_stars=4)
        await instance._handle_daily_job(job)
        self.store.complete(job.key)
        instance.document_compiler.publish.assert_not_called()
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 3)
        jobs = [self.store.claim_next('document') for _ in range(3)]
        self.assertEqual({j.payload['episode_id'] for j in jobs}, {'first', 'second', 'third'})
        self.assertTrue(all('authorization_pending' not in j.payload for j in jobs))
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_partial_failure_does_not_block_other_episode_and_only_edits_original(self):
        instance, base = await self.prepare()
        enqueue_enrichment(self.store, '2026-10-11')
        first, second = self.store.claim_next('document'), self.store.claim_next('document')
        compiler = Mock(api=self.api, _audience=instance._document_audience)
        compiler.publish.side_effect = TimeoutError('bad episode')
        instance.document_compiler = compiler
        with self.assertRaises(TimeoutError):
            await instance._handle_document_job(first)
        await instance._failure_notice(first, terminal=True)
        self.assertEqual(self.store.outbox_items(first.key), [])
        result = {'documents': [{'episode_id': second.payload['episode_id'], 'url': 'https://www.feishu.cn/docx/ready'}],
                  'targets': second.payload['targets'], 'notify': True}
        compiler.publish.side_effect = None
        compiler.publish.return_value = result
        await instance._handle_document_job(second)
        self.assertEqual(len(self.api.puts), 1)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertEqual(self.store.outbox_items(base.key)[0].content, self.original)
        compiler.publish.reset_mock()
        await instance._handle_document_job(second)
        compiler.publish.assert_not_called()
        self.assertEqual(len(self.api.puts), 1)

    async def test_lost_edit_receipt_recovers_without_second_put_or_message(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        self.api.lose_response = True
        with self.assertLogs('news_officer.daily_enrichment', level='WARNING'):
            update_daily_message(self.store, self.api, base.key)
        update_daily_message(self.store, self.api, base.key)
        self.assertEqual(len(self.api.puts), 1)
        self.assertEqual(len(self.store.list_job_results(base.key, 'daily_edit_receipt')), 1)
        self.ready(base.key, 'two')
        update_daily_message(self.store, self.api, base.key)
        self.assertEqual(len(self.api.puts), 2)
        content = self.api.puts[-1]['content']
        self.assertIn('/docx/one', content)
        self.assertIn('/docx/two', content)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertEqual(self.store.outbox_items(base.key)[0].content, self.original)

    async def test_manual_edits_wrong_sender_deleted_message_preserved(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        original = copy.deepcopy(self.api.messages['om_original'])
        for modification in ({'deleted': True}, {'sender': {'id': 'other'}},
                             {'body': {'content': json.dumps({'title': 'manual', 'content': []})}}):
            self.api.messages['om_original'] = {**original, **modification}
            with self.assertLogs('news_officer.daily_enrichment', level='WARNING'):
                update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])

    async def test_unsubscribed_recipient_is_not_edited_or_newly_enqueued(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        with self.store._connect() as db:
            db.execute('DELETE FROM subscriptions')
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 0)
        update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])

    async def test_group_chat_id_must_match_original_target(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET target_type='chat_id',target_id='expected'")
        self.api.messages['om_original']['chat_id'] = 'different'
        with self.assertLogs('news_officer.daily_enrichment', level='WARNING'):
            update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])

    async def test_pending_send_never_enqueues_or_edits(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET status='pending'")
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 0)
        with self.assertLogs('news_officer.daily_enrichment', level='WARNING'):
            update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])

    async def test_document_without_target_permission_not_injected(self):
        _, base = await self.prepare()
        self.store.save_job_result(base.key, 'wrong-audience', 'daily_document_ready', {
            'targets': [['chat_id', 'elsewhere']],
            'documents': [{'episode_id': 'one', 'url': 'https://www.feishu.cn/docx/one'}]})
        update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])

    def test_normalization_preserves_meaningful_style_and_links(self):
        a = json.dumps({'zh_cn': {'title': 'T', 'content': [[{'tag': 'text', 'text': 'x', 'style': ['bold']}]]}})
        b = json.dumps({'title': 'T', 'content': [[{'tag': 'text', 'text': 'x'}]], 'content_v2': []})
        self.assertNotEqual(normalized_content('post', a), normalized_content('post', b))

    def compiler(self, instance):
        from test_daily_document import API, notes

        from news_officer.episode_document import SelectedEpisodeCompiler
        self.document_api = API()
        self.document_api.messenger = self.api.messenger
        library_request = self.document_api.request
        self.document_api.request = lambda method, path, **kwargs: (
            self.api.request(method, path, **kwargs) if path.startswith('/im/')
            else library_request(method, path, **kwargs))
        self.writer = Mock()
        self.writer.generate.side_effect = notes
        compiler = SelectedEpisodeCompiler(self.store, self.document_api, self.writer,
            today=lambda: '2026-10-11', audience_filter=instance._document_audience)
        compiler.initialize()
        instance.document_compiler = compiler
        instance._main_loop = None
        return compiler

    async def test_real_shownotes_failure_preserves_body_and_successful_sibling(self):
        from test_daily_document import notes

        from news_officer.daily_enrichment import enqueue_followup
        instance, base = await self.prepare()
        compiler = self.compiler(instance)
        def generate(record):
            value = notes(record)
            if record.episode.id == 'one':
                value['core'][0]['evidence_ids'] = ['invented']
            return value
        self.writer.generate.side_effect = generate
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11', compiler=compiler,
            audience_filter=instance._document_audience), 2)
        jobs = [self.store.claim_next('document'), self.store.claim_next('document')]
        bad = next(j for j in jobs if j.payload['episode_id'] == 'one')
        good = next(j for j in jobs if j.payload['episode_id'] == 'two')
        with self.assertLogs('news_officer.runtime', level='WARNING') as logs, self.assertRaises(ValueError):
            await instance._handle_document_job(bad)
        self.assertIn('Traceback', '\n'.join(logs.output))
        self.assertTrue(logs.records[0].exc_info)
        self.assertIsNone(self.store.get_job_result(base.key, 'daily:document:one'))
        self.assertIsNone(self.store.get_job_result(base.key, 'daily:inline-documents'))
        await instance._handle_document_job(good)
        self.store.complete(good.key)
        self.assertEqual(self.document_api.creates, 1)
        self.assertEqual(len(self.api.puts), 1)
        self.assertEqual(len(self.messenger.attempts), 1)
        self.assertEqual(enqueue_followup(self.store, base.key, '2026-10-11'), 0)
        self.writer.generate.side_effect = notes
        await instance._handle_document_job(bad)
        self.store.complete(bad.key)
        followup = self.store.claim_next('document')
        self.assertEqual(followup.payload['mode'], 'daily_document_followup')
        await instance._handle_document_job(followup)
        await instance._handle_document_job(followup)
        self.store.complete(followup.key)
        self.assertEqual(len(self.messenger.attempts), 2)
        content = self.messenger.attempts[-1].content
        self.assertEqual(content.count('https://www.feishu.cn/docx/'), 2)
        self.assertNotIn('早间', content)
        self.assertNotIn('核心判断', content)
        self.assertEqual(enqueue_followup(self.store, base.key, '2026-10-11'), 0)
        self.assertEqual(enqueue_followup(self.store, base.key, '2026-10-12'), 0)
        self.assertEqual(self.store.outbox_items(base.key)[0].content, self.original)

    async def test_library_failure_is_bounded_after_notes_are_cached(self):
        from news_officer.episode_document import DocumentRetryStopped
        from news_officer.operations import business_snapshot
        instance, base = await self.prepare()
        compiler = self.compiler(instance)
        enqueue_enrichment(self.store, '2026-10-11', compiler=compiler)
        job = self.store.claim_next('document')
        self.document_api.deny_share = True
        for attempt in range(3):
            with self.assertLogs(level='WARNING') as logs, self.assertRaises(
                    DocumentRetryStopped if attempt == 2 else LibraryError):
                await instance._handle_document_job(job)
            self.assertIn('Traceback', '\n'.join(logs.output))
        self.assertEqual(self.writer.generate.call_count, 1)
        self.assertIsNone(self.store.get_job_result(base.key, 'daily:document:' + job.payload['episode_id']))
        self.assertEqual(len(self.messenger.attempts), 1)
        enqueue_enrichment(self.store, '2026-10-11', compiler=compiler)
        with self.store._connect() as db:
            state = db.execute('SELECT attempts,status FROM episode_document_attempts').fetchone()
            self.assertEqual(tuple(state), (3, 'failed'))
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE job_key=?', (job.key,)).fetchone()[0], 'failed')
        self.assertIn('episode_document_retry_stopped:1', business_snapshot(self.store.path)['warnings'])

    async def test_successful_links_follow_up_once_when_sibling_stops(self):
        from news_officer.daily_enrichment import enqueue_followup
        instance, base = await self.prepare()
        self.ready(base.key, 'two')
        enqueue_enrichment(self.store, '2026-10-11')
        jobs = [self.store.claim_next('document'), self.store.claim_next('document')]
        failed = next(j for j in jobs if j.payload['episode_id'] == 'one')
        self.store.fail(failed.key, 'DocumentRetryStopped', 1, max_attempts=1)
        self.assertEqual(enqueue_followup(self.store, base.key, '2026-10-11'), 1)
        followup = self.store.claim_next('document')
        await instance._handle_document_job(followup)
        await instance._handle_document_job(followup)
        self.store.complete(followup.key)
        self.assertEqual(len(self.messenger.attempts), 2)
        self.assertIn('/docx/two', self.messenger.attempts[-1].content)
        self.assertNotIn('/docx/one', self.messenger.attempts[-1].content)
        self.assertEqual(enqueue_followup(self.store, base.key, '2026-10-11'), 0)

    async def test_same_day_deadline_stops_independent_jobs_and_warns(self):
        from news_officer.operations import business_snapshot
        instance, _ = await self.prepare()
        compiler = self.compiler(instance)
        enqueue_enrichment(self.store, '2026-10-11', compiler=compiler)
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-12', compiler=compiler), 0)
        self.writer.generate.assert_not_called()
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM episode_document_attempts WHERE status='failed'").fetchone()[0], 2)
        self.assertIn('episode_document_retry_stopped:2', business_snapshot(self.store.path)['warnings'])

    async def test_allowlist_overrides_subscription_for_enqueue_edit_and_followup(self):
        from news_officer.daily_enrichment import enqueue_followup
        instance, job, _ = self.setup_job([self.item('one', 5)], key='daily:2026-10-11',
            payload={'scheduled_for': '2026-10-11T08:30:00+08:00'})
        instance.settings.daily_chat_ids = ('oc_frozen',)
        instance.settings.daily_require_chat_ids = True
        await instance._handle_daily_job(job)
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET remote_message_id='om_original' WHERE job_key=?", (job.key,))
            rows = db.execute('SELECT * FROM outbox WHERE job_key=?', (job.key,)).fetchall()
        self.api = MessageAPI(rows)
        compiler = self.compiler(instance)
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11', compiler=compiler,
            audience_filter=instance._document_audience), 1)
        document = self.store.claim_next('document')
        await instance._handle_document_job(document)
        self.store.complete(document.key)
        self.assertEqual(len(self.api.puts), 1)
        followup = self.store.claim_next('document')
        self.assertEqual(followup.payload['deliveries'][0]['target'], ['chat_id', 'oc_frozen'])
        # Newly allowed recipients are never added to the original frozen audience.
        instance.settings.daily_chat_ids = ('oc_new',)
        await instance._handle_document_job(followup)
        self.assertEqual(len(self.messenger.attempts), 1)
        update_daily_message(self.store, self.api, job.key, audience_filter=instance._document_audience)
        self.assertEqual(len(self.api.puts), 1)
        self.assertEqual(enqueue_followup(self.store, job.key, '2026-10-11',
            audience_filter=instance._document_audience), 0)
        instance.settings.daily_chat_ids = ('oc_frozen', 'oc_new')
        await instance._handle_document_job(followup)
        await instance._handle_document_job(followup)
        self.assertEqual(len(self.messenger.attempts), 2)
        self.assertEqual(self.messenger.attempts[-1].target_id, 'oc_frozen')
        self.assertNotIn(('chat_id', 'oc_frozen'), self.store.list_subscriptions())

    async def test_allowlist_rechecked_immediately_before_edit_put(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        allowed = True
        def audience(targets):
            return targets if allowed else []
        original = self.api.request
        def revoke_on_read(method, path, **kwargs):
            nonlocal allowed
            value = original(method, path, **kwargs)
            if method == 'GET':
                allowed = False
            return value
        self.api.request = revoke_on_read
        update_daily_message(self.store, self.api, base.key, audience_filter=audience)
        self.assertEqual(self.api.puts, [])


    async def test_revival_preserves_attempts_and_respects_configured_cap(self):
        instance, _ = await self.prepare()
        compiler = self.compiler(instance)
        compiler.max_attempts = 2
        enqueue_enrichment(self.store, '2026-10-11', compiler=compiler)
        first = self.store.claim_next('document')
        self.store.fail(first.key, 'outage', first.attempts, max_attempts=1,
                        failed_daily_requeue_seconds=0)
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11', compiler=compiler), 1)
        revived = self.store.claim_next('document')
        # The sibling was already pending; locate the revived job explicitly.
        if revived.key != first.key:
            revived = self.store.claim_next('document')
        self.assertEqual(revived.attempts, 2)
        self.store.fail(revived.key, 'outage', revived.attempts, max_attempts=1,
                        failed_daily_requeue_seconds=0)
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11', compiler=compiler), 0)
        self.writer.generate.assert_not_called()
        from news_officer.operations import business_snapshot
        warnings = business_snapshot(self.store.path)['warnings']
        self.assertTrue(any('document link missing:episode=' in w and 'day=2026-10-11' in w
                            for w in warnings))
        self.assertEqual(len(self.messenger.attempts), 1)
        with self.store._connect() as db:
            self.assertEqual(tuple(db.execute('SELECT attempts,status FROM jobs WHERE job_key=?',
                                              (first.key,)).fetchone()), (2, 'failed'))
