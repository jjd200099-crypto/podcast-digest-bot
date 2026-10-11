import copy
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import test_reader_daily as fixtures

from news_officer.feishu import FeishuMessenger
from news_officer.library import LibraryError
from news_officer.store import Store


class ReceiptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ReaderDailyTests.setUp
    tearDown = fixtures.ReaderDailyTests.tearDown
    item = fixtures.ReaderDailyTests.item
    setup_job = fixtures.ReaderDailyTests.setup_job

    def prepare(self):
        instance, job, _ = self.setup_job([self.item('one')])
        instance.settings.daily_chat_ids = ('oc_test',)
        instance._persist_daily_items(job, [self.item('one')])
        instance._prepare_combined_daily(job, finalize=True)
        item = self.store.pending_outbox(job.key)[0]
        messenger = FeishuMessenger('bot', 'unused')
        messenger.deliver = Mock(return_value='om_sent')
        instance.messenger = messenger
        self.message = {
            'message_id': 'om_sent', 'chat_id': 'oc_test', 'msg_type': item.msg_type,
            'create_time': str(int(time.time() * 1000)),
            'sender': {'id': 'bot', 'id_type': 'app_id', 'sender_type': 'app'},
            'body': {'content': item.content}}
        return instance, job, item, messenger

    async def test_crash_after_send_before_bookkeeping_recovers_after_uuid_window(self):
        instance, job, frozen, messenger = self.prepare()
        with (patch.object(self.store, 'mark_outbox_sent', side_effect=RuntimeError('crash')),
              self.assertRaisesRegex(RuntimeError, 'crash')):
            await instance._drain_outbox(job)
        self.assertEqual(messenger.deliver.call_count, 1)
        reopened = Store(self.store.path)
        reopened.initialize()
        instance.store = reopened
        pending = reopened.pending_outbox(job.key)[0]
        self.assertEqual(pending.uuid, frozen.uuid)
        self.assertEqual(pending.attempts, 1)
        # Restart after two hours: UUID deduplication can no longer save us.
        with (patch('news_officer.feishu.time.time', return_value=time.time() + 7200),
              patch('news_officer.library.FeishuLibraryAPI.request',
                    return_value={'items': [self.message], 'has_more': False}) as read):
            await instance._drain_outbox(job)
        self.assertEqual(messenger.deliver.call_count, 1)
        self.assertEqual(read.call_args.args, ('GET', '/im/v1/messages'))
        self.assertEqual(read.call_args.kwargs['params']['container_id'], 'oc_test')
        self.assertEqual(reopened.pending_outbox(job.key), [])
        with reopened._connect() as db:
            self.assertEqual(tuple(db.execute('SELECT status,remote_message_id FROM outbox').fetchone()),
                             ('sent', 'om_sent'))

    async def test_absent_receipt_retries_exact_uuid_after_readback(self):
        instance, job, frozen, messenger = self.prepare()
        messenger.deliver.side_effect = [TimeoutError('lost'), 'om_sent']
        with self.assertRaises(RuntimeError):
            await instance._drain_outbox(job)
        with patch('news_officer.library.FeishuLibraryAPI.request', return_value={'items': [], 'has_more': False}):
            await instance._drain_outbox(job)
        self.assertEqual([call.args[0].uuid for call in messenger.deliver.call_args_list], [frozen.uuid, frozen.uuid])

    async def test_unreadable_receipt_never_resends(self):
        instance, job, frozen, messenger = self.prepare()
        self.store.mark_outbox_attempt(frozen.id)
        with (patch('news_officer.library.FeishuLibraryAPI.request', side_effect=LibraryError('offline')),
              self.assertRaises(RuntimeError)):
            await instance._drain_outbox(job)
        messenger.deliver.assert_not_called()
        self.assertEqual(len(self.store.pending_outbox(job.key)), 1)

    def test_history_requires_exact_identity_content_and_time(self):
        _, _, item, messenger = self.prepare()
        started = self.store.outbox_created_at(item.id)
        for change in ({'sender': {'id': 'other'}}, {'deleted': True}, {'chat_id': 'oc_other'},
                       {'create_time': '0'}, {'msg_type': 'text'},
                       {'body': {'content': json.dumps({'zh_cn': {'title': 'manual', 'content': []}})}}):
            message = {**self.message, **change}
            with patch('news_officer.library.FeishuLibraryAPI.request', return_value={'items': [message]}):
                self.assertEqual(messenger.reconcile_delivery(item, started), '')

    def test_paginated_receipt_normalizes_platform_post(self):
        _, _, item, messenger = self.prepare()
        message = copy.deepcopy(self.message)
        content = json.loads(message['body']['content'])['zh_cn']
        content['content_v2'] = 'metadata'
        for line in content['content']:
            for node in line:
                node.setdefault('style', [])
        message['body']['content'] = json.dumps(content)
        with patch('news_officer.library.FeishuLibraryAPI.request', side_effect=[
                {'items': [], 'has_more': True, 'page_token': 'next'}, {'items': [message]}]) as read:
            self.assertEqual(messenger.reconcile_delivery(item, self.store.outbox_created_at(item.id)), 'om_sent')
        self.assertEqual(read.call_count, 2)

    def test_incomplete_or_ambiguous_history_fails_closed(self):
        _, _, item, messenger = self.prepare()
        for data in ({'items': [], 'has_more': True},
                     {'items': [self.message, {**self.message, 'message_id': 'om_duplicate'}]}):
            with (patch('news_officer.library.FeishuLibraryAPI.request', return_value=data),
                  self.assertRaises(RuntimeError)):
                messenger.reconcile_delivery(item, self.store.outbox_created_at(item.id))

    def test_private_unknown_receipt_replays_only_inside_uuid_window(self):
        _, _, item, messenger = self.prepare()
        private = replace(item, target_type='open_id', target_id='ou_test')
        started = self.store.outbox_created_at(item.id)
        self.assertEqual(messenger.reconcile_delivery(private, started), '')
        with (patch('news_officer.feishu.time.time', return_value=time.time() + 7200),
              self.assertRaisesRegex(RuntimeError, 'outside UUID window')):
            messenger.reconcile_delivery(private, started)

    def test_reply_receipt_is_scoped_to_parent(self):
        _, _, item, messenger = self.prepare()
        reply = replace(item, operation='reply', target_id='om_parent', target_type='')
        message = {**self.message, 'parent_id': 'om_parent'}
        with patch('news_officer.library.FeishuLibraryAPI.request', side_effect=[
                {'items': [{'message_id': 'om_parent', 'chat_id': 'oc_test'}]}, {'items': [message]}]):
            self.assertEqual(messenger.reconcile_delivery(reply, self.store.outbox_created_at(item.id)), 'om_sent')
