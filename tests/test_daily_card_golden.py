"""Production episode metadata plus approved editorial inputs for the v5 card."""
import json
import re
import unittest
from pathlib import Path

from news_officer.daily_report import reader_episode_v4
from news_officer.feishu import daily_card_delivery_parts, encoded_message_payload
from news_officer.models import DailyItem, Episode

FIXTURES = Path(__file__).parent / 'fixtures'


def render_fixture():
    fixture = json.loads((FIXTURES / 'daily-2026-10-14.json').read_text())
    sections = [reader_episode_v4(
        DailyItem(Episode.from_persisted_dict(row['episode']), row['status'], row['message']),
        number, row['document_url']) for number, row in enumerate(fixture['items'], 1)]
    return daily_card_delivery_parts('\n\n━━━━━━━━━━━━━━━━━━━━\n\n'.join(sections),
                                     'daily:2026-10-14', title=f'🎧 播客精选 · {fixture["day"]}')


class DailyCardGoldenTests(unittest.TestCase):
    def test_approved_card_structure_and_content(self):
        expected = json.loads((FIXTURES / 'daily-draft-v5.json').read_text())
        # The explicit tight-list requirement supersedes v5's blank list lines.
        for element in expected['body']['elements']:
            if element['tag'] == 'markdown':
                element['content'] = re.sub(r'\n\n(?=    \d+\. )', '\n', element['content'])
        parts = render_fixture()
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], 'interactive')
        self.assertEqual(json.loads(parts[0][1]), expected)

    def test_invalid_titles_keep_three_parts_and_summary(self):
        fixture = json.loads((FIXTURES / 'daily-2026-10-14.json').read_text())['items'][0]
        for title in ('Latent Space｜主题', 'Jason Ma｜主题', '主题',
                      'Latent Space｜｜主题', 'Show｜Guest｜Topic｜Extra', ''):
            with self.subTest(title=title):
                message = re.sub(r'^## [^\n]+', f'## {title}', fixture['message'])
                section = reader_episode_v4(DailyItem(
                    Episode.from_persisted_dict(fixture['episode']), 'summarized', message), 1)
                heading = re.search(r'【(.+)】', section)[1]
                self.assertEqual(len(heading.split('｜')), 3)
                self.assertTrue(all(heading.split('｜')))
                self.assertIn('Jason Ma 说明 Dyna', section)
                self.assertIn(fixture['episode']['url'], section)

    def assert_payloads_fit(self, parts):
        self.assertTrue(parts)
        for kind, content, _ in parts:
            envelope = {'receive_id': 'x' * 128, 'msg_type': kind, 'content': content,
                        'uuid': 'x' * 36, 'reply_in_thread': False}
            self.assertLessEqual(len(encoded_message_payload(envelope)),
                                 145_000 if kind == 'text' else 29_000)

    def test_oversized_day_compacts_card_and_preserves_episode_links(self):
        section = json.loads(render_fixture()[0][1])['body']['elements'][1]['content']
        section = section.replace('    2.', '    2. ' + '很长的论据。' * 4000)
        parts = daily_card_delivery_parts(section, 'large', title='今日')
        self.assert_payloads_fit(parts)
        self.assertEqual(parts[0][0], 'interactive')
        self.assertIn('简介：Jason Ma', parts[0][1])
        self.assertIn('https://www.latent.space/p/dyna', parts[0][1])
        self.assertIn('https://www.feishu.cn/docx/', parts[0][1])
        self.assertEqual(parts, daily_card_delivery_parts(section, 'large', title='今日'))

    def test_uncompactable_day_uses_previous_text_and_overflow_fallback(self):
        for length in (30000, 100000):
            with self.subTest(length=length):
                body = '文' * length
                parts = daily_card_delivery_parts(body, 'huge', title='今日')
                self.assert_payloads_fit(parts)
                self.assertTrue(all(part[0] == 'text' for part in parts))
                self.assertEqual(sum(json.loads(part[1])['text'].count('文') for part in parts), length)
                self.assertEqual(len({part[2] for part in parts}), len(parts))


class CardHeadroomTests(unittest.TestCase):
    def test_reserved_links_choose_compaction_before_initial_send(self):
        from news_officer.feishu import DailyCardTooLargeError
        url = 'https://xxxx.feishu.cn/docx/' + 'x' * 27
        # Find a full card fitting without links but exceeding the guard with them.
        for size in range(9000, 10000):
            body = '**1. 【Show｜Guest｜Topic】**\n- 简介：完整简介。\n- 要点总结：\n    1. 内容。\n推荐理由：' + '文' * size + '\n[收听节目](https://example.com/episode)'
            template = body + '\n[精读和完整中文对谈](' + url + ')'
            try:
                daily_card_delivery_parts(body, 'key', title='Daily', allow_compaction=False)
            except DailyCardTooLargeError:
                continue
            try:
                daily_card_delivery_parts(template, 'key', title='Daily', allow_compaction=False)
            except DailyCardTooLargeError:
                break
        else:
            self.fail('No boundary fixture found')
        initial = daily_card_delivery_parts(body, 'key', title='Daily', reserve_markdown=template)
        self.assertNotIn(url, initial[0][1])
        self.assertNotIn('推荐理由', initial[0][1])
        enriched = daily_card_delivery_parts(template, 'key', title='Daily', require_interactive=True)
        self.assertIn(url, enriched[0][1])
        DailyCardGoldenTests().assert_payloads_fit(initial)
        DailyCardGoldenTests().assert_payloads_fit(enriched)

    def test_v4_compaction_preserves_layout_metadata_intro_and_all_links(self):
        from news_officer.daily_report import compact_reader_episode
        section = ('**1. 【Show｜Guest｜Topic】**\n- 节目：Show\n- 时长：60分钟\n'
                   '- 简介：第一句。第二句。\n- 要点总结：\n'
                   '    1. 要点一。\n    2. 要点二。\n    3. 要点三。\n'
                   '[精读和完整中文对谈](https://xxxx.feishu.cn/docx/abc)\n'
                   '[收听节目](https://example.com/listen)')
        for level in (1, 2, 3):
            compact = compact_reader_episode(section, level, '')
            for required in ('【Show｜Guest｜Topic】', '- 节目：Show', '- 时长：60分钟',
                             '- 简介：第一句。第二句。', '- 要点总结：',
                             'https://xxxx.feishu.cn/docx/abc', 'https://example.com/listen'):
                self.assertIn(required, compact)
