import json
from dataclasses import replace
from unittest.mock import patch

import requests
from test_research_library import LibraryFixture
from test_youtube_rss_tracking import YouTubeRSSTrackingTests

from news_officer.models import IncomingMessage
from news_officer.podcast import FeedDiscoveryError
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import PodcastResearchAgent, ResearchTools
from news_officer.store import Store

QUESTION = '@情报官 RSS获取失败是什么情况。你有存历史检索过播客的数据库吗？如果没有，之后能不能加一个'
INVENTED = ['Anthropic', 'AI Engineer', 'Andrej Karpathy', 'Stripe',
            'Founders You Should Know', 'Generalist', 'Index Ventures']


class OpsAnswersTests(LibraryFixture):
    def setUp(self):
        super().setUp()
        self.agent = PodcastResearchAgent(self.store, self.registry, PodcastArchive(self.store),
                                          'test-key', 'test-model', chats=('team',))
        self.agent.initialize()
        self.message = IncomingMessage('incident', 'team', QUESTION, 'group', 'user')
        self.state = ResearchTools(self.agent, self.message.conversation_key, self.message)

    def test_real_incident_uses_status_without_episode_citations_or_request_id(self):
        self.store.save_fetch_status('2026-10-14T00:00:00+00:00', [
            {'source_name': 'Actual Channel', 'url': 'https://example.test/rss',
             'source_type': 'youtube', 'status': 'failed', 'reason': 'HTTP 404'}])
        result = self.state.execute('get_fetch_status', {})
        self.assertEqual(result['failed_feeds'][0]['reason'], 'HTTP 404')
        self.assertEqual(result['archived_transcripts'], 0)
        feature = self.state.execute('record_feature_request', {'summary': '历史检索数据库'})
        self.assertNotIn('id', feature)
        text = self.state.render({'kind': 'answer', 'message': '', 'points': [
            {'text': 'Actual Channel 的 YouTube 频道 RSS 接口返回404。已有档案库，目前全文0份。建议会由维护者评估。7d5a1c1af707',
             'citations': []}]})
        for name in INVENTED:
            self.assertNotIn(name, text)
        self.assertNotIn('7d5a1c1af707', text)
        self.assertNotIn('来源', text)
        self.assertEqual(Store(self.store.path).latest_fetch_status(), self.store.latest_fetch_status())
        self.store.initialize()
        self.assertEqual(len(self.store.latest_fetch_status()['failed_feeds']), 1)

    def test_unconfirmed_sources_and_generic_causes_are_rejected(self):
        self.state.execute('get_fetch_status', {})
        with patch.object(self.registry, 'list', return_value=[{'name': n} for n in INVENTED]):
            for name in INVENTED:
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Only name failed'):
                    self.state.render({'kind': 'answer', 'message': '', 'points': [
                        {'text': name + ' RSS获取失败。', 'citations': []}]})
        with self.assertRaisesRegex(ValueError, 'confirmed'):
            self.state.render({'kind': 'answer', 'message': '', 'points': [
                {'text': '可能是网络问题。', 'citations': []}]})

    def test_latest_empty_run_retains_unchecked_failures(self):
        self.store.save_fetch_status('2026-10-13', [{'source_name': 'Old', 'url': 'https://example.test',
                                                 'source_type': 'rss', 'status': 'failed', 'reason': 'HTTP 404'}])
        self.store.save_fetch_status('2026-10-14', [])
        self.assertEqual(len(self.store.latest_fetch_status()['failed_feeds']), 1)
        self.assertEqual(self.store.latest_fetch_status()['run_at'], '2026-10-14')

    def test_fetch_question_must_read_status(self):
        with self.assertRaisesRegex(ValueError, 'get_fetch_status'):
            self.state.render({'kind': 'answer', 'message': '', 'points': [
                {'text': '可能是网络故障。', 'citations': []}]})

    def test_system_answer_never_renders_unrelated_episode_links(self):
        self.state.execute('get_fetch_status', {})
        evidence = self.state.evidence_item('irrelevant', 'https://example.test/episode', 'Fei-Fei Li')
        with self.assertRaisesRegex(ValueError, 'system tool evidence'):
            self.state.render({'kind': 'answer', 'message': '', 'points': [
                {'text': '尚无抓取记录，无法确认失败原因。', 'citations': [{'id': evidence['evidence_id']}]}]})
        status = self.state.execute('get_fetch_status', {})
        text = self.state.render({'kind': 'answer', 'message': '', 'points': [
            {'text': '尚无抓取记录，无法确认失败原因。', 'citations': [{'id': status['evidence_id']}]}]})
        self.assertNotIn('Fei-Fei', text)
        self.assertNotIn('https://', text)

    def test_normal_fetch_and_rss_requests_keep_episode_rules_and_hex_text(self):
        for question in ('帮我抓取这期做详细版', '添加这个 RSS', '获取这期文字稿，成功的话总结一下',
                         '抓取失败的话换个来源总结', 'RSS 状态如何，顺便给我这期文字稿',
                         '系统状态怎么样？', '抓取成功了吗？', 'Summarize this episode and check fetch status'):
            with self.subTest(question=question):
                state = ResearchTools(self.agent, self.message.conversation_key,
                                      replace(self.message, text=question))
                with self.assertRaisesRegex(ValueError, 'Missing citations'):
                    state.render({'kind': 'answer', 'message': '', 'points': [
                        {'text': '内容编号 7d5a1c1af707。', 'citations': []}]})
                evidence = state.evidence_item('内容编号 7d5a1c1af707。', 'https://example.test/episode', '节目')
                text = state.render({'kind': 'answer', 'message': '', 'points': [
                    {'text': '内容编号 7d5a1c1af707。', 'citations': [{'id': evidence['evidence_id']}]}]})
                self.assertIn('7d5a1c1af707', text)
                self.assertIn('https://example.test/episode', text)

    def test_fetch_and_system_status_questions_require_status(self):
        for question in ('RSS 状态正常吗？', '抓取失败了吗？', '哪些 RSS 异常？', 'fetch status?', 'RSS获取失败是什么情况？'):
            self.state.message = replace(self.message, text=question)
            with self.subTest(question=question), self.assertRaisesRegex(ValueError, 'get_fetch_status'):
                self.state.render({'kind': 'conversation', 'message': '目前正常。', 'points': []})

    def test_status_tool_allows_mixed_reply_with_episode_citations(self):
        # No research task is set: status-tool use alone must not lock the reply.
        self.state.message = replace(self.message, text='RSS 状态如何？也说说这期讲了什么。')
        status = self.state.execute('get_fetch_status', {})
        episode = self.state.evidence_item('嘉宾讨论人工智能。', 'https://example.test/episode', '节目')
        text = self.state.render({'kind': 'answer', 'message': '', 'points': [
            {'text': '尚无抓取记录。', 'citations': [{'id': status['evidence_id']}]},
            {'text': '嘉宾讨论人工智能。', 'citations': [{'id': episode['evidence_id']}]}]})
        self.assertIn('https://example.test/episode', text)
        self.assertIn('尚无抓取记录', text)

    def test_partial_fetch_preserves_other_sources_and_replaces_recovered_source(self):
        sources = [{'name': name, 'type': 'rss', 'rss_url': f'https://example.test/{name}'}
                   for name in ('Lex', 'Other')]
        for method in ('recent', 'search_episodes'):
            with self.subTest(method=method), patch.object(self.registry, 'list', return_value=sources):
                def fetch(name='', method=method):
                    return (self.registry.recent(1, name=name) if method == 'recent'
                            else self.registry.search_episodes('AI', show=name))

                with patch('news_officer.source_registry.latest_rss_episodes', side_effect=RuntimeError('offline')):
                    fetch()
                with patch('news_officer.source_registry.latest_rss_episodes', return_value=[]):
                    fetch('Lex')
                failures = self.store.latest_fetch_status()['failed_feeds']
                self.assertEqual([r['source_name'] for r in failures], ['Other'])
                self.assertEqual(failures[0]['reason'], 'offline')
                with patch('news_officer.source_registry.latest_rss_episodes', return_value=[]):
                    fetch('Other')
                self.assertEqual(self.store.latest_fetch_status()['failed_feeds'], [])

    def test_manual_fetch_failure_is_visible_to_status_tool(self):
        for fetch in (lambda: self.registry.recent(1), lambda: self.registry.search_episodes('AI')):
            with patch.object(self.registry, 'list', return_value=[{
                    'name': 'Manual Feed', 'type': 'rss', 'rss_url': 'https://example.test/rss'}]), \
                    patch('news_officer.source_registry.latest_rss_episodes', side_effect=RuntimeError('offline')):
                fetch()
            result = self.state.execute('get_fetch_status', {})
            self.assertEqual(result['failed_feeds'][0]['source_name'], 'Manual Feed')
            self.assertEqual(result['failed_feeds'][0]['reason'], 'offline')

    def test_episode_answer_still_requires_citations(self):
        self.state.message = replace(self.message, text='这期讲了什么')
        with self.assertRaisesRegex(ValueError, 'Missing citations'):
            self.state.render({'kind': 'answer', 'message': '', 'points': [
                {'text': '嘉宾认为人工智能很重要。', 'citations': []}]})


