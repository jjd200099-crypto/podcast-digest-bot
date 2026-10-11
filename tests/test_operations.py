import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from news_officer.operations import business_snapshot, record_warning
from news_officer.store import Store


class WarningDates(unittest.TestCase):
    def test_warning_dates_preserve_parsing_and_safe_fallback(self):
        cases = (
            ('2026-01-05', '2026-01-05'),
            ('2026-1-5', '2026-01-05'),
            ('2024-02-29', '2024-02-29'),
            ('2026-02-29', 'unknown'),
            ('2026-13-01', 'unknown'),
            ('20260105', 'unknown'),
            ('2026-W02-1', 'unknown'),
            ('2026-01-05 trailing', 'unknown'),
            (None, 'unknown'),
        )
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / 'db.sqlite3')
            store.initialize()
            for day, expected in cases:
                with self.subTest(day=day):
                    recorder = Mock()
                    record_warning(recorder, 'job', 'test_warning', day)
                    self.assertEqual(recorder.save_job_result.call_args.args[3]['day'], expected)
                    with store._connect() as db:
                        db.execute('DELETE FROM jobs')
                    store.enqueue('document:test', 'document',
                                  {'mode': 'daily_episode', 'episode_id': 'episode', 'day': day})
                    with store._connect() as db:
                        db.execute("UPDATE jobs SET status='failed'")
                    warnings = business_snapshot(store.path)['warnings']
                    missing = [warning for warning in warnings if warning.startswith('document link missing:')]
                    self.assertEqual(len(missing), 1)
                    self.assertTrue(missing[0].endswith(':day=' + expected))
