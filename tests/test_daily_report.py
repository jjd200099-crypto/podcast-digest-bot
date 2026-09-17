import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_officer.daily_report import coverage_report
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
