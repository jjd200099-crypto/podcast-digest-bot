import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from news_officer.daily_report import coverage_report, render_daily_summary
from news_officer.models import Transcript
from news_officer.podcast import PodcastService
from news_officer.podwise_discovery import DiscoveryResult, PodwiseDiscovery
from news_officer.store import Store

NOW = datetime(2026, 9, 23, 0, 30, tzinfo=UTC)


def row(seq=1, **changes):
    return {'seq': seq, 'title': 'A substantive new model research interview',
            'podcastName': 'Previously Unknown Show', 'link': f'https://example.org/audio/{seq}',
            'duration': 1200, 'publishTime': (NOW - timedelta(hours=2)).timestamp(),
            'transcribed': True, **changes}


class DiscoveryTests(unittest.TestCase):
    def discovery(self, **kwargs):
        return PodwiseDiscovery('not-a-real-token', topics=kwargs.pop('topics', ()),
                                 podcast_topics=kwargs.pop('podcast_topics', ()), **kwargs)

    def test_popular_catalog_finds_new_episodes_outside_any_followed_list(self):
        d = self.discovery()
        calls = []

        def get(path, params):
            calls.append((path, params))
            if path == '/episodes/popular':
                return {'result': [{'seq': 99, 'podcastSeq': 7}]}
            if path == '/podcasts/7/episodes':
                return {'result': [row(1), row(99, publishTime=(NOW - timedelta(days=9)).timestamp())]}
            self.fail(path)

        d.api._get = get
        result = d.discover(NOW, 24)
        self.assertEqual([e.id for e in result.episodes], ['podwise:1'])
        self.assertEqual(result.episodes[0].published_at, NOW - timedelta(hours=2))
        self.assertEqual(result.episodes[0].metadata['discovery_origin'], 'podwise')
        self.assertEqual(calls[1][1], {'date': '2026-09-24', 'days': 3})
        self.assertIn('不自动订阅', result.notice)

    def test_search_paginates_even_when_first_page_is_old(self):
        d = self.discovery(topics=('OpenAI',), pages=2)
        old = [row(i + 10, publishTime=(NOW - timedelta(days=30)).timestamp()) for i in range(30)]
        d.api._get = Mock(side_effect=[{'result': []},
            {'result': old, 'estimatedTotalHits': 31}, {'result': [row()], 'estimatedTotalHits': 31}])
        result = d.discover(NOW, 24)
        self.assertEqual(len(result.episodes), 1)
        self.assertEqual(d.api._get.call_args_list[2].args[1]['page'], 1)

    def test_topic_show_search_reads_recent_catalog_without_following(self):
        d = self.discovery(podcast_topics=('AI research',))
        d.api._get = Mock(side_effect=[{'result': []}, {'result': [{'seq': 23}]}, {'result': [row()]}])
        result = d.discover(NOW, 24)
        self.assertEqual(len(result.episodes), 1)
        self.assertEqual(d.api._get.call_args_list[-1].args[0], '/podcasts/23/episodes')

    def test_popular_without_show_uses_info_not_discovery_time(self):
        d = self.discovery()
        d.api._get = Mock(side_effect=[{'result': [{'seq': 1}]}, {'result': row()}])
        self.assertEqual(len(d.discover(NOW, 24).episodes), 1)
        self.assertEqual(d.api._get.call_args_list[-1].args[0], '/episodes/1')

    def test_bad_dates_and_urls_are_not_invented_and_duplicates_collapse(self):
        d = self.discovery(topics=('research',))
        entries = [row(), row(), row(2, publishTime=None), row(3, publishTime=True),
                   row(4, publishTime=float('nan')), row(5, publishTime=NOW.timestamp() + 1),
                   row(6, link='javascript:alert(1)'), row(7, title=[])]
        d.api._get = Mock(side_effect=[{'result': []}, {'result': entries}])
        result = d.discover(NOW, 24)
        self.assertEqual([e.id for e in result.episodes], ['podwise:1'])
        self.assertIn('缺少有效日期或元数据', result.notice)

    def test_partial_outage_and_scan_cap_are_explicit(self):
        d = self.discovery(topics=('model',), pages=1, candidate_limit=2)
        d.api._get = Mock(side_effect=[RuntimeError('must not leak secret'),
                         {'result': [row(i + 1) for i in range(30)], 'estimatedTotalHits': 1000}])
        result = d.discover(NOW, 24)
        self.assertEqual(len(result.episodes), 2)
        self.assertIn('发现不完整', result.notice)
        self.assertIn('扫描上限', result.notice)
        self.assertNotIn('secret', result.notice)


class DiscoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.store.initialize()
        self.feeds = self.root / 'feeds.json'
        self.feeds.write_text(json.dumps({'sources': []}))
        self.episode = PodwiseDiscovery._episode(row(), NOW, NOW - timedelta(days=1))[0]
        self.resolver = Mock()
        self.resolver.providers = []
        self.resolver.fetch.return_value = Transcript('Verified complete interview content.', 'official',
                                                     self.episode.url, True)
        self.summarizer = Mock()
        self.summarizer.summarize.return_value = '推荐理由：有具体的模型研究方法。\n1. 具体观点。\n推荐星级：★★★★☆'
        self.policy = Mock()
        self.policy.assess.return_value = {'selected': True}
        self.policy.apply.side_effect = lambda summary, _: summary

    def service(self):
        s = PodcastService(self.store, self.feeds, self.summarizer, self.resolver,
                           daily_rss_only=True, editorial_policy=self.policy,
                           podwise_api_token='fake', podwise_discovery_enabled=True, lookback_hours=24)
        s.daily_transcript_resolver = self.resolver
        s.discovery = Mock()
        s.discovery.discover.return_value = DiscoveryResult([self.episode], '探索状态')
        return s

    def test_new_source_enters_normal_archive_summary_and_no_source_subscription(self):
        original = self.feeds.read_text()
        s = self.service()
        items = s.build_daily(NOW)
        self.assertEqual([i.status for i in items], ['summarized', 'discovery_status'])
        self.assertIsNotNone(self.store.get_verified_transcript(self.episode.id))
        self.assertEqual(self.feeds.read_text(), original)
        self.assertEqual(coverage_report(items), '探索状态')
        view = render_daily_summary(items[0].message, discovered=True)
        self.assertIn('Podwise 扩展发现', view)
        self.assertIn('值得看全文', view)
        self.assertNotIn('★', view)

    def test_low_value_exploration_is_archived_but_not_summarized(self):
        self.policy.assess.return_value = {'selected': False}
        items = self.service().build_daily(NOW)
        self.assertIn('discovery_filtered', [i.status for i in items])
        self.summarizer.summarize.assert_not_called()
        self.assertIsNotNone(self.store.get_verified_transcript(self.episode.id))
        self.assertIn('未达到推荐门槛', coverage_report(items))
        self.assertEqual(self.service().discover_daily_candidates(NOW), [])

    def test_missing_fulltext_stays_pending_with_discovery_identity(self):
        self.resolver.fetch.return_value = None
        items = self.service().build_daily(NOW)
        self.assertIn('no_transcript', [i.status for i in items])
        self.summarizer.summarize.assert_not_called()
        with self.store._connect() as db:
            value = json.loads(db.execute('SELECT episode_json FROM daily_transcript_backlog').fetchone()[0])
        self.assertEqual(value['metadata']['podwise_seq'], 1)

    def test_same_day_and_historical_rss_duplicates_are_not_resent(self):
        rss = replace(self.episode, id='rss:1', metadata={'rss_feed_url': 'https://example.org/feed'})
        self.feeds.write_text(json.dumps({'sources': [{'name': rss.show, 'type': 'youtube',
                                                     'rss_url': 'https://example.org/feed'}]}))
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[rss]):
            s = self.service()
            s.transcript_resolver.supports_url.return_value = False
            self.assertEqual([e.id for e in s.discover_daily_candidates(NOW)], ['rss:1'])
        self.store.record_episode(rss, 'sent')
        self.feeds.write_text(json.dumps({'sources': []}))
        self.assertEqual(self.service().discover_daily_candidates(NOW), [])

    def test_filtered_exploration_does_not_suppress_later_tracked_rss(self):
        self.store.record_episode(self.episode, 'discovery_filtered')
        rss = replace(self.episode, id='rss:1', metadata={'rss_feed_url': 'https://example.org/feed'})
        self.assertEqual(self.store.publisher_episode_aliases(rss), set())

    def test_discovery_backlog_cannot_starve_tracked_rss_or_other_discoveries(self):
        for identity in ('podwise:1', 'podwise:2', 'rss:priority'):
            self.store.defer_daily_transcript(replace(self.episode, id=identity), 'pending')
        with self.store._connect() as db:
            db.execute("UPDATE daily_transcript_backlog SET next_check_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.store.due_daily_transcripts(limit=1)[0].id, 'rss:priority')
        self.store.complete_daily_transcript('rss:priority')
        first = self.store.due_daily_transcripts(limit=1)[0]
        self.store.defer_daily_transcript(first, 'pending')
        self.assertNotEqual(self.store.due_daily_transcripts(limit=1)[0].id, first.id)

    def test_identical_titles_from_different_publishers_are_not_merged(self):
        other = replace(self.episode, id='podwise:2', show='Another Publisher', url='https://example.org/other')
        service = self.service()
        service.discovery.discover.return_value = DiscoveryResult([self.episode, other], 'status')
        self.assertEqual(len(service.discover_daily_candidates(NOW)), 2)

    def test_discovery_outage_does_not_erase_rss_results(self):
        s = self.service()
        s.discovery.discover.side_effect = RuntimeError('credential must not leak')
        items = s.build_daily(NOW)
        self.assertIn('discovery_status', [i.status for i in items])
        self.assertIn('failed', [i.status for i in items])
        self.assertIn('发现失败', coverage_report(items))
        self.assertNotIn('credential', coverage_report(items))


if __name__ == '__main__':
    unittest.main()
