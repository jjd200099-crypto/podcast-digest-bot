import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from news_officer.models import Episode, Transcript
from news_officer.podcast import FeedDiscoveryError, PodcastService, TranscriptResolver
from news_officer.podwise import PodwiseTranscriptProvider
from news_officer.store import Store
from news_officer.youtube import YouTubeTranscriptProvider


class RSSDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.store.initialize()
        self.feeds = self.root / 'feeds.json'
        self.feeds.write_text(json.dumps({'sources': [{
            'name': 'Show', 'type': 'youtube', 'url': 'https://www.youtube.com/@show/videos',
            'rss_url': 'https://example.org/feed',
        }]}))
        self.episode = Episode('rss:one', 'A full AI interview', 'https://example.org/episode', 'Show',
                               published_at=datetime.now(UTC), duration_seconds=1200,
                               metadata={'rss_feed_url': 'https://example.org/feed'})

    def service(self, resolver=None):
        return PodcastService(self.store, self.feeds, Mock(summarize=Mock(return_value='summary')),
                              transcript_resolver=resolver, daily_rss_only=True,
                              podwise_api_token='test-token')

    def test_daily_never_calls_youtube_and_uses_rss_date_and_podwise(self):
        podwise = PodwiseTranscriptProvider('test-token')
        podwise.fetch = Mock(return_value=Transcript('complete text', 'Podwise', 'https://example.org/text', True))
        youtube = YouTubeTranscriptProvider()
        youtube.fetch = Mock(side_effect=AssertionError('YouTube must not run'))
        service = self.service(TranscriptResolver([podwise, youtube]))
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[self.episode]), \
                patch('news_officer.podcast.latest_videos') as scan, \
                patch('news_officer.podcast.video_metadata') as metadata:
            results = service.build_daily()
        self.assertEqual([r.status for r in results], ['summarized'])
        self.assertEqual(results[0].episode.published_at, self.episode.published_at)
        scan.assert_not_called()
        metadata.assert_not_called()
        youtube.fetch.assert_not_called()
        podwise.fetch.assert_called_once()

    def test_missing_rss_date_is_not_replaced_with_youtube_guess(self):
        service = self.service()
        undated = replace(self.episode, published_at=None,
                          metadata={**self.episode.metadata, 'youtube_url': 'https://youtu.be/Qv2vMj0Uq3c'})
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[undated]), \
                patch('news_officer.podcast.video_metadata') as metadata:
            self.assertEqual(service.build_daily()[0].status, 'unverified_date')
        metadata.assert_not_called()

    def test_rss_outage_is_a_failure_not_a_youtube_fallback(self):
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=TimeoutError('offline')), \
                patch('news_officer.podcast.latest_videos') as scan, self.assertRaises(FeedDiscoveryError):
            self.service().discover_daily_candidates()
        scan.assert_not_called()

    def test_old_merged_id_is_not_resent_after_switching_to_rss_ids(self):
        self.store.record_episode(replace(self.episode, id='old-youtube-id'), 'summarized')
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[self.episode]):
            self.assertEqual(self.service().discover_daily_candidates(), [])

    def test_no_rss_source_is_reported_not_silently_scanned_on_youtube(self):
        self.feeds.write_text(json.dumps({'sources': [{
            'name': 'Missing RSS', 'type': 'youtube', 'url': 'https://www.youtube.com/@show/videos',
        }]}))
        with patch('news_officer.podcast.latest_videos') as scan, self.assertRaises(FeedDiscoveryError):
            self.service().discover_daily_candidates()
        scan.assert_not_called()

    def test_interactive_resolver_preserves_youtube_but_prioritizes_podwise(self):
        service = self.service()
        providers = [type(p) for p in service.transcript_resolver.providers]
        self.assertLess(providers.index(PodwiseTranscriptProvider), providers.index(YouTubeTranscriptProvider))
        self.assertFalse(any(isinstance(p, YouTubeTranscriptProvider)
                             for p in service.daily_transcript_resolver.providers))
