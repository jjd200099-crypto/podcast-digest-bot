import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_officer.daily_report import coverage_report, render_daily_summary
from news_officer.models import DailyItem, Episode


class DailyReportTests(unittest.TestCase):
    def item(self, status, index=0):
        return DailyItem(
            Episode(
                str(index), f"Episode {index}", f"https://example.org/{index}", "Show"
            ),
            status,
        )

    def test_missing_transcripts_are_not_no_updates(self):
        report = coverage_report(
            [self.item("no_transcript", i) for i in range(15)]
            + [self.item("unverified_date", 16)]
        )
        self.assertIn("16 期候选", report)
        self.assertIn("15 期：未取得完整文字稿", report)
        self.assertIn("1 期：发布日期未核验", report)
        self.assertIn("另有 10 期", report)
        self.assertNotIn("今日无可摘要", report)

    def test_partial_success_reports_pending(self):
        report = coverage_report(
            [self.item("summarized"), self.item("summary_format_error", 1)]
        )
        self.assertIn("已生成 1 期摘要", report)
        self.assertIn("已有全文", report)

    def test_all_summarized_has_no_extra_notice(self):
        self.assertIsNone(coverage_report([self.item("summarized")]))

    def test_empty_does_not_claim_no_source_updates(self):
        self.assertIn("不代表订阅源没有更新", coverage_report([]))

    def test_old_cached_scoring_is_hidden_without_changing_content(self):
        original = ('推荐理由：讨论推理成本下降（AI 40/40，投资 16/20，研究关联 0/5，增量 16/20，论据 9/15）\n'
                    '1. 成本下降75%是嘉宾观点。\n推荐星级：★★★★☆（4/5，编辑推荐）')
        rendered = render_daily_summary(original)
        self.assertNotIn('/40', rendered)
        self.assertNotIn('/5', rendered)
        self.assertIn('下降75%', rendered)
        self.assertTrue(rendered.endswith('阅读建议：值得看全文'))
        self.assertNotIn('★', rendered)
        self.assertNotIn('推荐星级', rendered)
        self.assertEqual(render_daily_summary(rendered), rendered)

    def test_stars_show_reading_action_without_numeric_subscores(self):
        for rating, advice in [(5, '值得编译'), (4, '值得看全文'), (3, '看摘要'),
                                (2, '无关'), (1, '无关')]:
            original = '推荐星级：' + '★' * rating + '☆' * (5 - rating)
            rendered = render_daily_summary(original)
            self.assertEqual(rendered, '阅读建议：' + advice)
            self.assertEqual(render_daily_summary(rendered), rendered)

    def test_previous_star_advice_is_migrated_without_touching_body(self):
        for original, expected in [('★★★☆☆｜看摘要即可', '看摘要'), ('★★☆☆☆｜可跳过', '无关'),
                                    ('★★★★★｜值得编译', '值得编译')]:
            body = '正文提到五星产品评价，不应改写。\n'
            self.assertEqual(render_daily_summary(body + '推荐星级：' + original),
                             body + '阅读建议：' + expected)

    def test_missing_transcript_is_not_classified_as_irrelevant(self):
        text = '未取得完整文字稿，本次不摘要、不评级'
        self.assertEqual(render_daily_summary(text), text)
