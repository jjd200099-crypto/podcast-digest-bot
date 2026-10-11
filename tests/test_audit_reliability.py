import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests
import test_delivery_improvements as fixtures
from test_reliability import FakeResponse

from news_officer.feishu import FeishuMessenger
from news_officer.health import health_snapshot
from news_officer.library import FeishuLibraryAPI, LibraryError
from news_officer.models import Job
from news_officer.runtime import NewsOfficerRuntime


class LibraryRetryTests(unittest.TestCase):
    def setUp(self):
        self.messenger = Mock()
        self.messenger.token.return_value = 'test-token'
        self.api = FeishuLibraryAPI(self.messenger)
        self.sleep = patch('news_officer.library.time.sleep').start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(status=200, body=None):
        result = FakeResponse(status, body if body is not None else {'code': 0, 'data': {'ok': True}})
        result.ok = status < 400
        return result

    def test_get_recovers_network_and_server_failures(self):
        with patch('news_officer.library.requests.request', side_effect=[
            requests.ReadTimeout(), self.response(503), self.response()]) as request:
            self.assertEqual(self.api.request('GET', '/docx/v1/documents/test'), {'ok': True})
            self.assertEqual(request.call_count, 3)

    def test_keyed_append_retries_same_payload_and_token(self):
        with patch('news_officer.library.requests.request', side_effect=[requests.ReadTimeout(), self.response()]) as request:
            self.api.request('POST', '/docx/v1/documents/test/blocks/test/descendant',
                             params={'client_token': 'durable-key'}, data={'children': ['same']})
            first, second = request.call_args_list
            self.assertEqual(first, second)

    def test_uncertain_unkeyed_writes_and_message_edits_return_to_reconciliation(self):
        for method, path in [('POST', '/docx/v1/documents'), ('PUT', '/im/v1/messages/om_test'),
                             ('DELETE', '/docx/v1/documents/test/blocks/test/children')]:
            with self.subTest(path=path), patch('news_officer.library.requests.request',
                    side_effect=requests.ReadTimeout('must-not-leak-token')) as request:
                with self.assertRaises(LibraryError) as error:
                    self.api.request(method, path, data={})
                self.assertEqual(request.call_count, 1)
                self.assertNotIn('must-not-leak-token', str(error.exception))

    def test_401_refreshes_token_once(self):
        with patch('news_officer.library.requests.request', side_effect=[self.response(401), self.response()]):
            self.api.request('GET', '/docx/v1/documents/test')
        self.messenger._invalidate_token.assert_called_once()

    def test_retry_is_bounded_and_malformed_error_is_safe(self):
        with patch('news_officer.library.requests.request', side_effect=requests.ReadTimeout()) as request:
            with self.assertRaises(LibraryError):
                self.api.request('GET', '/test')
            self.assertEqual(request.call_count, 4)
        for body in ([], {'code': 403, 'error': None}, {'code': 403, 'secret': 'dont-print'}):
            with patch('news_officer.library.requests.request', return_value=self.response(403, body)):
                with self.assertRaises(LibraryError) as error:
                    self.api.request('GET', '/test')
                self.assertNotIn('dont-print', str(error.exception))


    def test_delivery_cannot_be_called_confirmed_without_remote_message_id(self):
        for body in ({'code': 0}, {'data': {}}, {'data': {'message_id': ''}}, {'data': []}):
            with self.assertRaisesRegex(RuntimeError, 'delivery unconfirmed'):
                FeishuMessenger._message_receipt(body)
        self.assertEqual(FeishuMessenger._message_receipt({'data': {'message_id': 'om_ok'}}), 'om_ok')

    def test_server_errors_do_not_leak_response_bodies(self):
        with self.assertRaises(RuntimeError) as error:
            FeishuMessenger._check(self.response(body={'code': 123, 'secret': 'sensitive-value'}), 'send')
        self.assertIn('123', str(error.exception))
        self.assertNotIn('sensitive-value', str(error.exception))


class BackgroundWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def test_long_background_work_is_visible_without_exposing_identifiers(self):
        channel = SimpleNamespace(connection_snapshot=lambda: SimpleNamespace(ready=True, state='connected'))
        state = health_snapshot(channel, {}, background_jobs={'private-job': (0, 2700)}, now=2701)
        self.assertFalse(state['ok'])
        self.assertEqual(state['stalled_background_jobs'], 1)
        self.assertNotIn('private-job', json.dumps(state))
        self.assertTrue(health_snapshot(channel, {}, background_jobs={'doc': (0, 3600)}, now=3599)['ok'])

    async def test_worker_registers_and_cleans_up_background_deadlines(self):
        for kind, payload, budget in [('daily', {}, 2700), ('daily', {'prepare_only': True}, 900),
                                      ('document', {}, 3600)]:
            with self.subTest(kind=kind, payload=payload):
                runtime = object.__new__(NewsOfficerRuntime)
                runtime.active_jobs, runtime.background_jobs = {}, {}
                runtime.wake_workers = {kind: asyncio.Event()}
                runtime.store = Mock()
                runtime.store.claim_next.side_effect = [Job('job', kind, payload, 1), None]
                entered = asyncio.Event()

                async def handle(job, runtime=runtime, budget=budget, entered=entered):
                    self.assertEqual(runtime.background_jobs[job.key][1], budget)
                    entered.set()
                    await asyncio.Future()

                runtime._handle_daily_job = handle
                runtime._handle_document_job = handle
                runtime._failure_notice = AsyncMock()
                task = asyncio.create_task(runtime._worker(kind))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(runtime.background_jobs, {})
                runtime.store.complete.assert_not_called()


