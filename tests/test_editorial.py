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

    def test_daily_archives_but_does_not_display_unrelated_tracked_episodes(self):
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
        self.assertEqual([item.status for item in items], ['summarized', 'editorial_filtered'])
        self.assertEqual([item.episode.id for item in items], ['second', 'e'])
        self.assertEqual(summarizer.summarize.call_count, 1)
        self.assertIsNotNone(self.store.get_verified_transcript('e'))
        self.assertIn('★★★★☆', items[0].message)
        self.assertIn('关注列表另有 1 期', coverage_report(items))
        self.assertFalse(self.store.should_review_episode('e'))
        self.assertFalse(self.store.episode_is_delivered(self.episode))

    def test_filtered_report_is_not_no_updates_or_missing_transcript(self):
        report = coverage_report([DailyItem(self.episode, 'not_recommended')])
        self.assertIn('1 期已有全文', report)
        self.assertNotIn('未取得完整文字稿', report)
        self.assertNotIn('今日无', report)

    def test_strict_priority_does_not_retry_or_inflate_low_relevance(self):
        raw = source_assessment(relevance=2, density=4).model_dump()
        raw['relevance']['priority'] = 'model_lab'
        raw.update(ai_content_share='high', evidence_quality='high', reading_recommendation='值得看全文')
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=json.dumps(raw))
        policy = EditorialPolicy(client, 'strict', self.store, strict=True)
        result = policy.assess(self.episode, self.transcript)
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(result['assessment']['relevance']['priority'], 'none')
        self.assertEqual(result['stars'], 2)
        self.assertFalse(result['selected'])
        self.assertNotIn('命中者至少', client.responses.create.call_args.kwargs['instructions'])
        legacy = EditorialPolicy(client, 'strict', self.store, strict=False)
        with self.assertRaises(ValueError):
            legacy.assess(self.episode, self.transcript)
        self.assertEqual(client.responses.create.call_count, 3)

    def test_strict_evidence_and_reading_judgement_override_priority_floor(self):
        from news_officer.daily_report import render_daily_summary
        from news_officer.editorial import StrictAssessment
        for quality, advice, stars in [('low', '值得看全文', 2),
                                      ('mixed', '看摘要', 3),
                                      ('high', '值得看全文', 3)]:
            raw = assessment(relevance=5, density=2).model_dump()
            raw['relevance']['priority'] = 'model_lab'
            raw.update(ai_content_share='high', evidence_quality=quality, reading_recommendation=advice)
            result = decide(StrictAssessment.model_validate(raw), FULL_TEXT, strict=True)
            self.assertEqual(result['stars'], stars)
            rendered = render_daily_summary(EditorialPolicy.apply(SUMMARY, result))
            if stars <= 3:
                self.assertNotIn('值得看全文', rendered)
            else:
                self.assertIn('阅读建议：值得看全文', rendered)
            self.assertEqual(decide(assessment(relevance=5, density=2), FULL_TEXT)['stars'], 2)

    def test_strict_fields_are_required_and_reprompted(self):
        from news_officer.editorial import StrictAssessment, StrictSourceAssessment
        for field in ('evidence_quality', 'reading_recommendation', 'ai_content_share'):
            for schema, raw in ((StrictAssessment, assessment().model_dump()),
                                (StrictSourceAssessment, source_assessment().model_dump())):
                raw.update(ai_content_share='high', evidence_quality='high', reading_recommendation='值得看全文')
                raw.pop(field)
                with self.assertRaises(ValueError):
                    schema.model_validate(raw)
            valid = source_assessment().model_dump()
            valid.update(ai_content_share='high', evidence_quality='high', reading_recommendation='值得看全文')
            invalid = dict(valid)
            invalid.pop(field)
            client = Mock()
            client.responses.create.side_effect = [SimpleNamespace(output_text=json.dumps(v))
                                                   for v in (invalid, valid)]
            policy = EditorialPolicy(client, field, self.store, strict=True)
            self.assertTrue(policy.assess(self.episode, self.transcript)['selected'])
            self.assertEqual(client.responses.create.call_count, 2)
            feedback = client.responses.create.call_args.kwargs['input']
            for required in ('evidence_quality', 'reading_recommendation', 'ai_content_share'):
                self.assertIn(required, feedback)

    def test_strict_contradictory_evidence_reprompts(self):
        invalid = source_assessment(density=3).model_dump()
        invalid.update(ai_content_share='high', evidence_quality='low', reading_recommendation='值得看全文')
        valid = {**invalid, 'evidence_quality': 'high'}
        client = Mock()
        client.responses.create.side_effect = [SimpleNamespace(output_text=json.dumps(v))
                                               for v in (invalid, valid)]
        result = EditorialPolicy(client, 'contradiction', self.store, strict=True).assess(
            self.episode, self.transcript)
        self.assertEqual(result['stars'], 3)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_capped_failures_backfill_and_schedule_retry(self):
        from news_officer.summarizer import SummaryFormatError
        for editorial in (False, True):
            for error in (SummaryFormatError('invalid'), RuntimeError('offline')):
                with self.subTest(editorial=editorial, error=type(error).__name__):
                    policy = Mock() if editorial else None
                    if policy:
                        policy.assess.side_effect = [decide(assessment(relevance=r, density=d), FULL_TEXT)
                                                    for r, d in ((4, 4), (5, 5), (3, 3), (3, 3))]
                        policy.apply.side_effect = EditorialPolicy.apply
                    summarizer = Mock()
                    summarizer.summarize.side_effect = [error, SUMMARY]
                    resolver = Mock()
                    resolver.fetch.return_value = self.transcript
                    service = PodcastService(self.store, self.root / 'feeds.json', summarizer,
                                             resolver, editorial_policy=policy, max_daily_summaries=1)
                    prefix = f'{editorial}-{type(error).__name__}'
                    candidates = [Episode(prefix + str(i), str(i), f'https://example.org/{prefix}/{i}',
                                          'Show', published_at=datetime.now(UTC)) for i in range(4)]
                    service.discover_daily_candidates = Mock(return_value=candidates)
                    before = datetime.now(UTC)
                    items = service.build_daily()
                    self.assertEqual(summarizer.summarize.call_count, 2)
                    expected = candidates[0 if editorial else 1].id
                    self.assertEqual([i.episode.id for i in items if i.status == 'summarized'], [expected])
                    failed = candidates[1 if editorial else 0].id
                    with self.store._connect() as db:
                        row = db.execute('SELECT next_check_at FROM daily_transcript_backlog WHERE episode_id=?',
                                         (failed,)).fetchone()
                    delay = (datetime.fromisoformat(row[0]) - before).total_seconds()
                    self.assertGreaterEqual(delay, 1800)
                    self.assertLess(delay, 1810)

    def test_legacy_cap_summarizes_only_selected_candidates(self):
        summarizer = Mock()
        summarizer.summarize.return_value = SUMMARY
        resolver = Mock()
        resolver.fetch.return_value = self.transcript
        service = PodcastService(self.store, self.root / 'feeds.json', summarizer,
                                 resolver, max_daily_summaries=1)
        service.discover_daily_candidates = Mock(return_value=[
            Episode(str(i), str(i), f'https://example.org/{i}', 'Show',
                    published_at=datetime.now(UTC)) for i in range(4)])
        self.assertEqual([i.episode.id for i in service.build_daily()], ['0'])
        self.assertEqual(summarizer.summarize.call_count, 1)

    def test_cap_scores_all_candidates_then_selects_highest_stably(self):
        policy = Mock()
        policy.assess.side_effect = [decide(assessment(relevance=r, density=d), FULL_TEXT)
                                    for r, d in [(3, 3), (5, 5), (5, 5), (4, 4)]]
        policy.apply.side_effect = EditorialPolicy.apply
        summarizer = Mock()
        summarizer.summarize.return_value = SUMMARY
        resolver = Mock()
        resolver.fetch.return_value = self.transcript
        service = PodcastService(self.store, self.root / 'feeds.json', summarizer,
                                 resolver, editorial_policy=policy, max_daily_summaries=2)
        candidates = [Episode(str(i), str(i), f'https://example.org/{i}', 'Show',
                              published_at=datetime.now(UTC), duration_seconds=300)
                      for i in range(4)]
        service.discover_daily_candidates = Mock(return_value=candidates)
        items = service.build_daily()
        self.assertEqual([i.episode.id for i in items], ['1', '2'])
        self.assertEqual(policy.assess.call_count, 4)
        self.assertEqual(summarizer.summarize.call_count, 2)

    def test_flag_defaults_off_and_legacy_summary_bytes_are_unchanged(self):
        from unittest.mock import patch

        from news_officer.daily_report import render_daily_summary
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(EditorialPolicy(Mock(), 'test', self.store).strict)
        with patch.dict('os.environ', {'NEWS_OFFICER_STRICT_SELECTION': 'true'}):
            self.assertTrue(EditorialPolicy(Mock(), 'test', self.store).strict)
        decision = decide(assessment(), FULL_TEXT)
        self.assertEqual(decision, {'policy': 'relevance-density-priority-v4',
                                   'selected': True, 'total': 80, 'stars': 4,
                                   'assessment': assessment().model_dump()})
        expected = ('推荐理由：这期具体分析推理成本如何影响企业客户留存，能帮助检验商业模式。\n\n'
                    '1. 推理成本影响企业客户的单位经济模型。\n\n推荐星级：★★★★☆')
        self.assertEqual(EditorialPolicy.apply(SUMMARY, decision).encode(), expected.encode())
        self.assertEqual(render_daily_summary(expected).encode(),
                         expected.replace('推荐星级：★★★★☆', '阅读建议：值得看全文').encode())

    def test_strict_low_evidence_and_off_topic_cannot_be_selected_even_with_high_density(self):
        from news_officer.editorial import StrictAssessment
        for relevance, quality in [(5, 'low'), (2, 'high')]:
            raw = assessment(relevance=relevance, density=5).model_dump()
            raw['relevance']['priority'] = 'model_lab'
            raw.update(ai_content_share='high', evidence_quality=quality, reading_recommendation='值得编译')
            if quality == 'low':
                with self.assertRaises(ValueError):
                    decide(StrictAssessment.model_validate(raw), FULL_TEXT, strict=True)
                continue
            result = decide(StrictAssessment.model_validate(raw), FULL_TEXT, strict=True)
            self.assertLess(result['stars'], 3)
            self.assertFalse(result['selected'])

    def test_strict_small_ai_share_caps_priority_and_full_reading(self):
        from news_officer.daily_report import render_daily_summary
        from news_officer.editorial import (
            STRICT_POLICY_VERSION,
            STRICT_RUBRIC,
            StrictAssessment,
        )
        from news_officer.strict_selection import strict_document_allowed
        self.assertEqual(STRICT_POLICY_VERSION, 'strict-selection-v2')
        self.assertIn('ai_content_share', STRICT_RUBRIC)
        for priority in ('none', 'model_lab', 'ai_unicorn_founder'):
            for advice in ('值得看全文', '值得编译', '看摘要', '值得看摘要'):
                raw = assessment(relevance=5, density=5).model_dump()
                raw['relevance']['priority'] = priority
                raw.update(ai_content_share='low', evidence_quality='high', reading_recommendation=advice)
                result = decide(StrictAssessment.model_validate(raw), FULL_TEXT, strict=True)
                self.assertEqual(result['stars'], 3)
                self.assertEqual(result['assessment']['ai_content_share'], 'low')
                self.assertEqual(result['assessment']['reading_recommendation'], '值得看摘要')
                # apply also defends against an inconsistent incoming score.
                for stars in (3, 5):
                    summary = EditorialPolicy.apply(SUMMARY, {**result, 'stars': stars})
                    self.assertIn('推荐星级：★★★☆☆', summary)
                    self.assertIn('阅读建议：值得看摘要', render_daily_summary(summary))
                    self.assertNotIn('值得看全文', summary)
                    self.assertFalse(strict_document_allowed(summary))

    def test_default_rubric_retains_priority_full_read_floor(self):
        from news_officer.editorial import POLICY_VERSION, RUBRIC
        self.assertEqual(POLICY_VERSION, 'relevance-density-priority-v4')
        self.assertIn('命中者至少归为“值得看全文”', RUBRIC)
        self.assertNotIn('ai_content_share', RUBRIC)

    def test_strict_priority_floor_requires_density_three(self):
        from news_officer.editorial import StrictAssessment
        for priority in ('model_lab', 'ai_unicorn_founder'):
            for density in range(6):
                raw = assessment(relevance=5, density=density).model_dump()
                raw['relevance']['priority'] = priority
                raw.update(ai_content_share='high', evidence_quality='high', reading_recommendation='值得编译')
                result = decide(StrictAssessment.model_validate(raw), FULL_TEXT, strict=True)
                self.assertEqual(result['stars'], 5 if density == 5 else (4 if density >= 3 else 3))
                self.assertEqual(result['assessment']['density']['score'], density)
