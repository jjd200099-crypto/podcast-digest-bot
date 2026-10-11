import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import test_daily_document as fixtures

from news_officer.episode_document import SelectedEpisodeCompiler, stars
from news_officer.models import Job
from news_officer.shownotes import render_episode, transcript_evidence, validate_notes


class SelectedDocuments(unittest.TestCase):
    def enrichment_job(self, identity):
        f = self.fixture
        record = f.store.get_verified_transcript(identity)
        job = Job('enrich:' + identity, 'document', {'mode': 'daily_episode',
                   'base_job': f.base, 'day': '2026-09-21', 'episode_id': identity,
                   'record_revision': record.record_revision_sha256,
                   'targets': [('chat_id', 'oc_test')]}, 1)
        f.store.enqueue(job.key, job.kind, job.payload)
        return job

    def test_enrichment_compiles_one_episode_and_reuses_already_published_links(self):
        f = self.fixture
        f.add_episode('two')
        self.rate('two', 5)
        f.store.save_job_result(f.base, 'bundle', 'daily_bundle', {
            'enrichment': {'version': 1, 'eligible': ['one', 'two']},
            'deliveries': [{'target_type': 'chat_id', 'target_id': 'oc_test'}]})
        # A legacy published doc still needs to be linked in the new edition.
        self.compiler.publish(self.job())
        before = f.api.creates
        result = self.compiler.publish(self.enrichment_job('two'))
        self.assertEqual([d['episode_id'] for d in result['documents']], ['two'])
        self.assertEqual(f.api.creates, before)

    def test_enrichment_rejects_changed_transcript_or_ineligible_episode(self):
        f = self.fixture
        f.store.save_job_result(f.base, 'bundle', 'daily_bundle', {
            'enrichment': {'version': 1, 'eligible': ['one']},
            'deliveries': [{'target_type': 'chat_id', 'target_id': 'oc_test'}]})
        job = self.enrichment_job('one')
        job.payload['record_revision'] = 'stale'
        with self.assertRaisesRegex(Exception, '版本'):
            self.compiler.publish(job)
        f.add_episode('low')
        self.assertIsNone(self.compiler.publish(self.enrichment_job('low')))
        self.assertEqual(f.api.creates, 0)

    def setUp(self):
        self.fixture = fixtures.DailyDocuments()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.compiler = SelectedEpisodeCompiler(f.store, f.api, f.writer, start_date='2026-09-21', today=lambda: '2026-09-21')
        self.compiler.initialize()
        self.rate('one', 5)
        f.store.ensure_outbox(job_key=f.base, group_key='daily:bundle:test', delivery_key='daily-test',
                             operation='send', target_id='oc_test', target_type='chat_id',
                             reply_in_thread=False, parts=[('text', 'daily', 'daily-uuid')])
        f.store.mark_outbox_sent(f.store.outbox_items(f.base)[0].id)
        f.store.complete(f.base)

    def rate(self, identity, rating):
        store = self.fixture.store
        item = store.get_job_result(self.fixture.base, identity)
        item['message'] = '推荐星级：' + '★' * rating + '☆' * (5 - rating)
        with store._connect() as db:
            db.execute('UPDATE job_results SET payload_json=? WHERE job_key=? AND result_key=?',
                       (json.dumps(item), self.fixture.base, identity))

    def job(self):
        self.compiler.enqueue_ready('2026-09-21')
        return self.fixture.store.claim_next('document')

    def test_each_high_star_episode_has_own_document_and_daily_unchanged(self):
        f = self.fixture
        f.add_episode('two')
        f.add_episode('low')
        self.rate('two', 5)
        self.rate('low', 2)
        before = f.store.list_job_results(f.base, 'daily_item')
        job = self.job()
        result = self.compiler.publish(job)
        self.assertEqual(result['count'], 2)
        self.assertEqual(f.api.creates, 2)
        self.assertEqual({d['title'].split('｜')[0] for d in result['documents']}, {'one', 'two'})
        for doc in result['documents']:
            token = doc['url'].rsplit('/', 1)[-1]
            headings = [b['heading1']['elements'][0]['text_run']['content']
                        for b in f.api.blocks(token) if b['block_type'] == 3]
            self.assertEqual(len(headings), 1)
        self.assertEqual(before, f.store.list_job_results(f.base, 'daily_item'))
        self.assertEqual(self.compiler.publish(job), result)
        self.assertEqual(f.api.creates, 2)

    def test_inline_daily_document_reuses_daily_job_and_suppresses_later_notice(self):
        f = self.fixture
        revision, selected, _ = self.compiler.snapshot(f.base)
        self.assertTrue(selected)
        job = Job(f.base, 'daily', {'mode': 'selected_episodes', 'day': '2026-09-21',
            'base_job': f.base, 'revision': revision, 'targets': [('chat_id', 'oc_test')]}, 1)
        result = self.compiler.publish(job)
        self.assertEqual(result['count'], 1)
        f.store.save_job_result(f.base, 'daily:inline-documents', 'inline_documents',
                               {'documents': result['documents']})
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21', requested_only=True), 0)
        self.assertEqual(self.compiler.publish(job), result)
        self.assertEqual(f.api.creates, 1)

    def test_no_high_rating_means_no_extra_message_or_document(self):
        self.rate('one', 2)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        self.assertEqual(self.fixture.api.creates, 0)
        self.assertEqual(stars('没有评级'), 0)

    def test_default_selection_compiles_four_and_five_but_not_three_stars(self):
        self.assertEqual(self.compiler.min_stars, 4)
        self.rate('one', 4)
        self.fixture.add_episode('five')
        self.fixture.add_episode('three')
        self.rate('five', 5)
        self.rate('three', 3)
        result = self.compiler.publish(self.job())
        self.assertEqual([d['stars'] for d in result['documents']], [5, 4])
        self.assertEqual(self.fixture.api.creates, 2)

    def test_explicit_four_star_setting_is_supported(self):
        self.compiler.min_stars = 4
        for rating in (1, 2, 3):
            self.rate('one', rating)
            self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        self.rate('one', 4)
        self.fixture.add_episode('five')
        self.rate('five', 5)
        result = self.compiler.publish(self.job())
        self.assertEqual(result['count'], 2)
        self.assertEqual([d['stars'] for d in result['documents']], [5, 4])

    def test_daily_must_finish_delivery_before_document_enqueue(self):
        f = self.fixture
        with f.store._connect() as db:
            db.execute("UPDATE jobs SET status='pending' WHERE job_key=?", (f.base,))
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        with f.store._connect() as db:
            db.execute("UPDATE jobs SET status='completed' WHERE job_key=?", (f.base,))
            db.execute("UPDATE outbox SET status='pending' WHERE job_key=?", (f.base,))
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        f.store.mark_outbox_sent(f.store.outbox_items(f.base)[0].id)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)

    def test_frozen_old_batch_cannot_bypass_raised_threshold(self):
        self.compiler.min_stars = 5
        job = self.job()
        self.fixture.store.save_job_result(job.key, 'episodes:content', 'episode_documents',
                                           {'documents': [{'stars': 4}]})
        result = self.compiler.publish(job)
        self.assertFalse(result['notify'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(self.fixture.api.creates, 0)

    def test_legacy_combined_job_is_retired_without_writing(self):
        self.assertIsNone(self.compiler.publish(self.fixture.job()))
        self.assertEqual(self.fixture.api.creates, 0)

    def test_lost_receipt_reuses_same_individual_document(self):
        f = self.fixture
        f.api.lost_write = True
        job = self.job()
        with self.assertRaises(TimeoutError):
            self.compiler.publish(job)
        self.assertEqual(self.compiler.publish(job)['count'], 1)
        self.assertEqual(f.api.creates, 1)
        self.assertEqual(f.writer.generate.call_count, 1)

    def test_new_selection_only_announces_new_episode(self):
        f = self.fixture
        first = self.job()
        self.compiler.publish(first)
        f.store.complete(first.key)
        f.add_episode('late')
        self.rate('late', 5)
        result = self.compiler.publish(self.job())
        self.assertEqual(result['count'], 1)
        self.assertTrue(result['documents'][0]['title'].startswith('late｜'))
        self.assertEqual(f.api.creates, 2)

    def test_pending_retry_blocks_new_revision(self):
        f = self.fixture
        first = self.job()
        f.store.fail(first.key, 'offline', 5, failed_daily_requeue_seconds=0)
        f.add_episode('two')
        self.rate('two', 5)
        self.assertEqual(self.job().key, first.key)

    def test_grounded_native_comparison_tables(self):
        record = self.fixture.record
        value = fixtures.notes(record)
        key = next(iter(transcript_evidence(record.transcript.text)))
        value['parts'][0]['tables'] = [{'columns': ['指标', '区别'], 'rows': [
            {'cells': ['收入', '付费金额'], 'evidence_ids': [key]},
            {'cells': ['使用量', '产品使用'], 'evidence_ids': [key]}]}]
        nodes, markdown = render_episode(record, value)
        self.assertTrue(any(n['block_type'] == 31 for n in nodes))
        self.assertIn('| 指标 | 区别 |', markdown)
        value['parts'][0]['tables'][0]['rows'][0]['evidence_ids'] = ['invented']
        with self.assertRaisesRegex(ValueError, 'table evidence'):
            validate_notes(value, transcript_evidence(record.transcript.text))


class SelectedDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_allowlisted_never_subscribed_and_unsubscribed_receive_daily_and_links(self):
        from types import SimpleNamespace

        from test_reliability import FakeMessenger, FakePlugin, SequencePodcast, runtime

        for unsubscribed in (False, True):
            with self.subTest(unsubscribed=unsubscribed):
                test = SelectedDocuments()
                test.setUp()
                self.addCleanup(test.doCleanups)
                f = test.fixture
                target = ('chat_id', 'oc_allowlisted')
                if unsubscribed:
                    f.store.add_subscription(*target)
                    f.store.remove_subscription(*target)
                messenger = FakeMessenger()
                instance = runtime(f.store, messenger, SequencePodcast([]), FakePlugin())
                instance.settings = SimpleNamespace(daily_chat_ids=(target[1],),
                    daily_require_chat_ids=False, daily_combined_message=True)
                instance.document_compiler = test.compiler
                test.compiler.audience_filter = instance._document_audience
                # Replace the fixture's old frozen recipients with the runtime snapshot.
                with f.store._connect() as db:
                    db.execute("DELETE FROM job_results WHERE job_key=? AND result_key='daily:recipients'", (f.base,))
                    db.execute("UPDATE jobs SET analysis_complete=0 WHERE job_key=?", (f.base,))
                daily = Job(f.base, 'daily', {'scheduled_for': '2026-09-21T08:30:00+08:00'}, 1)
                await instance._handle_daily_job(daily)
                document = test.job()
                self.assertIsNotNone(document)
                await instance._handle_document_job(document)
                self.assertNotIn(target, f.store.list_subscriptions())
                self.assertEqual([(r.target_type, r.target_id) for r in messenger.attempts], [target, target])
                self.assertIn('https://www.feishu.cn/docx/', messenger.attempts[-1].content)

    async def test_multiple_documents_share_one_notification(self):
        from test_reliability import FakeMessenger, FakePlugin, SequencePodcast, runtime

        test = SelectedDocuments()
        test.setUp()
        self.addCleanup(test.doCleanups)
        f = test.fixture
        f.add_episode('two')
        test.rate('two', 5)
        messenger = FakeMessenger()
        instance = runtime(f.store, messenger, SequencePodcast([]), FakePlugin())
        instance.document_compiler = test.compiler
        job = test.job()
        await instance._handle_document_job(job)
        await instance._handle_document_job(job)
        rows = f.store.outbox_items(job.key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(messenger.delivered), 1)
        self.assertEqual(rows[0].content.count('https://www.feishu.cn/docx/'), 2)
        self.assertIn('早间文字日报保持不变', rows[0].content)
        self.assertNotIn('★', rows[0].content)
        self.assertNotIn('☆', rows[0].content)


if __name__ == '__main__':
    unittest.main()


class DocumentAllowlist(unittest.TestCase):
    setUp = SelectedDocuments.setUp
    rate = SelectedDocuments.rate

    def audience(self, allowlist, require=False):
        from types import SimpleNamespace

        from test_reliability import runtime
        owner = runtime(self.fixture.store, None, None)
        owner.settings = SimpleNamespace(daily_chat_ids=allowlist, daily_require_chat_ids=require)
        self.compiler.audience_filter = owner._document_audience
        return owner

    def freeze(self, targets):
        f = self.fixture
        for kind, target in targets:
            f.store.add_subscription(kind, target)
        f.store.save_job_result(f.base, 'daily:recipients', 'recipients', {'targets': [list(t) for t in targets]})

    def test_frozen_old_open_id_is_revalidated_against_allowlist(self):
        self.freeze([('open_id', 'ou_old'), ('chat_id', 'oc_test'), ('chat_id', 'oc_extra')])
        self.audience(('oc_test',))
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)
        job = self.fixture.store.claim_next('document')
        self.assertEqual(job.payload['targets'], [['chat_id', 'oc_test']])
        # A payload frozen before the allowlist is also filtered at publish time.
        job.payload['targets'] = [['open_id', 'ou_old'], ['chat_id', 'oc_test']]
        self.assertEqual(self.compiler._audience(job.payload['targets']), [('chat_id', 'oc_test')])

    def test_no_allowlisted_audience_means_no_document_job_or_publish(self):
        self.freeze([('open_id', 'ou_old'), ('chat_id', 'oc_test')])
        self.audience(('oc_other',))
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        job = Job('episode-documents:x', 'document', {'mode': 'selected_episodes', 'day': '2026-09-21',
            'base_job': self.fixture.base, 'revision': 'r', 'targets': [['open_id', 'ou_old']]}, 1)
        self.assertIsNone(self.compiler.publish(job))
        self.assertEqual(self.fixture.api.creates, 0)

    def test_required_but_empty_allowlist_blocks_documents(self):
        self.freeze([('chat_id', 'oc_test')])
        self.audience((), require=True)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)

    def test_without_allowlist_documents_keep_subscription_behavior(self):
        self.freeze([('chat_id', 'oc_test')])
        self.audience(())
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)

    def test_allowlisted_chat_does_not_need_subscription_for_documents(self):
        self.audience(('oc_new',))
        self.assertNotIn(('chat_id', 'oc_new'), self.fixture.store.list_subscriptions())
        self.assertEqual(self.compiler._audience([['chat_id', 'oc_new']]), [('chat_id', 'oc_new')])

    def test_failed_inline_retry_is_revived_even_in_requested_only_mode(self):
        payload = {'mode': 'inline_daily_retry', 'base_job': self.fixture.base,
                   'scheduled_for': '2026-09-21T08:30:00+08:00'}
        self.fixture.store.enqueue('inline-retry', 'document', payload)
        self.fixture.store.fail('inline-retry', 'ValueError', 5, failed_daily_requeue_seconds=0)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21', requested_only=True), 1)
        self.assertEqual(self.fixture.store.claim_next('document').key, 'inline-retry')


