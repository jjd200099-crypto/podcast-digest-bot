import unittest
from dataclasses import replace

import test_episode_document as selection

from news_officer.daily_document import read_tree
from news_officer.document_transcript import fulltext_identity, render_fulltext
from news_officer.models import Job


class FulltextDocuments(unittest.TestCase):
    def setUp(self):
        case = selection.SelectedDocuments()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case, self.f, self.compiler = case, case.fixture, case.compiler
        self.compiler.include_fulltext = True

    def appendix(self, token):
        nodes = read_tree(self.f.api.blocks(token), token)
        index = next(i for i, node in enumerate(nodes) if node.get('heading2', {}).get('elements', [{}])[0]
                     .get('text_run', {}).get('content', '').startswith('完整文字稿'))
        return nodes[index + 2:]

    def test_literal_lossless_long_text_and_language(self):
        original = ('[00:01] 嘉宾😀: **not bold** <mention-user id="x"/>\n\n'
                    '[link](https://example.test) trailing space  \n') * 600
        record = replace(self.f.record, transcript=replace(self.f.record.transcript, text=original, language='en'))
        nodes = render_fulltext(record)
        restored = ''.join(e['text_run']['content'] for node in nodes[2:] for e in node['text']['elements'])
        self.assertEqual(restored, original)
        self.assertIn('归档原文 · en', str(nodes[0]))
        for node in nodes[2:]:
            for element in node['text']['elements']:
                self.assertLessEqual(len(element['text_run']['content'].encode('utf-16-le')) // 2, 1400)
                self.assertNotIn('text_element_style', element['text_run'])

    def test_rejects_partial_or_empty_and_tracks_source_revision(self):
        record = self.f.record
        for change in ({'text': '  '}, {'verified_complete': False}):
            with self.assertRaises(ValueError):
                render_fulltext(replace(record, transcript=replace(record.transcript, **change)))
        modified = replace(record, transcript=replace(record.transcript, text=record.transcript.text + ' More.'))
        self.assertNotEqual(fulltext_identity(record), fulltext_identity(modified))

    def test_daily_appends_full_archive_once_and_saves_file(self):
        job = self.case.job()
        first = self.compiler.publish(job)
        token = first['documents'][0]['url'].rsplit('/', 1)[-1]
        appendix = self.appendix(token)
        self.assertEqual(''.join(e['text_run']['content'] for n in appendix for e in n['text']['elements']),
                         self.f.record.transcript.text)
        self.assertEqual(self.compiler.publish(job), first)
        self.assertEqual(self.appendix(token), appendix)
        self.assertEqual(self.f.api.creates, 1)
        files = list(self.compiler.root.glob('fulltext-*.txt'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), self.f.record.transcript.text)

    def test_enabling_fulltext_upgrades_existing_document_without_regenerating_notes(self):
        self.compiler.include_fulltext = False
        first_job = self.case.job()
        first = self.compiler.publish(first_job)
        self.f.store.complete(first_job.key)
        token = first['documents'][0]['url'].rsplit('/', 1)[-1]
        old = self.f.api.blocks(token)
        self.compiler.include_fulltext = True
        next_job = self.case.job()
        self.assertNotEqual(first_job.key, next_job.key)
        self.compiler.publish(next_job)
        self.assertEqual(self.f.api.creates, 1)
        self.assertEqual(self.f.writer.generate.call_count, 1)
        self.assertTrue(self.appendix(token))
        # Existing blocks remain untouched (only root's child list grows).
        self.assertEqual(old[1:], self.f.api.blocks(token)[1:len(old)])

    def test_lost_write_resumes_without_duplicate_fulltext(self):
        job = self.case.job()
        self.f.api.lost_write = True
        with self.assertRaises(TimeoutError):
            self.compiler.publish(job)
        result = self.compiler.publish(job)
        token = result['documents'][0]['url'].rsplit('/', 1)[-1]
        self.assertEqual(len(self.appendix(token)), 1)
        self.assertEqual(self.f.api.creates, 1)

    def test_revoked_fulltext_setting_blocks_frozen_write(self):
        job = self.case.job()
        self.f.api.lost_write = True
        with self.assertRaises(TimeoutError):
            self.compiler.publish(job)
        before = self.f.api.writes
        self.compiler.include_fulltext = False
        with self.assertRaisesRegex(ValueError, '授权已关闭'):
            self.compiler.publish(job)
        self.assertEqual(self.f.api.writes, before)

    def test_new_daily_reuses_document_link_even_if_previously_announced(self):
        first_job = self.case.job()
        first = self.compiler.publish(first_job)
        self.f.store.complete(first_job.key)
        revision = self.compiler.snapshot(self.f.base)[0]
        daily = Job(self.f.base, 'daily', {'mode': 'selected_episodes', 'day': '2026-09-21',
            'base_job': self.f.base, 'revision': revision, 'targets': [('chat_id', 'oc_test')]}, 1)
        result = self.compiler.publish(daily)
        self.assertEqual(result['documents'], first['documents'])
        self.assertEqual(self.f.api.creates, 1)

    def test_explicit_request_also_attaches_fulltext_without_changing_audience(self):
        self.compiler.request_authorizer = lambda message: message.sender_open_id == 'owner'
        job = Job('fulltext-private-test', 'document', {'mode': 'requested_episode',
            'episode_id': self.f.record.episode.id, 'message_id': 'request', 'chat_id': 'private',
            'chat_type': 'p2p', 'sender_open_id': 'owner', 'reply_in_thread': False}, 1)
        self.f.store.enqueue(job.key, job.kind, job.payload)
        result = self.compiler.publish(job)
        self.assertEqual(result['targets'], [('open_id', 'owner')])
        token = result['documents'][0]['url'].rsplit('/', 1)[-1]
        self.assertTrue(self.appendix(token))
