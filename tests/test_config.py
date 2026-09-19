import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.config import Settings


class SettingsTests(unittest.TestCase):
    def base_environment(self):
        return {
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "OPENAI_API_KEY": "key",
        }

    def test_railway_requires_the_persistent_data_volume(self):
        environment = {**self.base_environment(), "RAILWAY_DEPLOYMENT_ID": "deploy"}
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "persistent volume"),
        ):
            Settings.from_env()

    def test_railway_database_must_live_on_the_volume(self):
        environment = {
            **self.base_environment(),
            "RAILWAY_DEPLOYMENT_ID": "deploy",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data",
            "NEWS_OFFICER_DB_PATH": "/tmp/state.sqlite3",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "must live under"),
        ):
            Settings.from_env()

    def test_local_database_can_use_a_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **self.base_environment(),
                "NEWS_OFFICER_DB_PATH": str(Path(directory) / "state.sqlite3"),
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.db_path, Path(environment["NEWS_OFFICER_DB_PATH"]))

    def test_file_memory_defaults_next_to_database(self):
        with patch.dict(os.environ, {**self.base_environment(), "NEWS_OFFICER_DB_PATH": "/tmp/test/state.sqlite3"}, clear=True):
            self.assertEqual(Settings.from_env().podcast_memory_path, Path("/tmp/test/podcast-memory"))

    def test_cloud_file_memory_cannot_use_ephemeral_storage(self):
        with patch.dict(os.environ, {
            **self.base_environment(), "RAILWAY_DEPLOYMENT_ID": "deploy",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data", "NEWS_OFFICER_MEMORY_PATH": "/tmp/memory",
        }, clear=True), self.assertRaisesRegex(RuntimeError, "MEMORY_PATH"):
            Settings.from_env()

    def test_podwise_is_optional_and_configured_only_by_environment(self):
        with patch.dict(os.environ, self.base_environment(), clear=True):
            self.assertEqual(Settings.from_env().podwise_api_token, "")
        with patch.dict(os.environ, {**self.base_environment(), "PODWISE_API_TOKEN": " test-token "}, clear=True):
            self.assertEqual(Settings.from_env().podwise_api_token, "test-token")

    def test_daily_defaults_cover_all_candidates_without_files(self):
        with patch.dict(os.environ, self.base_environment(), clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.max_daily_candidates, 0)
        self.assertEqual(settings.max_daily_summaries, 0)
        self.assertFalse(settings.daily_transcript_attachments)
        self.assertTrue(settings.daily_rss_only)
        self.assertEqual(settings.agent_backend, "agents_sdk")

    def test_expression_advisor_is_opt_in_with_separate_credentials(self):
        with patch.dict(os.environ, self.base_environment(), clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.tone_advisor_enabled)
        self.assertEqual(settings.deepseek_api_key, '')
        with patch.dict(os.environ, {**self.base_environment(), 'DEEPSEEK_API_KEY': ' separate-key ',
                                    'NEWS_OFFICER_TONE_ADVISOR': 'true'}, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.tone_advisor_enabled)
        self.assertEqual(settings.deepseek_api_key, 'separate-key')
        self.assertEqual(settings.tone_advisor_model, 'deepseek-flash')

    def test_hermes_is_explicit_and_requires_absolute_interpreter(self):
        env = {**self.base_environment(), "NEWS_OFFICER_AGENT_BACKEND": "hermes"}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict(os.environ, {**env, "NEWS_OFFICER_HERMES_PYTHON": "/opt/hermes/bin/python"}, clear=True):
            self.assertEqual(Settings.from_env().agent_backend, "hermes")
        with patch.dict(os.environ, {**env, "NEWS_OFFICER_AGENT_BACKEND": "typo"}, clear=True), self.assertRaises(ValueError):
            Settings.from_env()

    def test_explicit_daily_limits_and_files_remain_supported(self):
        with patch.dict(os.environ, {
            **self.base_environment(), "NEWS_OFFICER_MAX_SUMMARIES": "5",
            "NEWS_OFFICER_MAX_CANDIDATES": "20",
            "NEWS_OFFICER_DAILY_TRANSCRIPT_ATTACHMENTS": "true",
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.max_daily_candidates, 20)
        self.assertEqual(settings.max_daily_summaries, 5)
        self.assertTrue(settings.daily_transcript_attachments)


if __name__ == "__main__":
    unittest.main()
