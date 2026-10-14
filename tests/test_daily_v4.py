import json
import unittest
from unittest.mock import Mock, patch

import test_combined_daily as fixtures
from test_summarizer import (
    EPISODE,
    LEGACY_SUMMARY,
    TRANSCRIPT,
    VALID_SUMMARY,
    summarizer_for,
)

from news_officer.config import Settings
from news_officer.daily_enrichment import DOCUMENT_PLACEHOLDER, _content
from news_officer.daily_report import (
    reader_episode_v4,
    render_reader_daily,
    v4_layout_enabled,
)
from news_officer.feishu import combined_delivery_parts
from news_officer.models import DailyItem
from news_officer.summarizer import _valid_editorial_summary, _valid_v4_summary

V4 = ('## Show｜AI 成本取决于任务\n节目：Show｜Very long original episode title\n'
      '链接：https://example.com/listen\n时长：40分钟\n'
      '简介：Mike 讨论模型选择和 AI 成本。\n要点总结：\n'
      '1. 成本取决于任务。Mike 认为简单任务可以使用更小的模型。\n'
      '2. 更低价格不一定带来更高回报。公司称需要先验证客户的真实需求。\n'
      '推荐理由：提供模型路由和成本管理的具体案例。\n推荐星级：★★★★☆')


class DailyV4Tests(unittest.TestCase):
    def test_flag_and_config_default_and_rollback(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertTrue(v4_layout_enabled())
            self.assertEqual(Settings.__dataclass_fields__['daily_layout'].default, 'v4')
        for value in ('v4', 'true', '1'):
            self.assertTrue(v4_layout_enabled(value))
        for value in ('compact', 'false', '0'):
            self.assertFalse(v4_layout_enabled(value))

    def test_new_structure_and_short_links(self):
        item = DailyItem(EPISODE, 'summarized', V4)
        text = reader_episode_v4(item, 1, 'https://example.com/doc')
        self.assertIn('**1. 【Show｜AI 成本取决于任务】**', text)
        self.assertNotIn('Very long original', text)
        self.assertIn('- 简介：Mike', text)
        self.assertIn('- 优先级/阅读建议：值得看全文，', text)
        self.assertIn('    1. 成本取决于任务。Mike 认为', text)
        self.assertIn('模型。\n\n    2. ', text)
        self.assertIn('[精读和完整中文对谈](https://example.com/doc)', text)
        self.assertIn(f'[收听节目]({EPISODE.url})', text)
        self.assertEqual(item.message, V4)

    def test_archive_claims_survive_without_rewriting(self):
        old = ('## Guest｜主题\n节目：Show｜Long title\n\n'
               '公司认为效率可能提高1000倍，但仍需验证。\n\n'
               '只有需求增长，成本下降才可能提高利润。\n'
               '推荐理由：有具体条件和数字作为支撑。\n推荐星级：★★★★☆')
        text = reader_episode_v4(DailyItem(EPISODE, 'summarized', old), 1)
        for claim in ('公司认为效率可能提高1000倍，但仍需验证。',
                      '只有需求增长，成本下降才可能提高利润。'):
            self.assertIn(claim, text)

    def test_short_title_deduplicates_verbose_rss_brand(self):
        from dataclasses import replace
        episode = replace(EPISODE, show='Show: A very long RSS brand about AI and business')
        for title in ('Show｜AI 成本取决于任务', 'Show｜Show｜AI 成本取决于任务'):
            text = reader_episode_v4(DailyItem(episode, 'summarized',
                V4.replace('Show｜AI 成本取决于任务', title)), 1)
            self.assertEqual(text.splitlines()[0], '**1. 【Show｜AI 成本取决于任务】**')

    def test_legacy_paragraphs_and_bullets_do_not_manufacture_v4_fields(self):
        for old in (VALID_SUMMARY, LEGACY_SUMMARY):
            text = reader_episode_v4(DailyItem(EPISODE, 'summarized', old), 1)
            self.assertNotIn('- 简介：', text)
            self.assertNotIn('- 要点总结：', text)
            self.assertNotIn('    1.', text)
            self.assertEqual(old, DailyItem(EPISODE, 'summarized', old).message)

    def test_v4_format_failure_keeps_usable_output(self):
        for usable in (VALID_SUMMARY, V4.replace('2. ', '3. '),
                       V4.replace('简介：', '介绍：'),
                       V4.split('推荐理由：')[0]):
            instance = summarizer_for(usable, 'invalid')
            with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
                self.assertEqual(instance.summarize(EPISODE, TRANSCRIPT), usable.strip())
            self.assertEqual(len(instance.client.responses.calls), 2)

    def test_fallback_survives_editorial_scoring_and_rendering(self):
        from news_officer.editorial import EditorialPolicy
        decision = {'stars': 4, 'assessment': {'reason': '提供模型路由和成本管理的具体案例。'}}
        for usable in (V4.replace('2. ', '3. '), V4.replace('简介：', '介绍：'),
                       V4.split('推荐理由：')[0]):
            instance = summarizer_for(usable, 'invalid')
            with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
                scored = EditorialPolicy.apply(instance.summarize(EPISODE, TRANSCRIPT), decision)
            self.assertEqual(len(instance.client.responses.calls), 2)
            self.assertIn('推荐理由：' + decision['assessment']['reason'], scored)
            self.assertIn('推荐星级：★★★★☆', scored)
            rendered = render_reader_daily([DailyItem(EPISODE, 'summarized', scored)], [], layout='v4')
            self.assertIn('Mike 认为简单任务可以使用更小的模型。', rendered)
            self.assertIn('公司称需要先验证客户的真实需求。', rendered)

    def test_fallback_preserves_strict_selection_and_rejects_empty_output(self):
        from news_officer.editorial import STRICT_POLICY_VERSION, EditorialPolicy
        from news_officer.summarizer import SummaryFormatError
        decision = {'policy': STRICT_POLICY_VERSION, 'stars': 5,
                    'assessment': {'reason': '提供模型路由和成本管理的具体案例。',
                                   'ai_content_share': 'low', 'evidence_quality': 'high',
                                   'reading_recommendation': '值得编译'}}
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
            scored = EditorialPolicy.apply(V4.split('推荐理由：')[0], decision)
            self.assertIn('推荐星级：★★★☆☆', scored)
            self.assertIn('阅读建议：值得看摘要', scored)
            with self.assertRaises(SummaryFormatError):
                EditorialPolicy.apply('## Show｜主题\n节目：Show', decision)
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'compact'}), self.assertRaises(SummaryFormatError):
            EditorialPolicy.apply(V4.split('推荐理由：')[0], decision)

    def test_legacy_long_program_line_is_omitted_only_at_render_time(self):
        old = VALID_SUMMARY.replace('节目：', '节目：' + '很长的原始节目标题' * 50)
        item = DailyItem(EPISODE, 'summarized', old)
        rendered = reader_episode_v4(item, 1)
        self.assertNotIn('很长的原始节目标题', rendered)
        self.assertNotIn('节目：', rendered)
        self.assertIn('收听节目', rendered)
        self.assertEqual(item.message, old)

    def test_rsi_parenthetical_annotations_in_both_directions(self):
        for summary in (V4, VALID_SUMMARY):
            for term in ('RSI（递归自我改进）', 'RSI(递归自我改进)',
                         '递归自我改进（RSI）', '递归自我改进(RSI)'):
                value = summary.replace('推荐理由：', '推荐理由：' + term)
                rendered = reader_episode_v4(DailyItem(EPISODE, 'summarized', value), 1)
                self.assertNotIn(term, rendered)
                self.assertIn('RSI', rendered)

    def test_clean_display_and_separator_in_actual_post(self):
        value = V4.replace('Mike 讨论', '[新]【新】王颢蓁讨论RSI（递归自我改进）和')
        items = [DailyItem(EPISODE, 'summarized', value)] * 2
        text = render_reader_daily(items, [], layout='v4')
        for forbidden in ('[新]', '【新】', '王颢蓁', 'RSI（'):
            self.assertNotIn(forbidden, text)
        kind, content, _ = combined_delivery_parts(text, 'test', title='🎧 播客精选 · 2026-10-15')[0]
        self.assertEqual(kind, 'post')
        post = json.loads(content)['zh_cn']
        self.assertEqual(post['title'], '🎧 播客精选 · 2026-10-15')
        self.assertTrue(any(node.get('text') == '━━━━━━━━━━━━━━━━━━━━'
                            for line in post['content'] for node in line))
        self.assertTrue(any(node.get('tag') == 'a' and node['text'] == '收听节目'
                            for line in post['content'] for node in line))

    def test_v4_generation_validation_and_cache_migration(self):
        self.assertTrue(_valid_v4_summary(V4))
        self.assertTrue(_valid_editorial_summary(V4))
        for invalid in (V4.replace('2. ', '3. '), V4.replace('简介：', '介绍：'),
                        V4.replace('Mike', '王颢蓁')):
            self.assertFalse(_valid_v4_summary(invalid))
        instance = summarizer_for(V4)
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
            identity = instance.cache_identity()
            self.assertEqual(instance.summarize(EPISODE, TRANSCRIPT), V4)
            self.assertIn('每条先给结论', instance.client.responses.calls[0]['instructions'])
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'compact'}):
            self.assertEqual(identity, instance.cache_identity())


