import copy
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import requests
from agents import Model, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.daily_archive import read_daily_digest
from news_officer.feishu import delivery_parts
from news_officer.library import (
    FeishuLibraryAPI,
    LibraryDocument,
    LibraryError,
    PodcastLibrary,
    block_signature,
    markdown_blocks,
)
from news_officer.models import DailyItem, Episode, IncomingMessage, Transcript
from news_officer.podcast import PodcastService, _readable_attachment
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import PodcastResearchAgent, ResearchTools
from news_officer.research_context import previous_task, quoted_context
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


class ScriptedModel(Model):
    """Fake inference only; the real SDK Runner executes every tool and turn."""

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.inputs = []
        self.settings = []

    async def get_response(self, **kwargs):
        self.inputs.append(copy.deepcopy(kwargs["input"]))
        self.settings.append(kwargs["model_settings"])
        return ModelResponse(next(self.outputs), Usage(requests=1), None)

    async def stream_response(self, **kwargs):
        raise NotImplementedError
        yield


def tool_call(name, args=None, call_id="c1"):
    return [
        ResponseFunctionToolCall(
            type="function_call",
            name=name,
            arguments=json.dumps(args or {}),
            call_id=call_id,
        )
    ]


def final_output(value):
    return [
        ResponseOutputMessage(
            type="message",
            id="answer",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text", text=json.dumps(value), annotations=[]
                )
            ],
        )
    ]


class FakeAPI:
    def __init__(self):
        self.catalog = {"folder": []}
        self.data = {}
        self.creates = 0
        self.appends = 0
        self.fail_after_write = False

    def files(self, folder):
        return copy.deepcopy(self.catalog[folder])

    def create(self, folder, title):
        self.creates += 1
        token = f"doc{self.creates}"
        self.catalog[folder].append(
            {
                "token": token,
                "name": title,
                "type": "docx",
                "url": f"https://feishu.cn/docx/{token}",
            }
        )
        self.data[token] = []
        return token

    def blocks(self, token):
        return copy.deepcopy(self.data[token])

    def append(self, token, blocks, start, identity):
        self.appends += 1
        blocks = [dict(b, parent_id=token) for b in copy.deepcopy(blocks)]
        self.data[token][start:start] = blocks
        if self.fail_after_write:
            self.fail_after_write = False
            raise LibraryError("ambiguous network failure")

    def text(self, token):
        return "\n".join(block_signature(b)[1] for b in self.data[token])


class LibraryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.store.initialize()
        self.api = FakeAPI()
        self.library = PodcastLibrary(self.store, self.api, "folder")
        self.library.initialize()
        self.feeds = Path(self.temp.name) / "feeds.json"
        self.feeds.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "name": "Original Show",
                            "type": "rss",
                            "rss_url": "https://example.test/rss",
                        }
                    ]
                }
            )
        )
        self.registry = SourceRegistry(self.store, self.feeds)
        self.registry.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def archive_record(self, identity="episode", text=None):
        ep = Episode(
            identity,
            "Acquisition strategy",
            "https://example.test/ep",
            "Example",
            published_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        return self.store.save_verified_transcript(
            ep,
            Transcript(
                text
                or "Luca: We acquire software businesses and improve their operations.",
                "official",
                "https://example.test/transcript",
                True,
            ),
        )


class LibraryTests(LibraryFixture):
    def test_one_failed_document_does_not_starve_other_episodes(self):
        first = self.archive_record("first")
        second = self.archive_record("second")
        self.api.fail_after_write = True
        with self.assertRaises(LibraryError):
            self.library.archive_pending()
        with self.store._connect() as db:
            states = dict(
                db.execute("SELECT episode_id,status FROM library_publications")
            )
        self.assertEqual(states[first.episode.id], "pending")
        self.assertEqual(states[second.episode.id], "complete")
        self.assertEqual(self.library.archive_pending(), ["doc1"])

    def test_document_timeout_does_not_hide_other_documents(self):
        self.archive_record("first")
        self.archive_record("second")
        self.library.archive_pending()
        text = self.api.text

        def read(token):
            if token == "doc1":
                raise requests.Timeout("credentials must not be copied to warnings")
            return text(token)

        with patch.object(self.api, "text", side_effect=read):
            docs, warnings = self.library.snapshot()
        self.assertEqual([d.token for d in docs], ["doc2"])
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("credentials", warnings[0])

    def test_subfolder_timeout_is_disclosed_without_hiding_root_docs(self):
        self.archive_record()
        self.library.archive_pending()
        self.api.catalog["folder"].append(
            {"token": "sub", "name": "Sub", "type": "folder"}
        )
        listing = self.api.files

        def files(folder):
            if folder == "sub":
                raise requests.Timeout()
            return listing(folder)

        with patch.object(self.api, "files", side_effect=files):
            docs, warnings = self.library.snapshot()
        self.assertEqual(len(docs), 1)
        self.assertIn("子文件夹", warnings[0])

    def test_verified_archive_and_idempotent_repeat(self):
        self.archive_record()
        self.assertEqual(self.library.archive_pending(), ["doc1"])
        self.assertIn("acquire software", self.api.text("doc1"))
        self.assertEqual(self.library.archive_pending(), [])
        self.assertEqual(self.api.creates, 1)

    def test_restart_resumes_ambiguous_append_without_duplicate(self):
        self.archive_record(
            text="\n\n".join(
                f"Luca: Detail {i} about acquiring a software business."
                for i in range(120)
            )
        )
        self.api.fail_after_write = True
        with self.assertRaises(LibraryError):
            self.library.archive_pending()
        resumed = PodcastLibrary(self.store, self.api, "folder")
        self.assertEqual(resumed.archive_pending(), ["doc1"])
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM library_publications").fetchone()
        self.assertEqual(
            len(self.api.data["doc1"]), len(json.loads(row["blocks_json"]))
        )
        self.assertEqual(self.api.creates, 1)

    def test_human_edit_during_partial_write_is_not_overwritten(self):
        self.archive_record()
        self.api.fail_after_write = True
        with self.assertRaises(LibraryError):
            self.library.archive_pending()
        self.api.data["doc1"][0] = dict(
            markdown_blocks("Human note")[0], parent_id="doc1"
        )
        before = copy.deepcopy(self.api.data)
        with self.assertRaisesRegex(LibraryError, "修改"):
            self.library.archive_pending()
        self.assertEqual(self.api.data, before)

    def test_ambiguous_create_is_excluded_then_recovered_by_title(self):
        self.archive_record()
        create = self.api.create

        def ambiguous(folder, title):
            create(folder, title)
            raise LibraryError("lost create response")

        with (
            patch.object(self.api, "create", side_effect=ambiguous),
            self.assertRaises(LibraryError),
        ):
            self.library.archive_pending()
        docs, warnings = self.library.snapshot()
        self.assertEqual(docs, [])
        self.assertTrue(warnings)
        self.assertEqual(self.library.archive_pending(), ["doc1"])
        self.assertEqual(self.api.creates, 1)

    def test_pending_document_excluded_then_reads_live_edits(self):
        self.archive_record()
        self.api.fail_after_write = True
        with self.assertRaises(LibraryError):
            self.library.archive_pending()
        docs, warnings = self.library.snapshot()
        self.assertEqual(docs, [])
        self.assertTrue(warnings)
        self.library.archive_pending()
        self.api.data["doc1"].extend(
            dict(b, parent_id="doc1") for b in markdown_blocks("New human evidence")
        )
        docs, _ = self.library.snapshot()
        self.assertIn("New human evidence", docs[0].text)

    def test_removed_document_not_searched_or_recreated(self):
        self.archive_record()
        self.library.archive_pending()
        self.api.catalog["folder"] = []
        self.assertEqual(self.library.snapshot()[0], [])
        self.library.archive_pending()
        self.assertEqual(self.api.creates, 1)

    def test_all_transcripts_beyond_recent_ten_are_archived(self):
        for i in range(12):
            self.archive_record(f"episode-{i}")
        self.assertEqual(len(self.library.archive_pending()), 12)

    def test_nested_folders_and_read_failures_are_visible(self):
        self.api.catalog["folder"] = [
            {"token": "child", "name": "sub", "type": "folder"}
        ]
        self.api.catalog["child"] = [
            {"token": "private", "name": "Private doc", "type": "docx"}
        ]
        with patch.object(self.api, "text", side_effect=LibraryError("403")):
            docs, warnings = self.library.snapshot()
        self.assertEqual(docs, [])
        self.assertIn("读取失败", warnings[0])

    def test_broken_pagination_fails_closed(self):
        api = FeishuLibraryAPI(None)
        with (
            patch.object(api, "request", return_value={"files": [], "has_more": True}),
            self.assertRaises(LibraryError),
        ):
            api.files("folder")

    def test_pagination_uses_next_token(self):
        api = FeishuLibraryAPI(None)
        with patch.object(
            api,
            "request",
            side_effect=[
                {"files": [1], "has_more": True, "next_page_token": "page2"},
                {"files": [2], "has_more": False},
            ],
        ) as request:
            self.assertEqual(api.files("folder"), [1, 2])
            self.assertEqual(request.call_args.kwargs["params"]["page_token"], "page2")

    def test_markdown_headings_links_bold_and_long_paragraphs(self):
        blocks = markdown_blocks(
            "# Title\n\n## Topic\n**Guest**: hi [source](https://example.test/)\n"
            + "x" * 5000
        )
        self.assertEqual(blocks[0]["block_type"], 4)
        self.assertEqual(block_signature(blocks[1])[1], "Guest: hi source")
        self.assertEqual(sum(len(block_signature(b)[1]) for b in blocks[2:]), 5000)


class SourceTests(LibraryFixture):
    def test_daily_discovery_uses_durable_added_sources(self):
        source = {
            "name": "Added Show",
            "type": "rss",
            "rss_url": "https://example.test/new",
            "url": "",
            "enabled": True,
        }
        with patch.object(self.registry, "validate", return_value=source):
            self.registry.add(source, "user")
        service = PodcastService(
            self.store, self.feeds, SimpleNamespace(), source_registry=self.registry
        )
        ep = Episode(
            "new",
            "New release",
            "https://example.test/new-ep",
            "Added Show",
            published_at=datetime.now(UTC),
        )
        with patch(
            "news_officer.podcast.latest_rss_episodes", return_value=[ep]
        ) as fetch:
            service.discover_daily_candidates()
        self.assertIn(
            "https://example.test/new", [call.args[1] for call in fetch.call_args_list]
        )

    def test_validate_and_add_survives_new_registry(self):
        xml = b'<rss><channel><title>New Show</title><item><enclosure url="https://example.test/a.mp3"/></item></channel></rss>'
        with patch(
            "news_officer.source_registry._response",
            return_value=SimpleNamespace(content=xml),
        ):
            result = self.registry.add(
                {"rss_url": "https://example.test/new.xml"}, "user"
            )
            self.assertEqual(result["status"], "added")
            self.assertEqual(
                self.registry.add(result["source"], "user")["status"], "already_tracked"
            )
        refreshed = SourceRegistry(self.store, self.feeds)
        self.assertEqual(
            [s["name"] for s in refreshed.list()], ["Original Show", "New Show"]
        )

    def test_invalid_feed_rejected(self):
        with (
            patch(
                "news_officer.source_registry._response",
                return_value=SimpleNamespace(content=b"<html/>"),
            ),
            self.assertRaises(ValueError),
        ):
            self.registry.validate("https://example.test/not-rss")

    def test_recent_applies_dates_without_requiring_transcripts(self):
        now = datetime.now(UTC)
        episodes = [
            Episode(
                str(days),
                "New episode",
                "https://example.test/ep",
                "Original Show",
                published_at=now - timedelta(days=days),
            )
            for days in [2, 6, 10]
        ]
        with patch(
            "news_officer.source_registry.latest_rss_episodes", return_value=episodes
        ):
            result = self.registry.recent(7)
        self.assertEqual(len(result["episodes"]), 2)
        self.assertEqual(result["failures"], [])

    def test_failed_source_not_reported_as_no_updates(self):
        with patch(
            "news_officer.source_registry.latest_rss_episodes",
            side_effect=RuntimeError(),
        ):
            result = self.registry.recent(7)
        self.assertEqual(len(result["failures"]), 1)


class ResearchTests(LibraryFixture):
    def test_unauthorized_conversation_never_reaches_expression_provider(self):
        from dataclasses import replace
        self.agent.tone_advisor = SimpleNamespace(advise=AsyncMock())
        self.agent.handle('你好', replace(self.message, chat_type='group', chat_id='not-authorized'))
        self.agent.tone_advisor.advise.assert_not_awaited()

    def test_expression_failure_fallback_still_reaches_main_model(self):
        self.agent.tone_advisor = SimpleNamespace(advise=AsyncMock(return_value=None))
        expected = '你好，今天想聊哪期播客？'
        self.agent.sdk_model = ScriptedModel([
            final_output({'kind': 'conversation', 'message': expected, 'points': []})])
        self.assertEqual(self.agent.handle('你好', self.message).messages[0], expected)

    def test_expression_advice_reaches_main_model_but_does_not_replace_its_answer(self):
        from test_tone_advisor import PLAN
        self.agent.tone_advisor = SimpleNamespace(advise=AsyncMock(return_value=PLAN))
        expected = '可以。把你想讨论的那期发来，我们就从你最关心的问题聊起。'
        model = self.agent.sdk_model = ScriptedModel([
            final_output({'kind': 'conversation', 'message': expected, 'points': []})])
        response = self.agent.handle('你好', self.message)
        self.assertEqual(response.messages[0], expected)
        context = json.loads(model.inputs[0][-1]['content'])
        self.assertEqual(context['expression_advice'], PLAN)
        # Repeated delivery must reuse the committed answer, not call either model again.
        self.agent.handle('你好', self.message)
        self.agent.tone_advisor.advise.assert_awaited_once()

    def test_actual_half_sentence_is_repaired_before_commit(self):
        self.agent.sdk_model = ScriptedModel([
            final_output({'kind': 'conversation', 'message': '主要错误有：', 'points': []}),
            final_output({'kind': 'conversation', 'message': '刚才只有开头，没有回答你的问题。应当补全具体原因和下一步。', 'points': []}),
        ])
        reply = self.agent.handle('你说话没说完', self.message)
        self.assertIn('补全具体原因', reply.messages[0])
        with self.store._connect() as db:
            audit = json.loads(db.execute('SELECT audit_json FROM research_run_state').fetchone()[0])
            self.assertEqual(db.execute('SELECT answer FROM research_turns').fetchone()[0], reply.messages[0])
        self.assertEqual(len(audit['validation_errors']), 1)

    def test_answer_preamble_is_not_silently_discarded(self):
        self.state.evidence_item('Source successfully added', identity='E0001')
        with self.assertRaisesRegex(ValueError, 'Do not lose answer text'):
            self.state.render({'kind': 'answer', 'message': '已添加成功。', 'points': [
                {'text': 'Practical AI', 'citations': [{'id': 'E0001'}]}]})

    def test_general_questions_do_not_require_podcast_citations(self):
        answer = 'Agent 会选择工具并根据结果继续执行。普通聊天主要生成回答。\n\n例如查播客时，Agent 先定位节目，再读全文。'
        self.agent.sdk_model = ScriptedModel([final_output({'kind': 'conversation', 'message': answer, 'points': []})])
        self.assertEqual(self.agent.handle('解释 Agent 和聊天机器人的区别', self.message).messages[0], answer)

    def test_diagnosis_requires_real_session_tool(self):
        from dataclasses import replace
        message = replace(self.message, text='为什么你说话会截断')
        self.agent.sdk_model = ScriptedModel([
            final_output({'kind': 'conversation', 'message': '可能是网络问题。', 'points': []}),
            tool_call('get_request_status'),
            final_output({'kind': 'conversation', 'message': '当前会话没有足够历史记录，不能确认截断原因；不能据此断言网络故障。', 'points': []}),
        ])
        self.assertIn('不能确认', self.agent.handle(message.text, message).messages[0])

    def test_feature_request_is_durable_idempotent_not_implemented(self):
        first = self.state.execute('record_feature_request', {'summary': '增加每周播客对比功能'})
        again = self.state.execute('record_feature_request', {'summary': '重复登记'})
        self.assertEqual(first, again)
        self.assertEqual(first['status'], 'recorded')
        self.assertIn('尚未实现', first['meaning'])

    def test_diagnostics_never_read_other_members(self):
        from news_officer.request_status import request_status
        for user in ('user', 'colleague'):
            message = IncomingMessage('status-' + user, 'team', '检查记录', 'group', user)
            self.store.enqueue('message:' + message.message_id, 'message', message.__dict__)
            with self.store._connect() as db:
                db.execute('INSERT INTO research_turns(session,message_id,question,answer) VALUES (?,?,?,?)',
                           (message.conversation_key, message.message_id, user + '-question', user + '-answer'))
        result = request_status(self.store, 'group:team:user')
        self.assertEqual(len(result['requests']), 1)
        self.assertNotIn('colleague', json.dumps(result))

    def saved_daily(self):
        record = self.archive_record()
        digest = "推荐理由：具体讨论经营方法。\n\n1. 观点来自完整文字稿。\n\n推荐星级：★★★★☆"
        self.store.save_transcript_digest(record.episode.id, digest,
                                         record.content_sha256, record.record_revision_sha256)
        key = "daily:2026-09-17"
        self.store.enqueue(key, "daily", {"scheduled_for": "2026-09-17T08:30:00+08:00"})
        item = DailyItem(record.episode, "summarized", digest, _readable_attachment(record, digest))
        self.store.save_job_result(key, "episode:episode", "daily_item", item.to_persisted_dict())
        self.store.mark_analysis_complete(key)
        return record, item

    def test_daily_tool_shared_by_private_and_different_group_members(self):
        self.saved_daily()
        replies = []
        for index, (chat, kind, user) in enumerate([
            ("private", "p2p", "user"), ("team", "group", "user"),
            ("team", "group", "colleague"),
        ]):
            self.agent.sdk_model = ScriptedModel([
                tool_call("get_daily_digest", {"date": "2026-09-17"}),
                final_output({"kind": "conversation", "message": "", "points": []}),
            ])
            message = IncomingMessage(f"daily-{index}", chat, "发一下今天的日报", kind, user)
            reply = self.agent.handle(message.text, message)
            replies.append(reply.messages)
            self.assertFalse(reply.attachments)
            self.assertIn("推荐星级", reply.messages[0])
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[1], replies[2])

    def test_daily_missing_does_not_claim_no_new_podcasts(self):
        value = self.state.execute("get_daily_digest", {"date": "2026-09-17"})
        self.assertEqual(value["status"], "not_generated")
        reply = self.state.render({"kind": "conversation", "message": "", "points": []})
        self.assertIn("尚未生成或归档", reply)

    def test_daily_reads_delivery_date_not_episode_publication_date(self):
        self.saved_daily()
        self.assertEqual(read_daily_digest(self.store, "2026-09-17")["count"], 1)
        self.assertEqual(read_daily_digest(self.store, "2026-09-14")["status"], "not_generated")
        with self.assertRaises(ValueError):
            read_daily_digest(self.store, "yesterday")

    def test_daily_rejects_stale_transcript_revision(self):
        record, _ = self.saved_daily()
        self.store.save_verified_transcript(record.episode, Transcript("changed full text", "official", "https://example.test/transcript", True))
        report = read_daily_digest(self.store, "2026-09-17")
        self.assertEqual(report["count"], 0)
        self.assertIn("版本不一致", report["markdown"])

    def test_daily_retry_does_not_duplicate_summary(self):
        _, item = self.saved_daily()
        self.store.enqueue("daily:retry", "daily", {"scheduled_for": "2026-09-17T10:00:00+08:00"})
        self.store.save_job_result("daily:retry", "episode:episode", "daily_item", item.to_persisted_dict())
        report = read_daily_digest(self.store, "2026-09-17")
        self.assertEqual(report["count"], 1)
        self.assertIn("未完成", report["markdown"])

    def test_recent_metadata_reaches_analysis_without_url_adapter_gate(self):
        episode = Episode("rss:fixture", "A new podcast", "https://example.test/ep", "Original Show",
                          published_at=datetime.now(UTC), metadata={"audio_url": "https://example.test/audio.mp3", "rss_feed_url": "https://example.test/rss"})
        seen = []
        def analyze(value):
            seen.append(value)
            return SimpleNamespace(message="完整文字稿摘要")
        self.agent.podcast_service = SimpleNamespace(analyze_discovered_episode=analyze)
        with patch("news_officer.source_registry.latest_rss_episodes", return_value=[episode]):
            payload = self.state.execute("recent_updates", {"days": 1, "show": ""})
        self.assertNotIn("audio.mp3", json.dumps(payload))
        result = self.state.execute("analyze_podcast", {"url": episode.url})
        self.assertIn("摘要", result["text"])
        self.assertEqual(seen, [episode])

    def test_same_url_multiple_episodes_is_not_silently_misidentified(self):
        episodes = [Episode(f"rss:{i}", f"Episode {i}", "https://example.test/feed", "Original Show", published_at=datetime.now(UTC)) for i in range(2)]
        self.agent.podcast_service = SimpleNamespace()
        with patch("news_officer.source_registry.latest_rss_episodes", return_value=episodes):
            self.state.execute("recent_updates", {"days": 1, "show": ""})
        result = self.state.execute("analyze_podcast", {"url": episodes[0].url})
        self.assertIn("无法唯一定位", result["error"])

    def test_discovered_rss_reuses_verified_digest_without_new_model_call(self):
        record, item = self.saved_daily()
        service = PodcastService(self.store, self.feeds, SimpleNamespace())
        with patch.object(service.transcript_resolver, "fetch", side_effect=AssertionError):
            result = service.analyze_discovered_episode(record.episode)
        self.assertEqual(result.message, item.message)

    def test_podwise_citation_uses_public_episode_page_not_token_required_api(self):
        record = self.archive_record()
        self.store.save_verified_transcript(record.episode, Transcript(
            record.transcript.text, "Podwise", "https://app.podwise.ai/api/open/v1/episodes/123/transcripts", True))
        docs, _ = PodcastArchive(self.store).snapshot()
        self.assertEqual(docs[0].url, "https://podwise.ai/episodes/123")

    def setUp(self):
        super().setUp()
        self.agent = PodcastResearchAgent(
            self.store,
            self.registry,
            self.library,
            "test-key",
            "test-model",
            users=("user",),
            chats=("team",),
        )
        self.agent.initialize()
        self.message = IncomingMessage("m1", "team", "帮我添加追踪", "group", "user")
        self.state = ResearchTools(self.agent, "group:team:user", self.message)

    def test_public_archive_reads_verified_text_without_any_feishu_call(self):
        record = self.archive_record()
        self.agent.library = PodcastArchive(self.store)
        with patch.object(self.api, "files", side_effect=AssertionError):
            results = self.state.execute(
                "search_library", {"query": "acquire software"}
            )
        self.assertTrue(results["matches"])
        self.assertIn(record.transcript.source_url, results["matches"][0]["url"])
        self.assertIn(record.reference, self.state.documents)

    def test_public_archive_download_survives_duplicate_event(self):
        record = self.archive_record()
        self.agent.library = PodcastArchive(self.store)
        model = self.agent.sdk_model = ScriptedModel(
            [
                tool_call("get_transcript", {"reference": record.reference}),
                final_output(
                    {"kind": "conversation", "message": "已附文字稿。", "points": []}
                ),
            ]
        )
        first = self.agent.handle("发文字稿", self.message)
        second = self.agent.handle("发文字稿", self.message)
        self.assertEqual(first, second)
        self.assertEqual(len(first.attachments), 1)
        self.assertEqual(first.attachments[0].episode_id, record.episode.id)
        self.assertEqual(first.attachment_episode_ids, (record.episode.id,))
        self.assertEqual(len(model.inputs), 2)

    def test_private_folder_cannot_fall_back_to_public_archive(self):
        record = self.archive_record()
        reply = self.state.execute("get_transcript", {"reference": record.reference})
        self.assertIn("error", reply)
        self.assertEqual(self.state.attachments, [])

    def test_public_archive_prompt_discloses_organization_docs_unavailable(self):
        self.agent.library = PodcastArchive(self.store)
        self.agent.sdk_model = ScriptedModel(
            [
                final_output(
                    {
                        "kind": "conversation",
                        "message": "组织云文档暂未接通。",
                        "points": [],
                    }
                )
            ]
        )
        from news_officer.agent_runtime import Runner

        with patch("news_officer.agent_runtime.Runner.run", wraps=Runner.run) as run:
            self.agent.handle("读组织文档", self.message)
        prompt = run.call_args.args[0].instructions
        self.assertIn("组织云文档尚未接通", prompt)
        self.assertIn("公开播客档案模式", prompt)

    def test_unknown_show_filter_requests_correction_not_no_updates(self):
        result = self.state.execute(
            "recent_updates", {"days": 7, "show": "全部追踪节目"}
        )
        self.assertIn("error", result)
        self.assertIn("空字符串", result["error"])
        self.assertIn("Original Show", result["available_sources"])
        self.assertNotIn("episodes", result)
        self.assertEqual(self.state.evidence, {})

    def test_successful_empty_update_query_has_citable_scope_evidence(self):
        response = {
            "from": "2026-09-10T00:00:00+00:00",
            "to": "2026-09-17T00:00:00+00:00",
            "checked": ["Original Show"],
            "episodes": [],
            "failures": [],
            "note": "RSS metadata only",
        }
        with patch.object(self.registry, "recent", return_value=response):
            result = self.state.execute("recent_updates", {"days": 7, "show": ""})
        scope = result["scope_evidence"]
        rendered = self.state.render(
            {
                "kind": "answer",
                "message": "",
                "points": [
                    {
                        "text": "该节目过去七天的 RSS 未找到更新。",
                        "citations": [
                            {"id": scope["evidence_id"], "quote": '"total": 0'}
                        ],
                    }
                ],
            }
        )
        self.assertIn("2026-09-10", rendered)
        self.assertIn("找到 0 期", rendered)
        self.assertEqual(result["total"], 0)

    def test_update_directory_keeps_all_metadata_and_discards_model_miscounts(self):
        response = {
            "from": "2026-09-10T00:00:00+00:00",
            "to": "2026-09-17T00:00:00+00:00",
            "checked": ["Original Show"],
            "episodes": [
                {
                    "title": f"Episode {i}",
                    "show": "Original Show",
                    "url": f"https://example.test/episode-{i}",
                    "published_at": "2026-09-15T20:00:00+00:00",
                }
                for i in range(26)
            ],
            "failures": [],
            "note": "RSS metadata only",
        }
        with patch.object(self.registry, "recent", return_value=response):
            result = self.state.execute("recent_updates", {"days": 7, "show": ""})
        rendered = self.state.render(
            {
                "kind": "answer",
                "message": "",
                "points": [
                    {
                        "text": "原节目更新了七期，证明 AI 已实现 AGI。",
                        "citations": [
                            {
                                "id": result["episodes"][0]["evidence_id"],
                                "quote": "Episode",
                            }
                        ],
                    }
                ],
            }
        )
        self.assertIn("找到 26 期", rendered)
        self.assertNotIn("更新了七期", rendered)
        self.assertNotIn("已实现 AGI", rendered)
        self.assertIn("09-16", rendered)  # UTC publication converted to Shanghai.
        for i in range(26):
            self.assertIn(f"[Episode {i}](https://example.test/episode-{i})", rendered)

    def test_outside_folder_read_rejected(self):
        with self.assertRaises(KeyError):
            self.state.execute("read_document", {"document_id": "outside", "start": 0})

    def test_search_reads_document_body_not_title(self):
        self.archive_record()
        self.library.archive_pending()
        found = self.state.execute("search_library", {"query": "acquire software"})
        self.assertTrue(found["matches"])
        self.assertIn("acquire software", found["matches"][0]["text"])

    def test_same_turn_source_confirmation_rejected(self):
        with patch.object(
            self.registry,
            "validate",
            return_value={"name": "New", "rss_url": "https://example.test/new"},
        ):
            proposal = self.state.execute(
                "propose_source", {"rss_url": "https://example.test/new"}
            )
        with self.assertRaises(ValueError):
            self.state.execute(
                "confirm_source", {"proposal_id": proposal["proposal_id"]}
            )

    def test_source_confirmation_is_session_and_user_message_scoped(self):
        source = {"name": "New", "rss_url": "https://example.test/new"}
        with patch.object(self.registry, "validate", return_value=source):
            proposal = self.state.execute(
                "propose_source", {"rss_url": source["rss_url"]}
            )
        message = IncomingMessage("m2", "team", "确认添加", "group", "user")
        other = ResearchTools(self.agent, "other-session", message)
        with self.assertRaises(ValueError):
            other.execute("confirm_source", {"proposal_id": proposal["proposal_id"]})
        same = ResearchTools(self.agent, self.state.key, message)
        with patch.object(
            self.registry, "add", return_value={"status": "added"}
        ) as add:
            same.execute("confirm_source", {"proposal_id": proposal["proposal_id"]})
            add.assert_called_once()

    def test_unrelated_followup_cannot_authorize_write(self):
        source = {"name": "New", "rss_url": "https://example.test/new"}
        with patch.object(self.registry, "validate", return_value=source):
            proposal = self.state.execute(
                "propose_source", {"rss_url": source["rss_url"]}
            )
        message = IncomingMessage("m2", "team", "你好", "group", "user")
        other = ResearchTools(self.agent, self.state.key, message)
        result = other.execute(
            "confirm_source", {"proposal_id": proposal["proposal_id"]}
        )
        self.assertIn("error", result)

    def test_confirmation_accepts_only_the_actual_candidate_name(self):
        source = {"name": "Hard Fork", "rss_url": "https://example.test/new"}
        with patch.object(self.registry, "validate", return_value=source):
            proposal = self.state.execute(
                "propose_source", {"rss_url": source["rss_url"]}
            )
        valid = IncomingMessage("m2", "team", "确认添加 Hard Fork", "group", "user")
        invalid = IncomingMessage(
            "m3", "team", "确认添加 Different Show", "group", "user"
        )
        with patch.object(
            self.registry, "add", return_value={"status": "added"}
        ) as add:
            ResearchTools(self.agent, self.state.key, valid).execute(
                "confirm_source", {"proposal_id": proposal["proposal_id"]}
            )
            result = ResearchTools(self.agent, self.state.key, invalid).execute(
                "confirm_source", {"proposal_id": proposal["proposal_id"]}
            )
        self.assertEqual(add.call_count, 1)
        self.assertIn("error", result)

    def test_fabricated_evidence_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown evidence"):
            self.state.render(
                {
                    "kind": "answer",
                    "message": "",
                    "points": [
                        {
                            "text": "claim",
                            "citations": [{"id": "invented", "quote": "quote"}],
                        }
                    ],
                }
            )

    def test_real_evidence_renders_document_link(self):
        self.state.evidence_item(
            "We acquire software", "https://feishu.cn/docx/doc1", "Example", "source"
        )
        result = self.state.render(
            {
                "kind": "answer",
                "message": "",
                "points": [
                    {
                        "text": "嘉宾谈到收购软件企业。",
                        "citations": [{"id": "source", "quote": "acquire software"}],
                    }
                ],
            }
        )
        self.assertIn("https://feishu.cn/docx/doc1", result)

    def test_unauthorized_chat_never_calls_model_or_reads_library(self):
        with patch("news_officer.research_agent.run_research") as model:
            result = self.agent.handle(
                "查库", IncomingMessage("m", "unknown", "查库", "group", "user")
            )
        self.assertIn("尚未获准", result.messages[0])
        model.assert_not_called()

    def test_real_tool_loop_and_persisted_answer_retry(self):
        final = final_output(
            {
                "kind": "answer",
                "message": "",
                "points": [
                    {
                        "text": "正在追踪 Original Show。",
                        "citations": [{"id": "E0001", "quote": "Original Show"}],
                    }
                ],
            }
        )
        model = self.agent.sdk_model = ScriptedModel([tool_call("list_sources"), final])
        result = self.agent.handle("监听哪些播客", self.message)
        repeated = self.agent.handle("监听哪些播客", self.message)
        self.assertEqual(len(model.inputs), 2)
        self.assertEqual(result, repeated)
        self.assertIn("Original Show", result.messages[0])
        self.assertFalse(model.settings[0].store)
        with self.store._connect() as db:
            steps = list(db.execute("SELECT tool,status FROM research_steps"))
        self.assertEqual([tuple(r) for r in steps], [("list_sources", "ok")])

    def test_sdk_repairs_invalid_evidence_without_sending_it(self):
        invalid = {
            "kind": "answer",
            "message": "",
            "points": [
                {
                    "text": "Invented fact",
                    "citations": [{"id": "missing", "quote": "fake"}],
                }
            ],
        }
        valid = {
            "kind": "answer",
            "message": "",
            "points": [
                {
                    "text": "Original Show",
                    "citations": [{"id": "E0001", "quote": "Original Show"}],
                }
            ],
        }
        model = self.agent.sdk_model = ScriptedModel(
            [final_output(invalid), tool_call("list_sources"), final_output(valid)]
        )
        reply = self.agent.handle("监听什么", self.message)
        self.assertNotIn("Invented", reply.messages[0])
        self.assertIn("Original Show", reply.messages[0])
        self.assertEqual(len(model.inputs), 3)

    def test_sdk_recovers_tool_failure_and_redacts_exception(self):
        model = self.agent.sdk_model = ScriptedModel(
            [
                tool_call("list_documents", {"offset": 0}),
                final_output(
                    {
                        "kind": "conversation",
                        "message": "资料库读取失败，请稍后再试。",
                        "points": [],
                    }
                ),
            ]
        )
        with patch.object(
            self.library, "snapshot", side_effect=RuntimeError("secret=never-output")
        ):
            self.agent.handle("看看资料库", self.message)
        self.assertNotIn("never-output", json.dumps(model.inputs))
        self.assertIn("工具暂时失败", json.dumps(model.inputs, ensure_ascii=False))

    def test_sdk_history_survives_restart_but_is_isolated_by_user(self):
        self.agent.sdk_model = ScriptedModel(
            [
                final_output(
                    {
                        "kind": "conversation",
                        "message": "可以继续讨论 Bending Spoons。",
                        "points": [],
                    }
                )
            ]
        )
        self.agent.handle("我想看 Bending Spoons", self.message)
        restarted = PodcastResearchAgent(
            self.store,
            self.registry,
            self.library,
            "test-key",
            "test-model",
            chats=("team",),
        )
        restarted.initialize()
        for user, expected in [("user", True), ("colleague", False)]:
            model = restarted.sdk_model = ScriptedModel(
                [
                    final_output(
                        {
                            "kind": "conversation",
                            "message": "请问具体关注什么？",
                            "points": [],
                        }
                    )
                ]
            )
            restarted.handle(
                "刚才那个呢",
                IncomingMessage("m2-" + user, "team", "刚才那个呢", "group", user),
            )
            self.assertEqual("Bending Spoons" in json.dumps(model.inputs[0]), expected)

    def test_sdk_loop_budget_is_enforced(self):
        from news_officer.research_checkpoint import ResearchContinuationPending
        model = self.agent.sdk_model = ScriptedModel(
            [tool_call("list_sources", call_id=f"c{i}") for i in range(60)]
        )
        for _ in range(2):
            with self.assertRaises(ResearchContinuationPending):
                self.agent.handle("一直找", self.message)
        reply = self.agent.handle("一直找", self.message)
        self.assertLessEqual(len(model.inputs), 48)
        self.assertIn("继续", reply.messages[0])
        self.assertNotIn("订阅变更", reply.messages[0])
        with self.store._connect() as db:
            audit = json.loads(db.execute("SELECT audit_json FROM research_run_state").fetchone()[0])
        self.assertEqual(audit["outcome"], "budget_exhausted")

    def test_provider_failure_resumes_saved_tool_result_not_fresh_research(self):
        class FlakyModel(ScriptedModel):
            failed = False

            async def get_response(self, **kwargs):
                if len(self.inputs) == 1 and not self.failed:
                    self.failed = True
                    raise TimeoutError('simulated provider timeout')
                return await super().get_response(**kwargs)

        model = self.agent.sdk_model = FlakyModel([
            tool_call('list_sources'),
            final_output({'kind': 'answer', 'message': '', 'points': [
                {'text': '追踪 Original Show。', 'citations': [{'id': 'E0001'}]}]}),
        ])
        with self.assertRaises(TimeoutError):
            self.agent.handle('追踪什么', self.message)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_turns').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM research_checkpoints').fetchone()[0], 1)
        reply = self.agent.handle('追踪什么', self.message)
        self.assertIn('Original Show', reply.messages[0])
        self.assertIn('function_call_output', json.dumps(model.inputs[-1]))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_checkpoints').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM research_steps').fetchone()[0], 1)

    def test_discovered_episode_can_be_analyzed_without_requiring_user_copy(self):
        self.agent.podcast_service = SimpleNamespace(
            supports_url=lambda _: True,
            analyze_url=lambda _: SimpleNamespace(message="完整文字稿的摘要"),
        )
        url = "https://example.test/new-episode"
        with self.assertRaises(ValueError):
            self.state.execute("analyze_podcast", {"url": url})
        self.state.discovered_episode_urls.add(url)
        result = self.state.execute("analyze_podcast", {"url": url})
        self.assertIn("摘要", result["text"])

    def test_full_document_read_has_no_silent_truncation(self):
        doc = LibraryDocument(
            "doc", "Long", "https://feishu.cn/docx/doc", "Content " * 4000
        )
        with patch.object(self.library, "snapshot", return_value=([doc], [])):
            first = self.state.execute(
                "read_document", {"document_id": "doc", "start": 0}
            )
            self.assertEqual(first["next_start"], 12)
            second = self.state.execute(
                "read_document", {"document_id": "doc", "start": 12}
            )
        self.assertEqual(
            "".join(c["text"] for c in first["chunks"] + second["chunks"]), doc.text
        )

    def publish_quote(self, group_key, target="team", target_type="chat_id", payload=None):
        key = "message:" + payload["message_id"] if payload else "quote-job"
        self.store.enqueue(key, "message" if payload else "daily", payload or {})
        self.store.ensure_outbox(
            job_key=key, group_key=group_key, delivery_key=key,
            operation="reply" if payload else "send",
            target_id=target, target_type=target_type, reply_in_thread=False,
            parts=delivery_parts("visible message", key),
        )
        self.store.mark_outbox_sent(self.store.outbox_items(key)[0].id, "quoted-bot")

    def test_quote_resolves_episode_for_colleague_without_other_chat_leak(self):
        record = self.archive_record()
        self.publish_quote("episode:" + record.episode.id)
        message = IncomingMessage("new", "team", "这篇做详细版", "group", "colleague",
                                  parent_message_id="quoted-bot")
        self.assertEqual(quoted_context(self.store, message)["episode"]["document_id"], record.reference)
        foreign = IncomingMessage("new", "foreign", "这篇", "group", "colleague",
                                  parent_message_id="quoted-bot")
        self.assertIsNone(quoted_context(self.store, foreign))

    def test_private_quote_cannot_be_read_in_group_or_by_other_user(self):
        record = self.archive_record()
        self.publish_quote("episode:" + record.episode.id, "user", "open_id")
        for chat, kind, sender in [("team", "group", "user"), ("dm2", "p2p", "colleague")]:
            self.assertIsNone(quoted_context(self.store, IncomingMessage(
                "m", chat, "这篇", kind, sender, parent_message_id="quoted-bot")))
        self.assertIsNotNone(quoted_context(self.store, IncomingMessage(
            "m", "dm", "这篇", "p2p", "user", parent_message_id="quoted-bot")))

    def test_quoted_reply_exposes_only_visible_turn_not_speakers_history(self):
        with self.store._connect() as db:
            db.executemany("INSERT INTO research_turns(session,message_id,question,answer) VALUES (?,?,?,?)", [
                ("group:team:other", "quoted-original", "Noam Brown", "visible answer"),
                ("group:team:other", "unrelated", "unrelated personal note", "do not expose"),
            ])
        self.publish_quote("message:result:1", payload={"message_id": "quoted-original", "chat_id": "team"})
        msg = IncomingMessage("m", "team", "展开", "group", "user", parent_message_id="quoted-bot")
        context = quoted_context(self.store, msg)
        self.assertEqual(context["answer"], "visible answer")
        self.assertNotIn("unrelated", json.dumps(context))
        self.assertIsNone(quoted_context(self.store, IncomingMessage(
            "m", "foreign", "展开", "group", "user", parent_message_id="quoted-bot")))

    def test_task_rejects_unknown_docs_and_long_internal_anchor(self):
        with (patch.object(self.library, "snapshot", return_value=([], [])),
              self.assertRaises(ValueError)):
            self.state.execute("set_research_task", {"goal": "task", "document_ids": ["fake"], "format": "detailed"})
        evidence = self.state.evidence_item("word " * 30)
        with self.assertRaisesRegex(ValueError, "anchor too long"):
            self.state.render({"kind": "answer", "message": "", "points": [{
                "text": "paraphrase", "citations": [{"id": evidence["evidence_id"], "quote": "word " * 26}]}]})

    def test_id_only_citations_require_real_selected_body_evidence(self):
        doc = LibraryDocument("d", "Noam Brown", "https://example.test/noam", "Evidence about agents")
        with patch.object(self.library, "snapshot", return_value=([doc], [])):
            self.state.execute("set_research_task", {"goal": "分析", "document_ids": ["d"], "format": "brief"})
            self.state.execute("list_documents", {"offset": 0})
            answer = {"kind": "answer", "message": "", "points": [
                {"text": "分析原文", "citations": [{"id": "d:metadata"}]}]}
            with self.assertRaisesRegex(ValueError, "not metadata"):
                self.state.render(answer)
            self.state.execute("read_document", {"document_id": "d", "start": 0})
            answer["points"][0]["citations"] = [{"id": "d:C0001"}]
            self.assertIn("https://example.test/noam", self.state.render(answer))

    def test_brief_citations_are_deduplicated_in_one_footer(self):
        doc = LibraryDocument("d", "Town CEO", "https://example.test/town", "Verified transcript body")
        with patch.object(self.library, "snapshot", return_value=([doc], [])):
            self.state.execute("set_research_task", {"goal": "重点总结", "document_ids": ["d"], "format": "brief"})
            self.state.execute("read_document", {"document_id": "d", "start": 0})
            points = [{"text": f"{i}. 原文支持的第{i}条要点。", "citations": [{"id": "d:C0001"}]}
                      for i in range(1, 9)]
            value = {"kind": "answer", "message": "", "points": points}
            rendered = self.state.render(value)
            self.assertEqual(rendered.count(doc.url), 1)
            self.assertEqual(rendered.split('\n\n来源：')[0], '\n\n'.join(p['text'] for p in points))
            self.assertTrue(rendered.endswith(f"来源：[{doc.title}]({doc.url})"))
            # Deduplicating presentation must not skip validation of later points.
            points[-1]['citations'] = [{'id': 'invented'}]
            with self.assertRaises(ValueError):
                self.state.render(value)

    def test_comparison_keeps_each_distinct_source_once_in_first_use_order(self):
        first = self.state.evidence_item('first source', 'https://example.test/first', 'First')
        second = self.state.evidence_item('second source', 'https://example.test/second', 'Second')
        rendered = self.state.render({'kind': 'answer', 'message': '', 'points': [
            {'text': '比较两个来源。', 'citations': [{'id': first['evidence_id']}, {'id': second['evidence_id']}]},
            {'text': '继续解释两个来源。', 'citations': [{'id': second['evidence_id']}, {'id': first['evidence_id']}]},
        ]})
        self.assertEqual(rendered.count(first['url']), 1)
        self.assertEqual(rendered.count(second['url']), 1)
        self.assertTrue(rendered.endswith('来源：[First](https://example.test/first)；[Second](https://example.test/second)'))

    def test_quoted_episode_is_in_real_sdk_input(self):
        record = self.archive_record()
        self.publish_quote("episode:" + record.episode.id)
        model = self.agent.sdk_model = ScriptedModel([final_output(
            {"kind": "conversation", "message": "test", "points": []})])
        msg = IncomingMessage("new", "team", "这篇做详细版", "group", "colleague",
                              parent_message_id="quoted-bot")
        self.agent.handle(msg.text, msg)
        context = json.loads(model.inputs[0][-1]["content"])
        self.assertEqual(context["quoted_message"]["episode"]["document_id"], record.reference)

    def test_detailed_requires_every_page_and_accepts_internal_anchors(self):
        doc = LibraryDocument("long", "Noam Brown", "https://example.test/noam",
                              "agents learn from experience. " * 4000)
        with patch.object(self.library, "snapshot", return_value=([doc], [])):
            self.state.execute("set_research_task", {
                "goal": "完整详细版", "document_ids": ["long"], "format": "detailed"})
            self.state.execute("read_document", {"document_id": "long", "start": 0})
            result = {"kind": "answer", "message": "", "points": [
                {"text": "## 主题\n\n" + "基于正文解释观点及条件。" * 50,
                 "citations": [{"id": "long:C0001", "quote": "agents learn from experience"}]}
                for _ in range(14)]}
            with self.assertRaisesRegex(ValueError, "Full reading incomplete"):
                self.state.render(result)
            while missing := self.state.incomplete_documents():
                self.state.execute("read_document", {
                    "document_id": "long", "start": missing[0]["next_start"]})
            answer = self.state.render(result)
        self.assertEqual(answer.count("https://example.test/noam"), 1)
        self.assertNotIn("agents learn from experience", answer)
        self.assertEqual(self.state.outcome, "completed")

    def test_task_persists_and_followup_restores_without_other_user_leak(self):
        doc = LibraryDocument("d", "Noam Brown", "https://example.test/noam", "Research evidence")
        self.agent.sdk_model = ScriptedModel([
            tool_call("set_research_task", {"goal": "Noam Brown 的完整详细纪要",
                                           "document_ids": ["d"], "format": "detailed"}),
            final_output({"kind": "conversation", "message": "待继续", "points": []}),
        ])
        with patch.object(self.library, "snapshot", return_value=([doc], [])):
            self.agent.handle("做详细版", self.message)
        self.assertEqual(previous_task(self.store, "group:team:user")["format"], "detailed")
        restarted = PodcastResearchAgent(self.store, self.registry, self.library,
                                         "test-key", "test-model", chats=("team",))
        restarted.initialize()
        for sender, expected in [("user", True), ("colleague", False)]:
            model = restarted.sdk_model = ScriptedModel([final_output(
                {"kind": "conversation", "message": "test", "points": []})])
            msg = IncomingMessage("continue-" + sender, "team", "继续", "group", sender)
            restarted.handle(msg.text, msg)
            context = json.loads(model.inputs[0][-1]["content"])
            self.assertEqual(bool(context["previous_task"]), expected)

    def test_validation_repair_gets_specific_cause_and_is_audited(self):
        bad = {"kind": "answer", "message": "", "points": [
            {"text": "Original Show", "citations": [{"id": "fake", "quote": "fake"}]}]}
        good = copy.deepcopy(bad)
        good["points"][0]["citations"] = [{"id": "E0001", "quote": "Original Show"}]
        model = self.agent.sdk_model = ScriptedModel([
            tool_call("list_sources"), final_output(bad), final_output(good)])
        self.agent.handle("监听什么", self.message)
        self.assertIn("Unknown evidence", json.dumps(model.inputs[-1]))
        with self.store._connect() as db:
            audit = json.loads(db.execute("SELECT audit_json FROM research_run_state").fetchone()[0])
        self.assertEqual(len(audit["validation_errors"]), 1)
        self.assertEqual(audit["outcome"], "completed")


if __name__ == "__main__":
    unittest.main()
