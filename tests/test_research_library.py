import copy
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.library import (
    FeishuLibraryAPI,
    LibraryDocument,
    LibraryError,
    PodcastLibrary,
    block_signature,
    markdown_blocks,
)
from news_officer.models import Episode, IncomingMessage, Transcript
from news_officer.podcast import PodcastService
from news_officer.research_agent import PodcastResearchAgent, ResearchTools
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


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
        with self.assertRaises(KeyError):
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
        with patch.object(self.agent.client.responses, "create") as model:
            result = self.agent.handle(
                "查库", IncomingMessage("m", "unknown", "查库", "group", "user")
            )
        self.assertIn("尚未获准", result.messages[0])
        model.assert_not_called()

    def test_real_tool_loop_and_persisted_answer_retry(self):
        call = SimpleNamespace(
            type="function_call", name="list_sources", arguments="{}", call_id="c1"
        )
        final = json.dumps(
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
        responses = [
            SimpleNamespace(output=[call], output_text=""),
            SimpleNamespace(output=[], output_text=final),
        ]
        with patch.object(
            self.agent.client.responses, "create", side_effect=responses
        ) as model:
            result = self.agent.handle("监听哪些播客", self.message)
            repeated = self.agent.handle("监听哪些播客", self.message)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result, repeated)
        self.assertIn("Original Show", result.messages[0])

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


if __name__ == "__main__":
    unittest.main()
