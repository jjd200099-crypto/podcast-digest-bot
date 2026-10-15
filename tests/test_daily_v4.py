import json
import unittest
from pathlib import Path
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
from news_officer.feishu import combined_delivery_parts, daily_card_delivery_parts
from news_officer.models import DailyItem, Episode
from news_officer.summarizer import (
    _valid_editorial_summary,
    _valid_v4_summary,
    normalize_v4_summary,
)

V4 = ('## Show｜AI 成本取决于任务\n节目：Show｜Very long original episode title\n'
      '链接：https://example.com/listen\n时长：40分钟\n'
      '简介：Mike 讨论模型选择和 AI 成本。\n要点总结：\n'
      '1. 成本取决于任务。Mike 认为简单任务可以使用更小的模型。\n'
      '2. 更低价格不一定带来更高回报。公司称需要先验证客户的真实需求。\n'
      '推荐理由：提供模型路由和成本管理的具体案例。\n推荐星级：★★★★☆')


class DailyV4Tests(unittest.TestCase):
    def test_real_blank_line_output_normalizes_and_delivers_v4_card(self):
        raw = (Path(__file__).parent / 'fixtures' /
               'v4-summary-blank-lines-2026-10-14.md').read_text()
        normalized = normalize_v4_summary(raw)
        self.assertFalse(_valid_v4_summary(raw))
        self.assertTrue(_valid_v4_summary(normalized))
        self.assertEqual(normalize_v4_summary(normalized), normalized)
        self.assertEqual(normalized.splitlines(), [line for line in raw.splitlines() if line.strip()])
        item = DailyItem(EPISODE, 'summarized', raw)
        rendered = reader_episode_v4(item, 1)
        parts = daily_card_delivery_parts(rendered, 'blank-lines', title='播客精选')
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], 'interactive')
        content = json.loads(parts[0][1])['body']['elements'][1]['content']
        for label in ('- 简介：', '- 优先级/阅读建议：', '- 要点总结：'):
            self.assertIn(label, content)
        points = [line for line in normalized.splitlines() if line[:1].isdigit()]
        self.assertEqual(len(points), 5)
        for point in points:
            self.assertIn('\n    ' + point, content)
        self.assertNotRegex(content, r'\n\s*\n    \d+\. ')
        self.assertEqual(item.message, raw)
        instance = summarizer_for(raw)
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
            self.assertEqual(instance.summarize(EPISODE, TRANSCRIPT), normalized)
        self.assertEqual(len(instance.client.responses.calls), 1)

    def test_blank_lines_at_every_v4_boundary_preserve_strict_validation(self):
        padded = V4.replace('\n', '\n\n \t\n')
        self.assertEqual(normalize_v4_summary(padded), V4)
        for malformed in (padded.replace('2. ', '3. '),
                          padded.replace('简介：', '介绍：'),
                          padded.replace('推荐理由：', '额外正文。\n推荐理由：'),
                          padded.replace('节目：', '节目第二行。\n节目：'),
                          padded.replace('★★★★☆', '★☆★★☆'),
                          padded + '\n\n额外正文。'):
            with self.subTest(malformed=malformed):
                self.assertFalse(_valid_v4_summary(normalize_v4_summary(malformed)))

    def test_real_model_output_normalizes_and_delivers_v4_card(self):
        raw = (Path(__file__).parent / 'fixtures' /
               'v4-summary-model-output-2026-10-14.md').read_text()
        normalized = normalize_v4_summary(raw)
        self.assertFalse(_valid_v4_summary(raw))
        self.assertTrue(_valid_v4_summary(normalized))
        self.assertEqual(normalize_v4_summary(normalized), normalized)
        self.assertNotRegex(normalized, r'\n\s*\n\d+\. ')
        item = DailyItem(EPISODE, 'summarized', raw)
        rendered = reader_episode_v4(item, 1)
        parts = daily_card_delivery_parts(rendered, 'fixture', title='播客精选')
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], 'interactive')
        card = json.loads(parts[0][1])
        content = card['body']['elements'][1]['content']
        for label in ('- 简介：', '- 优先级/阅读建议：', '- 要点总结：'):
            self.assertIn(label, content)
        self.assertEqual(sum(line.startswith('    ') for line in content.splitlines()), 6)
        for number in range(1, 7):
            self.assertIn(f'\n    {number}. ', content)
        self.assertNotRegex(content, r'\n\s*\n    \d+\. ')
        self.assertEqual(item.message, raw)
        instance = summarizer_for(raw)
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
            summary = instance.summarize(EPISODE, TRANSCRIPT)
        self.assertEqual(summary, normalized)
        self.assertTrue(_valid_v4_summary(summary))
        self.assertEqual(len(instance.client.responses.calls), 1)

    def test_normalization_tolerates_labels_but_rejects_malformed_content(self):
        for heading in ('要点总结:', '**要点总结：**', '- **要点总结**:', '- 要点总结：'):
            with self.subTest(heading=heading):
                value = V4.replace('要点总结：', heading).replace('\n2. ', '\n\n2. ')
                self.assertEqual(normalize_v4_summary(value), V4)
                self.assertIn('- 要点总结：', reader_episode_v4(DailyItem(EPISODE, 'summarized', value), 1))
        for malformed in (V4.replace('2. ', '3. '), V4.replace('简介：', '介绍：'),
                          V4.replace('2. ', 'un-numbered prose\n2. '),
                          V4.replace('要点总结：', '要点总结：\n要点总结：'),
                          V4.replace('★★★★☆', '★☆★★☆'), V4.split('推荐理由：')[0]):
            with self.subTest(malformed=malformed):
                self.assertFalse(_valid_v4_summary(normalize_v4_summary(malformed)))
                self.assertNotIn('- 要点总结：', reader_episode_v4(DailyItem(EPISODE, 'summarized', malformed), 1))

    def test_multiline_intro_and_extra_lines_are_rejected(self):
        for value in (V4.replace('。\n要点总结', '。\n简介的第二行。\n要点总结'),
                      V4.replace('推荐星级：', '额外正文。\n推荐星级：'),
                      V4.replace('简介：', '额外正文。\n简介：')):
            with self.subTest(value=value):
                self.assertFalse(_valid_v4_summary(normalize_v4_summary(value)))
                rendered = reader_episode_v4(DailyItem(EPISODE, 'summarized', value), 1)
                self.assertNotIn('    1.', rendered)
                self.assertNotIn('简介的第二行', rendered)

    def test_inline_and_indented_points_normalize(self):
        value = V4.replace('要点总结：\n1. ', '要点总结：1. ').replace('\n2. ', '\n  2. ')
        self.assertEqual(normalize_v4_summary(value), V4)
        self.assertEqual(normalize_v4_summary(normalize_v4_summary(value)), V4)

    def test_fallback_excerpt_is_bounded_and_keeps_links(self):
        value = V4.replace('Mike 讨论模型选择和 AI 成本。', '长简介' * 100).replace('2. ', '3. ')
        item = DailyItem(EPISODE, 'summarized', value)
        rendered = reader_episode_v4(item, 1, 'https://example.com/doc')
        excerpt = next(line[5:] for line in rendered.splitlines() if line.startswith('- 简介：'))
        self.assertEqual(len(excerpt), 160)
        self.assertTrue(excerpt.endswith('…'))
        self.assertNotIn('成本取决于任务。Mike', rendered)
        self.assertIn('[精读和完整中文对谈](https://example.com/doc)', rendered)
        self.assertIn(f'[收听节目]({EPISODE.url})', rendered)
        self.assertEqual(item.message, value)

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
        self.assertIn('**1. 【Show｜嘉宾未注明｜AI 成本取决于任务】**', text)
        self.assertNotIn('Very long original', text)
        self.assertIn('- 简介：Mike', text)
        self.assertIn('- 优先级/阅读建议：值得看全文，', text)
        self.assertIn('    1. 成本取决于任务。Mike 认为', text)
        self.assertIn('模型。\n    2. ', text)
        self.assertIn('[精读和完整中文对谈](https://example.com/doc)', text)
        self.assertIn(f'[收听节目]({EPISODE.url})', text)
        self.assertEqual(item.message, V4)

    def test_archive_fallback_shows_only_first_excerpt(self):
        old = ('## Guest｜主题\n节目：Show｜Long title\n\n'
               '公司认为效率可能提高1000倍，但仍需验证。\n\n'
               '只有需求增长，成本下降才可能提高利润。\n'
               '推荐理由：有具体条件和数字作为支撑。\n推荐星级：★★★★☆')
        text = reader_episode_v4(DailyItem(EPISODE, 'summarized', old), 1)
        self.assertIn('- 简介：公司认为效率可能提高1000倍，但仍需验证。', text)
        self.assertNotIn('只有需求增长', text)

    def test_short_title_deduplicates_verbose_rss_brand(self):
        from dataclasses import replace
        episode = replace(EPISODE, show='Show: A very long RSS brand about AI and business')
        for title in ('Show｜AI 成本取决于任务', 'Show｜Show｜AI 成本取决于任务'):
            text = reader_episode_v4(DailyItem(episode, 'summarized',
                V4.replace('Show｜AI 成本取决于任务', title)), 1)
            self.assertEqual(text.splitlines()[0], '**1. 【Show｜嘉宾未注明｜AI 成本取决于任务】**')

    def test_legacy_paragraphs_and_bullets_do_not_manufacture_v4_fields(self):
        for old in (VALID_SUMMARY, LEGACY_SUMMARY):
            text = reader_episode_v4(DailyItem(EPISODE, 'summarized', old), 1)
            self.assertIn('- 简介：', text)
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
            self.assertIn('- 简介：', rendered)
            self.assertIn('Mike 讨论模型选择和 AI 成本。', rendered)
            if _valid_v4_summary(scored):
                self.assertIn('- 要点总结：', rendered)
            else:
                self.assertNotIn('Mike 认为简单任务可以使用更小的模型。', rendered)
                self.assertNotIn('公司称需要先验证客户的真实需求。', rendered)

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

    async def test_runtime_reserves_realistic_urls_for_every_eligible_section(self):
        items = [self.item('one', 4), self.item('two', 5), self.item('low', 3)]
        instance, job, podcast = fixtures.CombinedDailyTests.setup_job(self, items,
            payload={'scheduled_for': '2026-10-15T08:30:00+08:00'})
        instance.settings.daily_reader_mode = True
        instance.settings.daily_layout = 'v4'
        podcast.build_pending = Mock(return_value=[])
        with patch('news_officer.runtime.daily_card_delivery_parts',
                   wraps=daily_card_delivery_parts) as build:
            await instance._handle_daily_job(job)
        for call in build.call_args_list:
            reserved = call.kwargs['reserve_markdown']
            self.assertEqual(reserved.count('https://xxxx.feishu.cn/docx/' + 'x' * 27), 2)
            self.assertNotIn(DOCUMENT_PLACEHOLDER, reserved)
        bundle = self.store.list_job_results(job.key, 'daily_bundle')[0]
        self.assertNotIn('https://xxxx.feishu.cn/docx/', bundle['deliveries'][0]['parts'][0][1])

    async def test_indivisible_oversized_v4_day_still_delivers_safe_parts(self):
        from news_officer.feishu import encoded_message_payload
        item = self.item('huge', 4)
        instance, job, podcast = fixtures.CombinedDailyTests.setup_job(self, [item])
        podcast.build_pending = Mock(return_value=[])
        instance.settings.daily_reader_mode = True
        instance.settings.daily_layout = 'v4'
        section = ('**1. 【Show｜Guest｜Topic】**\n\n' + '核心判断' * 20000
                   + '\n- [收听节目](https://example.test/huge)')
        with patch('news_officer.daily_report.reader_episode_v4', return_value=section):
            await instance._handle_daily_job(job)
        self.assertGreater(len(self.messenger.attempts), 1)
        self.assertTrue(all(part.msg_type == 'text' for part in self.messenger.attempts))
        for part in self.messenger.attempts:
            self.assertLessEqual(len(encoded_message_payload({
                'receive_id': 'x' * 128, 'msg_type': part.msg_type,
                'content': part.content, 'uuid': part.uuid})), 145_000)
        self.assertIn('https://example.test/huge', self.messenger.attempts[-1].content)

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