class InlineFollowup(unittest.IsolatedAsyncioTestCase):
    async def test_followup_is_durable_links_only_and_revalidates_frozen_targets(self):
        from types import SimpleNamespace

        from test_reliability import FakeMessenger, runtime
        test = SelectedDocuments()
        test.setUp()
        self.addCleanup(test.doCleanups)
        f = test.fixture
        messenger = FakeMessenger()
        owner = runtime(f.store, messenger, None)
        owner.settings = SimpleNamespace(daily_chat_ids=('oc_test',), daily_require_chat_ids=True)
        with f.store._connect() as db:
            db.execute("UPDATE job_results SET payload_json=? WHERE job_key=? AND result_key='daily:recipients'",
                       (json.dumps({'targets': [['chat_id', 'oc_test'], ['chat_id', 'oc_removed']]}), f.base))
        documents = [{'title': 'one', 'url': 'https://www.feishu.cn/docx/one'}]
        owner.document_compiler = test.compiler
        test.compiler.audience_filter = owner._document_audience
        job = Job(f.base + ':inline-document-retry', 'document',
                  {'mode': 'inline_daily_retry', 'base_job': f.base,
                   'scheduled_for': '2026-09-21T08:30:00+08:00'}, 1)
        f.store.enqueue(job.key, job.kind, job.payload)
        await owner._handle_document_job(job)
        await owner._handle_document_job(job)
        self.assertEqual(len(messenger.delivered), 1)
        rows = f.store.outbox_items(job.key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target_id, 'oc_test')
        self.assertNotIn('早间', rows[0].content)
        self.assertIn(documents[0]['title'], rows[0].content)
        owner.settings = SimpleNamespace(daily_chat_ids=('oc_other',), daily_require_chat_ids=True)
        await owner._handle_document_job(job)
        self.assertEqual(len(messenger.delivered), 1)

    async def test_no_documents_no_followup(self):
        from test_reliability import FakeMessenger, runtime
        test = SelectedDocuments()
        test.setUp()
        self.addCleanup(test.doCleanups)
        f = test.fixture
        messenger = FakeMessenger()
        owner = runtime(f.store, messenger, None)
        f.store.save_job_result(f.base, 'daily:inline-documents', 'inline_documents', {'documents': []})
        await owner._handle_document_job(Job('retry', 'document',
            {'mode': 'inline_daily_retry', 'base_job': f.base,
             'scheduled_for': '2026-09-21T08:30:00+08:00'}, 1))
        self.assertEqual(messenger.delivered, {})