class YouTubeFallbackTests(YouTubeRSSTrackingTests):
    def test_rss_404_falls_back_and_retains_reason(self):
        error = requests.HTTPError('bad feed', response=type('Response', (), {'status_code': 404})())
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=error), \
                patch('news_officer.podcast.latest_videos', return_value=[self.episode]) as videos:
            self.service(None).discover_daily_candidates()
        videos.assert_called_once()
        status = self.store.latest_fetch_status()
        self.assertEqual(status['failed_feeds'], [])
        self.assertIn('HTTP 404', status['recovered_feeds'][0]['reason'])
        self.assertEqual(status['recovered_feeds'][0]['source_type'], 'youtube')

    def test_rss_only_channel_without_source_url_falls_back(self):
        feeds = json.loads(self.feeds.read_text())
        feeds['sources'][0].pop('url')
        self.feeds.write_text(json.dumps(feeds))
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=RuntimeError('HTTP 404')), \
                patch('news_officer.podcast.latest_videos', return_value=[self.episode]) as videos:
            self.assertEqual(len(self.service(None).discover_daily_candidates()), 1)
        videos.assert_called_once_with(
            'https://www.youtube.com/channel/UCXUPKJO5MZQN11PqgIvyuvQ/videos', playlist_end=4)
        self.assertEqual(self.store.latest_fetch_status()['failed_feeds'], [])

    def test_successful_channel_rss_does_not_use_ytdlp(self):
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[]), \
                patch('news_officer.podcast.latest_videos') as videos:
            self.service(None).discover_daily_candidates()
        videos.assert_not_called()
        self.assertEqual(self.store.latest_fetch_status()['failed_feeds'], [])

    def test_empty_youtube_fallback_remains_a_fetch_failure(self):
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=RuntimeError('HTTP 404')), \
                patch('news_officer.podcast.latest_videos', return_value=[]), \
                self.assertRaises(FeedDiscoveryError):
            self.service(None).discover_daily_candidates()
        status = self.store.latest_fetch_status()
        self.assertEqual(len(status['failed_feeds']), 1)
        self.assertIn('HTTP 404', status['failed_feeds'][0]['reason'])
        self.assertEqual(status['recovered_feeds'], [])

    def test_both_failures_persist_before_discovery_error(self):
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=RuntimeError('HTTP 404')), \
                patch('news_officer.podcast.latest_videos', side_effect=RuntimeError('unavailable')), \
                self.assertRaises(FeedDiscoveryError):
            self.service(None).discover_daily_candidates()
        reason = self.store.latest_fetch_status()['failed_feeds'][0]['reason']
        self.assertEqual(reason, 'YouTube channel RSS HTTP 404; yt-dlp unavailable')

    def test_publisher_rss_failure_never_falls_back(self):
        feeds = json.loads(self.feeds.read_text())
        feeds['sources'][0]['rss_url'] = 'https://example.test/rss'
        self.feeds.write_text(json.dumps(feeds))
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=RuntimeError('offline')), \
                patch('news_officer.podcast.latest_videos') as videos, \
                self.assertRaises(FeedDiscoveryError):
            self.service(None).discover_daily_candidates()
        videos.assert_not_called()
