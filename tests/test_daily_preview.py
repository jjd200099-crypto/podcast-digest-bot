import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from news_officer.daily_preview import main, render_preview
from news_officer.daily_report import (
    reader_episode,
    render_daily_summary,
    summary_paragraph_spans,
)
from news_officer.models import DailyItem, Episode
from news_officer.store import Store


class DailyPreviewTests(unittest.TestCase):
    def test_mixed_durations_and_whitespace(self):
        for duration in ['01:02:03', '62:03', '3723', '1小时2分', '62 分钟']:
            with self.subTest(duration=duration):
                source = '## 标题\n时长：' + duration + ' \t'
                self.assertEqual(render_daily_summary(source), source)
                self.assertIn('时长：62 分钟', reader_episode(source, compact=True))
        for duration, expected in [('', '未提供'), ('未提供', '未提供'), ('unknown', 'unknown')]:
            self.assertIn('时长：' + expected,
                          reader_episode('## 标题\n时长：' + duration, compact=True))
        rendered = reader_episode('## 标题 \t\n节目：Show  \n时长：3723 \t\n\n正文。  \n\n更多正文。 \t', compact=True)
        self.assertTrue(all(line == line.rstrip() for line in rendered.splitlines()))

    def test_three_paragraph_model_output_is_merged_with_attribution(self):
        from unittest.mock import Mock

        from news_officer.daily_report import compact_paragraphs
        sources = [
            'Guest认为可能有收益。' + '背景。' * 8 + '\n\nCompany称仍有风险。' + '背景。' * 8,
            'Guest认为可能有收益。' + '背景。' * 5 + '\n\nCompany称仍有风险。' + '背景。' * 5
            + '\n\n仍需观察。' + '背景。' * 4,
        ]
        for source in sources:
            with self.subTest(source=source):
                model = Mock()
                model.rewrite_paragraphs.return_value = ['Guest认为可能有收益。', 'Company称仍有风险。', '仍需观察。']
                audit = []
                rendered = compact_paragraphs(source, model, audit)
                self.assertFalse(audit[0]['fallback'])
                self.assertEqual(len(summary_paragraph_spans(rendered)), 2)
                self.assertIn('Company称仍有风险。仍需观察。', rendered)
                model.rewrite_paragraphs.assert_called_once()

    def test_one_paragraph_retries_then_falls_back(self):
        from unittest.mock import Mock

        from news_officer.daily_report import compact_paragraphs
        source = 'Guest认为可能有收益。背景。\n\nCompany称仍有风险。背景。\n\n更多背景。'
        model = Mock()
        model.rewrite_paragraphs.return_value = ['Guest认为可能有收益。Company称仍有风险。']
        audit = []
        rendered = compact_paragraphs(source, model, audit)
        self.assertTrue(audit[0]['fallback'])
        self.assertEqual(model.rewrite_paragraphs.call_count, 2)
        self.assertEqual(len(summary_paragraph_spans(rendered)), 2)
        self.assertIn('Guest认为可能', rendered)
        self.assertIn('Company称', rendered)

    def test_duration_prefers_seconds_and_inserts_missing_line(self):
        from news_officer.daily_report import render_reader_daily

        for seconds, text, expected in [(3723, '20分', '62 分钟'),
                                        (None, '1小时2分', '62 分钟'),
                                        (None, None, '未提供')]:
            item = DailyItem(Episode('ep', 'Title', 'https://example.test', 'Show',
                                     duration_seconds=seconds, duration_string=text),
                             'summarized', '## 标题\n节目：Show\n\n正文。')
            self.assertIn('时长：' + expected, render_reader_daily([item], [], compact=True))

    def test_one_paragraph_can_succeed_on_retry(self):
        from unittest.mock import Mock

        from news_officer.daily_report import compact_paragraphs
        source = 'Guest认为可能有收益。' + '背景。' * 8 + '\n\nCompany称仍有风险。' + '背景。' * 8
        model = Mock()
        model.rewrite_paragraphs.side_effect = [
            ['Guest认为可能有收益。'],
            ['Guest认为可能有收益。', 'Company称仍有风险。仍需观察。']]
        audit = []
        result = compact_paragraphs(source, model, audit)
        self.assertEqual(model.rewrite_paragraphs.call_count, 2)
        self.assertFalse(audit[0]['fallback'])
        self.assertEqual(len(summary_paragraph_spans(result)), 2)

    def test_surplus_paragraphs_cannot_drop_claim_ownership(self):
        from unittest.mock import Mock

        from news_officer.daily_report import compact_paragraphs
        source = 'Guest认为可能有收益。' + '背景。' * 8 + '\n\nCompany称仍有风险。' + '背景。' * 8
        model = Mock()
        model.rewrite_paragraphs.return_value = ['Guest认为可能有收益。', 'Guest称仍有风险。', '仍需观察。']
        audit = []
        result = compact_paragraphs(source, model, audit)
        self.assertTrue(audit[0]['fallback'])
        self.assertIn('Company称仍有风险。', result)
        self.assertEqual(len(summary_paragraph_spans(result)), 2)

    def test_preview_is_read_only_and_never_constructs_sender_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = Store(path)
            store.initialize()
            store.enqueue('daily:test', 'daily', {'scheduled_for': '2026-10-13T08:30:00+08:00'})
            item = DailyItem(Episode('ep', 'Title', 'https://example.test', 'Show'), 'summarized',
                             '## 标题\n节目：Show\n时长：62:03\n\n观点。依据。\n\n其他观点。依据。')
            store.save_job_result('daily:test', 'episode:ep', 'daily_item', item.to_persisted_dict())
            before = path.read_bytes()
            with (patch('news_officer.feishu.FeishuMessenger', side_effect=AssertionError('sender')),
                  patch('news_officer.store.Store.ensure_outbox', side_effect=AssertionError('outbox')),
                  patch('news_officer.__main__.build_runtime', side_effect=AssertionError('runtime'))):
                result = render_preview(path, '2026-10-13', compact=True)
                self.assertIn('时长：62 分钟', result)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    main(['--dry-run', '--db', str(path), '--date', '2026-10-13'])
                self.assertIn('标题', output.getvalue())
            self.assertEqual(path.read_bytes(), before)

    def test_missing_db_is_not_created(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                render_preview(path, '2026-10-13')
            self.assertFalse(path.exists())
