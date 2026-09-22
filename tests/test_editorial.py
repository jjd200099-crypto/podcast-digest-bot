import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from news_officer.daily_report import coverage_report
from news_officer.editorial import (
    Assessment,
    EditorialPolicy,
    SourceAssessment,
    active_companies,
    decide,
    resolve_evidence,
    transcript_blocks,
)
from news_officer.models import DailyItem, Episode, Transcript
from news_officer.podcast import PodcastService
from news_officer.store import Store
from news_officer.summarizer import _valid_editorial_summary

TEXT = 'ExampleCo describes model inference costs and enterprise customer retention with concrete figures.'
DETAIL = 'The research team explains its experiment design and the failure cases behind its revised training method.'
FULL_TEXT = TEXT + ' ' + DETAIL
PROFILE = [{'name': 'ExampleCo', 'aliases': ['Example Company'], 'expires_on': '2026-12-31'}]
SUMMARY = '推荐理由：这期讨论推理成本与企业客户留存。\n\n1. 推理成本影响企业客户的单位经济模型。\n\n推荐星级：★★★★★（5/5，编辑推荐）'


def assessment(**scores):
    return Assessment.model_validate({
        **{key: {'score': scores.get(key, 4), 'quotes': [TEXT, DETAIL]}
           for key in ('relevance', 'density')},
        'reason': '这期具体分析推理成本如何影响企业客户留存，能帮助检验商业模式。',
    })


