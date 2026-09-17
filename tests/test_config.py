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

    def test_podwise_is_optional_and_configured_only_by_environment(self):
        with patch.dict(os.environ, self.base_environment(), clear=True):
            self.assertEqual(Settings.from_env().podwise_api_token, "")
        with patch.dict(os.environ, {**self.base_environment(), "PODWISE_API_TOKEN": " test-token "}, clear=True):
            self.assertEqual(Settings.from_env().podwise_api_token, "test-token")


if __name__ == "__main__":
    unittest.main()
