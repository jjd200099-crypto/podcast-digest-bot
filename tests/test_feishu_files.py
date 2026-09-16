import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.feishu import (
    FEISHU_API,
    MAX_FILE_BYTES,
    FeishuMessenger,
    file_delivery_part,
    idempotency_uuid,
    sanitize_file_name,
)
from news_officer.models import OutboxItem


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"code": 0}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FeishuFileTests(unittest.TestCase):
    def messenger_with_token(self):
        messenger = FeishuMessenger("app", "secret")
        messenger._token = "token"
        messenger._token_expires_at = time.monotonic() + 1000
        return messenger

    def test_sanitize_file_name_removes_paths_controls_and_unsafe_characters(self):
        self.assertEqual(
            sanitize_file_name(" ../../folder\\Episode\nOne?:.md "),
            "Episode_One_.md",
        )
        self.assertEqual(sanitize_file_name("..."), "transcript.md")
        self.assertEqual(sanitize_file_name("  "), "transcript.md")
        self.assertEqual(sanitize_file_name("播客文字稿.md"), "播客文字稿.md")

    @patch("news_officer.feishu.requests.post")
    def test_upload_file_uses_expected_multipart_payload(self, post):
        post.return_value = FakeResponse(
            body={"code": 0, "data": {"file_key": "file_123"}}
        )
        messenger = self.messenger_with_token()

        file_key = messenger.upload_file(b"# Full transcript", "../Episode?.md")

        self.assertEqual(file_key, "file_123")
        post.assert_called_once()
        call = post.call_args
        self.assertEqual(call.args[0], f"{FEISHU_API}/im/v1/files")
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer token"})
        self.assertEqual(
            call.kwargs["data"],
            {"file_type": "stream", "file_name": "Episode_.md"},
        )
        self.assertEqual(
            call.kwargs["files"]["file"],
            ("Episode_.md", b"# Full transcript", "application/octet-stream"),
        )
        self.assertEqual(call.kwargs["timeout"], 30)

    @patch("news_officer.feishu.requests.post")
    def test_upload_file_rejects_empty_and_oversized_content_before_network(self, post):
        messenger = self.messenger_with_token()
        self.assertEqual(MAX_FILE_BYTES, 30 * 1024 * 1024)

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            messenger.upload_file(b"", "empty.txt")
        with (
            patch("news_officer.feishu.MAX_FILE_BYTES", 4),
            self.assertRaisesRegex(ValueError, "exceeds Feishu"),
        ):
            messenger.upload_file(b"12345", "large.txt")

        post.assert_not_called()

    @patch("news_officer.feishu.requests.post")
    def test_upload_file_refreshes_token_after_401_and_retries(self, post):
        post.side_effect = [
            FakeResponse(status_code=401),
            FakeResponse(
                body={"code": 0, "tenant_access_token": "fresh", "expire": 7200}
            ),
            FakeResponse(body={"code": 0, "data": {"file_key": "file_fresh"}}),
        ]
        messenger = self.messenger_with_token()

        self.assertEqual(messenger.upload_file(b"text", "episode.txt"), "file_fresh")

        upload_calls = [
            call for call in post.call_args_list if call.args[0].endswith("/im/v1/files")
        ]
        self.assertEqual(len(upload_calls), 2)
        self.assertEqual(
            upload_calls[0].kwargs["headers"]["Authorization"], "Bearer token"
        )
        self.assertEqual(
            upload_calls[1].kwargs["headers"]["Authorization"], "Bearer fresh"
        )
        self.assertEqual(upload_calls[0].kwargs["data"], upload_calls[1].kwargs["data"])
        self.assertEqual(
            upload_calls[0].kwargs["files"], upload_calls[1].kwargs["files"]
        )

    @patch("news_officer.feishu.time.sleep")
    @patch("news_officer.feishu.requests.post")
    def test_upload_file_retries_retryable_statuses(self, post, sleep):
        post.side_effect = [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(503),
            FakeResponse(body={"code": 0, "data": {"file_key": "file_retry"}}),
        ]
        messenger = self.messenger_with_token()

        self.assertEqual(messenger.upload_file(b"text", "episode.txt"), "file_retry")

        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 1.0])

    @patch("news_officer.feishu.requests.post")
    def test_upload_file_requires_file_key_in_success_response(self, post):
        post.return_value = FakeResponse(body={"code": 0, "data": {}})

        with self.assertRaisesRegex(RuntimeError, "missing file_key"):
            self.messenger_with_token().upload_file(b"text", "episode.txt")

    def test_file_delivery_part_is_stable_and_uses_file_message_content(self):
        first = file_delivery_part("file_123", "digest:episode", 2)
        second = file_delivery_part("file_123", "digest:episode", 2)

        self.assertEqual(first, second)
        self.assertEqual(first[0], "file")
        self.assertEqual(json.loads(first[1]), {"file_key": "file_123"})
        self.assertEqual(first[2], idempotency_uuid("digest:episode", 2))
        self.assertNotEqual(
            first[2], file_delivery_part("file_123", "digest:episode", 3)[2]
        )

    @patch("news_officer.feishu.requests.post")
    def test_deliver_returns_remote_message_id(self, post):
        post.return_value = FakeResponse(
            body={"code": 0, "data": {"message_id": "om_remote"}}
        )
        msg_type, content, item_uuid = file_delivery_part(
            "file_123", "digest:episode", 1
        )
        item = OutboxItem(
            id=1,
            job_key="daily:1",
            group_key="digest:episode",
            delivery_key="digest:episode",
            operation="send",
            target_id="oc_chat",
            target_type="chat_id",
            reply_in_thread=False,
            part=1,
            total_parts=1,
            msg_type=msg_type,
            content=content,
            uuid=item_uuid,
        )

        message_id = self.messenger_with_token().deliver(item)

        self.assertEqual(message_id, "om_remote")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msg_type"], "file")
        self.assertEqual(json.loads(payload["content"]), {"file_key": "file_123"})
        self.assertEqual(payload["uuid"], item_uuid)


if __name__ == "__main__":
    unittest.main()
