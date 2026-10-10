import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import test_document_transcript as documents

from news_officer.daily_document import read_tree, tree_signature
from news_officer.document_transcript import (
    ChineseTranscriptWriter,
    clean_timestamps,
    source_segments,
    validate_translation,
)
from news_officer.library import LibraryError
from news_officer.models import Job
from news_officer.shownotes import text_node


class ChineseTranscript(unittest.TestCase):
    def test_cues_and_clock_times(self):
        self.assertEqual(clean_timestamps('[00:01] Alex: Meet at 10:30, ratio 10:1.\n01:23 Next.'),
                         'Alex: Meet at 10:30, ratio 10:1.\nNext.')
        self.assertEqual(clean_timestamps('00:01:23.000 --> 00:01:25.000\nHello'), 'Hello')

    def test_segmentation_preserves_all_non_timestamp_characters(self):
        source = ('[00:03] Alex: Original sentence and exact details.\n' * 500).strip()
        segments = source_segments(source)
        self.assertEqual(''.join(p['text'] for p in segments), clean_timestamps(source))
        self.assertEqual([p['id'] for p in segments], list(range(len(segments))))

    def test_rejects_missing_reordered_english_truncated_and_lost_numbers(self):
        source = [{'id': 0, 'text': 'Our revenue grew 15 times and we are discussing the actual business results.'}]
        for output in ([], [{'id': 1, 'text': '中文正文'}],
                       [{'id': 0, 'text': source[0]['text']}], [{'id': 0, 'text': '增长15倍'}],
                       [{'id': 0, 'text': '我们的收入增长了很多倍，并且我们在讨论实际的业务表现。'}]):
            with self.assertRaises(ValueError):
                validate_translation(source, output)
        valid = [{'id': 0, 'text': '我们的收入增长了15倍，现在讨论的是实际业务表现。'}]
        self.assertEqual(validate_translation(source, valid), valid)

    def test_durable_resume_does_not_retranslate_completed_batches(self):
        fixture = documents.FulltextDocuments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = replace(fixture.f.record, transcript=replace(fixture.f.record.transcript,
                         text=('We discuss revenue and usage in substantial detail. ' * 300)))
        client = Mock()
        fail = True

        def parse(**kwargs):
            parts = json.loads(kwargs['input'])['segments']
            if fail and parts[0]['id'] == 5:
                raise TimeoutError('test failure')
            output = [{'id': p['id'], 'text': '我们正在详细讨论收入和使用量之间的联系。' * 25} for p in parts]
            return SimpleNamespace(output_parsed=Mock(model_dump=lambda: {'segments': output}))

        client.responses.parse.side_effect = parse
        writer = ChineseTranscriptWriter(fixture.f.store, client, 'test')
        with self.assertRaises(TimeoutError):
            writer.generate(record)
        fail = False
        client.responses.parse.reset_mock()
        result = writer.generate(record)
        self.assertEqual(len(result), len(source_segments(record.transcript.text)))
        self.assertEqual(client.responses.parse.call_count, 1)
        client.responses.parse.reset_mock()
        self.assertEqual(writer.generate(record), result)
        client.responses.parse.assert_not_called()


class LegacyMigration(unittest.TestCase):
    def setUp(self):
        fixture = documents.FulltextDocuments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f, self.c = fixture.f, fixture.compiler
        self.c.include_fulltext = False
        job = fixture.case.job()
        result = self.c.publish(job)
        self.f.store.complete(job.key)
        self.token = result['documents'][0]['url'].rsplit('/', 1)[-1]
        with self.f.store._connect() as db:
            row = db.execute('SELECT * FROM daily_documents').fetchone()
            self.base = json.loads(row['nodes_json']) + [text_node('完整文字稿（归档原文 · en）', 'heading2'),
                                                       text_node('原稿说明'), text_node('[00:01] Original.')]
            self.entries = json.loads(row['entries_json']) + ['fulltext:legacy']
            db.execute('UPDATE daily_documents SET nodes_json=?,entries_json=?',
                       (json.dumps(self.base), json.dumps(self.entries)))
        self.c._write(self.token, self.base)
        self.original = self.f.api.blocks(self.token)
        self.c.include_fulltext = True
        request = self.f.api.request
        self.lost_delete = False

        def api(method, path, *, params=None, data=None):
            if method == 'GET':
                return {'document': {'revision_id': 4}}
            if method == 'DELETE':
                self.assertEqual(params['document_revision_id'], 4)
                roots = self.f.api.data[self.token][0]['children']
                removed = set(roots[data['start_index']:data['end_index']])
                del roots[data['start_index']:data['end_index']]
                self.f.api.data[self.token] = [b for b in self.f.api.data[self.token] if b['block_id'] not in removed]
                if self.lost_delete:
                    self.lost_delete = False
                    raise TimeoutError('lost delete receipt')
                return {}
            return request(method, path, params=params, data=data)

        self.f.api.request = api
        self.c.request_authorizer = lambda _: True
        self.job = Job('migration', 'document', {'mode': 'requested_episode', 'episode_id': self.f.record.episode.id,
            'message_id': 'test', 'chat_id': 'test', 'chat_type': 'p2p', 'sender_open_id': 'owner',
            'reply_in_thread': False}, 1)
        self.f.store.enqueue(self.job.key, self.job.kind, self.job.payload)

    def test_migration_preserves_front_and_recovers_lost_delete(self):
        prefix = [tree_signature(n) for n in self.base[:-3]]
        self.lost_delete = True
        with self.assertRaises(TimeoutError):
            self.c.publish(self.job)
        result = self.c.publish(self.job)
        self.assertTrue(result['documents'][0]['url'].endswith(self.token))
        actual = read_tree(self.f.api.blocks(self.token), self.token)
        self.assertEqual([tree_signature(n) for n in actual[:len(prefix)]], prefix)
        self.assertNotIn('归档原文', str(actual))
        self.assertIn('完整中文文字稿', str(actual))
        self.assertEqual(self.c.publish(self.job), result)

    def test_manual_edit_aborts_without_deletion(self):
        self.f.api.data[self.token][-1]['text']['elements'][0]['text_run']['content'] = '同事的修改'
        before = copy.deepcopy(self.f.api.data)
        with self.assertRaises(LibraryError):
            self.c.publish(self.job)
        self.assertEqual(before, self.f.api.data)

    def test_failed_translation_leaves_live_original_untouched(self):
        self.c.transcript_writer.generate.side_effect = TimeoutError('model unavailable')
        with self.assertRaises(TimeoutError):
            self.c.publish(self.job)
        self.assertEqual(self.original, self.f.api.blocks(self.token))


if __name__ == '__main__':
    unittest.main()
