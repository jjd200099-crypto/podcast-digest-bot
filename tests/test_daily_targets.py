import unittest

from news_officer.daily_targets import (
    allowlist_warnings,
    filter_daily_targets,
    valid_allowlist,
)


class DailyTargetTests(unittest.TestCase):
    def test_allowlist_keeps_only_valid_chat_ids(self):
        self.assertEqual(valid_allowlist(('oc_a', 'ou_a', 'bad', 'oc_a', 'oc_b c')), ('oc_a',))

    def test_restricted_filter_drops_open_ids_and_other_chats(self):
        targets = [('open_id', 'ou_x'), ('chat_id', 'oc_a'), ('chat_id', 'oc_b'), ('chat_id', 'bad')]
        self.assertEqual(filter_daily_targets(targets, ('oc_a',)), [('chat_id', 'oc_a')])
        self.assertEqual(filter_daily_targets(targets, (), require=True), [])
        self.assertEqual(filter_daily_targets(targets), [('open_id', 'ou_x'), ('chat_id', 'oc_a'), ('chat_id', 'oc_b')])

    def test_warning_codes(self):
        code = lambda *a: [w.split(':')[0] for w in allowlist_warnings(*a)]
        self.assertEqual(code((), False), ['daily_allowlist_unset'])
        self.assertEqual(code((), True), ['daily_allowlist_required_but_missing'])
        self.assertEqual(code(('bad',), False), ['daily_allowlist_invalid'])
        self.assertEqual(code(('bad',), True), ['daily_allowlist_required_but_missing'])
        self.assertEqual(code(('oc_a',), True, 'oc_a'), [])