class CandidateCheckpointTests(unittest.TestCase):
    setUp = fixtures.DeliveryImprovements.setUp
    service = fixtures.DeliveryImprovements.service

    def test_candidate_exists_before_provider_call(self):
        service, resolver, _ = self.service()
        transcript = resolver.fetch.return_value

        def fetch(episode):
            self.assertEqual([e.id for e in self.store.due_daily_transcripts()], [episode.id])
            return transcript

        resolver.fetch.side_effect = fetch
        self.assertEqual(service.build_daily(self.now)[0].status, 'summarized')

    def test_checkpoint_keeps_provider_metadata_for_restart(self):
        from dataclasses import replace
        episode = replace(self.episode, metadata={'audio_url': 'https://example.org/audio.mp3', 'podwise_seq': 123})
        self.store.track_daily_candidates([episode])
        self.assertEqual(self.store.due_daily_transcripts()[0].metadata, episode.metadata)

    def test_repeated_background_crashes_back_off_instead_of_starving_next_job(self):
        self.store.enqueue('old', 'document', {})
        self.store.claim_next('document')
        self.store.recover_interrupted_jobs()
        self.assertEqual(self.store.claim_next('document').attempts, 2)
        self.store.enqueue('next', 'document', {})
        self.store.recover_interrupted_jobs()
        self.assertEqual(self.store.claim_next('document').key, 'next')
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET status='processing',attempts=5 WHERE job_key='old'")
        self.store.recover_interrupted_jobs()
        with self.store._connect() as db:
            row = db.execute("SELECT status,available_at FROM jobs WHERE job_key='old'").fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertGreater(datetime.fromisoformat(row['available_at']), datetime.now(UTC))

    def test_recording_seen_candidate_preserves_cooldown_and_attempts(self):
        self.store.defer_daily_transcript(self.episode, 'limited', retry_at=datetime.now(UTC) + timedelta(hours=2))
        with self.store._connect() as db:
            before = tuple(db.execute('SELECT * FROM daily_transcript_backlog').fetchone())
        self.store.track_daily_candidates([self.episode])
        with self.store._connect() as db:
            after = tuple(db.execute('SELECT * FROM daily_transcript_backlog').fetchone())
        self.assertEqual(before, after)

    def test_summary_cap_keeps_unprocessed_release_for_later(self):
        from dataclasses import replace
        service, _, _ = self.service()
        second = replace(self.episode, id='rss:second')
        service.discover_daily_candidates.return_value.append(second)
        service.max_daily_summaries = 1
        self.assertEqual(len(service.build_daily(self.now)), 1)
        self.assertIn(second.id, [e.id for e in self.store.due_daily_transcripts()])


class SchedulerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrichment_failure_preserves_ticks_traceback_and_cancellation(self):
        runtime = object.__new__(NewsOfficerRuntime)
        runtime.settings = SimpleNamespace(timezone=UTC, daily_time=datetime.min.time(),
            manual_digest_id='', daily_reader_mode=True)
        runtime.store = Mock()
        runtime._has_daily_recipients = Mock(return_value=True)
        runtime._document_audience = Mock()
        runtime.document_compiler = Mock()
        runtime._wake_worker = Mock()
        ticks = 0

        async def sleep(_):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                raise asyncio.CancelledError

        async def thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch('news_officer.runtime.asyncio.to_thread', side_effect=thread), \
                patch('news_officer.runtime.asyncio.sleep', side_effect=sleep), \
                patch('news_officer.daily_enrichment.enqueue_enrichment',
                      side_effect=ValueError('sensitive-value')) as enrichment, \
                self.assertLogs('news_officer.runtime', level='ERROR') as logs, \
                self.assertRaises(asyncio.CancelledError):
            await runtime._scheduler()
        self.assertEqual(enrichment.call_count, 2)
        self.assertEqual(runtime.store.enqueue.call_count, 2)
        self.assertTrue(all(record.exc_info for record in logs.records))
        self.assertIn('Traceback', logs.output[0])
        self.assertNotIn('sensitive-value', ''.join(logs.output))

    async def test_cancelled_step_propagates(self):
        runtime = object.__new__(NewsOfficerRuntime)
        runtime.settings = SimpleNamespace(timezone=UTC)
        runtime._has_daily_recipients = Mock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await runtime._scheduler()
