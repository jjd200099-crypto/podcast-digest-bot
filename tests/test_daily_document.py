import copy
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from news_officer.daily_document import DailyDocumentCompiler, read_tree, tree_signature
from news_officer.library import LibraryError
from news_officer.models import DailyItem, Episode, Job, Transcript
from news_officer.shownotes import (
    INTRO,
    render_episode,
    table_node,
    text_node,
    transcript_evidence,
    validate_notes,
)
from news_officer.store import Store


def notes(record):
    key = next(iter(transcript_evidence(record.transcript.text)))
    point = {'text': '嘉宾认为需求增长，但要区分收入与使用量。', 'evidence_ids': [key]}
    return {'participants': '原稿未明确标注', 'participant_evidence_ids': [],
            'core': [copy.deepcopy(point) for _ in range(6)],
            'parts': [{'title': f'主题{i}', 'points': [point, point]} for i in range(8)],
            'quotes': [], 'corrections': []}


class API:
    def __init__(self):
        self.files, self.data, self.creates, self.writes = [], {}, 0, 0
        self.lost_write = self.lost_create = self.deny_share = False

    def pages(self, *args):
        return copy.deepcopy(self.files)

    def create(self, folder, title):
        self.creates += 1
        token = 'doc' + str(self.creates)
        self.files.append({'token': token, 'name': title, 'type': 'docx'})
        self.data[token] = [{'block_id': token, 'block_type': 1, 'children': []}]
        if self.lost_create:
            self.lost_create = False
            raise TimeoutError('lost create receipt')
        return token

    def blocks(self, token):
        return copy.deepcopy(self.data[token])

    def request(self, method, path, *, params, data):
        if 'permissions' in path:
            if self.deny_share:
                raise LibraryError('permission denied')
            return {}
        token = path.split('/')[4]
        self.writes += 1
        mapping = {b['block_id']: f"native{len(self.data[token])}-{i}" for i, b in enumerate(data['descendants'])}
        for node in copy.deepcopy(data['descendants']):
            node['block_id'] = mapping[node['block_id']]
            if 'children' in node:
                node['children'] = [mapping[c] for c in node['children']]
            self.data[token].append(node)
        roots = self.data[token][0]['children']
        roots[data['index']:data['index']] = [mapping[c] for c in data['children_id']]
        if self.lost_write:
            self.lost_write = False
            raise TimeoutError('lost append receipt')
        return {}


class DailyDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.sqlite')
        self.store.initialize()
        self.store.add_subscription('chat_id', 'oc_test')
        self.api, self.writer = API(), Mock()
        self.writer.generate.side_effect = notes
        self.compiler = DailyDocumentCompiler(self.store, self.api, self.writer, start_date='2026-09-21')
        self.compiler.initialize()
        self.base = 'daily:2026-09-21'
        self.store.enqueue(self.base, 'daily', {})
        self.store.save_job_result(self.base, 'daily:recipients', 'recipients', {'targets': [['chat_id', 'oc_test']]})
        self.store.mark_analysis_complete(self.base)
        self.record = self.add_episode('one')

    def add_episode(self, identity, *, status='summarized', full=True):
        ep = Episode(identity, identity, 'https://example.test/' + identity, 'Test', 3600,
                     published_at=datetime(2026, 9, 21, tzinfo=UTC))
        self.store.save_job_result(self.base, identity, 'daily_item', DailyItem(ep, status).to_persisted_dict())
        if full:
            return self.store.save_verified_transcript(ep, Transcript('[00:12] Revenue is not usage. ' + identity,
                                                                      'official', ep.url, True))

    def job(self):
        revision = self.compiler.snapshot(self.base)[0]
        job = Job('daily-document:2026-09-21:' + revision[:20], 'document',
                   {'day': '2026-09-21', 'base_job': self.base, 'revision': revision,
                    'targets': [['chat_id', 'oc_test']]}, 1)
        self.store.enqueue(job.key, job.kind, job.payload)
        return job

    def test_retry_reuses_content_document_and_link_identity(self):
        job = self.job()
        first = self.compiler.publish(job)
        writes = self.api.writes
        second = self.compiler.publish(job)
        self.assertEqual(first, second)
        self.assertTrue(first['notify'])
        self.assertEqual((self.api.creates, self.api.writes, self.writer.generate.call_count), (1, writes, 1))
        self.assertEqual(len(list(self.compiler.root.glob('*.md'))), 1)

    def test_lost_append_receipt_resumes_verified_prefix(self):
        self.api.lost_write = True
        with self.assertRaises(TimeoutError):
            self.compiler.publish(self.job())
        self.assertTrue(self.compiler.publish(self.job())['notify'])
        tree = read_tree(self.api.blocks('doc1'), 'doc1')
        frozen = self.store.get_job_result(self.job().key, 'document:content')['nodes']
        self.assertEqual([tree_signature(n) for n in tree], [tree_signature(n) for n in frozen])
        self.assertEqual(self.writer.generate.call_count, 1)

    def test_lost_create_receipt_adopts_exact_title(self):
        self.api.lost_create = True
        with self.assertRaises(TimeoutError):
            self.compiler.publish(self.job())
        self.compiler.publish(self.job())
        self.assertEqual(self.api.creates, 1)

    def test_unknown_create_does_not_duplicate(self):
        self.api.lost_create = True
        with self.assertRaises(TimeoutError):
            self.compiler.publish(self.job())
        self.api.files = []
        with self.assertRaisesRegex(LibraryError, '回执'):
            self.compiler.publish(self.job())
        self.assertEqual(self.api.creates, 1)

    def test_existing_intro_is_reused_and_human_edits_protected(self):
        self.api.create('', '情报官播客精读｜2026-09-21')
        self.compiler._write('doc1', [text_node(INTRO, 'quote')])
        self.compiler.publish(self.job())
        self.assertEqual(self.api.creates, 1)
        self.api.data['doc1'][1]['quote']['elements'][0]['text_run']['content'] = 'human edit'
        with self.assertRaisesRegex(LibraryError, '人工修改'):
            self.compiler.publish(self.job())

    def test_share_failure_cannot_announce_success(self):
        self.api.deny_share = True
        with self.assertRaises(LibraryError):
            self.compiler.publish(self.job())
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT notification_job FROM daily_documents').fetchone()[0], '')
        self.api.deny_share = False
        self.assertTrue(self.compiler.publish(self.job())['notify'])

    def test_catchup_appends_same_doc_without_second_link(self):
        self.compiler.publish(self.job())
        self.add_episode('two')
        result = self.compiler.publish(self.job())
        self.assertEqual(result['count'], 2)
        self.assertFalse(result['notify'])
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(self.writer.generate.call_count, 2)

    def test_scope_requires_daily_membership_and_verified_date(self):
        self.add_episode('outside', status='outside_window')
        self.add_episode('unverified', status='unverified_date')
        self.add_episode('pending', full=False, status='no_transcript')
        _, records, pending = self.compiler.snapshot(self.base)
        self.assertEqual([r.episode.id for _, r in records], ['one'])
        self.assertEqual(pending[0]['title'], 'pending')

    def test_stale_unwritten_job_is_skipped(self):
        old = self.job()
        self.add_episode('two')
        self.assertIsNone(self.compiler.publish(old))
        self.assertEqual(self.api.creates, 0)

    def test_scheduler_finishes_failed_revision_before_newer_revision(self):
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)
        old = self.store.claim_next('document')
        self.add_episode('two')
        self.store.fail(old.key, 'test', 5, failed_daily_requeue_seconds=0)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)
        self.assertEqual(self.store.claim_next('document').key, old.key)
        self.store.complete(old.key)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)
        self.assertNotEqual(self.store.claim_next('document').key, old.key)

    def test_unsubscribed_audience_not_published(self):
        self.store.remove_subscription('chat_id', 'oc_test')
        self.assertIsNone(self.compiler.publish(self.job()))
        self.assertEqual(self.api.creates, 0)

    def test_evidence_and_quote_checks_and_no_invented_times(self):
        evidence = transcript_evidence(self.record.transcript.text)
        value = notes(self.record)
        value['core'][0]['evidence_ids'] = ['invented']
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            validate_notes(value, evidence)
        value = notes(self.record)
        value['quotes'] = [{'text': 'made up', 'speaker': 'guest', 'evidence_id': next(iter(evidence))}]
        with self.assertRaisesRegex(ValueError, 'Quote'):
            validate_notes(value, evidence)
        rendered, md = render_episode(self.record, notes(self.record))
        self.assertIn('[00:12]', md)
        self.assertEqual(md.count('https://example.test/one'), 1)
        self.assertEqual(rendered[1]['block_type'], 19)
        self.assertEqual(next(iter(transcript_evidence('no time here').values()))['time'], '')

    def test_table_validation(self):
        with self.assertRaises(ValueError):
            table_node([])
        self.assertEqual(table_node([['A', 'B'], ['C', 'D']])['table']['property']['row_size'], 2)


class DocumentDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_one_link_and_one_message_even_after_retry(self):
        from test_reliability import FakeMessenger, FakePlugin, SequencePodcast, runtime

        fixture = DailyDocuments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        messenger = FakeMessenger()
        instance = runtime(fixture.store, messenger, SequencePodcast([]), FakePlugin())
        instance.document_compiler = fixture.compiler
        job = fixture.job()
        messenger.fail_groups_once.add('document:link')
        with self.assertRaises(RuntimeError):
            await instance._handle_document_job(job)
        await instance._handle_document_job(job)
        await instance._handle_document_job(job)
        self.assertEqual(len(messenger.delivered), 1)
        rows = fixture.store.outbox_items(job.key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content.count('https://www.feishu.cn/docx/'), 1)


if __name__ == '__main__':
    unittest.main()
