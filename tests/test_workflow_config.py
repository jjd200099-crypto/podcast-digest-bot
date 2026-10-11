import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'delivery-watchdog.yml'


class WatchdogWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding='utf-8')

    def test_daily_time_is_passed_with_default(self):
        self.assertRegex(self.text, r"(?m)^\s+NEWS_OFFICER_DAILY_TIME: \$\{\{ vars\.NEWS_OFFICER_DAILY_TIME \|\| '08:30' \}\}$")

    def test_allowlist_mirrors_are_passed_for_warnings_only(self):
        for name in ('NEWS_OFFICER_DAILY_CHAT_IDS', 'NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS'):
            self.assertRegex(self.text, rf'(?m)^\s+{name}: \$\{{\{{ vars\.{name} \}}\}}$')

    def test_watchdog_env_never_uses_secrets_for_non_credentials(self):
        for name in re.findall(r'(?m)^\s+(NEWS_OFFICER_\w+):', self.text):
            self.assertNotRegex(self.text, rf'{name}: \$\{{\{{ secrets\.')


if __name__ == '__main__':
    unittest.main()
