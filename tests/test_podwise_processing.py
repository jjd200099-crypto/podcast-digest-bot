import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from news_officer.daily_report import ranked_daily_items
from news_officer.models import DailyItem, Episode, Transcript
from news_officer.podcast import PodcastService, TranscriptResolver
from news_officer.podwise import PodwiseAPIError, PodwiseTranscriptProvider
from news_officer.store import Store


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'test.sqlite3')
        self.store.initialize()
        self.episode = Episode('rss:test', 'Ep. 032 - 300 Data Center Bans, 3 Projects Delayed: Moratoriums Explained (Datacenter, Energy) | Guest',
            'https://publisher.test/episode', 'SemiAnalysis Weekly', duration_seconds=1200,
            published_at=datetime.now(UTC),
            metadata={'rss_feed_url': 'https://publisher.test/feed', 'audio_url': 'https://publisher.test/full.mp3'})
        self.rss = {'seq': 123, 'title': self.episode.title, 'podcastName': self.episode.show,
            'publishTime': self.episode.published_at.timestamp(), 'duration': 1200,
            'link': self.episode.metadata['audio_url'], 'transcribed': False}
        self.video = {**self.rss, 'seq': 456, 'duration': None, 'podcastName': 'SemiAnalysis',
            'title': self.episode.title.replace('032', '033').split('|')[0].strip(),
            'link': 'https://www.youtube.com/watch?v=alternate', 'transcribed': True}
        self.segments = [{'time': f'{i//2:02}:{i%2*30:02}', 'start': i*30, 'end': (i+1)*30,
                         'content': 'We discuss AI infrastructure and capital allocation with concrete evidence. ' * 5}
                         for i in range(40)]
        self.provider = PodwiseTranscriptProvider('fake', processing_store=self.store, auto_process=True)
        self.status = 'not_requested'
        self.credit = 5
        self.alternate = True
        self.provider._get = Mock(side_effect=self.get)
        self.provider._process = Mock(return_value={'result': {'status': 'waiting'}})

    def get(self, path, params=None):
        if path == '/episodes/search':
            return {'result': [self.rss] if params['q'] == self.episode.title[:300] else ([self.video, self.rss] if self.alternate else [self.rss])}
        if path == '/episodes/456/transcripts':
            return {'episode': self.video, 'result': self.segments}
        if path == '/episodes/123/transcripts':
            return {'episode': {**self.rss, 'transcribed': True}, 'result': self.segments}
        if path == '/episodes/456/status':
            return {'result': {'status': 'done', 'progress': 100}}
        if path == '/episodes/123/status':
            return {'result': {'status': self.status}}
        if path == '/me':
            return {'result': {'plan': 'Pro', 'credits': {'aiProcessing': self.credit, 'aiProcessingAddOn': 0}}}
        raise AssertionError(path)

    def test_cross_version_uses_existing_full_text_before_spending_credit(self):
        result = self.provider.fetch(self.episode)
        self.assertTrue(result.verified_complete)
        self.assertIn('/456/transcripts', result.source_url)
        self.provider._process.assert_not_called()

    def test_cross_version_does_not_accept_wrong_publisher_clip_date_or_gaps(self):
        original = self.video.copy()
        for changes in ({'podcastName': 'Impostor'}, {'publishTime': 0}, {'duration': 300},
                        {'title': self.video['title'] + ' highlight clip'}):
            self.video = {**original, **changes}
            self.provider.auto_process = False
            self.assertIsNone(self.provider.fetch(self.episode))
        self.video = original
        self.segments = self.segments[:8] + self.segments[15:]
        self.assertIsNone(self.provider.fetch(self.episode))
        self.provider._process.assert_not_called()

    def test_auto_process_is_opt_in_and_only_for_rss(self):
        self.alternate = False
        self.provider.auto_process = False
        self.assertIsNone(self.provider.fetch(self.episode))
        self.provider._process.assert_not_called()
        self.provider.auto_process = True
        self.assertIsNone(self.provider.fetch(replace(self.episode, metadata={})))
        self.provider._process.assert_not_called()

    def test_submit_once_durable_across_restart(self):
        self.alternate = False
        self.assertIsNone(self.provider.fetch(self.episode))
        self.provider._process.assert_called_once_with(123)
        self.assertIn('提交转写', self.provider.diagnostics[self.episode.id])
        restarted = PodwiseTranscriptProvider('fake', processing_store=Store(self.store.path), auto_process=True)
        restarted._get = self.provider._get
        restarted._process = Mock()
        restarted.fetch(self.episode)
        restarted._process.assert_not_called()

    def test_pending_done_failed_quota_do_not_trigger_billable_repeat(self):
        self.alternate = False
        for status in ('waiting', 'processing', 'failed', 'done'):
            self.status = status
            self.provider.fetch(self.episode)
        self.status = 'not_requested'
        self.credit = 0
        self.provider.fetch(self.episode)
        self.assertIn('额度不足', self.provider.diagnostics[self.episode.id])
        self.provider._process.assert_not_called()

    def test_uncertain_post_is_not_repeated(self):
        self.alternate = False
        self.provider._process.side_effect = PodwiseAPIError('uncertain')
        self.provider.fetch(self.episode)
        self.provider.fetch(self.episode)
        self.provider._process.assert_called_once()

    def test_completed_status_beats_stale_search_index_without_spending(self):
        self.alternate = False
        self.status = 'done'
        self.assertTrue(self.provider.fetch(self.episode).verified_complete)
        self.provider._process.assert_not_called()

    def test_same_asset_reservation_is_global_not_per_episode(self):
        self.assertTrue(self.store.reserve_podwise_processing(123))
        self.assertFalse(Store(self.store.path).reserve_podwise_processing(123))

    def test_backlog_retains_publisher_metadata_and_survives_24h_window(self):
        old = replace(self.episode, published_at=datetime.now(UTC)-timedelta(days=3))
        self.store.defer_daily_transcript(old, 'waiting')
        self.assertEqual(self.store.due_daily_transcripts(), [])
        with self.store._connect() as db:
            db.execute("UPDATE daily_transcript_backlog SET next_check_at='2000-01-01'")
        self.assertEqual(self.store.due_daily_transcripts()[0].metadata, old.metadata)
        transcript = Transcript('full text', 'Podwise', 'https://example.test/full', True)
        service = PodcastService(self.store, self.root/'feeds.json', Mock(summarize=Mock(return_value='summary')),
            TranscriptResolver([Mock(fetch=Mock(return_value=transcript))]), lookback_hours=24)
        with patch.object(service, 'discover_daily_candidates', side_effect=AssertionError('must not rescan')):
            result = service.build_pending()
        self.assertEqual(result[0].status, 'summarized')
        self.store.record_episode(old, 'sent')
        self.assertEqual(service.build_pending(), [])
        self.assertEqual(self.store.due_daily_transcripts(), [])

    def test_no_transcript_is_deferred_with_specific_reason(self):
        self.alternate = False
        service = PodcastService(self.store, self.root/'feeds.json', Mock(), TranscriptResolver([self.provider]))
        service.discover_daily_candidates = Mock(return_value=[self.episode])
        result = service.build_daily()
        self.assertEqual(result[0].status, 'no_transcript')
        self.assertIn('提交转写', result[0].message)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_transcript_backlog').fetchone()[0], 1)

    def test_sorting_preserves_low_scores_and_works_with_legacy_stars(self):
        items = [DailyItem(self.episode, 'summarized', f'推荐星级：{stars}') for stars in
                 ('★☆☆☆☆', '★★★★★（5/5，编辑推荐）', '★★★☆☆')]
        items.insert(0, DailyItem(self.episode, 'no_transcript'))
        ordered = ranked_daily_items(items)
        self.assertEqual([i.message for i in ordered[:3]], [items[2].message, items[3].message, items[1].message])
        self.assertEqual(ordered[-1].status, 'no_transcript')


if __name__ == '__main__':
    unittest.main()
