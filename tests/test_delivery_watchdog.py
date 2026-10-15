import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime
from unittest.mock import Mock, patch

from news_officer.delivery_watchdog import (
    CheckError,
    api,
    check_receipt,
    config_warnings,
    is_daily_receipt,
    main,
    run_check,
)


class WatchdogTests(unittest.TestCase):
    def test_http_error_retains_only_safe_numeric_api_code(self):
        session = Mock()
        session.request.return_value.status_code = 400
        session.request.return_value.json.return_value = {'code': 230001, 'msg': 'secret response body'}
        with self.assertRaisesRegex(CheckError, '^Feishu HTTP 400; API code 230001$'):
            api(session, 'GET', '/im/v1/messages')

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
            for call in api.call_args_list:
                self.assertEqual(call.kwargs['params']['card_msg_content_type'], 'raw_card_content')
        with patch('news_officer.delivery_watchdog.api') as api:
            self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', self.day,
                                          datetime(2026, 10, 8, 0, 45, tzinfo=UTC)), 'not_due')
            api.assert_not_called()

    def test_raw_card_receipt_and_placeholder(self):
        day = date(2026, 10, 15)
        now = datetime(2026, 10, 15, 2, tzinfo=UTC)
        card = {'schema': '2.0', 'header': {'title': {
            'tag': 'plain_text', 'content': '🎧 播客精选 · 2026-10-15'}},
            'body': {'elements': []}}
        placeholder = {'text': '请升级至最新版本客户端，以查看内容'}
        for body, expected in (
                (card, 'delivered'),
                ({'json_card': json.dumps(card)}, 'delivered'),
                ({'json_card': json.dumps({'card': json.dumps(card)})}, 'delivered'),
                (placeholder, 'missing')):
            with self.subTest(body=body):
                message = {**self.message, 'msg_type': 'interactive',
                    'create_time': str(int(datetime(2026, 10, 15, 0, 33, tzinfo=UTC).timestamp() * 1000)),
                    'body': {'content': json.dumps(body)}}
                with patch('news_officer.delivery_watchdog.api',
                           return_value={'data': {'items': [message], 'has_more': False}}):
                    self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', day, now), expected)

    def test_post_receipt_still_delivered(self):
        with patch('news_officer.delivery_watchdog.api',
                   return_value={'data': {'items': [self.message], 'has_more': False}}):
            self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', self.day, self.now), 'delivered')

    def test_configured_early_window_counts_receipt_and_moves_grace_period(self):
        message = {**self.message, 'create_time': str(int(
            datetime(2026, 10, 8, 0, 15, tzinfo=UTC).timestamp() * 1000))}
        with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_TIME': '08:00'}), patch(
                'news_officer.delivery_watchdog.api',
                return_value={'data': {'items': [message], 'has_more': False}}) as api:
            self.assertEqual(check_receipt(Mock(), 'cli_bot', 'chat', self.day,
                datetime(2026, 10, 8, 0, 48, tzinfo=UTC)), 'delivered')
            self.assertEqual(api.call_args.kwargs['params']['start_time'], str(int(
                datetime(2026, 10, 8, 0, tzinfo=UTC).timestamp())))

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

    def run_main(self, extra):
        env = {'FEISHU_APP_ID': 'cli_bot', 'FEISHU_APP_SECRET': 'hidden', 'NEWS_OFFICER_WATCH_CHAT_ID': 'oc_watch'}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {**env, **extra}, clear=True), \
                patch('news_officer.delivery_watchdog.run_check',
                      return_value={'status': 'delivered', 'alert_sent': False}), \
                redirect_stdout(out), redirect_stderr(err), patch('sys.argv', ['watchdog']):
            code = main()
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_unset_allowlist_is_reported_as_non_fatal_warning(self):
        code, result, err = self.run_main({})
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'delivered')
        self.assertEqual(len(result['warnings']), 1)
        self.assertTrue(result['warnings'][0].startswith('daily_allowlist_unset'))
        self.assertIn('::warning title=Daily allowlist::daily_allowlist_unset', err)

    def test_configured_allowlist_has_no_warning_and_mismatch_is_flagged(self):
        code, result, err = self.run_main({'NEWS_OFFICER_DAILY_CHAT_IDS': 'oc_watch'})
        self.assertEqual((code, result['warnings'], err), (0, [], ''))
        code, result, _ = self.run_main({'NEWS_OFFICER_DAILY_CHAT_IDS': 'oc_other'})
        self.assertEqual(code, 0)
        self.assertTrue(result['warnings'][0].startswith('watch_chat_not_in_daily_allowlist'))
        with patch.dict(os.environ, {'NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS': 'true'}, clear=True):
            self.assertTrue(config_warnings()[0].startswith('daily_allowlist_required_but_missing'))
