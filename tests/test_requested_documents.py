import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import test_daily_document as fixtures

from news_officer.episode_document import SelectedEpisodeCompiler
from news_officer.models import IncomingMessage, Transcript
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import ResearchTools
from news_officer.research_checkpoint import (
    initialize_checkpoints,
    restore_checkpoint,
    save_checkpoint,
)
from news_officer.shownotes import (
    chapter_outline,
    note_identity,
    render_episode,
    transcript_evidence,
)


class RequestedDocuments(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DailyDocuments()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.allowed = lambda m: m.chat_id == 'team' if m.chat_type == 'group' else m.sender_open_id == 'owner'
        self.compiler = SelectedEpisodeCompiler(self.f.store, self.f.api, self.f.writer,
                                               request_authorizer=self.allowed)
        self.compiler.initialize()
        self.message = IncomingMessage('msg1', 'team', '重点总结这期播客', 'group', 'colleague', 'thread1')
        self.agent = SimpleNamespace(store=self.f.store, library=PodcastArchive(self.f.store),
                                     registry=None, allowed=self.allowed, document_compiler=self.compiler)

    def request(self, message=None):
        self.compiler.enqueue_request(self.f.record.reference, message or self.message)
        return self.f.store.claim_next('document')

    def test_colleague_can_request_without_subscription_or_rating(self):
        job = self.request()
        result = self.compiler.publish(job)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['targets'], [('chat_id', 'team')])
        self.assertEqual(result['reply_to'], 'msg1')
        self.assertTrue(result['reply_in_thread'])
        self.assertEqual(self.compiler.publish(job), result)
        self.assertEqual(self.f.api.creates, 1)

    def test_same_episode_reused_across_people_and_private_chat(self):
        first = self.request()
        a = self.compiler.publish(first)
        self.f.store.complete(first.key)
        second = self.request(replace(self.message, message_id='msg2', chat_id='private',
                                      chat_type='p2p', sender_open_id='owner'))
        b = self.compiler.publish(second)
        self.assertEqual(a['documents'][0]['url'], b['documents'][0]['url'])
        self.assertEqual(b['targets'], [('open_id', 'owner')])
        self.assertEqual(self.f.api.creates, 1)
        self.assertEqual(self.f.writer.generate.call_count, 1)

    def test_unapproved_chat_or_revoked_access_does_not_publish(self):
        with self.assertRaises(ValueError):
            self.request(replace(self.message, chat_id='foreign'))
        job = self.request()
        self.compiler.request_authorizer = lambda m: False
        self.assertIsNone(self.compiler.publish(job))
        self.assertEqual(self.f.api.creates, 0)

    def test_missing_transcript_and_share_failure_do_not_claim_success(self):
        with self.assertRaises(ValueError):
            self.compiler.enqueue_request('unknown', self.message)
        job = self.request()
        self.f.api.deny_share = True
        with self.assertRaisesRegex(Exception, 'permission'):
            self.compiler.publish(job)

    def test_tool_uses_current_audience_and_real_corpus_only(self):
        state = ResearchTools(self.agent, 'session', self.message)
        with self.assertRaises(ValueError):
            state.execute('create_episode_document', {'reference': 'foreign'})
        result = state.execute('create_episode_document', {'reference': self.f.record.reference})
        self.assertEqual(result['status'], 'queued')
        message = state.render({'kind': 'conversation', 'message': '', 'points': []})
        self.assertIn('正在整理飞书文档', message)
        self.assertNotIn('https://', message)
        state.execute('create_episode_document', {'reference': self.f.record.reference})
        with self.f.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='document'").fetchone()[0], 1)

    def test_disabled_tool_is_explicit_and_checkpoint_preserves_receipt(self):
        state = ResearchTools(self.agent, 'session', self.message)
        self.agent.document_compiler = None
        self.assertIn('error', state.execute('create_episode_document', {'reference': self.f.record.reference}))
        self.agent.document_compiler = self.compiler
        state.execute('create_episode_document', {'reference': self.f.record.reference})
        initialize_checkpoints(self.f.store)
        save_checkpoint(state, [])
        resumed = ResearchTools(self.agent, 'session', self.message)
        restore_checkpoint(resumed)
        self.assertEqual(resumed.document_requests, state.document_requests)

    def test_failed_interactive_job_is_revived_without_daily_subscription(self):
        job = self.request()
        self.f.store.fail(job.key, 'network', 5, failed_daily_requeue_seconds=0)
        self.assertEqual(self.compiler.enqueue_ready('2026-09-21'), 1)
        self.assertEqual(self.f.store.claim_next('document').key, job.key)

    def test_publisher_metadata_survives_archive_and_anchors_are_exact(self):
        ep = replace(self.f.record.episode, duration_seconds=1432,
                     metadata={'description': '(0:00) Intro (3:07) Ads (10:04) IPO (16:00) Privacy (20:00) Team'})
        record = self.f.store.save_verified_transcript(ep, Transcript('[00:00] Intro\n[03:34] Advertising is ML.\n', 'official', ep.url, True))
        self.assertEqual(len(chapter_outline(record.episode)), 5)
        evidence = transcript_evidence(record.transcript.text)
        self.assertEqual(''.join(v['text'] for v in evidence.values()), record.transcript.text)
        self.assertEqual(list(evidence.values())[1]['time'], '03:34')
        value = fixtures.notes(record)
        value['parts'] = value['parts'][:5]
        nodes, md = render_episode(record, value)
        self.assertIn('23 分 52 秒', md)
        self.assertEqual(md.count('## Part'), 5)
        self.assertNotEqual(note_identity(record), note_identity(replace(record, episode=replace(ep, metadata={}))))
        self.assertIn('节目说明', json.dumps(nodes, ensure_ascii=False))


class RequestedDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_link_replies_to_original_message_once(self):
        from test_reliability import FakeMessenger, FakePlugin, SequencePodcast, runtime
        fixture = RequestedDocuments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        messenger = FakeMessenger()
        instance = runtime(fixture.f.store, messenger, SequencePodcast([]), FakePlugin())
        instance.document_compiler = fixture.compiler
        job = fixture.request()
        await instance._handle_document_job(job)
        await instance._handle_document_job(job)
        rows = fixture.f.store.outbox_items(job.key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].operation, 'reply')
        self.assertEqual(rows[0].target_id, 'msg1')
        self.assertTrue(rows[0].reply_in_thread)
        self.assertEqual(len(messenger.delivered), 1)
        self.assertEqual(rows[0].content.count('https://www.feishu.cn/docx/'), 1)