class DocumentRetryLimits(unittest.TestCase):
    setUp = SelectedDocuments.setUp
    rate = SelectedDocuments.rate
    job = SelectedDocuments.job

    def test_total_revision_budget_survives_revival_and_restart_and_warns(self):
        from news_officer.episode_document import DocumentRetryStopped
        from news_officer.operations import business_snapshot
        f = self.fixture
        f.writer.generate.side_effect = ValueError('deterministic invalid notes')
        job = self.job()
        for _ in range(3):
            self.assertTrue(self.compiler.publish(job)['deferred'])
        self.assertEqual(f.writer.generate.call_count, 3)
        restarted = SelectedEpisodeCompiler(f.store, f.api, f.writer, today=lambda: '2026-09-21')
        with self.assertRaises(DocumentRetryStopped):
            restarted.publish(job)
        self.assertEqual(restarted.enqueue_ready('2026-09-21'), 0)
        with f.store._connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE job_key=?', (job.key,)).fetchone()[0], 'failed')
            row = db.execute('SELECT attempts,status FROM episode_document_attempts').fetchone()
        self.assertEqual(tuple(row), (3, 'failed'))
        self.assertIn('episode_document_retry_stopped:1', business_snapshot(f.store.path)['warnings'])
        self.assertEqual(f.writer.generate.call_count, 3)

    def test_next_day_stops_model_calls_and_reviving_inline_jobs(self):
        from news_officer.episode_document import DocumentRetryStopped
        f = self.fixture
        f.writer.generate.side_effect = ValueError('invalid')
        job = self.job()
        self.compiler.publish(job)
        payload = {'mode': 'inline_daily_retry', 'base_job': f.base,
                   'scheduled_for': '2026-09-21T08:30:00+08:00'}
        f.store.enqueue('inline', 'document', payload)
        f.store.fail('inline', 'ValueError', 5, failed_daily_requeue_seconds=0)
        self.compiler.today = lambda: '2026-09-22'
        with self.assertRaises(DocumentRetryStopped):
            self.compiler.publish(job)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-22', requested_only=True), 0)
        self.assertEqual(f.writer.generate.call_count, 1)
        with f.store._connect() as db:
            self.assertEqual(db.execute('SELECT status FROM episode_document_attempts').fetchone()[0], 'failed')

    def test_new_transcript_revision_has_a_fresh_budget(self):
        from news_officer.models import Transcript
        f = self.fixture
        f.writer.generate.side_effect = ValueError('invalid')
        job = self.job()
        for _ in range(3):
            self.compiler.publish(job)
        updated = f.store.save_verified_transcript(f.record.episode,
            Transcript(f.record.transcript.text + ' revised', 'official', f.record.episode.url, True))
        revision = self.compiler.snapshot(f.base)[0]
        new = Job('new-revision', 'document', {**job.payload, 'revision': revision}, 1)
        self.assertTrue(self.compiler.publish(new)['deferred'])
        with f.store._connect() as db:
            row = db.execute('SELECT attempts,status FROM episode_document_attempts WHERE revision=?',
                             (self.compiler.document_identity(updated),)).fetchone()
        self.assertEqual(tuple(row), (1, 'pending'))
        self.assertEqual(f.writer.generate.call_count, 4)

    def test_successful_siblings_do_not_revive_an_exhausted_episode(self):
        f = self.fixture
        f.add_episode('two')
        self.rate('two', 5)
        def generate(record):
            if record.episode.id == 'one':
                raise ValueError('invalid')
            return fixtures.notes(record)
        f.writer.generate.side_effect = generate
        job = self.job()
        for _ in range(3):
            self.compiler.publish(job)
        self.assertFalse(self.compiler.retry_available(job.payload, '2026-09-21'))
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        self.assertEqual(f.writer.generate.call_count, 4)


