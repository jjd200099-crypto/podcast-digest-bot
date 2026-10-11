import copy
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from news_officer.shownotes import (
    INSTRUCTIONS,
    EpisodeNotes,
    ShownotesWriter,
    validate_notes,
)


class ShownotesValidation(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            'C0001': {'text': 'The first exact quote.', 'time': '00:01'},
            'C0002': {'text': 'The second exact quote.', 'time': '00:02'},
            'C0003': {'text': 'The third exact quote.', 'time': '00:03'},
        }
        point = {'text': '嘉宾解释产品需求。', 'evidence_ids': ['C0001']}
        self.value = {
            'participants': '原稿未明确标注', 'participant_evidence_ids': [],
            'core': [copy.deepcopy(point) for _ in range(6)],
            'parts': [{'title': '需求', 'points': [point, point]}],
            'quotes': [], 'corrections': [],
        }

    def quote(self, text, key='C0001'):
        return {'text': text, 'speaker': 'guest', 'evidence_id': key, 'part_index': 0}

    def test_adjacent_chunk_corrects_evidence_id_in_both_directions(self):
        for actual in ('C0001', 'C0003'):
            with self.subTest(actual=actual):
                self.value['quotes'] = [self.quote(self.evidence[actual]['text'], 'C0002')]
                result = validate_notes(self.value, self.evidence)
                self.assertEqual(result['quotes'][0]['evidence_id'], actual)
                self.assertEqual(result['quotes'][0]['text'], self.evidence[actual]['text'])

    def test_fabricated_and_nonadjacent_quotes_are_rejected(self):
        for text, key in (
            ('An invented quote.', 'C0002'),
            ('The first exact quote!', 'C0002'),
            ('The third exact quote.', 'C0001'),
            ('The first exact quote.', 'unknown'),
            ('', 'C0002'),
        ):
            with self.subTest(text=text, key=key):
                self.value['quotes'] = [self.quote(text, key)]
                with self.assertRaisesRegex(ValueError, 'Quote not found in cited transcript'):
                    validate_notes(self.value, self.evidence)

    def test_remaining_budget_under_four_drops_tail_quotes(self):
        first = 'One two three four five six seven eight nine ten eleven twelve.'
        second = 'Thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour.'
        tail = 'This entire sentence must be dropped.'
        self.evidence['C0001']['text'] = f'{first} {second} {tail}'
        self.value['quotes'] = [self.quote(text) for text in (first, second, tail, tail)]
        result = validate_notes(self.value, self.evidence)
        self.assertEqual(result['quotes'], self.value['quotes'][:2])

    def assert_grounded_budget(self, result):
        self.assertLessEqual(sum(len(re.findall(r"\w+(?:['’-]\w+)*", quote['text']))
                                 for quote in result['quotes']), 25)
        for quote in result['quotes']:
            self.assertIn(quote['text'], self.evidence[quote['evidence_id']]['text'])

    def test_overlong_first_quote_is_shortened_to_word_limit(self):
        text = ' '.join(f'word{i}' for i in range(40))
        self.evidence['C0001']['text'] = text + ' Short quote.'
        self.value['quotes'] = [self.quote(text)]
        result = validate_notes(self.value, self.evidence)
        self.assertEqual([quote['text'] for quote in result['quotes']],
                         [' '.join(f'word{i}' for i in range(25))])
        self.assertIn(result['quotes'][0]['text'], text)
        self.assert_grounded_budget(result)

    def test_second_quote_shortens_at_latest_clause_boundary(self):
        first = ' '.join(f'first{i}' for i in range(12))
        second = 'One two three four. Five six seven eight, nine ten; eleven twelve thirteen fourteen fifteen.'
        self.evidence['C0001']['text'] = first
        self.evidence['C0002']['text'] = second
        self.value['quotes'] = [self.quote(first), self.quote(second, 'C0002')]
        result = validate_notes(self.value, self.evidence)
        self.assertEqual(result['quotes'][0], self.value['quotes'][0])
        self.assertEqual(result['quotes'][1]['text'], 'One two three four. Five six seven eight, nine ten;')
        self.assertIn(result['quotes'][1]['text'], second)
        self.assert_grounded_budget(result)

    def test_shortening_preserves_regex_words_and_adjacent_chunk_correction(self):
        text = '  ' + "don't well-known it’s " * 14
        self.evidence['C0003']['text'] = text
        self.value['quotes'] = [self.quote(text, 'C0002')]
        result = validate_notes(self.value, self.evidence)
        quote = result['quotes'][0]
        self.assertEqual(quote['evidence_id'], 'C0003')
        self.assertEqual(quote['text'], ("don't well-known it’s " * 8 + "don't"))
        self.assertIn(quote['text'], text)
        self.assert_grounded_budget(result)

    def test_fabricated_tail_is_rejected_before_budget_trimming(self):
        text = ' '.join(f'word{i}' for i in range(25))
        self.evidence['C0001']['text'] = text
        self.value['quotes'] = [self.quote(text), self.quote('invented tail')]
        with self.assertRaisesRegex(ValueError, 'Quote not found in cited transcript'):
            validate_notes(self.value, self.evidence)

    def test_generate_retries_with_repair_instructions(self):
        bad = copy.deepcopy(self.value)
        bad['quotes'] = [self.quote('invented')]
        good = copy.deepcopy(self.value)
        good['quotes'] = [self.quote('The first exact quote.')]
        client = Mock()
        client.responses.parse.side_effect = [
            SimpleNamespace(output_parsed=EpisodeNotes.model_validate(value))
            for value in (bad, good)
        ]
        record = SimpleNamespace(
            episode=SimpleNamespace(title='Test', show='Test show', metadata={}, duration_seconds=0),
            transcript=SimpleNamespace(text='[00:01] The first exact quote.', verified_complete=True),
        )
        result = ShownotesWriter(client, 'fake-model').generate(record)
        self.assertEqual(result, EpisodeNotes.model_validate(good).model_dump())
        self.assertEqual(client.responses.parse.call_count, 2)
        first, second = client.responses.parse.call_args_list
        self.assertEqual(first.kwargs['instructions'], INSTRUCTIONS)
        self.assertIn('上次输出未通过校验：Quote not found in cited transcript', second.kwargs['instructions'])
        self.assertIn('引文总词数 ≤25、逐条核对 evidence_id', INSTRUCTIONS)
        self.assertIn('引文总词数 ≤25、逐条核对 evidence_id', second.kwargs['instructions'][len(INSTRUCTIONS):])
        self.assertEqual(first.kwargs['input'], second.kwargs['input'])
