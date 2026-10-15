import os
import unittest
from unittest.mock import patch

from news_officer.daily_report import select_daily_episodes
from news_officer.models import DailyItem, Episode


def item(index, priority='B', advice='值得看全文', show=None, stars=4):
    return DailyItem(Episode(str(index), f'Episode {index}', f'https://example.org/{index}',
                            show or f'Show {index}', metadata={'source_priority': priority}),
                     'summarized', f'阅读建议：{advice}\n推荐星级：' + '★' * stars + '☆' * (5-stars))


class DailySelectionTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        os.environ.pop('NEWS_OFFICER_DAILY_MAX_EPISODES', None)

    def test_source_dedupe_ranking_and_unchanged(self):
        candidates = [item(0, show='Same'), item(1, 'A', show='same'),
                      item(2, 'A', '值得编译', show='Same'),
                      item(3, 'A', '值得编译', show='Same', stars=5)]
        before = [i.to_persisted_dict() for i in candidates]
        with self.assertLogs('news_officer.daily_report', level='INFO') as logs:
            self.assertEqual(select_daily_episodes(candidates), [candidates[3]])
        self.assertIn('per-source cap dropped', '\n'.join(logs.output))
        self.assertEqual(before, [i.to_persisted_dict() for i in candidates])

    def test_default_cap_and_log(self):
        with self.assertLogs('news_officer.daily_report', level='INFO') as logs:
            self.assertEqual(len(select_daily_episodes([item(i) for i in range(9)])), 5)
        self.assertIn('total cap dropped 5', '\n'.join(logs.output))

    def test_sixth_strict_bar_and_log(self):
        for priority, advice, expected in [
                ('A', '值得编译', 6), ('B', '值得编译', 5), ('A', '值得看全文', 5)]:
            with self.subTest(priority=priority, advice=advice):
                candidates = [item(i, priority, advice) for i in range(8)]
                with self.assertLogs('news_officer.daily_report', level='INFO') as logs:
                    self.assertEqual(len(select_daily_episodes(candidates)), expected)
                log_text = '\n'.join(logs.output)
                self.assertEqual('sixth episode admitted' in log_text, expected == 6)
                if expected == 6:
                    self.assertIn('source priority A and reading level 值得编译', log_text)
                    self.assertEqual(log_text.count('sixth episode admitted'), 1)

    def test_no_padding(self):
        for count in [0, 1, 2]:
            with self.subTest(count=count):
                self.assertEqual(len(select_daily_episodes([item(i) for i in range(count)])), count)

    def test_env_override(self):
        for value, expected in [('3', 3), ('2', 2), ('99', 5), ('bad', 5)]:
            with self.subTest(value=value), patch.dict(os.environ, NEWS_OFFICER_DAILY_MAX_EPISODES=value):
                self.assertEqual(len(select_daily_episodes([item(i) for i in range(8)])), expected)