class InlineFailureNotice(unittest.IsolatedAsyncioTestCase):
    async def test_missing_payload_targets_notifies_frozen_allowlisted_recipients(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from test_reliability import FakeMessenger, runtime
        test = SelectedDocuments()
        test.setUp()
        self.addCleanup(test.doCleanups)
        f = test.fixture
        messenger = FakeMessenger()
        owner = runtime(f.store, messenger, None)
        # Frozen allowed chat has no subscription. Newly allowed chat was not frozen.
        owner.settings = SimpleNamespace(daily_chat_ids=('oc_frozen', 'oc_new'), daily_require_chat_ids=True)
        with f.store._connect() as db:
            db.execute("UPDATE job_results SET payload_json=? WHERE job_key=? AND result_key='daily:recipients'",
                       (json.dumps({'targets': [['chat_id', 'oc_frozen'], ['chat_id', 'oc_removed'],
                                                ['open_id', 'ou_old']]}), f.base))
        job = Job('inline-notice', 'document', {'mode': 'inline_daily_retry', 'base_job': f.base,
                  'scheduled_for': '2026-09-21T08:30:00+08:00'}, 5)
        f.store.enqueue(job.key, job.kind, job.payload)
        with patch('news_officer.runtime.logger.warning') as warning:
            await owner._failure_notice(job, terminal=True)
            warning.assert_not_called()
        await owner._failure_notice(job, terminal=True)
        self.assertEqual(len(messenger.delivered), 1)
        self.assertEqual([(r.target_type, r.target_id) for r in messenger.attempts],
                         [('chat_id', 'oc_frozen')])
        self.assertIn('2026-09-21', messenger.attempts[0].content)

    async def test_notice_rechecks_allowlist_immediately_before_delivery(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from test_reliability import FakeMessenger, runtime
        test = SelectedDocuments()
        test.setUp()
        self.addCleanup(test.doCleanups)
        f = test.fixture
        messenger = FakeMessenger()
        owner = runtime(f.store, messenger, None)
        owner.settings = SimpleNamespace(daily_chat_ids=('oc_test',), daily_require_chat_ids=True)
        job = Job('inline-notice', 'document', {'mode': 'inline_daily_retry', 'base_job': f.base,
                  'scheduled_for': '2026-09-21T08:30:00+08:00'}, 5)
        f.store.enqueue(job.key, job.kind, job.payload)
        original = f.store.ensure_outbox
        def change_allowlist(**kwargs):
            result = original(**kwargs)
            owner.settings = SimpleNamespace(daily_chat_ids=('oc_other',), daily_require_chat_ids=True)
            return result
        with patch.object(f.store, 'ensure_outbox', side_effect=change_allowlist):
            await owner._failure_notice(job, terminal=True)
        self.assertEqual(messenger.delivered, {})


class CompleteDocumentBudget(unittest.TestCase):
    setUp = SelectedDocuments.setUp
    rate = SelectedDocuments.rate
    job = SelectedDocuments.job

    def test_pre_merge_notes_revision_budget_is_not_reset(self):
        from news_officer.episode_document import DocumentRetryStopped
        from news_officer.shownotes import note_identity
        f = self.fixture
        job = self.job()
        with f.store._connect() as db:
            db.execute("INSERT INTO episode_document_attempts VALUES (?,?,?,3,'failed')",
                       (f.record.episode.id, note_identity(f.record), '2026-09-21'))
        with self.assertRaises(DocumentRetryStopped):
            self.compiler.publish(job)
        f.writer.generate.assert_not_called()
        with f.store._connect() as db:
            rows = db.execute('SELECT revision,attempts,status FROM episode_document_attempts').fetchall()
        self.assertEqual([tuple(r) for r in rows], [(self.compiler.document_identity(f.record), 3, 'failed')])

    def test_translation_failures_share_the_document_revision_budget(self):
        from unittest.mock import Mock

        from news_officer.episode_document import DocumentRetryStopped
        f = self.fixture
        self.compiler.include_fulltext = True
        self.compiler.transcript_writer = Mock()
        self.compiler.transcript_writer.generate.side_effect = ValueError('invalid translation')
        job = self.job()
        for attempt in range(3):
            with self.assertRaises(DocumentRetryStopped if attempt == 2 else ValueError):
                self.compiler.publish(job)
        with self.assertRaises(DocumentRetryStopped):
            self.compiler.publish(job)
        self.assertEqual(f.writer.generate.call_count, 1)
        self.assertEqual(self.compiler.transcript_writer.generate.call_count, 3)
        self.assertEqual(f.api.creates, 0)
        self.assertIsNone(f.store.get_job_result(job.key, 'episodes:content'))
        with f.store._connect() as db:
            self.assertEqual(tuple(db.execute('SELECT attempts,status FROM episode_document_attempts').fetchone()),
                             (3, 'failed'))


class BackoffRecoveryCaps(unittest.TestCase):
    setUp = SelectedDocuments.setUp
    rate = SelectedDocuments.rate
    job = SelectedDocuments.job

    def test_real_parse_calls_remain_bounded_after_backoff_and_failed_revival(self):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from news_officer.episode_document import DocumentRetryStopped
        from news_officer.operations import business_snapshot
        from news_officer.shownotes import ShownotesWriter
        f = self.fixture
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(output_parsed=None)
        self.compiler.writer = ShownotesWriter(client, 'test-model')
        job = self.job()
        now = datetime.now(UTC)
        for attempt in range(self.compiler.max_attempts):
            before = client.responses.parse.call_count
            self.assertTrue(self.compiler.publish(job)['deferred'])
            self.assertEqual(client.responses.parse.call_count - before, 3)
            f.store.fail(job.key, 'validation', job.attempts)
            self.assertIsNone(f.store.claim_next('document'))
            with f.store._connect() as db:
                due = datetime.fromisoformat(db.execute(
                    'SELECT available_at FROM jobs WHERE job_key=?', (job.key,)).fetchone()[0])
            now = max(now, due) + timedelta(seconds=1)
            with patch('news_officer.store._now', return_value=now.isoformat()):
                job = f.store.claim_next('document')
            self.assertIsNotNone(job)
        self.assertEqual(client.responses.parse.call_count, self.compiler.max_attempts * 3)
        f.store.fail(job.key, 'DocumentRetryStopped', job.attempts, max_attempts=1,
                     failed_daily_requeue_seconds=0)
        client.responses.parse.reset_mock()
        # #47 can revive a failed job after its cooldown, resetting only job attempts.
        self.assertTrue(f.store.enqueue(job.key, 'document', job.payload))
        revived = f.store.claim_next('document')
        restarted = SelectedEpisodeCompiler(f.store, f.api, self.compiler.writer,
                                            today=lambda: '2026-09-21')
        with self.assertRaises(DocumentRetryStopped):
            restarted.publish(revived)
        f.store.fail(revived.key, 'DocumentRetryStopped', revived.attempts, max_attempts=1)
        client.responses.parse.assert_not_called()
        with f.store._connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE job_key=?',
                                        (job.key,)).fetchone()[0], 'failed')
        self.assertIn('episode_document_retry_stopped:1', business_snapshot(f.store.path)['warnings'])

    def test_cross_day_failed_recovery_after_six_day_window_never_calls_model(self):
        from unittest.mock import Mock

        from news_officer.daily_enrichment import enqueue_enrichment
        from news_officer.episode_document import DocumentRetryStopped
        from news_officer.operations import business_snapshot
        from news_officer.shownotes import ShownotesWriter
        f = self.fixture
        f.store.save_job_result(f.base, 'bundle', 'daily_bundle', {
            'group_key': 'daily:bundle:test',
            'enrichment': {'version': 1, 'day': '2026-09-21', 'eligible': ['one']},
            'deliveries': [{'target_type': 'chat_id', 'target_id': 'oc_test'}]})
        from dataclasses import replace

        from test_reliability import attachment_for

        from news_officer.models import DailyItem
        item = DailyItem.from_persisted_dict(f.store.get_job_result(f.base, 'one'))
        item = replace(item, attachment=attachment_for(f.record, item.message))
        with f.store._connect() as db:
            db.execute('UPDATE job_results SET payload_json=? WHERE job_key=? AND result_key=?',
                       (json.dumps(item.to_persisted_dict()), f.base, 'one'))
        self.assertEqual(enqueue_enrichment(f.store, '2026-09-21', compiler=self.compiler), 1)
        job = f.store.claim_next('document')
        f.store.fail(job.key, 'outage', 5, failed_daily_requeue_seconds=0)
        client = Mock()
        self.compiler.writer = ShownotesWriter(client, 'test-model')
        self.compiler.today = lambda: '2026-10-01'
        # Exercise #47's recovery without the scheduler guard, then the guarded path.
        self.assertEqual(enqueue_enrichment(f.store, '2026-10-01'), 1)
        revived = f.store.claim_next('document')
        with self.assertRaises(DocumentRetryStopped):
            self.compiler.publish(revived)
        f.store.fail(revived.key, 'DocumentRetryStopped', revived.attempts, max_attempts=1)
        self.assertEqual(enqueue_enrichment(f.store, '2026-10-01', compiler=self.compiler), 0)
        client.responses.parse.assert_not_called()
        with f.store._connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE job_key=?',
                                        (job.key,)).fetchone()[0], 'failed')
        self.assertIn('episode_document_retry_stopped:1', business_snapshot(f.store.path)['warnings'])
