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

    def test_no_rss_source_is_silently_skipped(self):
        self.feeds.write_text(json.dumps({'sources': [{
            'name': 'Missing RSS', 'type': 'youtube', 'url': 'https://www.youtube.com/@show/videos',
        }]}))
        with patch('news_officer.podcast.latest_videos') as scan:
            service = self.service()
            self.assertEqual(service.discover_daily_candidates(), [])
            self.assertEqual(service._last_failed_feeds, ())
        scan.assert_not_called()

    def test_interactive_resolver_preserves_youtube_but_prioritizes_podwise(self):
        service = self.service()
        providers = [type(p) for p in service.transcript_resolver.providers]
        self.assertLess(providers.index(PodwiseTranscriptProvider), providers.index(YouTubeTranscriptProvider))
        self.assertTrue(any(isinstance(p, YouTubeTranscriptProvider)
                             for p in service.daily_transcript_resolver.providers))

    def test_configured_identity_for_rss_youtube_and_official_titles(self):
        for title in ['RSS renamed show', 'YouTube channel title', 'Sequoia Capital']:
            with self.subTest(title=title), patch('news_officer.podcast.latest_rss_episodes',
                                                return_value=[replace(self.episode, show=title)]):
                [episode] = self.service().discover_daily_candidates()
                self.assertEqual(episode.metadata['source_name'], 'Show')
                self.assertEqual(episode.metadata['source_priority'], 'B')

    def test_youtube_fallback_retains_configured_identity(self):
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[]), \
                patch('news_officer.podcast.latest_videos', return_value=[self.episode]):
            service = self.service()
            service.daily_rss_only = False
            [episode] = service.discover_daily_candidates()
        self.assertEqual(episode.metadata['source_name'], 'Show')

    def test_podwise_resolves_configured_identity_and_priority(self):
        self.feeds.write_text(self.feeds.read_text().replace('"type": "youtube"',
                                                           '"priority": "A", "type": "youtube"'))
        episode = replace(self.episode, id='podwise:123', show='Podwise own title',
                          metadata={'source_priority': 'B', 'source_name': 'Show'})
        service = self.service()
        service.discovery = Mock(discover=Mock(return_value=Mock(
            episodes=[episode], notice='', failed_requests=0)))
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[]):
            [candidate] = service.discover_daily_candidates()
        self.assertEqual(candidate.metadata['source_name'], 'Show')
        self.assertEqual(candidate.metadata['source_priority'], 'A')

    def test_official_metadata_merge_preserves_configured_source(self):
        configured = replace(self.episode, metadata={
            **self.episode.metadata, 'source_name': 'Sequoia Capital — Training Data',
            'source_priority': 'A'})
        official = replace(self.episode, show='Sequoia Capital', metadata={
            'source_name': 'Provider title', 'source_priority': 'B'})
        merged = PodcastService._merge_episode(configured, official)
        self.assertEqual(merged.metadata['source_name'], 'Sequoia Capital — Training Data')
        self.assertEqual(merged.metadata['source_priority'], 'A')
