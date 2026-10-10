import copy
import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_chinese_transcript as migrations
import test_document_transcript as fixtures

from news_officer.document_transcript import (
    ChineseTranscriptWriter,
    dialogue_turns,
    fulltext_identity,
    prose_chunks,
    render_fulltext,
    source_segments,
    translation_identity,
)
from news_officer.library import LibraryError


class DialogueLayout(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FulltextDocuments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture

    def record(self, text):
        r = self.f.f.record
        return replace(r, transcript=replace(r.transcript, text=text))

    def test_turns_cross_chunk_boundaries_without_deleting_any_body_text(self):
        record = self.record('[00:01] Alex Kent: Question?\n[00:02] Dylan Lee: Answer.\n[00:03] Dylan Lee: More.\n[00:04] Alex Kent: Okay.')
        translated = [{'id': 0, 'text': 'Alex Kent：问题？\n\nDylan Lee：回答。'},
                      {'id': 1, 'text': 'Dylan Lee：补充。\n\n继续解释。\n\nAlex Kent：好的。'}]
        self.assertEqual(dialogue_turns(record, translated),
                         [('Alex Kent', '问题？'), ('Dylan Lee', '回答。 补充。 继续解释。'), ('Alex Kent', '好的。')])

    def test_reference_heading_bold_name_inline_body_and_no_preface(self):
        record = self.record('[00:01] Alex Kent: Why?\n[00:02] Dylan Lee: Yes.')
        nodes = render_fulltext(record, [{'id': 0, 'text': 'Alex Kent：为什么？\n\nDylan Lee：是的。'}])
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]['heading2']['elements'][0]['text_run']['content'], '完整对谈实录')
        label, body = nodes[1]['text']['elements']
        self.assertEqual(label['text_run'], {'content': 'Alex：', 'text_element_style': {'bold': True}})
        self.assertEqual(body['text_run'], {'content': ' 为什么？'})

    def test_ambiguous_first_names_are_not_shortened_or_guessed(self):
        record = self.record('Alex Kent: Why?\nAlex Lee: Yes.')
        nodes = render_fulltext(record, [{'id': 0, 'text': 'Alex Kent：为什么？\nAlex Lee：是的。'}])
        self.assertEqual(nodes[1]['text']['elements'][0]['text_run']['content'], 'Alex Kent：')
        with self.assertRaisesRegex(ValueError, '说话人轮次'):
            dialogue_turns(record, [{'id': 0, 'text': 'Alex：为什么？\nAlex：是的。'}])

    def test_lost_or_reordered_speaker_is_rejected(self):
        record = self.record('Alex: Why?\nDylan: Yes.\nAlex: Good.')
        for text in ('Alex：为什么？\nDylan：是的。', 'Dylan：是的。\nAlex：为什么？'):
            with self.assertRaisesRegex(ValueError, '说话人轮次'):
                dialogue_turns(record, [{'id': 0, 'text': text}])

    def test_unknown_speaker_is_translated_but_not_assigned_to_a_guest(self):
        record = self.record('Alex Kent: Why?\nUnknown Speaker: Yes.\nAlex Kent: Good.')
        nodes = render_fulltext(record, [{'id': 0, 'text': 'Alex Kent：为什么？\n未知说话人：是。\nAlex Kent：好。'}])
        self.assertEqual(nodes[2]['text']['elements'][0]['text_run']['content'], '未知说话人：')

    def test_invalid_cached_speaker_order_is_repaired_without_overwriting_old_cache(self):
        record = self.record('Alex: Why?\nDylan: Yes.')
        batch = source_segments(record.transcript.text)
        key = hashlib.sha256(json.dumps([translation_identity(record), 'test', 0, batch], ensure_ascii=False).encode()).hexdigest()
        old = [{'id': 0, 'text': 'Alex：为什么？\nDylan：是的。\nAlex：真的吗？\nDylan：是的。'}]
        store = self.f.f.store
        with store._connect() as db:
            db.execute('CREATE TABLE transcript_translations (cache_key TEXT PRIMARY KEY, segments_json TEXT NOT NULL)')
            db.execute('INSERT INTO transcript_translations VALUES (?,?)', (key, json.dumps(old)))
        valid = [{'id': 0, 'text': 'Alex：为什么？\nDylan：是的。'}]
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(output_parsed=Mock(model_dump=lambda: {'segments': valid}))
        writer = ChineseTranscriptWriter(store, client, 'test')
        self.assertEqual(writer.generate(record), valid)
        self.assertEqual(client.responses.parse.call_count, 1)
        self.assertEqual(writer.generate(record), valid)
        self.assertEqual(client.responses.parse.call_count, 1)
        with store._connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT segments_json FROM transcript_translations WHERE cache_key=?', (key,)).fetchone()[0]), old)
            self.assertEqual(db.execute('SELECT count(*) FROM transcript_translations').fetchone()[0], 2)

    def test_unlabelled_source_does_not_invent_a_speaker_or_parse_links(self):
        record = self.record('An unlabelled transcript.\nhttps://example.test')
        result = dialogue_turns(record, [{'id': 0, 'text': '正文。\n\n[原文](https://example.test)'}])
        self.assertEqual(result, [('', '正文。'), ('', '[原文](https://example.test)')])

    def test_exceptionally_long_turn_splits_at_sentence_and_preserves_text(self):
        body = ('这是完整句子，不应拆在中间。' * 130) + '这是结尾。'
        chunks = list(prose_chunks(body))
        self.assertEqual(''.join(chunks), body)
        self.assertTrue(chunks[0].endswith('。'))
        self.assertLessEqual(max(map(len, chunks)), 1400)

    def test_layout_version_does_not_invalidate_translation_cache(self):
        record = self.f.f.record
        old = ['authorized-chinese-appendix-v2', record.episode.id, record.transcript.text,
               record.transcript.language, record.transcript.source, record.transcript.source_url]
        legacy_key = 'fulltext:' + hashlib.sha256(json.dumps(old, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(translation_identity(record), legacy_key)
        old_render = fulltext_identity(record)
        with patch('news_officer.document_transcript.VERSION', 'future-layout'):
            self.assertEqual(translation_identity(record), legacy_key)
            self.assertNotEqual(fulltext_identity(record), old_render)

    def test_missing_bold_in_remote_readback_is_not_accepted(self):
        record = self.record('Alex: Why?\nDylan: Yes.')
        nodes = render_fulltext(record, [{'id': 0, 'text': 'Alex：为什么？\nDylan：是的。'}])
        api, compiler = self.f.f.api, self.f.compiler
        token = api.create('', 'test')
        compiler._write(token, nodes)
        compiler._verify_speaker_labels(token, nodes)
        api.data[token][2]['text']['elements'][0]['text_run'].pop('text_element_style')
        with self.assertRaisesRegex(LibraryError, '姓名加粗'):
            compiler._verify_speaker_labels(token, nodes)

    def test_v2_chinese_appendix_is_migrated_at_same_url(self):
        case = migrations.LegacyMigration()
        case.setUp()
        self.addCleanup(case.doCleanups)
        # Simulate the previous deployed Chinese heading, not just English v1.
        for node in case.base:
            if 'heading2' in node and '归档原文' in str(node):
                node['heading2']['elements'][0]['text_run']['content'] = '完整中文文字稿'
        for node in case.f.api.data[case.token]:
            if 'heading2' in node and '归档原文' in str(node):
                node['heading2']['elements'][0]['text_run']['content'] = '完整中文文字稿'
        with case.f.store._connect() as db:
            db.execute('UPDATE daily_documents SET nodes_json=?', (json.dumps(case.base),))
        before = copy.deepcopy(case.f.api.data[case.token])
        result = case.c.publish(case.job)
        self.assertTrue(result['documents'][0]['url'].endswith(case.token))
        self.assertNotIn('完整中文文字稿', str(case.f.api.blocks(case.token)))
        self.assertIn('完整对谈实录', str(case.f.api.blocks(case.token)))
        self.assertEqual(before[1], case.f.api.data[case.token][1])


if __name__ == '__main__':
    unittest.main()
