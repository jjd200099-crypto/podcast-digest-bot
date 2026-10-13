import os
import unittest
from unittest.mock import Mock, patch

from news_officer.config import Settings
from news_officer.daily_report import (
    compact_paragraphs,
    exactly_two_paragraphs,
    reader_episode,
    source_numbers,
)


class CompactParagraphTests(unittest.TestCase):
    def test_surplus_paragraph_join_boundaries(self):
        cases = [
            ('中文', '中文', '中文中文'),
            ('本身。', '对于', '本身。对于'),
            ('Hello', 'World', 'Hello World'),
            ('2026', 'October', '2026 October'),
            ('中文', 'Latin', '中文Latin'),
            ('Latin', '中文', 'Latin中文'),
            ('Latin', '，中文', 'Latin，中文'),
            ('Ａ', 'Latin', 'ＡLatin'),
            (' Hello  ', '  World ', 'Hello World'),
        ]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(exactly_two_paragraphs(['首段。', left, right]), ['首段。', expected])

    def test_surplus_paragraph_join_checks_each_boundary(self):
        self.assertEqual(
            exactly_two_paragraphs(['首段。', '中文', 'Latin', '2026', '。结束']),
            ['首段。', '中文Latin 2026。结束'],
        )

    source = ('## Guest｜产品\n节目：Show｜Show\n时长：20 分钟\n\n'
              'Guest认为需求可能增长10倍。团队反复研究这一问题并逐步建立完整的评测流程，讨论各种技术选择及其适用条件。'
              '\n\n推荐理由：有具体评测。\n阅读建议：值得看全文\n链接：https://example.test/listen')

    def test_default_off_is_identical_and_never_calls_model(self):
        model = Mock()
        self.assertEqual(reader_episode(self.source), reader_episode(self.source, rewriter=model, compact=False))
        model.rewrite_paragraphs.assert_not_called()
        with patch.dict(os.environ, {'FEISHU_APP_ID': 'test', 'FEISHU_APP_SECRET': 'test', 'OPENAI_API_KEY': 'test'}, clear=True):
            self.assertFalse(Settings.from_env().daily_compact_paragraphs)
            with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_COMPACT_PARAGRAPHS': 'true'}):
                self.assertTrue(Settings.from_env().daily_compact_paragraphs)
            with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_COMPACT_PARAGRAPHS': '1'}):
                self.assertTrue(Settings.from_env().daily_compact_paragraphs)
            with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_COMPACT_PARAGRAPHS': '0'}):
                self.assertFalse(Settings.from_env().daily_compact_paragraphs)

    def test_model_only_changes_prose_and_retains_layout(self):
        model = Mock(model='mock')
        model.rewrite_paragraphs.return_value = ['Guest认为需求可能增长10倍。', '团队建立评测。']
        result = compact_paragraphs(self.source, model)
        original = self.source.split('\n\n')[1]
        self.assertEqual(result, self.source.replace(original, 'Guest认为需求可能增长10倍。\n\n团队建立评测。'))
        model.rewrite_paragraphs.assert_called_once()
        self.assertNotIn('推荐理由', str(model.rewrite_paragraphs.call_args))
        rendered = reader_episode(self.source, rewriter=model, compact=True)
        self.assertNotIn('• ', rendered)
        self.assertNotIn('Show｜Show', rendered)

    def test_invalid_model_falls_back_deterministically(self):
        for bad in ('Guest认为需求可能增长。', '需求增长10倍。', 'Guest认为需求可能增长11倍。', '• Guest认为需求可能增长10倍。'):
            model = Mock(model='mock')
            model.rewrite_paragraphs.return_value = [bad]
            audit = []
            result = compact_paragraphs(self.source, model, audit)
            self.assertEqual(result, compact_paragraphs(self.source))
            self.assertTrue(audit[0]['fallback'])
            self.assertEqual(audit[0]['missing_numbers'], [])
            self.assertEqual(model.rewrite_paragraphs.call_count, 2)
            self.assertIn('Guest认为需求可能增长10倍。', result)

    def test_transport_failure_preserves_conditional_sentences(self):
        model = Mock()
        model.rewrite_paragraphs.side_effect = RuntimeError('offline')
        source = '若Model 1.3能提高50到100倍，Guest称可能有益。否则仍需等待两到四天。其他讨论。'
        result = compact_paragraphs(source, model)
        self.assertEqual(source_numbers(source), source_numbers(result))
        self.assertIn('若Model 1.3能提高50到100倍，Guest称可能有益。', result)

    def test_model_request_uses_only_prose_and_explicit_character_budgets(self):
        from news_officer.summarizer import TranscriptSummarizer

        with patch('news_officer.summarizer.OpenAI') as client:
            model = TranscriptSummarizer('mock-key', 'mock-model')
            client.return_value.responses.create.return_value.output_text = '{"paragraphs":["short"]}'
            self.assertEqual(model.rewrite_paragraphs(['long paragraph'], []), ['short'])
            kwargs = client.return_value.responses.create.call_args.kwargs
            self.assertFalse(kwargs['store'])
            self.assertIn('target_character_counts', kwargs['input'])

    def test_ratio_budget(self):
        import json

        from news_officer.summarizer import TranscriptSummarizer
        with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_COMPACT_TARGET_RATIO': '0.6'}), patch('news_officer.summarizer.OpenAI') as client:
            client.return_value.responses.create.return_value.output_text = '{"paragraphs":["short"]}'
            TranscriptSummarizer('key', 'mock').rewrite_paragraphs(['x' * 100], [])
            payload = json.loads(client.return_value.responses.create.call_args.kwargs['input'])
            self.assertEqual(payload['target_character_counts'], [60])
            self.assertEqual(payload['allowed_total_character_count'], [51, 69])
            TranscriptSummarizer('key', 'mock').rewrite_paragraphs(['产品不仅能运行，还能测试。'], [])
            payload = json.loads(client.return_value.responses.create.call_args.kwargs['input'])
            self.assertNotIn('仅', payload['required_qualifiers'][0])

    def test_blank_lines_only_when_compact(self):
        source = self.source.replace('时长：20 分钟\n\n', '时长：20 分钟\n\n\n').replace('\n阅读建议', '\n\n阅读建议')
        result = reader_episode(source, compact=True)
        self.assertIn('时长：20 分钟\n推荐理由：有具体评测。\n阅读建议：值得看全文\n\nGuest', result)
        self.assertIn('推荐理由：有具体评测。\n阅读建议', result)
        self.assertIn('时长：20 分钟\n\n\n', reader_episode(source))

    def test_compact_episode_order_with_two_paragraphs_and_links(self):
        model = Mock(model='mock')
        paragraph = 'Guest认为需求可能增长10倍。团队建立评测。'
        source = self.source.replace('\n\n推荐理由', '\n\n' + self.source.split('\n\n')[1] + '\n\n推荐理由')
        model.rewrite_paragraphs.return_value = [paragraph, paragraph]
        result = reader_episode(source, 'https://example.test/doc', compact=True, rewriter=model)
        expected = ('## Guest｜产品\n**节目：Show**\n时长：20 分钟\n'
                    '推荐理由：有具体评测。\n阅读建议：值得看全文\n\n'
                    + paragraph + '\n\n' + paragraph + '\n\n'
                    '[精读和完整中文对谈](https://example.test/doc)\n\n'
                    '[收听节目](https://example.test/listen)')
        self.assertEqual(result, expected)

    def test_conditional_connectors(self):
        from news_officer.daily_report import validate_conditionals
        for clipped in ('测试能降低风险则合理。', '测试若降低风险合理。', '测试若降低风险则合理。'):
            with self.assertRaises(ValueError):
                validate_conditionals('测试若能降低风险则合理。', clipped)
        validate_conditionals('测试若能降低风险则合理。', '测试若能降低风险则合理。')

    def test_length_failure_retries_at_default_ratio(self):
        model = Mock(model='mock')
        model.rewrite_paragraphs.side_effect = [[self.source.split('\n\n')[1], '背景。'], ['Guest认为需求可能增长10倍。', '团队建立评测。']]
        audit = []
        compact_paragraphs(self.source, model, audit)
        self.assertEqual(model.rewrite_paragraphs.call_count, 2)
        self.assertFalse(audit[0]['fallback'])
        self.assertEqual(audit[0]['target_ratio'], 0.4)
        self.assertIn('ratio 0.4', audit[0]['validation_errors'][0])

    def test_fallback_preserves_separate_conditional_result(self):
        source = '团队讨论流程。测试若能降低风险则合理。否则仍需等待。其他背景。'
        model = Mock()
        model.rewrite_paragraphs.side_effect = RuntimeError('offline')
        result = compact_paragraphs(source, model)
        self.assertIn('测试若能降低风险则合理。否则仍需等待。', result)

    def test_episode_ratio_allows_uneven_paragraph_budgets(self):
        source = 'Guest认为流程复杂，团队持续研究各种技术，逐步完善方案。\n\n团队持续研究各种技术，逐步完善方案，讨论经验和适用范围。'
        model = Mock(model='mock')
        model.rewrite_paragraphs.return_value = ['Guest认为团队逐步完善方案。', '团队讨论经验。']
        audit = []
        result = compact_paragraphs(source, model, audit)
        self.assertFalse(audit[0]['fallback'])
        self.assertIn('\n\n', result)

    def test_required_names_allow_secondary_examples_to_be_omitted(self):
        from news_officer.daily_report import required_prose_names
        self.assertEqual(required_prose_names('Guest认为ToolA和ToolB是案例。产品Model 1.3有10个用途。'), ['Guest', 'Model 1'])

    def test_conditional_modal_is_scoped_and_not_part_of_a_noun(self):
        from news_officer.daily_report import validate_conditionals
        validate_conditionals('软件能运行。若风险降低，则合理。', '若风险降低，则合理。')
        validate_conditionals('若模型提升设计能力，则有收益。', '若模型改善设计，则有收益。')
        with self.assertRaises(ValueError):
            validate_conditionals('若能改善就好；若能降低风险则合理。', '若能降低风险则合理。')

    def test_generic_verb_phrase_clipping_is_rejected(self):
        from news_officer.daily_report import validate_unclipped_prose
        with self.assertRaises(ValueError):
            validate_unclipped_prose('团队需要提高质量。', '团队需要提量。')
        validate_unclipped_prose('团队需要提高质量。', '团队能提高质量。')

    def test_modal_in_result_does_not_change_antecedent(self):
        from news_officer.daily_report import validate_conditionals
        validate_conditionals('若模型提升设计能力，措施不能只看软件。', '若模型改善设计，则须检查软件。')

    def test_distinct_claim_ownership_counts(self):
        from news_officer.daily_report import claim_owner_counts
        self.assertEqual(claim_owner_counts('Guest认为流程合理。Company称有优势，但Guest也强调仍有风险。'), {'Guest': 2, 'Company': 1})

    def test_conditional_duration_modifier_keeps_function_word(self):
        from news_officer.daily_report import validate_conditionals
        with self.assertRaises(ValueError):
            validate_conditionals('三天的观察若能提高质量则有益。', '三天观察若能提高质量则有益。')
        validate_conditionals('三天的观察若能提高质量则有益。', '三天的观察若能提高质量则有益。')

    def test_restricted_scope_hedge_is_retained(self):
        source = 'Guest认为这仅是一种假设。' + '背景讨论。' * 8
        model = Mock(model='mock')
        model.rewrite_paragraphs.return_value = ['Guest认为这是一种假设。', '团队仍需验证。']
        audit = []
        compact_paragraphs(source, model, audit)
        self.assertTrue(audit[0]['fallback'])
        self.assertIn('Retain attribution/hedging: 仅', audit[0]['validation_errors'][0])
