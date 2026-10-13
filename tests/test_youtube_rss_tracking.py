import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from news_officer.models import Episode, Transcript
from news_officer.podcast import (
    FeedDiscoveryError,
    PodcastService,
    TranscriptResolver,
    YouTubeFeedSource,
)
from news_officer.rss import latest_rss_episodes
from news_officer.store import Store
from news_officer.youtube import YouTubeFinalSkip, YouTubeTranscriptProvider


class YouTubeRSSTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.store.initialize()
        self.feeds = Path(self.temp.name) / 'feeds.json'
        self.feeds.write_text(json.dumps({'sources': [{
            'name': 'Channel', 'type': 'youtube',
            'url': 'https://www.youtube.com/channel/UCXUPKJO5MZQN11PqgIvyuvQ/videos',
            'rss_url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UCXUPKJO5MZQN11PqgIvyuvQ',
        }]}))
        self.episode = Episode('abcdefghijk', '【Channel｜Long video】',
                               'https://www.youtube.com/watch?v=abcdefghijk', 'Channel',
                               published_at=datetime.now(UTC), duration_seconds=1200,
                               metadata={'source_type': 'youtube'})

    def service(self, provider):
        return PodcastService(self.store, self.feeds, Mock(summarize=Mock(return_value='summary')),
                              transcript_resolver=TranscriptResolver([provider]), daily_rss_only=True)

    def atom(self, *, title='Long video', link='https://www.youtube.com/watch?v=abcdefghijk', duration='1200', description='Full talk'):
        return f'''<feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
          <entry><yt:videoId>abcdefghijk</yt:videoId><title>{title}</title>
          <published>2026-10-13T00:00:00Z</published><link rel="alternate" href="{link}"/>
          <media:group><media:content duration="{duration}"/><media:description>{description}</media:description></media:group>
          </entry></feed>'''.encode()

    def parse(self, **kwargs):
        with patch('news_officer.rss._response', return_value=Mock(content=self.atom(**kwargs))):
            return latest_rss_episodes('Channel', 'https://www.youtube.com/feeds/videos.xml')

    def test_atom_identity_media_date_and_title(self):
        episode = self.parse()[0]
        self.assertEqual(episode.id, 'abcdefghijk')
        self.assertEqual(episode.title, '【Channel｜Long video】')
        self.assertEqual(episode.duration_seconds, 1200)
        self.assertEqual(episode.metadata['description'], 'Full talk')
        self.assertEqual(episode.metadata['source_type'], 'youtube')
        self.assertEqual(episode.published_at, datetime(2026, 10, 13, tzinfo=UTC))

    def test_shorts_and_short_duration_filtered_at_listing(self):
        for kwargs in [{'title': 'Clip #Shorts'}, {'description': '#shorts'},
                       {'link': 'https://www.youtube.com/shorts/abcdefghijk'}, {'duration': '899'}]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.parse(**kwargs), [])
        self.assertEqual(len(self.parse(duration='900')), 1)
        self.assertIsNone(self.parse(duration='')[0].duration_seconds)

    def test_channel_name_does_not_satisfy_topic_filter(self):
        source = YouTubeFeedSource('', include_keywords=('AI',))
        episode = replace(self.episode, title='【AI Channel｜Cooking】',
                          metadata={'youtube_title': 'Cooking'})
        self.assertFalse(source.accepts(episode))

    def test_missing_transcript_not_source_failure(self):
        provider = YouTubeTranscriptProvider()
        service = self.service(provider)
        with patch.object(provider, '_fetch_captions', side_effect=TimeoutError('offline')), \
                patch.object(provider, '_fetch_api', side_effect=RuntimeError('missing')), \
                patch('news_officer.podcast.latest_rss_episodes', return_value=[self.episode]):
            results = service.build_daily()
        self.assertEqual([r.status for r in results], ['no_transcript'])
        self.assertEqual(service._last_failed_feeds, ())

    def test_transcript_is_archived_before_summary(self):
        provider = YouTubeTranscriptProvider()
        provider.fetch = Mock(return_value=Transcript('full transcript ' * 1000, 'YouTube', self.episode.url, True))
        service = self.service(provider)
        def summarize(episode, transcript):
            self.assertEqual(self.store.get_verified_transcript(episode.id).transcript.text, transcript.text)
            self.assertEqual(episode.title, '【Channel｜Long video】')
            return 'summary'
        service.summarizer.summarize.side_effect = summarize
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[self.episode]):
            self.assertEqual(service.build_daily()[0].status, 'summarized')
        provider.fetch.assert_called_once()

    def test_duration_discovered_during_caption_fetch_is_final_skip(self):
        episode = replace(self.episode, duration_seconds=None)
        def extract(*args, **kwargs):
            target = Path(args[args.index('-o') + 1]).parent
            (target / 'video.info.json').write_text(json.dumps({'duration': 899}))
        provider = YouTubeTranscriptProvider()
        service = self.service(provider)
        with patch('news_officer.youtube.run', side_effect=extract), \
                patch.object(provider, '_fetch_api', return_value=None) as api, \
                patch('news_officer.podcast.latest_rss_episodes', return_value=[episode]):
            self.assertEqual(service.build_daily()[0].status, 'final_skipped')
        api.assert_called_once()
        self.assertIsNone(self.store.episode_review_state(episode.id))
        self.assertEqual(self.store.due_daily_transcripts(), [])

    def test_known_short_never_fetches(self):
        with patch('news_officer.youtube.run') as run, self.assertRaises(YouTubeFinalSkip):
            YouTubeTranscriptProvider().fetch(replace(self.episode, duration_seconds=100))
        run.assert_not_called()

    def test_retry_cap_for_all_source_types_survives_restart(self):
        for index, url in enumerate([self.episode.url, 'https://example.org/podcast', 'https://example.org/article']):
            episode = replace(self.episode, id=str(index), url=url)
            self.assertFalse(self.store.defer_daily_transcript(episode, 'missing'))
            self.assertFalse(self.store.defer_daily_transcript(episode, 'missing'))
            self.assertTrue(self.store.defer_daily_transcript(episode, 'missing'))
            reopened = Store(self.store.path)
            self.assertIsNone(reopened.episode_review_state(episode.id))
            self.assertEqual(reopened.due_daily_transcripts(), [])

    def test_rss_only_disabled_sources_and_text_off_are_silent(self):
        self.feeds.write_text(json.dumps({'sources': [
            {'name': 'No RSS', 'type': 'youtube', 'url': 'https://www.youtube.com/@show'},
            {'name': 'Text', 'type': 'text', 'rss_url': 'https://example.org/rss'},
        ]}))
        service = self.service(Mock())
        with patch.dict('os.environ', {'NEWS_OFFICER_TEXT_SOURCES': 'false'}), \
                patch('news_officer.podcast.latest_videos') as videos, \
                patch('news_officer.podcast.discover_articles') as articles:
            self.assertEqual(service.discover_daily_candidates(), [])
        self.assertEqual(service._last_failed_feeds, ())
        videos.assert_not_called()
        articles.assert_not_called()

    def test_api_captions_are_verified_without_subtitle_download(self):
        snippets = [SimpleNamespace(text=' '.join(f'word{i}_{j}' for j in range(17)),
                                   start=i * 10, duration=10) for i in range(120)]
        class Captions(list):
            language_code = 'en'
        with patch('youtube_transcript_api.YouTubeTranscriptApi') as api, \
                patch('news_officer.youtube.run') as run:
            api.return_value.fetch.return_value = Captions(snippets)
            transcript = YouTubeTranscriptProvider().fetch(self.episode)
        self.assertTrue(transcript.verified_complete)
        self.assertGreater(len(transcript.text), 5000)
        api.return_value.fetch.assert_called_once()
        run.assert_not_called()

    def test_api_short_duration_is_final_without_caption_fallback(self):
        episode = replace(self.episode, duration_seconds=None)
        with patch('news_officer.youtube.video_metadata', return_value=replace(episode, duration_seconds=899)), \
                patch('news_officer.youtube.run') as run, self.assertRaises(YouTubeFinalSkip):
            YouTubeTranscriptProvider().fetch(episode)
        run.assert_not_called()

    def test_three_day_retry_cap(self):
        self.store.defer_daily_transcript(self.episode, 'missing')
        with self.store._connect() as db:
            db.execute('UPDATE daily_transcript_backlog SET created_at=?',
                       ((datetime.now(UTC) - timedelta(days=3)).isoformat(),))
        self.assertTrue(self.store.defer_daily_transcript(self.episode, 'missing'))
        self.assertIsNone(self.store.episode_review_state(self.episode.id))

    def test_no_rss_sources_are_excluded_from_health_ratio(self):
        sources = json.loads(self.feeds.read_text())['sources']
        sources.extend([{'name': 'Offline', 'type': 'rss', 'rss_url': 'https://example.org/offline'},
                        {'name': 'No RSS', 'type': 'youtube', 'url': 'https://www.youtube.com/@show'}])
        self.feeds.write_text(json.dumps({'sources': sources}))
        service = self.service(Mock())
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=[[self.episode], TimeoutError('offline')]), \
                self.assertRaisesRegex(FeedDiscoveryError, '1 of 2 source'):
            service.discover_daily_candidates()

    def test_all_configured_youtube_feeds_have_exact_atom_url(self):
        feeds = json.loads((Path(__file__).resolve().parents[1] / 'feeds.json').read_text())
        for source in feeds['sources']:
            if source['type'] == 'youtube':
                channel_id = source['url'].split('/channel/')[1].split('/')[0]
                self.assertEqual(source['rss_url'], f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}')
