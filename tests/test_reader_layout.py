import json
import unittest

from news_officer.daily_report import episode_document_url, reader_episode
from news_officer.feishu import combined_delivery_parts
from news_officer.models import Episode


class ReaderLayout(unittest.TestCase):
    def test_title_bold_and_listen_link_under_reading_link(self):
        source = '## 嘉宾｜核心主题\n节目：Show｜Episode  \n链接：https://example.test/audio  \n时长：45分钟\n\n正文内容不改写。\n推荐星级：★★★★☆'
        rendered = reader_episode(source, 'https://example.test/doc')
        parts = combined_delivery_parts(rendered, 'test')
        self.assertEqual(len(parts), 1)
        rows = json.loads(parts[0][1])['zh_cn']['content']
        self.assertEqual(rows[0][0]['style'], ['bold'])
        self.assertEqual(rows[1][0]['style'], ['bold'])
        text = str(rows)
        self.assertLess(text.index('正文内容不改写'), text.index('精读与完整中文对谈'))
        self.assertLess(text.index('精读与完整中文对谈'), text.index('收听节目'))
        self.assertEqual(text.count('https://example.test/audio'), 1)
        self.assertNotIn('**', text)
        self.assertNotIn('style', str(next(row for row in rows if any('正文内容' in e.get('text', '') for e in row))))

    def test_no_document_retains_listening_link_at_end(self):
        rendered = reader_episode('【主题】\n链接：https://example.test/episode\n正文')
        self.assertTrue(rendered.endswith('[收听节目](https://example.test/episode)'))
        self.assertNotIn('精读与', rendered)
        self.assertTrue(rendered.startswith('**【主题】**'))

    def test_document_mapping_is_identity_based_not_position(self):
        episode = Episode('one', 'Title', 'https://example.test/audio', 'Show')
        docs = [{'episode_id': 'two', 'title': 'Other', 'url': 'wrong'},
                {'episode_id': 'one', 'title': 'Title', 'url': 'right'}]
        self.assertEqual(episode_document_url(episode, docs), 'right')
        self.assertEqual(episode_document_url(episode, docs[:1]), '')
        legacy = [{'title': 'Title｜Show｜2026-10-10', 'url': 'legacy'}]
        self.assertEqual(episode_document_url(episode, legacy), 'legacy')
        with self.assertRaises(ValueError):
            episode_document_url(episode, legacy * 2)

    def test_markdown_source_link_preserved(self):
        self.assertTrue(reader_episode('标题\n链接：[原节目](https://example.test/e)\n正文', 'https://example.test/doc')
                        .endswith('[原节目](https://example.test/e)'))
