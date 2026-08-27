import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


requests_stub = types.ModuleType("requests")
requests_stub.Response = object
requests_stub.post = None
sys.modules.setdefault("requests", requests_stub)
openai_stub = types.ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)
sys.path.insert(0, str(ROOT / "src"))

import digest
import handle_request


class DummyFeishuResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"code": 0}


class RoutingTests(unittest.TestCase):
    def test_branding_is_stable(self):
        branded = digest.brand_message("测试正文")
        self.assertEqual(branded, f"{digest.BRAND_HEADER}\n\n测试正文")
        self.assertEqual(digest.brand_message(branded), branded)

    def test_message_split_handles_one_oversized_paragraph(self):
        chunks = digest.split_feishu_message("洞察" * 2_000, max_bytes=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 500 for chunk in chunks))

    def test_vtt_must_cover_nearly_the_whole_episode(self):
        complete = "\n\n".join(
            f"00:{minute:02d}:00.000 --> 00:{minute:02d}:50.000\n第 {minute} 分钟"
            for minute in range(10)
        )
        partial = complete.split("\n\n")[0] + "\n\n00:09:50.000 --> 00:10:00.000\n结尾"
        self.assertTrue(digest.vtt_covers_episode(complete, 600))
        self.assertFalse(digest.vtt_covers_episode(partial, 600))

    def test_request_without_link_returns_help(self):
        self.assertIn("完整文字稿", handle_request.build_reply("你能做什么？"))

    def test_reply_uses_body_uuid_for_idempotency(self):
        with patch.object(digest, "feishu_token", return_value="token"), patch.object(
            digest.requests, "post", return_value=DummyFeishuResponse()
        ) as post:
            digest.reply_feishu("结果", "om_123", "evt-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["uuid"], digest.feishu_uuid("evt-1", 1))
        self.assertLessEqual(len(payload["uuid"]), 50)
        self.assertNotIn("params", post.call_args.kwargs)

    def test_link_requires_complete_transcript(self):
        with patch.object(handle_request, "video_metadata", return_value={
            "id": "x", "title": "Test", "url": "https://www.youtube.com/watch?v=x", "channel": "Channel",
            "duration_seconds": 600, "duration_string": "10:00",
        }), patch.object(handle_request, "fetch_full_subtitles", return_value=None):
            reply = handle_request.build_reply("请分析 https://www.youtube.com/watch?v=x")
        self.assertIn("未取得完整文字稿，本次不摘要", reply)

    def test_non_youtube_url_is_not_fetched(self):
        with patch.object(handle_request, "video_metadata") as metadata:
            reply = handle_request.build_reply("请分析 https://example.com/private")
        metadata.assert_not_called()
        self.assertIn("只接受公开 YouTube 链接", reply)

if __name__ == "__main__":
    unittest.main()
