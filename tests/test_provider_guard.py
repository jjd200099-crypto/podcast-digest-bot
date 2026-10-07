import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from news_officer.models import Episode, Transcript
from news_officer.podcast import TranscriptResolver
from news_officer.podwise import PodwiseTranscriptProvider
from news_officer.provider_guard import PodwiseRateLimited, RequestGuard, podwise_guard
from news_officer.store import Store


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.sqlite3')
        self.store.initialize()
        self.now = 1000.
        self.sleeps = []
        self.guard = RequestGuard('test', store=self.store, clock=lambda: self.now, sleep=self.sleep)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def response(self, status=200, retry='600'):
        response = MagicMock(status_code=status, headers={'Retry-After': retry})
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps({'success': True, 'result': []}).encode()]
        return response

    def test_shared_credential_and_serial_pacing(self):
        self.assertIs(podwise_guard('unique-test-token'), podwise_guard('unique-test-token'))
        provider = PodwiseTranscriptProvider('test', request_guard=self.guard)
        with patch('news_officer.podwise.requests.get', return_value=self.response()) as get:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(provider._get, ['/one', '/two', '/three', '/four']))
            self.assertEqual(get.call_count, 4)
        self.assertEqual(self.sleeps, [1, 1, 1])

    def test_429_stops_all_subsequent_network_calls_and_survives_restart(self):
        provider = PodwiseTranscriptProvider('test', request_guard=self.guard)
        with patch('news_officer.podwise.requests.get', return_value=self.response(429)) as get:
            with self.assertRaises(PodwiseRateLimited) as error:
                provider._get('/one')
            self.assertEqual(error.exception.retry_at, 1600)
            restarted = RequestGuard('test', store=self.store, clock=lambda: self.now)
            provider = PodwiseTranscriptProvider('test', request_guard=restarted)
            for _ in range(12):
                with self.assertRaises(PodwiseRateLimited):
                    provider._get('/other')
            self.assertEqual(get.call_count, 1)

    def test_cooldown_probe_failure_backs_off_success_resets(self):
        for delay in (300, 600, 1200):
            with self.assertRaises(PodwiseRateLimited), self.guard.request():
                self.guard.limited('not-a-date')
            self.assertEqual(self.guard.retry_at, self.now + delay)
            self.now = self.guard.retry_at
        with self.guard.request():
            self.guard.succeeded()
        self.assertEqual(self.store.provider_cooldown('test')['failures'], 0)

    def test_retry_after_http_date_and_untrusted_values(self):
        for value, delay in [('Thu, 01 Jan 1970 01:00:00 GMT', 2600), ('nan', 300),
                             ('-1', 300), ('99999999', 86400)]:
            guard = RequestGuard('isolated', clock=lambda: 1000)
            with self.assertRaises(PodwiseRateLimited):
                guard.limited(value)
            self.assertEqual(guard.retry_at, 1000 + delay)

    def test_cooldown_blocks_paid_post_without_network(self):
        with self.assertRaises(PodwiseRateLimited):
            self.guard.limited()
        provider = PodwiseTranscriptProvider('test', request_guard=self.guard)
        with patch('news_officer.podwise.requests.post') as post:
            with self.assertRaises(PodwiseRateLimited):
                provider._process(123)
            post.assert_not_called()

    def test_fallbacks_can_succeed_despite_podwise_cooldown(self):
        episode = Episode('e', 'Title', 'https://example.org/e', 'Show')
        source = Mock(name='podwise')
        source.name = 'podwise'
        source.fetch.side_effect = PodwiseRateLimited(2000)
        fallback = Mock()
        transcript = Transcript('full', 'https://example.org/e', 'official', True)
        fallback.fetch.return_value = transcript
        self.assertEqual(TranscriptResolver([source, fallback]).fetch(episode), transcript)
        with self.assertRaises(PodwiseRateLimited):
            TranscriptResolver([source]).fetch(episode)

    def test_backlog_cooldown_and_daily_priority(self):
        episode = Episode('e', 'Title', 'https://example.org/e', 'Show')
        future = datetime.now(UTC) + timedelta(hours=2)
        self.store.defer_daily_transcript(episode, 'limited', retry_at=future)
        self.assertEqual(self.store.due_daily_transcripts(), [])
        self.store.enqueue('daily:transcript-catchup:old', 'daily', {})
        self.assertTrue(self.store.has_unfinished_transcript_catchup())
        self.store.enqueue('daily:2026-10-08', 'daily', {})
        self.assertEqual(self.store.claim_next('daily').key, 'daily:2026-10-08')
        self.store.complete('daily:transcript-catchup:old')
        self.assertFalse(self.store.has_unfinished_transcript_catchup())
