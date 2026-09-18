import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from news_officer.daily_report import coverage_report
from news_officer.editorial import Assessment, EditorialPolicy, active_companies, decide
from news_officer.models import DailyItem, Episode, Transcript
from news_officer.podcast import PodcastService
from news_officer.store import Store
from news_officer.summarizer import _valid_editorial_summary

TEXT = 'ExampleCo describes model inference costs and enterprise customer retention with concrete figures.'
PROFILE = [{'name': 'ExampleCo', 'aliases': ['Example Company'], 'expires_on': '2026-12-31'}]
SUMMARY = '推荐理由：这期讨论推理成本与企业客户留存。\n\n1. 推理成本影响企业客户的单位经济模型。\n\n推荐星级：★★★★★（5/5，编辑推荐）'


def assessment(**scores):
    return Assessment.model_validate({
        **{key: {'score': scores.get(key, 4), 'quote': TEXT}
           for key in ('ai', 'investment', 'focus', 'novelty', 'evidence')},
        'focus_company': 'ExampleCo', 'reason': '这期具体分析推理成本如何影响企业客户留存，能帮助检验商业模式。',
    })


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

    def test_high_quality_without_ai_or_investment_is_not_selected(self):
        for weak in ('ai', 'investment'):
            decision = decide(assessment(**{weak: 2, 'novelty': 5, 'evidence': 5}), TEXT, PROFILE)
            self.assertFalse(decision['selected'])
        self.assertFalse(decide(assessment(ai=3, investment=5, focus=5, novelty=5, evidence=5), TEXT, PROFILE)['selected'])

    def test_weights_thresholds_and_five_star_quality_cap(self):
        decision = decide(assessment(), TEXT, PROFILE)
        self.assertEqual((decision['total'], decision['stars'], decision['selected']), (80, 4, True))
        decision = decide(assessment(ai=5, investment=5, focus=5, novelty=5, evidence=5), TEXT, PROFILE)
        self.assertEqual((decision['total'], decision['stars']), (100, 5))
        unfocused = decide(assessment(ai=5, investment=5, focus=5, novelty=5, evidence=5), TEXT, [])
        self.assertEqual((unfocused['total'], unfocused['stars'], unfocused['selected']), (95, 5, True))
        decision = decide(assessment(ai=5, investment=5, focus=5, novelty=5, evidence=3), TEXT, PROFILE)
        self.assertEqual(decision['total'], 94)
        self.assertEqual(decision['stars'], 4)

    def test_incidental_unknown_and_substring_company_get_no_bonus(self):
        for value, profile in [(assessment(focus=1), PROFILE), (assessment(), []),
                               (assessment(), [{'name': 'ExampleCo', 'aliases': ['AI']}])]:
            text = TEXT if profile != [{'name': 'ExampleCo', 'aliases': ['AI']}] else TEXT.replace('ExampleCo', 'NotExampleCoXYZ')
            if text != TEXT:
                value.focus.quote = text
                for key in ('ai', 'investment', 'novelty', 'evidence'):
                    getattr(value, key).quote = text
            decision = decide(value, text, profile)
            self.assertEqual(decision['assessment']['focus']['score'], 0)

    def test_invented_evidence_and_unverified_transcript_rejected(self):
        value = assessment()
        value.ai.quote = 'This evidence does not exist in the transcript.'
        with self.assertRaises(ValueError):
            decide(value, TEXT, PROFILE)
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
        invalid = assessment()
        invalid.ai.quote = 'This invented quotation does not exist in the source.'
        client = Mock()
        client.responses.create.side_effect = [
            SimpleNamespace(output_text=invalid.model_dump_json()),
            SimpleNamespace(output_text=assessment().model_dump_json()),
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

    def test_score_and_reason_override_freeform_stars_with_valid_format(self):
        decision = decide(assessment(), TEXT, PROFILE)
        summary = EditorialPolicy.apply(SUMMARY, decision)
        self.assertTrue(_valid_editorial_summary(summary))
        self.assertIn('AI 32/40', summary)
        self.assertIn('★★★★☆（4/5', summary)

    def test_audit_cache_changes_when_transcript_or_profile_changes(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=assessment().model_dump_json())
        path = self.root / 'focus.json'
        path.write_text(json.dumps({'companies': PROFILE}))
        policy = EditorialPolicy(client, 'test-model', self.store, path)
        first = policy.assess(self.episode, self.transcript)
        request = client.responses.create.call_args.kwargs
        self.assertIn('json', request['input'].lower())
        self.assertFalse(request['store'])
        self.assertEqual(policy.assess(self.episode, self.transcript), first)
        self.assertEqual(client.responses.create.call_count, 1)
        path.write_text(json.dumps({'companies': []}))
        self.assertEqual(policy.assess(self.episode, self.transcript)['assessment']['focus']['score'], 0)
        self.assertEqual(client.responses.create.call_count, 2)
        changed = Transcript(TEXT + ' More material.', 'official', self.transcript.source_url, True)
        policy.assess(self.episode, changed)
        self.assertEqual(client.responses.create.call_count, 3)

    def test_daily_filter_skips_summary_but_preserves_fulltext_and_continues(self):
        policy = Mock()
        rejected = decide(assessment(ai=1), TEXT, PROFILE)
        accepted = decide(assessment(), TEXT, PROFILE)
        policy.assess.side_effect = [rejected, accepted]
        policy.apply.side_effect = EditorialPolicy.apply
        summarizer = Mock()
        summarizer.summarize.return_value = SUMMARY
        resolver = Mock()
        resolver.fetch.return_value = self.transcript
        service = PodcastService(self.store, self.root / 'feeds.json', summarizer,
                                 resolver, editorial_policy=policy, max_daily_summaries=1)
        second = Episode('second', 'Accepted', 'https://example.org/2', 'Show',
                         published_at=datetime.now(UTC), duration_seconds=300)
        service.discover_daily_candidates = Mock(return_value=[self.episode, second])
        items = service.build_daily()
        self.assertEqual([item.status for item in items], ['not_recommended', 'summarized'])
        self.assertEqual(summarizer.summarize.call_count, 1)
        self.assertIsNotNone(self.store.get_verified_transcript('e'))
        self.assertIn('★★★★☆', items[1].message)
        self.assertIn('未达到', coverage_report(items))

    def test_filtered_report_is_not_no_updates_or_missing_transcript(self):
        report = coverage_report([DailyItem(self.episode, 'not_recommended')])
        self.assertIn('1 期已有全文', report)
        self.assertNotIn('未取得完整文字稿', report)
        self.assertNotIn('今日无', report)
