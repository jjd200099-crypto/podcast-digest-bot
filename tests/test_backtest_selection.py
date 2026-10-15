import importlib.util
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from news_officer.editorial import Assessment, decide
from news_officer.models import DailyItem, Episode
from news_officer.store import Store

spec = importlib.util.spec_from_file_location('backtest_selection', Path(__file__).resolve().parents[1] / 'scripts/backtest_selection.py')
backtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backtest)


class BacktestSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_backtest_read_only_stored_floor_and_optional_rescore(self):
        tmp_path = Path(self.temp_dir.name)
        path = tmp_path / 'state.sqlite3'
        store = Store(path)
        store.initialize()
        text = 'A sufficiently long original evidence passage on concrete model inference costs.'
        assessment = Assessment.model_validate({'relevance': {'score': 5, 'quotes': [text], 'priority': 'model_lab'}, 'density': {'score': 2, 'quotes': [text]}, 'reason': '原文提供模型推理成本的一手细节，信息偏薄。'})
        decision = decide(assessment, text)
        self.assertEqual(decision['stars'], 4)
        episode = Episode('e', 'Test episode', 'https://example.test/e', 'Show')
        from news_officer.models import Transcript
        store.save_verified_transcript(episode, Transcript(text, 'official', '', True))
        with store._connect() as db:
            db.execute("INSERT INTO jobs (job_key,kind,payload_json,available_at,created_at,updated_at) VALUES ('daily','daily','{}',?,?,?)", ('2026-10-01T01:00:00+00:00',) * 3)
            db.execute("INSERT INTO job_results VALUES ('daily','item','daily_item',?,?)", (json.dumps(DailyItem(episode, 'summarized').to_persisted_dict()), '2026-10-01T01:00:00+00:00'))
            db.execute('INSERT INTO editorial_reviews VALUES (?,?,?,?)', ('e', 'old', json.dumps(decision), '2026-10-01T00:00:00+00:00'))
            strict = {**decision, 'policy': 'strict-selection-v2', 'stars': 3}
            db.execute('INSERT INTO editorial_reviews VALUES (?,?,?,?)',
                       ('e', 'strict', json.dumps(strict), '2026-10-01T00:30:00+00:00'))
        before = path.read_bytes()
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            output = io.StringIO()
            with redirect_stdout(output):
                backtest.print_backtest(db, date(2026, 10, 1), date(2026, 10, 1))
            self.assertIn('4\t5\t2\tmodel_lab\t2\tTrue', output.getvalue())
            policies = [Mock(), Mock()]
            policies[0].assess.return_value = decision
            policies[1].assess.return_value = {**decision, 'stars': 2, 'selected': False}
            with redirect_stdout(output):
                backtest.print_backtest(db, date(2026, 10, 1), date(2026, 10, 1), policies)
            self.assertIn('\t4\t2\tout\t', output.getvalue())
            for policy in policies:
                policy.assess.assert_called_once()
            policies[0].assess.side_effect = ValueError("Editorial review failed")
            policies[1].assess.reset_mock()
            with redirect_stdout(output):
                backtest.print_backtest(db, date(2026, 10, 1), date(2026, 10, 1), policies)
            policies[1].assess.assert_called_once()
            self.assertIn("old: Editorial review failed", output.getvalue())

            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                db.execute("DELETE FROM editorial_reviews")
        self.assertEqual(path.read_bytes(), before)


    def test_missing_database_is_not_created(self):
        missing = Path(self.temp_dir.name) / "missing.sqlite3"
        with patch("sys.argv", ["backtest_selection.py", str(missing), "2026-10-01", "2026-10-02"]), self.assertRaises(sqlite3.OperationalError):
            backtest.main()
        self.assertFalse(missing.exists())
