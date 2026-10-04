import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import test_daily_document as fixtures

from news_officer.episode_document import SelectedEpisodeCompiler, stars
from news_officer.shownotes import render_episode, transcript_evidence, validate_notes


class SelectedDocuments(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DailyDocuments()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.compiler = SelectedEpisodeCompiler(f.store, f.api, f.writer, start_date='2026-09-21')
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

    def test_no_high_rating_means_no_extra_message_or_document(self):
        self.rate('one', 2)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        self.assertEqual(self.fixture.api.creates, 0)
        self.assertEqual(stars('没有评级'), 0)

    def test_only_five_stars_is_automatically_compiled(self):
        for rating in (3, 4):
            self.rate('one', rating)
            self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 0)
        self.rate('one', 5)
        self.assertEqual(self.compiler.publish(self.job())['count'], 1)

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