class FullSixEpisodeCardTests(unittest.TestCase):
    def test_realistic_full_six_episode_card_fits_without_compaction(self):
        # Real Chinese model output: long introduction and six substantive bullets.
        # The 3.2KB source is in the requested production-sized 2.5–3.5KB range.
        raw = normalize_v4_summary((Path(__file__).parent / 'fixtures' /
                                   'v4-summary-model-output-2026-10-14.md').read_text())
        self.assertTrue(2500 <= len(raw.encode()) <= 3500)
        sections = []
        points = [line for line in raw.splitlines() if line[:1].isdigit()]
        self.assertEqual(len(points), 6)
        for index in range(6):
            episode = Episode(f'full-{index}', f'AI 安全与工程实践 {index}',
                              f'https://example.org/episodes/production-2026-10-15-{index}',
                              f'Configured Show {index}')
            sections.append(reader_episode_v4(DailyItem(episode, 'summarized', raw), index + 1,
                document_url=f'https://example.feishu.cn/docx/ProductionReadingDocument20261015{index}'))
        markdown = '\n━━━━━━━━━━━━━━━━━━━━\n'.join(sections)
        [part] = daily_card_delivery_parts(markdown, 'full-six', title='播客精选 2026-10-15',
                                           allow_compaction=False, require_interactive=True)
        from news_officer.feishu import encoded_message_payload
        envelope = {'receive_id': 'x' * 128, 'msg_type': 'interactive', 'content': part[1],
                    'uuid': 'x' * 36, 'reply_in_thread': False}
        self.assertLessEqual(len(encoded_message_payload(envelope)), 29000)
        content = '\n'.join(e.get('content', '') for e in json.loads(part[1])['body']['elements'])
        for point in points:
            self.assertEqual(content.count(point), 6)
        self.assertEqual(content.count('[精读和完整中文对谈]('), 6)
        self.assertEqual(content.count('[收听节目]('), 6)