class DailyV4RuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CombinedDailyTests.setUp
    tearDown = fixtures.CombinedDailyTests.tearDown
    item = fixtures.CombinedDailyTests.item

    async def test_production_freeze_enrichment_and_retry(self):
        items = [self.item('one', 4), self.item('two', 3)]
        instance, job, podcast = fixtures.CombinedDailyTests.setup_job(self, items,
            payload={'scheduled_for': '2026-10-15T08:30:00+08:00'})
        instance.settings.daily_reader_mode = True
        instance.settings.daily_layout = 'v4'
        instance.settings.daily_compact_paragraphs = True
        podcast.summarizer = Mock()
        podcast.build_pending = Mock(return_value=[])
        await instance._handle_daily_job(job)
        podcast.summarizer.rewrite_paragraphs.assert_not_called()
        bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
        sections = bundle['enrichment']['sections']
        self.assertIn('**1. 【', sections[0]['body'])
        self.assertIn('**2. 【', sections[1]['body'])
        self.assertIn('━━━━━━━━━━━━━━━━━━━━', sections[1]['body'])
        self.assertIn(DOCUMENT_PLACEHOLDER, sections[0]['template'])
        delivery = bundle['deliveries'][0]
        kind, edited = _content(bundle, {'one': {'url': 'https://example.test/doc'}}, delivery)
        self.assertEqual(kind, 'interactive')
        card = json.loads(edited)
        self.assertEqual(card['schema'], '2.0')
        self.assertEqual([e['tag'] for e in card['body']['elements']],
                         ['markdown', 'markdown', 'hr', 'markdown'])
        self.assertIn('精读和完整中文对谈', edited)
        self.assertNotIn('━━━━━━━━━━━━━━━━━━━━', edited)
        instance.settings.daily_layout = 'compact'
        instance._prepare_combined_daily(job, finalize=True)
        self.assertEqual(bundle, self.store.list_job_results(job.key, 'daily_bundle')[0])
