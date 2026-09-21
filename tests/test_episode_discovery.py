import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_research_library import LibraryFixture, ScriptedModel, final_output, tool_call

from news_officer.models import Episode, IncomingMessage
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import PodcastResearchAgent, ResearchTools
from news_officer.research_checkpoint import restore_checkpoint, save_checkpoint

QUESTION = '最近听说 Town 的 founder CEO 的播客不错，给我拉出来重点总结一下'
EARLY = '我这里没法唯一定位你说的 Town，请发一下节目链接或补充姓名。'


class DiscoveryTests(LibraryFixture):
    def setUp(self):
        super().setUp()
        self.episode = Episode('town', 'Interview with JD, Founder of Town',
                               'https://example.test/town', '20VC',
                               published_at=datetime.now(UTC) - timedelta(days=14),
                               metadata={'description': 'Jean-Denis Grèze is the co-founder and CEO of Town.',
                                         'audio_url': 'https://example.test/town.mp3'})
        self.agent = PodcastResearchAgent(self.store, self.registry, PodcastArchive(self.store),
                                          'test-key', 'test-model', chats=('team',))
        self.agent.initialize()
        self.message = IncomingMessage('town-case', 'team', QUESTION, 'group', 'colleague')
        self.state = ResearchTools(self.agent, self.message.conversation_key, self.message)

    def search(self, episodes, query='Town', **kwargs):
        with patch('news_officer.source_registry.latest_rss_episodes', return_value=episodes):
            return self.registry.search_episodes(query, **kwargs)

    def test_guest_candidate_found_beyond_daily_window_and_newest_sixty(self):
        noise = [replace(self.episode, id=str(i), title='Other interview', metadata={}) for i in range(80)]
        result = self.search(noise + [self.episode])
        self.assertEqual([e['title'] for e in result['episodes']], [self.episode.title])
        self.assertEqual(result['episodes'][0]['_metadata']['audio_url'], self.episode.metadata['audio_url'])

    def test_word_boundaries_accents_and_company_clues(self):
        distractor = replace(self.episode, id='other', title='Downtown business', metadata={})
        self.assertEqual(len(self.search([self.episode, distractor])['episodes']), 1)
        self.assertEqual(len(self.search([self.episode], 'Jean Denis Greze')['episodes']), 1)
        self.assertEqual(self.search([self.episode], 'Town unrelated')['episodes'], [])

    def test_dates_unknown_dates_and_scope_are_explicit(self):
        old = replace(self.episode, id='old', published_at=datetime.now(UTC) - timedelta(days=120))
        future = replace(self.episode, id='future', published_at=datetime.now(UTC) + timedelta(days=2))
        unknown = replace(self.episode, id='unknown', published_at=None)
        result = self.search([old, future, unknown])
        self.assertEqual(len(result['episodes']), 1)
        self.assertIsNone(result['episodes'][0]['published_at'])
        self.assertEqual(len(self.search([old], days=365)['episodes']), 1)
        self.assertEqual(self.search([self.episode], show='missing')['checked'], [])

    def test_failure_not_reported_as_exhaustive_no_results(self):
        with patch('news_officer.source_registry.latest_rss_episodes', side_effect=RuntimeError('private')):
            result = self.registry.search_episodes('Town')
        self.assertTrue(result['failures'])
        self.assertNotIn('private', json.dumps(result))
        with self.assertRaises(ValueError):
            self.registry.search_episodes('Town', days=0)

    def discover(self, episodes=None):
        with patch('news_officer.source_registry.latest_rss_episodes', return_value=episodes or [self.episode]):
            return self.state.execute('search_episodes', {'query': 'Town', 'days': 90, 'show': ''})

    def test_discovery_authorizes_exact_analysis_but_not_content_evidence_or_directory(self):
        service = self.agent.podcast_service = SimpleNamespace(analyze_discovered_episode=Mock(return_value=SimpleNamespace(message='全文已核验')))
        result = self.discover()
        self.assertEqual(self.state.recent_directory(), '')
        self.assertFalse(self.state.body_evidence)
        self.assertNotIn('_metadata', result['episodes'][0])
        self.state.execute('analyze_podcast', {'url': self.episode.url})
        service.analyze_discovered_episode.assert_called_once_with(self.episode)
        with self.assertRaises(ValueError):
            self.state.execute('analyze_podcast', {'url': 'https://example.test/invented'})

    def test_shared_url_stays_ambiguous(self):
        self.agent.podcast_service = SimpleNamespace(analyze_discovered_episode=Mock())
        self.discover([self.episode, replace(self.episode, id='second')])
        result = self.state.execute('analyze_podcast', {'url': self.episode.url})
        self.assertIn('error', result)
        self.agent.podcast_service.analyze_discovered_episode.assert_not_called()

    def test_premature_clarification_rejected_but_real_ambiguity_allowed(self):
        value = {'kind': 'conversation', 'message': EARLY, 'points': []}
        self.state.execute('search_library', {'query': 'Town'})
        with self.assertRaisesRegex(ValueError, 'Premature clarification'):
            self.state.render(value)
        self.discover([self.episode, replace(self.episode, id='second', url='https://example.test/second')])
        text = '找到两期 Town CEO 访谈：20VC 与另一场访谈。你更想看哪一期？'
        self.assertEqual(self.state.render({**value, 'message': text}), text)

    def test_greeting_and_non_research_conversation_not_blocked(self):
        self.state.message = replace(self.message, text='你好')
        text = '你好，你想聊哪一期？'
        self.assertEqual(self.state.render({'kind': 'conversation', 'message': text, 'points': []}), text)

    def test_checkpoint_preserves_discovery_and_old_checkpoint_is_compatible(self):
        self.discover()
        save_checkpoint(self.state, [])
        restored = ResearchTools(self.agent, self.state.key, self.message)
        restore_checkpoint(restored)
        self.assertTrue(restored.episode_search_attempted)
        self.assertEqual(restored.discovered_episodes[self.episode.url], self.episode)
        with self.store._connect() as db:
            payload = json.loads(db.execute('SELECT payload_json FROM research_checkpoints').fetchone()[0])
            payload['state'].pop('episode_search_attempted')
            db.execute('UPDATE research_checkpoints SET payload_json=?', (json.dumps(payload),))
        restore_checkpoint(restored)
        self.assertFalse(restored.episode_search_attempted)

    def test_sdk_repairs_unsearched_clarification_for_colleague(self):
        answer = '查过已追踪 RSS，暂未定位到匹配项；可以补充大概的发布日期吗？'
        self.agent.sdk_model = ScriptedModel([
            tool_call('search_library', {'query': 'Town'}),
            final_output({'kind': 'conversation', 'message': EARLY, 'points': []}),
            tool_call('search_episodes', {'query': 'Town', 'days': 90, 'show': ''}),
            final_output({'kind': 'conversation', 'message': answer, 'points': []}),
        ])
        with patch('news_officer.source_registry.latest_rss_episodes', return_value=[]):
            result = self.agent.handle(QUESTION, self.message)
        self.assertEqual(result.messages, (answer,))
        with self.store._connect() as db:
            steps = [r[0] for r in db.execute('SELECT tool FROM research_steps ORDER BY step')]
        self.assertEqual(steps, ['search_library', 'search_episodes'])
