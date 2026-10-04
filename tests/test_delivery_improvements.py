import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from test_editorial import SUMMARY
from test_reliability import FakeMessenger, runtime

from news_officer.daily_archive import read_daily_digest
from news_officer.models import Episode, Transcript
from news_officer.operations import business_snapshot
from news_officer.podcast import PodcastService
from news_officer.store import Store


class DeliveryImprovements(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'db.sqlite3')
        self.store.initialize()
        self.now = datetime(2026, 10, 4, 0, 30, tzinfo=UTC)
        self.episode = Episode('rss:one', 'An AI interview', 'https://example.org/one', 'Show',
                               published_at=self.now - timedelta(hours=1))

    def service(self):
        resolver = Mock()
        resolver.fetch.return_value = Transcript('Verified interview content.', 'official', self.episode.url, True)
        summarizer = Mock()
        summarizer.summarize.return_value = SUMMARY
        summarizer.cache_identity.return_value = 'narrative-v1'
        service = PodcastService(self.store, self.root / 'feeds.json', summarizer, resolver)
        service.discover_daily_candidates = Mock(return_value=[self.episode])
        return service, resolver, summarizer

    def test_prepared_summary_reused_without_marking_sent(self):
        service, _, summarizer = self.service()
        first = service.build_daily(self.now, preparation=True)
        second = service.build_daily(self.now)
        self.assertEqual(first[0].message, second[0].message)
        self.assertEqual(summarizer.summarize.call_count, 1)
        self.assertFalse(self.store.episode_is_delivered(self.episode))

    def test_source_model_and_digest_tampering_invalidate_cache(self):
        service, resolver, summarizer = self.service()
        service.build_daily(self.now, preparation=True)
        resolver.fetch.return_value = Transcript('New complete source.', 'official', self.episode.url, True)
        service.build_daily(self.now)
        summarizer.cache_identity.return_value = 'new-model-or-prompt'
        service.build_daily(self.now)
        with self.store._connect() as db:
            db.execute("UPDATE episode_digests SET digest_markdown='tampered'")
        result = service.build_daily(self.now)
        self.assertEqual(result[0].status, 'failed')
        self.assertEqual(summarizer.summarize.call_count, 3)

    def test_preparation_keeps_filtered_item_available_for_daily_coverage(self):
        service, _, summarizer = self.service()
        service.editorial_policy = Mock()
        service.editorial_policy.assess.return_value = {'selected': False, 'stars': 1}
        self.assertEqual(service.build_daily(self.now, preparation=True)[0].status, 'editorial_filtered')
        self.assertTrue(self.store.should_review_episode(self.episode.id))
        self.assertEqual(service.build_daily(self.now)[0].status, 'editorial_filtered')
        self.assertFalse(self.store.should_review_episode(self.episode.id))
        self.assertIsNotNone(self.store.get_verified_transcript(self.episode.id))
        summarizer.summarize.assert_not_called()

    async def test_preparation_never_sends_or_appears_as_delivered_daily(self):
        messenger, podcast = FakeMessenger(), Mock()
        podcast.build_daily.return_value = []
        instance = runtime(self.store, messenger, podcast)
        instance.settings.timezone = ZoneInfo('Asia/Shanghai')
        scheduled = datetime.now(UTC) + timedelta(hours=2)
        key = f'daily:prepare:{scheduled.date()}'
        self.store.enqueue(key, 'daily', {'prepare_only': True, 'scheduled_for': scheduled.isoformat()})
        job = self.store.claim_next('daily')
        await instance._handle_daily_job(job)
        podcast.build_daily.assert_called_once_with(preparation=True)
        self.assertEqual(messenger.attempts, [])
        self.assertEqual(self.store.outbox_items(key), [])
        self.assertEqual(read_daily_digest(self.store, str(scheduled.date()))['status'], 'not_generated')

    def test_business_monitor_distinguishes_disabled_pending_overdue_and_sent(self):
        self.assertEqual(business_snapshot(self.store.path, now=self.now)['daily_state'], 'not_subscribed')
        self.store.add_subscription('chat_id', 'test', source='test')
        self.assertEqual(business_snapshot(self.store.path, now=self.now)['daily_state'], 'pending')
        late = self.now + timedelta(hours=1)
        self.assertTrue(business_snapshot(self.store.path, now=late)['daily_overdue'])
        key = 'daily:2026-10-04'
        self.store.enqueue(key, 'daily', {})
        self.store.ensure_outbox(job_key=key, group_key='daily:bundle:test', delivery_key='d', operation='send',
            target_id='test', target_type='chat_id', reply_in_thread=False,
            parts=[('text', json.dumps({'text': 'private material must not be in status'}), 'u')])
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET status='sent',sent_at=?", (late.isoformat(),))
        self.assertEqual(business_snapshot(self.store.path, now=self.now)['daily_state'], 'partial')
        self.store.mark_analysis_complete(key)
        status = business_snapshot(self.store.path, now=late)
        self.assertEqual(status['daily_state'], 'sent')
        self.assertNotIn('private material', json.dumps(status))

    def test_backlog_reasons_are_counted_not_only_literal_pending(self):
        self.store.defer_daily_transcript(self.episode, '尚未取得全文')
        with self.store._connect() as db:
            db.execute("UPDATE daily_transcript_backlog SET created_at='2026-09-01T00:00:00+00:00'")
        snapshot = business_snapshot(self.store.path, now=self.now)
        self.assertEqual(snapshot['transcripts_pending'], 1)
        self.assertEqual(snapshot['transcripts_pending_over_72h'], 1)

    def test_watch_pool_requires_distinct_good_episodes(self):
        from dataclasses import replace

        episode = replace(self.episode, id='podwise:1', metadata={'podwise_podcast_seq': 12})
        for _ in range(3):
            self.store.observe_discovery(episode, 'summarized', 4)
        self.assertEqual(self.store.discovery_watch_catalogs(datetime.now(UTC)), [])
        self.store.observe_discovery(replace(episode, id='podwise:2'), 'summarized', 4)
        self.assertEqual(self.store.discovery_watch_catalogs(datetime.now(UTC)), [12])
        self.assertEqual(self.store.discovery_watch_catalogs(datetime.now(UTC) + timedelta(days=31)), [])

    def test_monitor_database_open_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            business_snapshot(self.root / 'missing.db', now=self.now)
        self.assertFalse((self.root / 'missing.db').exists())