def source_assessment(**scores):
    value = assessment(**scores).model_dump()
    for key in ('relevance', 'density'):
        value[key] = {'score': value[key]['score'], 'evidence_ids': [1]}
    value['relevance']['priority'] = 'none'
    return SourceAssessment.model_validate(value)


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.store.initialize()
        self.episode = Episode('e', 'Test', 'https://example.org/e', 'Show',
                               published_at=datetime.now(UTC), duration_seconds=300)
        self.transcript = Transcript(TEXT, 'official', 'https://example.org/transcript', True)

    def test_either_low_dimension_prevents_selection(self):
        for weak in ('relevance', 'density'):
            decision = decide(assessment(**{weak: 2}), FULL_TEXT)
            self.assertFalse(decision['selected'])

    def test_two_dimensions_and_exceptional_double_five_gate(self):
        for relevance, density, stars in [(5, 5, 5), (5, 4, 4), (4, 5, 4),
                                           (4, 4, 4), (3, 5, 4), (5, 3, 3),
                                           (3, 3, 3), (5, 2, 2), (2, 5, 2), (1, 5, 1)]:
            with self.subTest(relevance=relevance, density=density):
                decision = decide(assessment(relevance=relevance, density=density), FULL_TEXT)
                self.assertEqual(decision['stars'], stars)
                self.assertEqual(decision['total'], (relevance + density) * 10)
                self.assertEqual(decision['selected'], stars >= 3)

    def test_private_company_profile_cannot_add_a_third_score(self):
        self.assertEqual(decide(assessment(), FULL_TEXT, PROFILE), decide(assessment(), FULL_TEXT, []))
        value = assessment().model_dump()
        value['focus'] = {'score': 5, 'quotes': [TEXT]}
        with self.assertRaises(ValueError):
            Assessment.model_validate(value)

    def test_priority_topics_have_full_read_floor_without_inflating_density(self):
        for topic in ('model_lab', 'ai_unicorn_founder'):
            for density in range(6):
                value = assessment(relevance=5, density=density)
                value.relevance.priority = topic
                decision = decide(value, FULL_TEXT)
                self.assertTrue(decision['selected'])
                self.assertEqual(decision['stars'], 5 if density == 5 else 4)
                self.assertEqual(decision['assessment']['density']['score'], density)

    def test_priority_requires_valid_relevance_evidence(self):
        value = assessment(relevance=1, density=4)
        value.relevance.priority = 'model_lab'
        with self.assertRaises(ValueError):
            decide(value, FULL_TEXT)
        value.relevance.score = 5
        value.relevance.quotes = ['OpenAI source evidence invented outside the transcript']
        with self.assertRaises(ValueError):
            decide(value, FULL_TEXT)

    def test_source_priority_is_preserved_and_required(self):
        value = source_assessment(relevance=5, density=2)
        value.relevance.priority = 'model_lab'
        resolved = resolve_evidence(value, [TEXT])
        self.assertEqual(resolved.relevance.priority, 'model_lab')
        self.assertEqual(decide(resolved, TEXT)['stars'], 4)
        raw = value.model_dump()
        raw['relevance'].pop('priority')
        with self.assertRaises(ValueError):
            SourceAssessment.model_validate(raw)
        raw['relevance']['priority'] = 'celebrity'
        with self.assertRaises(ValueError):
            SourceAssessment.model_validate(raw)

    def test_density_five_needs_two_distinct_source_excerpts(self):
        value = assessment(density=5)
        for quotes in ([TEXT], [TEXT, TEXT]):
            value.density.quotes = quotes
            with self.assertRaises(ValueError):
                decide(value, FULL_TEXT)

    def test_invented_evidence_and_unverified_transcript_rejected(self):
        value = assessment()
        value.relevance.quotes = ['This evidence does not exist in the transcript.']
        with self.assertRaises(ValueError):
            decide(value, FULL_TEXT)
        client = Mock()
        policy = EditorialPolicy(client, 'test', self.store)
        with self.assertRaises(ValueError):
            policy.assess(self.episode, Transcript(TEXT, 'partial', 'https://example.org', False))
        client.responses.create.assert_not_called()

    def test_profile_expires_and_missing_file_does_not_silently_disable(self):
        path = self.root / 'focus.json'
        path.write_text(json.dumps({'companies': PROFILE}))
        self.assertEqual(len(active_companies(path, date(2026, 12, 31))), 1)
        self.assertEqual(active_companies(path, date(2027, 1, 1)), [])
        with self.assertRaises(FileNotFoundError):
            active_companies(self.root / 'missing.json')

    def test_invalid_evidence_has_one_bounded_repair_and_never_gets_cached(self):
        invalid = source_assessment()
        invalid.relevance.evidence_ids = [999]
        client = Mock()
        client.responses.create.side_effect = [
            SimpleNamespace(output_text=invalid.model_dump_json()),
            SimpleNamespace(output_text=source_assessment().model_dump_json()),
        ]
        policy = EditorialPolicy(client, 'repair', self.store)
        self.assertTrue(policy.assess(self.episode, self.transcript)['selected'])
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertIn('validation_feedback', client.responses.create.call_args.kwargs['input'])
        client.responses.create.side_effect = None
        client.responses.create.return_value = SimpleNamespace(output_text=invalid.model_dump_json())
        policy = EditorialPolicy(client, 'always-invalid', self.store)
        client.responses.create.reset_mock()
        with self.assertRaisesRegex(ValueError, 'Editorial review failed'):
            policy.assess(self.episode, self.transcript)
        self.assertEqual(client.responses.create.call_count, 2)
        with self.assertRaises(ValueError):
            policy.assess(self.episode, self.transcript)
        self.assertEqual(client.responses.create.call_count, 4)

    def test_source_blocks_are_lossless_and_quotes_are_extracted_not_generated(self):
        text = (TEXT + '\n有中文及引号“AI”\t') * 25
        blocks = transcript_blocks(text)
        self.assertEqual(''.join(blocks), text)
        self.assertTrue(all(len(block) <= 450 for block in blocks))
        result = resolve_evidence(source_assessment(), blocks)
        self.assertEqual(result.relevance.quotes, [blocks[0]])
        invalid = source_assessment()
        for ids in ([0], [1, 1], [len(blocks) + 1], []):
            invalid.relevance.evidence_ids = ids
            with self.assertRaises(ValueError):
                resolve_evidence(invalid, blocks)
        for ids in ([True], ['1'], [0]):
            value = source_assessment().model_dump()
            value['relevance']['evidence_ids'] = ids
            with self.assertRaises(ValueError):
                SourceAssessment.model_validate(value)

    def test_score_and_reason_override_freeform_stars_with_valid_format(self):
        decision = decide(assessment(), FULL_TEXT)
        summary = EditorialPolicy.apply(SUMMARY, decision)
        self.assertTrue(_valid_editorial_summary(summary))
        self.assertNotIn('/40', summary)
        self.assertNotIn('研究关联', summary)
        self.assertTrue(summary.endswith('推荐星级：★★★★☆'))

    def test_audit_cache_tracks_transcript_not_private_company_profile(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=source_assessment().model_dump_json())
        path = self.root / 'focus.json'
        path.write_text(json.dumps({'companies': PROFILE}))
        policy = EditorialPolicy(client, 'test-model', self.store, path)
        first = policy.assess(self.episode, self.transcript)
        request = client.responses.create.call_args.kwargs
        self.assertIn('json', request['input'].lower())
        self.assertFalse(request['store'])
        self.assertNotIn('active_focus_companies', request['input'])
        self.assertEqual(policy.assess(self.episode, self.transcript), first)
        self.assertEqual(client.responses.create.call_count, 1)
        path.write_text(json.dumps({'companies': []}))
        self.assertEqual(policy.assess(self.episode, self.transcript), first)
        self.assertEqual(client.responses.create.call_count, 1)
        changed = Transcript(TEXT + ' More material.', 'official', self.transcript.source_url, True)
        policy.assess(self.episode, changed)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_daily_retains_low_rating_and_sorts_high_rating_first(self):
        policy = Mock()
        rejected = decide(assessment(relevance=1), FULL_TEXT)
        accepted = decide(assessment(), FULL_TEXT)
        policy.assess.side_effect = [rejected, accepted]
        policy.apply.side_effect = EditorialPolicy.apply
        summarizer = Mock()
        summarizer.summarize.return_value = SUMMARY
        resolver = Mock()
        resolver.fetch.return_value = self.transcript
        service = PodcastService(self.store, self.root / 'feeds.json', summarizer,
                                 resolver, editorial_policy=policy, max_daily_summaries=0)
        second = Episode('second', 'Accepted', 'https://example.org/2', 'Show',
                         published_at=datetime.now(UTC), duration_seconds=300)
        service.discover_daily_candidates = Mock(return_value=[self.episode, second])
        items = service.build_daily()
        self.assertEqual([item.status for item in items], ['summarized', 'summarized'])
        self.assertEqual([item.episode.id for item in items], ['second', 'e'])
        self.assertEqual(summarizer.summarize.call_count, 2)
        self.assertIsNotNone(self.store.get_verified_transcript('e'))
        self.assertIn('★★★★☆', items[0].message)
        self.assertIsNone(coverage_report(items))

    def test_filtered_report_is_not_no_updates_or_missing_transcript(self):
        report = coverage_report([DailyItem(self.episode, 'not_recommended')])
        self.assertIn('1 期已有全文', report)
        self.assertNotIn('未取得完整文字稿', report)
        self.assertNotIn('今日无', report)
