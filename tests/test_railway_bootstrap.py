import argparse
import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.bootstrap_railway import (
    BootstrapError,
    Railway,
    decode_security_password,
    default_lark_config_path,
    has_mount_path,
    has_named_service,
    runtime_variables,
    safe_keychain_filename,
    select_lark_app,
    set_variables,
)


class LarkConfigTests(unittest.TestCase):
    def test_current_profile_is_selected_before_first_app(self):
        app = select_lark_app(
            {
                "currentApp": "news",
                "apps": [
                    {"name": "old", "appId": "cli_old", "appSecret": "old"},
                    {
                        "name": "news",
                        "appId": "cli_news",
                        "appSecret": {
                            "source": "keychain",
                            "id": "appsecret:cli_news",
                        },
                    },
                ],
            }
        )
        self.assertEqual(app.app_id, "cli_news")
        self.assertEqual(app.secret_source, "keychain")

    def test_explicit_profile_name_wins_over_matching_app_id(self):
        app = select_lark_app(
            {
                "apps": [
                    {"name": "cli_two", "appId": "cli_one", "appSecret": "one"},
                    {"name": "second", "appId": "cli_two", "appSecret": "two"},
                ]
            },
            "cli_two",
        )
        self.assertEqual(app.app_id, "cli_one")

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(BootstrapError):
            select_lark_app(
                {"apps": [{"appId": "cli_one", "appSecret": "secret"}]},
                "missing",
            )

    def test_config_dir_environment_matches_lark_cli_schema(self):
        path = default_lark_config_path(
            {"LARKSUITE_CLI_CONFIG_DIR": "/private/lark-config"}
        )
        self.assertEqual(path, Path("/private/lark-config/config.json"))

    def test_keychain_account_is_mapped_to_lark_cli_encrypted_filename(self):
        self.assertEqual(
            safe_keychain_filename("appsecret:cli_example"),
            "appsecret_cli_example.enc",
        )

    def test_go_keyring_binary_wrapper_is_decoded(self):
        master_key = b"k" * 32
        lark_encoded = base64.b64encode(master_key)
        security_value = b"go-keyring-base64:" + base64.b64encode(lark_encoded)
        self.assertEqual(decode_security_password(security_value), master_key)


class RailwayPayloadTests(unittest.TestCase):
    def test_service_and_mount_detection_accept_nested_cli_json(self):
        services = {"services": [{"id": "svc_1", "name": "news-officer"}]}
        volumes = [
            {"id": "vol_1", "service": {"id": "svc_1"}, "mountPath": "/data"}
        ]
        self.assertTrue(has_named_service(services, "news-officer"))
        self.assertTrue(has_mount_path(volumes, "/data"))
        self.assertFalse(has_mount_path(volumes, "/cache"))

    def test_secret_values_are_only_sent_via_stdin(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return argparse.Namespace(returncode=0, stdout=b"{}", stderr=b"")

        railway = Railway(["railway"])
        feishu = bytearray(b"feishu-private-value")
        openai = bytearray(b"openai-private-value")
        with patch("scripts.bootstrap_railway.subprocess.run", side_effect=fake_run):
            set_variables(
                railway,
                "news-officer",
                {"FEISHU_APP_ID": "cli_public"},
                feishu,
                openai,
            )

        flattened_argv = " ".join(part for call, _ in calls for part in call)
        self.assertNotIn("feishu-private-value", flattened_argv)
        self.assertNotIn("openai-private-value", flattened_argv)
        self.assertEqual(calls[1][1]["input"], bytes(feishu))
        self.assertEqual(calls[2][1]["input"], bytes(openai))

    def test_runtime_variables_do_not_contain_secrets(self):
        args = argparse.Namespace(
            openai_model="gpt-5.6-terra",
            daily_time="08:30",
            timezone="Asia/Shanghai",
            lookback_hours=72,
            max_summaries=3,
            max_candidates=16,
            user_open_ids="",
            group_chat_ids="",
        )
        values = runtime_variables(args, "cli_public")
        self.assertNotIn("FEISHU_APP_SECRET", values)
        self.assertNotIn("OPENAI_API_KEY", values)

    def test_secret_environment_is_not_forwarded_to_railway(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs["env"])
            return argparse.Namespace(returncode=0, stdout=b"", stderr=b"")

        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "hidden",
                    "FEISHU_APP_SECRET": "hidden",
                    "CUSTOM_OPENAI_KEY": "hidden",
                    "RAILWAY_TOKEN": "kept",
                },
                clear=True,
            ),
            patch("scripts.bootstrap_railway.subprocess.run", side_effect=fake_run),
        ):
            Railway(
                ["railway"], {"CUSTOM_OPENAI_KEY"}
            ).run(["whoami"], quiet=True)

        self.assertNotIn("OPENAI_API_KEY", captured)
        self.assertNotIn("FEISHU_APP_SECRET", captured)
        self.assertNotIn("CUSTOM_OPENAI_KEY", captured)
        self.assertEqual(captured["RAILWAY_TOKEN"], "kept")


if __name__ == "__main__":
    unittest.main()
