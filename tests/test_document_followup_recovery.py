import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_episode_document as fixtures
from test_reliability import FakeMessenger, runtime

from news_officer.daily_enrichment import enqueue_followup


class FollowupRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.SelectedDocuments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture.fixture
        self.store = self.f.store
        self.messenger = FakeMessenger()
        self.settings = SimpleNamespace(daily_chat_ids=('oc_test', 'oc_other'),
                                        daily_require_chat_ids=True, daily_document_followup=True)
        self.owner = self.restart()
        self.store.save_job_result(self.f.base, 'bundle', 'daily_bundle', {
            'group_key': 'daily:bundle:test',
            'enrichment': {'version': 1, 'day': '2026-09-21', 'eligible': ['one']},
            'deliveries': [{'target_type': 'chat_id', 'target_id': t}
                           for t in self.settings.daily_chat_ids]})
        self.store.save_job_result(self.f.base, 'daily:document:one', 'daily_document_ready', {
            'documents': [{'episode_id': 'one', 'title': 'One',
                           'url': 'https://www.feishu.cn/docx/one'}],
            'targets': [('chat_id', t) for t in self.settings.daily_chat_ids]})
        self.assertEqual(self.enqueue(), 1)
        self.job = self.store.claim_next('document')
        self.assertEqual(self.job.key, 'daily-document-followup:2026-09-21')

    def restart(self):
        owner = runtime(self.store, self.messenger, None)
        owner.settings = self.settings
        return owner

    def enqueue(self):
        return enqueue_followup(self.store, self.f.base, '2026-09-21',
                                audience_filter=self.owner._document_audience,
                                enabled=self.settings.daily_document_followup)

    def receipts(self):
        with self.store._connect() as db:
            return db.execute('SELECT status,remote_message_id FROM outbox WHERE job_key=?',
                              (self.job.key,)).fetchall()

    def advance(self):
        with self.store._connect() as db:
            due = db.execute('SELECT available_at FROM jobs WHERE job_key=?',
                             (self.job.key,)).fetchone()[0]
        return max(datetime.now(UTC), datetime.fromisoformat(due)) + timedelta(seconds=1)

    async def test_disabled_existing_job_completes_without_outbox_or_send(self):
        self.settings.daily_document_followup = False
        with patch.object(self.owner, '_drain_outbox', new_callable=AsyncMock) as drain:
            await self.owner._handle_document_job(self.job)
            drain.assert_not_awaited()
        self.store.complete(self.job.key)
        self.assertEqual(self.store.outbox_items(self.job.key), [])
        self.assertEqual(self.messenger.attempts, [])
        self.assertEqual(self.enqueue(), 0)

    async def test_disabled_retry_cancels_pending_outbox_preserving_history(self):
        with patch.object(self.owner, '_drain_outbox', new_callable=AsyncMock):
            await self.owner._handle_document_job(self.job)
        before = [r.uuid for r in self.store.outbox_items(self.job.key)]
        self.settings.daily_document_followup = False
        await self.restart()._handle_document_job(self.job)
        self.store.complete(self.job.key)
        self.assertEqual([r.uuid for r in self.store.outbox_items(self.job.key)], before)
        self.assertTrue(all(r['status'] == 'cancelled' for r in self.receipts()))
        self.assertEqual(self.messenger.attempts, [])

    async def test_lost_receipt_backoff_restart_and_failed_revival_keep_one_message_per_group(self):
        self.messenger.fail_groups_once.add('document:link')
        with self.assertRaises(RuntimeError):
            await self.owner._handle_document_job(self.job)
        self.store.fail(self.job.key, 'lost receipt', self.job.attempts)
        self.assertIsNone(self.store.claim_next('document'))
        before = {r.target_id: r.uuid for r in self.store.outbox_items(self.job.key)}
        now = self.advance()
        self.owner = self.restart()
        with patch('news_officer.store._now', return_value=now.isoformat()):
            retry = self.store.claim_next('document')
        await self.owner._handle_document_job(retry)
        # Crash after persisting receipts but before completing the job.
        self.assertEqual(self.store.recover_interrupted_jobs(), 1)
        now = self.advance()
        self.owner = self.restart()
        with patch('news_officer.store._now', return_value=now.isoformat()):
            recovered = self.store.claim_next('document')
        await self.owner._handle_document_job(recovered)
        self.store.complete(recovered.key)
        self.assertEqual(self.enqueue(), 0)
        # #47's failed job recovery must also reuse the sent outbox.
        self.store.fail(recovered.key, 'recovered', 5, failed_daily_requeue_seconds=0)
        self.assertEqual(self.enqueue(), 1)
        revived = self.store.claim_next('document')
        await self.restart()._handle_document_job(revived)
        self.store.complete(revived.key)
        self.assertEqual(len(self.messenger.delivered), 2)
        self.assertEqual(len(self.messenger.attempts), 3)
        rows = self.store.outbox_items(revived.key)
        self.assertEqual({r.target_id: r.uuid for r in rows}, before)
        self.assertTrue(all(r['status'] == 'sent' and r['remote_message_id']
                            for r in self.receipts()))

    async def test_empty_receipt_remains_pending_and_retry_reuses_uuid(self):
        for receipt in (None, '', '   '):
            with (self.subTest(receipt=receipt),
                  patch.object(self.messenger, 'deliver', return_value=receipt),
                  self.assertRaisesRegex(RuntimeError, 'outbox delivery')):
                await self.owner._handle_document_job(self.job)
            self.assertTrue(all(r['status'] == 'pending' and not r['remote_message_id']
                                for r in self.receipts()))
            with self.assertRaisesRegex(RuntimeError, 'pending outbox'):
                self.store.complete(self.job.key)
        before = [r.uuid for r in self.store.outbox_items(self.job.key)]
        self.store.fail(self.job.key, 'empty receipt', self.job.attempts)
        with patch('news_officer.store._now', return_value=self.advance().isoformat()):
            retry = self.store.claim_next('document')
        await self.restart()._handle_document_job(retry)
        self.store.complete(retry.key)
        self.assertEqual([r.uuid for r in self.store.outbox_items(retry.key)], before)
        self.assertEqual(len(self.messenger.delivered), 2)
        self.assertEqual(self.enqueue(), 0)
