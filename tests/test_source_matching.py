import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from news_officer.models import Episode
from news_officer.podcast import (
    PodcastService,
    YouTubeFeedSource,
    _configured_podwise_source,
    _source_name_key,
    load_youtube_sources,
)
from news_officer.podwise_discovery import PodwiseDiscovery
from news_officer.source_matching import normalize_rss_url
from news_officer.store import Store


class SourceMatchingTests(unittest.TestCase):
    def test_feed_normalization(self):
        tracking = ('utm_source', 'UTM_campaign', 'fbclid', 'gclid', 'mc_cid', 'mc_eid',
                    'igshid', 'ref', 'ref_src', 'si', '_hsenc', '_hsmi')
        for port in ('80', '443'):
            with self.subTest(port=port):
                url = f'http://EXAMPLE.org:{port}/feed/?z=2&a=3&a=1&empty='
                url += ''.join(f'&{key}=tracking' for key in tracking)
                self.assertEqual(normalize_rss_url(url),
                                 'https://example.org/feed?a=1&a=3&empty=&z=2')
        self.assertEqual(normalize_rss_url('https://example.org:8080/feed/'),
                         'https://example.org:8080/feed')
        self.assertNotEqual(normalize_rss_url('https://example.org/feed?id=1'),
                            normalize_rss_url('https://example.org/feed?id=2'))
        for url in ('', 'bad', 'ftp://example.org/feed', 'https://example.org:bad/feed'):
            self.assertEqual(normalize_rss_url(url), '')

    def test_name_normalization(self):
        for name in (' The LATENT Space! 🎙️ ', 'latent   space', 'Latent—Space'):
            self.assertEqual(_source_name_key(name), 'latent space')

    def test_malformed_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feeds.json'
            for value in (None, 'alias', 4, {'alias': 1}, [None, 4, '', '  ', ' alias ']):
                with self.subTest(value=value):
                    path.write_text(json.dumps({'sources': [
                        {'name': 'Show', 'type': 'rss', 'rss_url': 'https://example.org/feed',
                         'aliases': value}]}))
                    expected = ('alias',) if isinstance(value, list) else ()
                    self.assertEqual(load_youtube_sources(path)[0].aliases, expected)

    def test_real_http_fixtures_through_daily_discovery(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'podwise'
        samples = {p.stem: json.loads(p.read_text()) for p in fixtures.glob('*.json')}
        catalog_responses = {
            11659: samples['podcast_episodes'],
            653: {'success': True, 'result': [
                dict(samples['podcast_episodes']['result'][0], seq=99999999,
                     podcastName='Latent Space: The AI Engineer Podcast',
                     link='https://www.latent.space/p/fixture-catalog-653')
            ]},
        }
        now = datetime(2026, 10, 16, tzinfo=UTC)
        for mode in ('name', 'alias', 'id', 'unmatched'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = Store(root / 'state.sqlite3')
                store.initialize()
                source = {'name': 'The Latent Space 🎙️' if mode == 'name' else 'Configured Show',
                          'type': 'youtube', 'url': 'https://www.youtube.com/@configured',
                          'priority': 'A'}
                if mode == 'alias':
                    source['aliases'] = ['Latent Space', 'Latent Space: The AI Engineer Podcast']
                if mode == 'id':
                    source['podwise_podcast_ids'] = [r['podcastId'] for r in samples['podcasts_search']['result']]
                path = root / 'feeds.json'
                path.write_text(json.dumps({'sources': [source]}))
                service = PodcastService(store, path, Mock(), Mock(), daily_rss_only=True,
                                         editorial_policy=Mock(),
                                         podwise_api_token='fixture-token', podwise_discovery_enabled=True,
                                         lookback_hours=24 * 365)
                service.discovery.topics = ('fixture',)
                service.discovery.podcast_topics = ('fixture',)

                def get(url, **kwargs):
                    endpoint = url.split('/api/v1')[-1]
                    if endpoint.endswith('/episodes/popular'):
                        data = {'success': True, 'result': []}
                    elif endpoint.endswith('/episodes/search'):
                        data = samples['episodes_search']
                    elif endpoint.endswith('/podcasts/search'):
                        data = samples['podcasts_search']
                    elif '/podcasts/' in endpoint and endpoint.endswith('/episodes'):
                        data = catalog_responses[int(endpoint.split('/')[-2])]
                    else:
                        self.fail(url)
                    response = Mock(status_code=200)
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    response.iter_content.return_value = [json.dumps(data).encode()]
                    return response

                with patch('news_officer.podwise.requests.get', side_effect=get):
                    episodes = service.discover_daily_candidates(now)
                self.assertTrue(episodes)
                self.assertTrue(any('youtube.com/watch' in e.url for e in episodes))
                for episode in episodes:
                    expected = mode in {'alias', 'id'} or (mode == 'name' and episode.show == 'Latent Space')
                    self.assertEqual(episode.metadata.get('source_name'), source['name'] if expected else None)
                    self.assertEqual(episode.metadata['source_priority'], 'A' if expected else 'B')
                    self.assertFalse(episode.metadata.get('rss_feed_url'))

    def test_similar_names_are_not_matches(self):
        for configured, discovered in (
            ('Latent Space', 'Latent Space TV (see @LatentSpacePod for Pod)'),
            ('Generalist AI', 'The Generalist'),
        ):
            with self.subTest(discovered=discovered):
                episode = Episode('podwise:1', 'Interview', 'https://example.org/1', discovered)
                self.assertIsNone(_configured_podwise_source(
                    episode, [YouTubeFeedSource('', name=configured)]))

    def test_collisions_warn_and_require_id_disambiguation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feeds.json'
            path.write_text(json.dumps({'sources': [
                {'name': 'The Shared Show!', 'aliases': ['Shared Show'], 'type': 'rss',
                 'rss_url': 'https://example.org/one'},
                {'name': 'Other Show', 'aliases': ['ＳＨＡＲＥＤ Show'], 'type': 'rss',
                 'rss_url': 'https://example.org/two', 'podwise_podcast_ids': [653]},
            ]}))
            with self.assertLogs('news_officer.podcast', level='WARNING') as logs:
                sources = load_youtube_sources(path)
            self.assertEqual(len(logs.output), 1)
            for text in ('shared show', 'The Shared Show!', 'Other Show'):
                self.assertIn(text, logs.output[0])
            episode = Episode('podwise:1', 'Interview', 'https://example.org/1', 'Shared Show')
            self.assertIsNone(_configured_podwise_source(episode, sources))
            self.assertIs(_configured_podwise_source(replace(
                episode, metadata={'podwise_podcast_seq': 653}), sources), sources[1])

    def test_id_precedes_alias(self):
        episode = Episode('podwise:1', 'Interview', 'https://example.org/1', 'Alias')
        for configured_id in ('a' * 64, '653', 653):
            with self.subTest(configured_id=configured_id):
                sources = [YouTubeFeedSource('', name='Alias owner', aliases=('Alias',)),
                           YouTubeFeedSource('', name='ID owner',
                                             podwise_podcast_ids=(configured_id,))]
                key = 'podwise_podcast_id' if configured_id == 'a' * 64 else 'podwise_podcast_seq'
                self.assertIs(_configured_podwise_source(replace(
                    episode, metadata={key: configured_id}), sources), sources[1])

    def test_catalog_parent_ids_resolve_seq_only_episode_rows(self):
        now = datetime(2026, 10, 16, tzinfo=UTC)
        discovery = PodwiseDiscovery('fake', topics=(), podcast_topics=('fixture',))
        catalogs = [{'seq': 653, 'podcastId': 'a' * 64, 'name': 'Catalog One'},
                    {'seq': 11659, 'podcastId': 'b' * 64, 'name': 'Catalog Two'}]
        responses = {
            '/episodes/popular': {'result': []},
            '/podcasts/search': {'result': catalogs},
        }
        for catalog in catalogs:
            seq = catalog['seq']
            responses[f'/podcasts/{seq}/episodes'] = {'result': [
                {'seq': seq + 100000, 'title': f'Interview {seq}',
                 'link': f'https://example.org/{seq}',
                 'publishTime': (now - timedelta(hours=1)).timestamp()}]}
        discovery.api._get = lambda path, params: responses[path]
        episodes = discovery.discover(now, 24).episodes
        self.assertEqual(len(episodes), 2)
        for episode in episodes:
            catalog = next(c for c in catalogs if c['name'] == episode.show)
            self.assertEqual(episode.metadata['podwise_podcast_id'], catalog['podcastId'])
            self.assertEqual(episode.metadata['podwise_podcast_seq'], catalog['seq'])
            for identifier in (catalog['podcastId'], catalog['seq'], str(catalog['seq'])):
                source = YouTubeFeedSource('', name='Different name',
                                           podwise_podcast_ids=(identifier,))
                self.assertIs(_configured_podwise_source(episode, [source]), source)
