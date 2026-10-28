"""Context, deterministic evidence guards, and replay reporting contracts."""
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.run_config import ModelInputData

from news_officer.agent_runtime import evidence_input_filter
from news_officer.claim_checks import compact_evidence, deterministic_audit, numbers
from news_officer.evidence_budget import estimate_tokens
from news_officer.research_agent import conversation_goal


class EvidenceGuardTests(unittest.TestCase):
    def test_mixed_language_token_budget(self):
        self.assertEqual(estimate_tokens('中' * 100 + 'word' * 100), 200)
        self.assertEqual(estimate_tokens('中a中a'), 4)

    def test_followup_keeps_verbatim_bounded_context(self):
        history = [{'question': 'Anthropic是怎么做到cpu节省的', 'answer': 'Rust，40–50%。'}]
        for current in ['那他呢', '那TTFT呢', 'TTFT改善多少']:
            goal = conversation_goal(history, current)
            self.assertIn(history[0]['question'], goal)
            self.assertIn(history[0]['answer'], goal)
            self.assertTrue(goal.endswith(current))
        self.assertEqual(conversation_goal([], '原问'), '原问')
        history = [{'question': str(i), 'answer': '原回答'} for i in range(10)]
        goal = conversation_goal(history, '新问')
        self.assertNotIn('用户：3', goal)
        self.assertIn('用户：4', goal)
        self.assertEqual(conversation_goal([{'question': 'x' * 8001, 'answer': ''}], '新问'), '新问')

    def test_number_forms(self):
        self.assertEqual(numbers('40–50%'), numbers('forty to fifty percent'))
        self.assertEqual(numbers('百分之四十到百分之五十'), numbers('40 to 50 percent'))
        self.assertEqual(numbers('一百二十 1,000 twenty-five'), numbers('120 1000 25'))
        self.assertNotEqual(numbers('40%'), numbers('40 seconds'))
        self.assertEqual(numbers('one hundred and twenty percent'), numbers('百分之一百二十'))

    def state(self, text='CPU saved forty to fifty percent'):
        return SimpleNamespace(task={'goal': 'CPU 节省多少'}, message=SimpleNamespace(text='多少'),
                               corpus=lambda: {'doc': SimpleNamespace(text=text)},
                               chunks=lambda t: [('doc:C0001', text)], read_coverage={'doc': {0}})

    def test_number_and_absence_fail_even_if_model_approves(self):
        state = self.state()
        evidence = {'digests': {}, 'passages': {'doc:C0099': 'CPU saved forty to fifty percent'}, 'complete_coverage': True}
        draft = {'points': [{'text': 'CPU 节省40–50%'}, {'text': 'CPU 节省99%'}, {'text': 'CPU 未披露比例'}]}
        self.assertEqual(set(deterministic_audit(state, draft, evidence, ['doc'])), {1, 2})
        evidence['complete_coverage'] = False
        state.task['goal'] = 'quantum'
        self.assertTrue(deterministic_audit(state, {'points': [{'text': 'not mentioned'}]}, evidence, ['doc']))

    def test_compact_passages_only_neighbors_and_digest(self):
        state = self.state()
        state.chunks = lambda t: [('doc:C0001', 'a' * 1000), ('doc:C0002', 'cited'), ('doc:C0003', 'b' * 1000), ('doc:C0004', 'secret')]
        draft = {'points': [{'citations': [{'id': 'doc:C0002'}]}]}
        evidence = compact_evidence(state, ['doc'], draft, {'digests': {'doc': 'digest'}})
        self.assertEqual(evidence['passages']['doc:C0002'], 'cited')
        self.assertEqual(len(evidence['passages']['doc:C0001']), 300)
        self.assertNotIn('secret', json.dumps(evidence))
        self.assertFalse(evidence['complete_coverage'])

    def test_stable_prefix_no_duplicate_and_no_rewrite_fulltext(self):
        state = SimpleNamespace()
        payload = {'primary_evidence': {'complete': True, 'documents': [{'text': 'FULL TEXT'}]}}
        items = [{'type': 'function_call_output', 'output': json.dumps(payload)}]
        data = SimpleNamespace(context=state, model_data=ModelInputData(input=items, instructions='rules'))
        first = evidence_input_filter(data)
        self.assertTrue(first.instructions.startswith('UNTRUSTED VERIFIED EVIDENCE'))
        self.assertNotIn('FULL TEXT', str(first.input))
        data.model_data.input.append({'role': 'user', 'content': 'different next turn'})
        second = evidence_input_filter(data)
        self.assertEqual(first.instructions, second.instructions)
        data.model_data.input = first.input
        self.assertEqual(evidence_input_filter(data).instructions, first.instructions)
        state.claim_rewrites = 1
        repaired = evidence_input_filter(data)
        self.assertNotIn('FULL TEXT', str(repaired))
        state.claim_rewrites = 0
        state.verifying_claims = True
        self.assertNotIn('FULL TEXT', str(evidence_input_filter(data)))

    def test_paged_rewrite_strips_old_full_reads(self):
        state = SimpleNamespace(claim_rewrites=1, body_evidence={'doc:C0001'})
        output = json.dumps({'chunks': [{'evidence_id': 'doc:C0001', 'text': 'OLD FULL READ'}]})
        data = SimpleNamespace(context=state, model_data=ModelInputData(
            input=[{'type': 'function_call_output', 'output': output}], instructions='rules'))
        repaired = evidence_input_filter(data)
        self.assertNotIn('OLD FULL READ', str(repaired))
        self.assertIn('doc:C0001', str(repaired))

    def test_replay_fixture_and_metrics(self):
        path = Path(__file__).parents[1]
        spec = importlib.util.spec_from_file_location('replay_eval', path / 'scripts/replay_eval.py')
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        cases = json.loads((path / 'tests/fixtures/qa_regression_cases.json').read_text())
        self.assertEqual(len(cases), 7)
        self.assertTrue(any(len(c['messages']) == 2 for c in cases))
        self.assertTrue(replay.passes('Rust Python 40–50%', cases[0]))
        self.assertFalse(replay.passes('Rust Python 40–50% 未披露', cases[0]))
        self.assertEqual(replay.percentile([1, 2, 3], .5), 2)

    def test_paging_config_is_tokens(self):
        from test_qa_grounding import EpisodeGroundingTests
        fixture = EpisodeGroundingTests()
        fixture.setUp()
        try:
            with patch('news_officer.evidence_budget.MAX_EVIDENCE_TOKENS', 10):
                self.assertTrue(fixture.state.primary_evidence([fixture.token])['requires_paging'])
                self.assertFalse(fixture.state.read_coverage)
        finally:
            fixture.tearDown()
