"""Offline article fixtures; compatible with pytest and CI unittest discovery."""
import json
import os
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from news_officer.feishu import _post_elements
from news_officer.podcast import (
    FeedDiscoveryError,
    PodcastService,
    TranscriptResolver,
    load_youtube_sources,
)
from news_officer.store import Store
from news_officer.text_sources import (
    article_links,
    discover_articles,
    extract_article,
    load_text_sources,
)

ARTICLE = '<html><nav>noise</nav><article><h1>AI systems</h1><time datetime="2026-10-13T00:00:00Z"></time><p>' + ('Evidence and detailed analysis. ' * 100) + '</p><script>noise</script></article></html>'
RSS = '<rss><channel><item><title>AI systems</title><link>https://example.org/post/ai</link><pubDate>Tue, 13 Oct 2026 00:00:00 GMT</pubDate><description>Only an excerpt</description></item></channel></rss>'
ATOM = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI systems</title><link href="https://example.org/post/ai"/><published>2026-10-13T00:00:00Z</published><summary>Excerpt</summary></entry></feed>'
INDEX = '<a href="/post/ai">AI</a><a href="/post/ai#intro">Again</a><a href="/about">About</a><a href="https://evil.org/post/ai">Outside</a>'


class TextSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'feeds.json'
        self.source = {'name': 'Lilian Weng', 'type': 'text', 'mode': 'rss',
                       'rss_url': 'https://example.org/feed'}
        self.path.write_text(json.dumps({'sources': [self.source]}))
        self.flag = patch.dict(os.environ, {'NEWS_OFFICER_TEXT_SOURCES': 'true'})
        self.flag.start()
        self.addCleanup(self.flag.stop)

    def test_rss_without_audio_and_atom_fetch_full_page(self):
        for feed in (RSS, ATOM):
            with self.subTest(feed=feed), patch('news_officer.text_sources._fetch', side_effect=[feed, ARTICLE]):
                [(episode, transcript)] = discover_articles(self.source)
                self.assertEqual(episode.show, 'Lilian Weng')
                self.assertEqual(episode.published_at, datetime(2026, 10, 13, tzinfo=UTC))
                self.assertIn('Evidence and detailed analysis.', transcript.text)
                self.assertNotIn('Excerpt', transcript.text)
                self.assertTrue(transcript.verified_complete)

    def test_html_index_dedup_and_main_text(self):
        source = {'name': 'Author', 'mode': 'html', 'index_url': 'https://example.org/',
                  'link_regex': '^/post/'}
        self.assertEqual(article_links(INDEX, source), ['https://example.org/post/ai'])
        with patch('news_officer.text_sources._fetch', side_effect=[INDEX, ARTICLE]) as fetch:
            [(episode, transcript)] = discover_articles(source)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(episode.title, 'AI systems')
        self.assertNotIn('noise', transcript.text)
        self.assertEqual(extract_article('<nav>Navigation only</nav>')[1], '')

    def test_paywall_and_truncation_skipped(self):
        for body in ('short', 'Teaser\nSubscribe to Stratechery Plus for full access. ' + 'text ' * 500,
                     'Subscribe to read ' + 'text ' * 500,
                     'Stratechery Plus ' + 'text ' * 500, 'This is a paid ' + 'text ' * 500):
            with patch('news_officer.text_sources._fetch', side_effect=[RSS, f'<article>{body}</article>']):
                self.assertEqual(discover_articles(dict(self.source, free_only=True)), [])

    def test_title_precedence_suffixes_and_inline_words(self):
        fixtures = [
            (('<meta property="og:title" content="Dario Amodei — Machines of Loving Grace">'
             '<header><h1>Dario Amodei</h1></header><article><h1>Machines of Loving Grace</h1>'
             '<p>Real essay</p></article>'), 'Machines of Loving Grace'),
            (('<meta property="og:title" content="Memory — A Roadmap">'
             '<article><h1>Memory — A Roadmap</h1></article>'), 'Memory — A Roadmap'),
            ('<title>Essay | Publisher</title><main><p>Body</p></main>', 'Essay'),
            ('<article><h1>How to Get B<span>etter</span> at AI</h1><p>Body</p></article>',
             'How to Get Better at AI'),
        ]
        for page, expected in fixtures:
            self.assertEqual(extract_article(page)[0], expected)
        page = ('<main><nav>noise</nav><article><p>B<span>etter</span> AI <a>tools</a>.</p>'
                '<p>Next paragraph</p><script>noise</script><style>noise</style>'
                '<footer>noise</footer><header>noise</header></article></main>')
        self.assertEqual(extract_article(page)[1], 'Better AI tools.\nNext paragraph')

    def test_nfx_and_chemistry_body_containers(self):
        for css in ('post_post__Hm4aF', 'text-rich-text w-richtext'):
            page = f'<title>Essay</title><nav>noise</nav><div class="{css}"><p>Full body</p></div>'
            self.assertEqual(extract_article(page)[1], 'Full body')

    def test_shunyu_atom_http_permalink_upgrades_to_https(self):
        feed = ATOM.replace('https://example.org/post/ai', 'http://example.org/post/ai')
        with patch('news_officer.text_sources._fetch', side_effect=[feed, ARTICLE]) as fetch:
            self.assertEqual(len(discover_articles(self.source)), 1)
            self.assertEqual(fetch.call_args.args[0], 'https://example.org/post/ai')
        external = feed.replace('http://example.org/post/ai', 'http://outside.org/post/ai')
        with patch('news_officer.text_sources._fetch', return_value=external) as fetch:
            self.assertEqual(discover_articles(self.source), [])
            self.assertEqual(fetch.call_count, 1)

    def test_free_full_content_with_subscription_mentions_and_paid_teaser(self):
        body = 'Complete public analysis. ' * 100 + 'Our paid subscribers support this work.'
        for paywall, count in [('', 1), ('<div class="paywall">Subscribe</div>', 0)]:
            with patch('news_officer.text_sources._fetch', side_effect=[RSS,
                       f'<article><p>{body}</p>{paywall}</article>']):
                self.assertEqual(len(discover_articles(dict(self.source, free_only=True))), count)

    def test_default_and_configured_caps_and_source_failure(self):
        items = ''.join(f'<item><title>Essay {i}</title><link>https://example.org/post/{i}</link>'
                        f'<pubDate>2026-10-{i + 1:02d}T00:00:00Z</pubDate></item>'
                        for i in range(12))
        feed = f'<rss><channel>{items}</channel></rss>'
        for limit in (None, 2):
            source = dict(self.source, **({'max_articles': limit} if limit else {}))
            with patch('news_officer.text_sources._fetch', side_effect=lambda url:
                       feed if url.endswith('/feed') else ARTICLE) as fetch:
                results = discover_articles(source)
            self.assertEqual(len(results), limit or 10)
            self.assertEqual(results[0][0].title, 'Essay 11')
            self.assertEqual(fetch.call_count, len(results) + 1)
        for failure in (TimeoutError(), '<invalid'):
            with patch('news_officer.text_sources._fetch', side_effect=[failure]):
                self.assertEqual(discover_articles(self.source), [])

    def test_flag_off_never_loads_or_fetches(self):
        store = Store(Path(self.temp.name) / 'state.db')
        store.initialize()
        for value in ('', 'false', '0'):
            with patch.dict(os.environ, {'NEWS_OFFICER_TEXT_SOURCES': value}), \
                    patch('news_officer.podcast.discover_articles') as fetch:
                self.assertEqual(load_text_sources(self.path), [])
                self.assertEqual(load_youtube_sources(self.path), [])
                self.assertEqual(PodcastService(store, self.path, Mock()).build_daily(), [])
                fetch.assert_not_called()
        with patch.dict(os.environ, {'NEWS_OFFICER_TEXT_SOURCES': ''}):
            self.assertEqual(load_text_sources(Path('/missing')), [])

    def test_full_text_persisted_before_scoring_and_summary_and_daily_title(self):
        store = Store(Path(self.temp.name) / 'state.db')
        store.initialize()
        def assess(episode, transcript):
            self.assertEqual(store.get_verified_transcript(episode.id).transcript.text, transcript.text)
            return {'selected': True, 'stars': 4}
        def summarize(episode, transcript):
            self.assertEqual(store.get_verified_transcript(episode.id).transcript.text, transcript.text)
            return '## Model title\n节目：Lilian Weng｜AI systems\nSummary'
        editorial = Mock(assess=Mock(side_effect=assess))
        editorial.apply.side_effect = lambda summary, decision: summary
        service = PodcastService(store, self.path, Mock(summarize=Mock(side_effect=summarize)),
                                 editorial_policy=editorial, daily_rss_only=True)
        with patch('news_officer.text_sources._fetch', side_effect=[RSS, ARTICLE]):
            [item] = service.build_daily(datetime(2026, 10, 13, 1, tzinfo=UTC))
        self.assertEqual(item.status, 'summarized')
        self.assertEqual(item.episode.metadata['source_name'], self.source['name'])
        self.assertEqual(store.get_verified_transcript(item.episode.id).episode.metadata['source_type'], 'text')
        self.assertEqual(_post_elements(item.message.splitlines()[0])[0]['text'], '【Lilian Weng｜AI systems】')
        with patch.dict(os.environ, {'NEWS_OFFICER_TEXT_SOURCES': 'false'}):
            self.assertEqual(service.build_daily(pending=[item.episode]), [])

    def test_source_and_article_failures_are_isolated(self):
        bad = dict(self.source, name='Broken', rss_url='https://broken.org/feed')
        self.path.write_text(json.dumps({'sources': [bad, self.source, dict(self.source, name='Healthy')]}))
        store = Store(Path(self.temp.name) / 'state.db')
        store.initialize()
        with patch('news_officer.text_sources._fetch', side_effect=[TimeoutError(), RSS, ARTICLE, RSS, ARTICLE]):
            candidates = PodcastService(store, self.path, Mock()).discover_daily_candidates(
                datetime(2026, 10, 13, 1, tzinfo=UTC))
        self.assertEqual(len(candidates), 1)
        with patch('news_officer.text_sources._fetch', side_effect=[RSS, TimeoutError()]):
            self.assertEqual(discover_articles(self.source), [])

    def test_failed_text_sources_do_not_mask_podcast_health_alert(self):
        from news_officer.models import Episode
        podcast = {'name': 'Podcast', 'type': 'rss', 'rss_url': 'https://example.org/podcast'}
        healthy = Episode('rss:healthy', 'Interview', 'https://example.org/interview',
                          'Podcast', published_at=datetime(2026, 10, 13, tzinfo=UTC))
        self.path.write_text(json.dumps({'sources': [podcast, self.source]}))
        store = Store(Path(self.temp.name) / 'health.db')
        store.initialize()
        service = PodcastService(store, self.path, Mock(), daily_rss_only=True)
        for failure in (TimeoutError(), '<invalid'):
            with self.subTest(failure=failure), \
                    patch('news_officer.podcast.latest_rss_episodes', return_value=[healthy]), \
                    patch('news_officer.text_sources._fetch', side_effect=[failure]):
                with self.assertRaisesRegex(FeedDiscoveryError, '1 of 2'):
                    service.discover_daily_candidates(datetime(2026, 10, 13, 1, tzinfo=UTC))
                self.assertEqual(service._last_failed_feeds, ('Lilian Weng',))
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[healthy]), \
                patch('news_officer.text_sources._fetch', return_value='<rss><channel/></rss>'):
            self.assertEqual(len(service.discover_daily_candidates(
                datetime(2026, 10, 13, 1, tzinfo=UTC))), 1)
            self.assertEqual(service._last_failed_feeds, ())
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[healthy]), \
                patch('news_officer.text_sources._fetch', side_effect=[RSS, TimeoutError()]), \
                self.assertRaises(FeedDiscoveryError):
            service.discover_daily_candidates(datetime(2026, 10, 13, 1, tzinfo=UTC))

    def test_failed_podcast_and_text_sources_trigger_alert_with_healthy_article(self):
        podcast = {'name': 'Podcast', 'type': 'rss', 'rss_url': 'https://example.org/podcast'}
        bad = dict(self.source, name='Broken', rss_url='https://broken.org/feed')
        self.path.write_text(json.dumps({'sources': [podcast, bad, self.source]}))
        store = Store(Path(self.temp.name) / 'mixed-health.db')
        store.initialize()
        service = PodcastService(store, self.path, Mock(), daily_rss_only=True)
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=TimeoutError()), \
                patch('news_officer.text_sources._fetch', side_effect=[TimeoutError(), RSS, ARTICLE]):
            with self.assertRaisesRegex(FeedDiscoveryError, '2 of 3'):
                service.discover_daily_candidates(datetime(2026, 10, 13, 1, tzinfo=UTC))
            self.assertEqual(service._last_failed_feeds, ('Podcast', 'Broken'))

    def test_undated_first_seen_survives_restart_and_dated_behavior_is_unchanged(self):
        feed = RSS.replace('<pubDate>Tue, 13 Oct 2026 00:00:00 GMT</pubDate>', '')
        page = ARTICLE.replace('<time datetime="2026-10-13T00:00:00Z"></time>', '')
        db_path = Path(self.temp.name) / 'seen.db'
        now = datetime(2026, 10, 13, 1, tzinfo=UTC)
        for day, expected in ((13, 1), (13, 1), (14, 0)):
            store = Store(db_path)
            store.initialize()
            service = PodcastService(store, self.path, Mock())
            with patch('news_officer.text_sources._fetch', side_effect=[feed, page]):
                self.assertEqual(len(service.discover_daily_candidates(now.replace(day=day))), expected)
            self.assertEqual(store.article_first_seen('https://example.org/post/ai', now.replace(day=day)), now)
        # A newly available publication date follows the existing lookback rules.
        with patch('news_officer.text_sources._fetch', side_effect=[RSS, ARTICLE]):
            self.assertEqual(len(service.discover_daily_candidates(now.replace(day=14))), 1)

    def test_every_paid_preview_is_skipped(self):
        source = next(s for s in load_text_sources(
            Path(__file__).resolve().parents[1] / 'feeds.json') if s['name'] == 'Every')
        index = '<a href="/p/essay">Essay</a>'
        page = '<article><h1>Essay</h1><p>Public preview</p></article>'
        with patch('news_officer.text_sources._fetch', side_effect=[index, page]):
            self.assertEqual(discover_articles(source), [])

    def test_configured_sources_and_first_round_exclusions(self):
        sources = load_text_sources(Path(__file__).resolve().parents[1] / 'feeds.json')
        self.assertEqual(len(sources), 12)
        self.assertEqual({s['name'] for s in sources if s.get('free_only')},
                         {'Every', 'SemiAnalysis', 'Stratechery', 'Latent Space (text)'})
        first = next(s for s in sources if s['name'] == 'First Round')
        page = ('<a href="/articles/ai/">Category</a><a href="/glossary/ai/">Glossary</a>'
                '<a href="/collections/">Collection</a><a href="/series/pmf/">Series</a>'
                '<a href="/podcast/">Podcast</a><a href="/signin/">Login</a>'
                '<a href="https://review.firstround.com/building-ai-products/">Article</a>')
        self.assertEqual(article_links(page, first),
                         ['https://review.firstround.com/building-ai-products/'])

    def test_madrona_article_paths_and_listing_exclusions(self):
        configured = json.loads((Path(__file__).resolve().parents[1] / 'feeds.json').read_text())
        source = next(s for s in configured['sources'] if s['name'] == 'Madrona (text)')
        # Article slugs observed on the live homepage; trailing slash is optional.
        slugs = ('why-ai-made-your-buyers-harder-to-sell-to',
                 'is-ai-a-bubble-what-pitchbook-nizar-tarhuni-sees-in-the-data',
                 'the-best-infrastructure-moment-since-cloud')
        for slug in slugs:
            for suffix in ('', '/', '/?utm_source=test#intro'):
                path = '/' + slug + suffix
                with self.subTest(path=path):
                    self.assertEqual(article_links(f'<a href="{path}">Article</a>', source),
                                     [source['index_url'] + slug + suffix.split('?')[0]])
        for route in ('about', 'team', 'companies', 'insights', 'contact', 'search',
                      'privacy-policy', 'jobs', 'news', 'careers', 'index', 'tag',
                      'category', 'author', 'page'):
            for suffix in ('', '/', '/x', '/2/', '/?utm_source=test#intro'):
                path = '/' + route + suffix
                with self.subTest(path=path):
                    self.assertIsNotNone(re.search(source['exclude_regex'], path.split('?')[0]))
                    self.assertEqual(article_links(f'<a href="{path}">Listing</a>', source), [])
        for path in ('/', '/article/nested/', '/logo.png', '/feed.xml', '/123.html',
                     'https://outside.org/the-best-infrastructure-moment-since-cloud/',
                     'https://jobs.madrona.com/the-best-infrastructure-moment-since-cloud/'):
            with self.subTest(path=path):
                self.assertEqual(article_links(f'<a href="{path}">Other</a>', source), [])
        # Exclusions match whole route segments, not article slug prefixes.
        self.assertEqual(article_links('<a href="/news-from-madrona/">Article</a>', source),
                         ['https://www.madrona.com/news-from-madrona/'])

    def test_shared_text_sources_are_gated_and_discoverable(self):
        path = Path(__file__).resolve().parents[1] / 'feeds.json'
        configured = json.loads(path.read_text())['sources']
        andrew = next(s for s in configured if s['name'] == 'Andrew Ng')
        self.assertFalse(andrew['enabled'])
        self.assertTrue(andrew['disabled_reason'])
        with patch.dict(os.environ, {'NEWS_OFFICER_TEXT_SOURCES': 'false'}), \
                patch('pathlib.Path.read_text') as read:
            self.assertEqual(load_text_sources(path), [])
            read.assert_not_called()
        sources = {s['name']: s for s in load_text_sources(path)}
        self.assertNotIn('Andrew Ng', sources)
        latent = sources['Latent Space (text)']
        self.assertEqual(latent['rss_url'], 'https://www.latent.space/feed')
        madrona = sources['Madrona (text)']
        index = ('<a href="/why-ai-made-your-buyers-harder-to-sell-to/">Essay</a>'
                 '<a href="/team/">Team</a><a href="/insights/">Index</a>'
                 '<a href="/companies/ai/">Companies</a>'
                 '<a href="https://outside.org/essay/">Outside</a>')
        links = article_links(index, madrona)
        self.assertEqual(links, ['https://www.madrona.com/why-ai-made-your-buyers-harder-to-sell-to/'])
        for source, listing in ((latent, RSS), (madrona, index)):
            with self.subTest(source=source['name']), \
                    patch('news_officer.text_sources._fetch', side_effect=[listing, ARTICLE]):
                [(episode, transcript)] = discover_articles(source, raise_on_error=True)
                self.assertEqual(episode.show, source['name'])
                self.assertTrue(transcript.verified_complete)

    def test_flag_off_matches_legacy_daily_bytes(self):
        podcast = {'name': 'Podcast', 'type': 'rss', 'rss_url': 'https://example.org/podcast'}
        from news_officer.models import Episode, Transcript
        episode = Episode('rss:podcast', 'AI interview', 'https://example.org/interview',
                          'Podcast', published_at=datetime(2026, 10, 13, tzinfo=UTC))
        outputs = []
        with patch.dict(os.environ, {'NEWS_OFFICER_TEXT_SOURCES': 'false'}), \
                patch('news_officer.podcast.latest_rss_episodes', return_value=[episode]), \
                patch('news_officer.podcast.discover_articles') as article_fetch:
            for index, sources in enumerate(([podcast], [podcast, self.source])):
                self.path.write_text(json.dumps({'sources': sources}))
                store = Store(Path(self.temp.name) / f'legacy-{index}.db')
                store.initialize()
                service = PodcastService(store, self.path, Mock(summarize=Mock(return_value='## Podcast｜AI\nSummary')),
                                         daily_rss_only=True,
                                         transcript_resolver=TranscriptResolver([Mock(
                                             supports_url=Mock(return_value=False),
                                             fetch=Mock(return_value=Transcript(
                                                 'Full interview', 'publisher', episode.url, True)))]))
                items = service.build_daily(datetime(2026, 10, 13, 1, tzinfo=UTC))
                outputs.append(json.dumps([item.to_persisted_dict() for item in items],
                                          sort_keys=True).encode())
            self.assertEqual(outputs[0], outputs[1])
            article_fetch.assert_not_called()
