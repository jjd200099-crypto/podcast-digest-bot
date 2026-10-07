import json
import unittest
from datetime import UTC, date, datetime
from unittest.mock import Mock, patch

from news_officer.delivery_watchdog import (
    CheckError,
    check_receipt,
    is_daily_receipt,
    run_check,
)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 2, tzinfo=UTC)
        self.day = date(2026, 10, 8)
        self.message = {'sender': {'sender_type': 'app', 'id_type': 'app_id', 'id': 'cli_bot'},
            'create_time': str(int(datetime(2026, 10, 8, 0, 31, tzinfo=UTC).timestamp() * 1000)),
            'body': {'content': json.dumps({'content': [[{'tag': 'text', 'text': '情报官日报｜2026-10-08｜3 期'}]]})}}

    def test_only_own_current_day_undeleted_report_counts(self):
        start, end = self.now.timestamp() - 5400, self.now.timestamp()
        self.assertTrue(is_daily_receipt(self.message, 'cli_bot', self.day, start, end))
        for changes in ({'deleted': True}, {'sender': {'sender_type': 'user', 'id': 'cli_bot'}},
                        {'create_time': '0'}, {'body': {'content': 'broken json'}},
                        {'body': {'content': json.dumps({'text': '全文已补齐｜补充摘要｜2026-10-08'})}}):
            self.assertFalse(is_daily_receipt({**self.message, **changes}, 'cli_bot', self.day, start, end))
        self.assertFalse(is_daily_receipt(self.message, 'another_bot', self.day, start, end))
        self.assertFalse(is_daily_receipt(self.message, 'cli_bot', date(2026, 10, 9), start, end))

    def test_pagination_and_not_due(self):
        with patch('news_officer.delivery_watchdog.api', side_effect=[
                {'data': {'items': [], 'has_more': True, 'page_token': 'next'}},
                {'data': {'items': [self.message], 'has_more': False}}]) as api:
            self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', self.day, self.now), 'delivered')
            self.assertEqual(api.call_count, 2)
        with patch('news_officer.delivery_watchdog.api') as api:
            self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', self.day,
                                          datetime(2026, 10, 8, 0, 45, tzinfo=UTC)), 'not_due')
            api.assert_not_called()

    def test_missing_is_not_confused_with_check_failure(self):
        with patch('news_officer.delivery_watchdog.api', return_value={'data': {'items': [], 'has_more': False}}):
            self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', self.day, self.now), 'missing')
        with patch('news_officer.delivery_watchdog.api', return_value={'data': {}}), self.assertRaises(CheckError):
            check_receipt(Mock(), 'cli_bot', 'chat', self.day, self.now)

    def test_default_read_only_and_explicit_private_alert(self):
        for notify in (False, True):
            with patch('news_officer.delivery_watchdog.requests.Session'), \
                    patch('news_officer.delivery_watchdog.api', side_effect=[
                        {'tenant_access_token': 'hidden'}, {'data': {'items': [], 'has_more': False}},
                        {'data': {'message_id': 'receipt'}}]) as api:
                result = run_check('cli_bot', 'hidden-secret', 'chat', 'owner', now=self.now, notify=notify)
                self.assertEqual(result['status'], 'missing')
                self.assertEqual(result['alert_sent'], notify)
                self.assertEqual(api.call_count, 3 if notify else 2)
                self.assertNotIn('hidden', json.dumps(result))
                if notify:
                    self.assertEqual(api.call_args.kwargs['json']['receive_id'], 'owner')
                    self.assertEqual(api.call_args.kwargs['params']['receive_id_type'], 'open_id')

    def test_read_permission_error_is_reported_as_unknown_not_no_report(self):
        with patch('news_officer.delivery_watchdog.requests.Session'), \
                patch('news_officer.delivery_watchdog.api', side_effect=[{'tenant_access_token': 'hidden'},
                    CheckError('Feishu API code 99991672'), {'data': {'message_id': 'receipt'}}]) as api:
            result = run_check('cli_bot', 'secret', 'chat', 'owner', now=self.now, notify=True)
            self.assertEqual(result['status'], 'check_failed')
            self.assertIn('无法确认送达', api.call_args.kwargs['json']['content'])

    def test_historical_checks_cannot_send_alerts(self):
        with self.assertRaises(ValueError), patch('news_officer.delivery_watchdog.requests.Session') as session:
            run_check('cli_bot', 'secret', 'chat', 'owner', day=date(2026, 10, 6), now=self.now, notify=True)
        session.assert_not_called()

    def test_auth_failure_is_not_reported_as_alert_sent(self):
        with patch('news_officer.delivery_watchdog.api', side_effect=CheckError('auth unavailable')):
            result = run_check('bot', 'secret', 'chat', 'owner', now=self.now, notify=True)
            self.assertEqual(result['status'], 'check_failed')
            self.assertFalse(result['alert_sent'])
