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
        items = [self.item('one', 5), self.item('two', 4), self.item('low', 3)]
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

    async def test_old_frozen_template_uses_current_label_without_mutating_archive(self):
        _, base = await self.prepare()
        bundle = enrichment_bundle(self.store, base.key)
        for section in bundle['enrichment']['sections']:
            section['template'] = section['template'].replace('精读和完整中文对谈', '精读与完整中文对谈')
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
        compiler = Mock(api=self.api)
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
        with self.assertRaises(TimeoutError):
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
            with self.assertRaises(LibraryError):
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
        with self.assertRaisesRegex(LibraryError, '身份或会话'):
            update_daily_message(self.store, self.api, base.key)
        self.assertEqual(self.api.puts, [])

    async def test_pending_send_never_enqueues_or_edits(self):
        _, base = await self.prepare()
        self.ready(base.key, 'one')
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET status='pending'")
        self.assertEqual(enqueue_enrichment(self.store, '2026-10-11'), 0)
        with self.assertRaises(LibraryError):
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
