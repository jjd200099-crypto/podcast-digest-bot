"""New-item badges must not leak into deep-read titles."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import test_episode_document as document_fixtures
import test_shownotes as shownotes_fixtures
from test_deepread_new_style import record, sample

from news_officer.daily_archive import read_daily_digest
from news_officer.deepread import INSTRUCTIONS as DEEPREAD_INSTRUCTIONS
from news_officer.shownotes import INSTRUCTIONS, render_episode
from news_officer.titles import normalize_deepread_title


class TitleNormalizationTests(unittest.TestCase):
    def test_leading_badges_are_stripped(self):
        for prefix in ['[新]', '【新】', '[NEW]', '[new]', '  [新] 【新】 [NEW]  ']:
            with self.subTest(prefix=prefix):
                self.assertEqual(normalize_deepread_title(prefix + 'RSI interview'), 'RSI interview')

    def test_unbadged_titles_are_byte_identical(self):
        for title in ['  Original title  ', 'Interview [NEW] tools', '【新闻】访谈', '[NEWspaper]']:
            with self.subTest(title=title):
                self.assertEqual(normalize_deepread_title(title), title)

    def test_title_blocks_and_markdown_are_normalized_without_other_changes(self):
        for new_style in [False, True]:
            for prefix in ['[新] ', '【新】', '[NEW] ']:
                with self.subTest(new_style=new_style, prefix=prefix):
                    rec = record()
                    fixture = shownotes_fixtures.ShownotesValidation()
                    fixture.setUp()
                    value = sample() if new_style else fixture.value
                    if not new_style:
                        rec.transcript.text = '[00:01] The first exact quote.'
                    expected = render_episode(rec, value, new_style=new_style)
                    rec.episode.title = prefix + rec.episode.title
                    self.assertEqual(render_episode(rec, value, new_style=new_style), expected)

    def test_daily_deepread_link_text_is_normalized(self):
        for prefix in ['', '[新]', '【新】', '[NEW]']:
            with self.subTest(prefix=prefix):
                store = Mock()
                store._connect.return_value.__enter__ = Mock(return_value=Mock())
                store._connect.return_value.__exit__ = Mock(return_value=False)
                store._connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
                    {'job_key': 'daily', 'payload_json': '{"scheduled_for":"2026-10-09T07:00:00+08:00"}',
                     'analysis_complete': True}]
                store.list_job_results.side_effect = (
                    lambda key, kind: [{'reader_mode': True}] if kind == 'daily_bundle' else [])
                store.get_job_result.return_value = {
                    'documents': [{'title': prefix + 'Interview', 'url': 'https://example.test/doc'}]}
                self.assertEqual(read_daily_digest(store, '2026-10-09')['markdown'], (
                    '今天暂无可推送的播客摘要。\n\n精读文档：[Interview](https://example.test/doc)'))

    def test_prompts_keep_english_terms_without_parenthetical_chinese(self):
        for prompt in [INSTRUCTIONS, DEEPREAD_INSTRUCTIONS]:
            with self.subTest(prompt=prompt):
                self.assertIn('保留 RSI 等通用英文术语和缩写，不添加括号中文释义。', prompt)
                self.assertNotIn('第一次出现可补源文支持的简短中文释义', prompt)

    def test_generated_document_titles_are_normalized(self):
        for new_style in [False, True]:
            for prefix in ['[新]', '【新】', '[NEW]']:
                with self.subTest(new_style=new_style, prefix=prefix):
                    fixture = document_fixtures.SelectedDocuments()
                    fixture.setUp()
                    try:
                        compiler = fixture.compiler
                        compiler.new_style = new_style
                        revision, records, pending = compiler.snapshot(fixture.fixture.base)
                        item, rec = records[0]
                        rec = replace(rec, episode=replace(rec.episode, title=prefix + rec.episode.title))
                        nodes = [{'block_type': 3, 'heading1': {'elements': [
                            {'text_run': {'content': 'one'}}]}}]
                        with (patch.object(compiler, 'snapshot', return_value=(revision, [(item, rec)], pending)),
                              patch.object(compiler, '_notes', side_effect=lambda *args, nodes=nodes, **kwargs:
                                           nodes)):
                            result = compiler.publish(fixture.job())
                        self.assertEqual(result['documents'][0]['title'], 'one｜Test｜2026-09-21')
                        with fixture.fixture.store._connect() as db:
                            self.assertEqual(db.execute('SELECT title FROM daily_documents').fetchone()[0],
                                             'one｜Test｜2026-09-21')
                    finally:
                        fixture.doCleanups()
