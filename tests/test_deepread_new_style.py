import copy
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_officer.config import Settings
from news_officer.deepread import INSTRUCTIONS as NEW_INSTRUCTIONS
from news_officer.deepread import DeepRead, validate_deepread
from news_officer.shownotes import (
    INSTRUCTIONS,
    EpisodeNotes,
    ShownotesWriter,
    note_identity,
    render_episode,
    transcript_evidence,
)


def sample():
    return {
        "speakers": [
            {"name": "Jane Doe", "role": "host", "evidence_ids": ["C0001"]},
            {"name": "John Smith", "role": "guest", "evidence_ids": ["C0002"]},
        ],
        "core": [
            {
                "text": "**产品机制：**John Smith 解释 OpenAI 的 MVP。",
                "evidence_ids": ["C0002"],
            }
        ],
        "chapters": [
            {
                "title": "产品如何工作",
                "turns": [
                    {
                        "speaker": "Jane Doe",
                        "paragraphs": ["你如何用 OpenAI 构建 MVP？"],
                        "evidence_ids": ["C0001"],
                    },
                    {
                        "speaker": "John Smith",
                        "paragraphs": [
                            "我使用 OpenAI 构建 MVP，让 Agent 处理任务。",
                            "我会先检查结果；这种方法仍有局限。",
                        ],
                        "evidence_ids": ["C0002"],
                    },
                ],
            }
        ],
        "proper_nouns": [
            {"original": "OpenAI", "canonical": "OpenAI", "evidence_ids": ["C0001"]}
        ],
        "exclusions": [],
        "corrections": [],
    }


def record():
    return SimpleNamespace(
        transcript=SimpleNamespace(
            text="[00:01] Jane Doe: How do you use OpenAI?\n[00:02] John Smith: I use OpenAI for an MVP. An Agent does the work.",
            verified_complete=True,
        ),
        episode=SimpleNamespace(
            title="John Smith on OpenAI",
            show="Interview",
            url="https://example.com/episode",
            published_at=None,
            duration_seconds=120,
            metadata={},
        ),
        record_revision_sha256="revision",
    )


class NewStyleTests(unittest.TestCase):
    def test_flag_defaults_off_and_explicitly_enables(self):
        env = {
            "FEISHU_APP_ID": "id",
            "FEISHU_APP_SECRET": "secret",
            "OPENAI_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(Settings.from_env().deepread_new_style)
            with patch.dict(os.environ, {"NEWS_OFFICER_DEEPREAD_NEW_STYLE": "true"}):
                self.assertTrue(Settings.from_env().deepread_new_style)

    def test_default_writer_retains_old_spec_and_identity(self):
        client = Mock()
        old = {
            "participants": "原稿未明确标注",
            "participant_evidence_ids": [],
            "core": [{"text": "观点", "evidence_ids": ["C0001"]}] * 6,
            "parts": [
                {
                    "title": "产品",
                    "tables": [],
                    "points": [{"text": "观点", "evidence_ids": ["C0001"]}] * 2,
                }
            ],
            "quotes": [],
            "corrections": [],
        }
        client.responses.parse.return_value.output_parsed = EpisodeNotes.model_validate(
            old
        )
        self.assertEqual(ShownotesWriter(client, "model").generate(record()), old)
        self.assertEqual(
            client.responses.parse.call_args.kwargs["instructions"], INSTRUCTIONS
        )
        self.assertIs(
            client.responses.parse.call_args.kwargs["text_format"], EpisodeNotes
        )
        self.assertEqual(
            note_identity(record()), note_identity(record(), new_style=False)
        )
        self.assertNotEqual(
            note_identity(record()), note_identity(record(), new_style=True)
        )
        self.assertEqual(
            render_episode(record(), old),
            render_episode(record(), old, new_style=False),
        )

    def test_enabled_writer_uses_new_prompt_and_complete_dialogue_schema(self):
        client = Mock()
        client.responses.parse.return_value.output_parsed = DeepRead.model_validate(
            sample()
        )
        self.assertEqual(
            ShownotesWriter(client, "model", new_style=True).generate(record()),
            sample(),
        )
        args = client.responses.parse.call_args.kwargs
        self.assertEqual(args["instructions"], NEW_INSTRUCTIONS)
        self.assertIs(args["text_format"], DeepRead)
        self.assertGreater(args["max_output_tokens"], 11000)
        self.assertFalse(args["store"])

    def test_native_heading_bullets_and_highlighted_named_speakers(self):
        nodes, md = render_episode(record(), sample(), new_style=True)
        headings = [
            (
                n["block_type"],
                "".join(
                    e["text_run"]["content"]
                    for e in n.get("heading1", n.get("heading2", {})).get(
                        "elements", []
                    )
                ),
            )
            for n in nodes
            if n["block_type"] in (3, 4)
        ]
        self.assertIn((3, "核心内容"), headings)
        self.assertIn((3, "QA 整理"), headings)
        self.assertIn((4, "01. 产品如何工作"), headings)
        self.assertEqual(sum(n["block_type"] == 12 for n in nodes), 1)
        label = next(
            n
            for n in nodes
            if n["block_type"] == 2
            and n["text"]["elements"][0]["text_run"]["content"] == "John Smith："
        )
        self.assertEqual(
            label["text"]["elements"][0]["text_run"]["text_element_style"][
                "text_color"
            ],
            3,
        )
        self.assertIn("John Smith：我使用 OpenAI", md)
        self.assertIn("MVP", md)
        self.assertIn("Agent", md)
        self.assertNotIn("[00:", md)
        self.assertNotIn("王颢蓁", json.dumps(nodes, ensure_ascii=False) + md)
        self.assertTrue(
            any(
                n["block_type"] == 2
                and n["text"]["elements"][0]["text_run"]["content"] == " "
                for n in nodes
            )
        )

    def test_proper_nouns_are_required_and_never_translated_by_renderer(self):
        self.assertIn("English 专有名词保留 English", NEW_INSTRUCTIONS)
        self.assertNotIn("王颢蓁", NEW_INSTRUCTIONS)
        self.assertNotIn("A/B", NEW_INSTRUCTIONS)
        value = sample()
        value["chapters"][0]["turns"][0]["paragraphs"] = ["你怎样构建产品？"]
        value["chapters"][0]["turns"][1]["paragraphs"] = ["我使用开放人工智能。"]
        with self.assertRaisesRegex(ValueError, "proper noun"):
            validate_deepread(value, transcript_evidence(record().transcript.text))

    def test_rejects_missing_source_unknown_speaker_and_workflow_leaks(self):
        for mutate in (
            lambda v: v["chapters"][0]["turns"].pop(),
            lambda v: v["chapters"][0]["turns"][0].update(speaker="Unknown"),
            lambda v: v["chapters"][0]["turns"][0].update(paragraphs=["王颢蓁"]),
            lambda v: v["core"][0].update(evidence_ids=["missing"]),
        ):
            value = copy.deepcopy(sample())
            mutate(value)
            with self.assertRaises(ValueError):
                validate_deepread(value, transcript_evidence(record().transcript.text))

    def test_chronology_and_explicit_ad_exclusions(self):
        evidence = transcript_evidence(
            record().transcript.text + "\n[00:03] Buy sponsor product."
        )
        value = sample()
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_deepread(value, evidence)
        value["exclusions"] = [{"evidence_ids": ["C0003"], "reason": "advertisement"}]
        validate_deepread(value, evidence)
        value["chapters"][0]["turns"].reverse()
        with self.assertRaisesRegex(ValueError, "chronolog"):
            validate_deepread(value, evidence)


class NewStyleCompilerTests(unittest.TestCase):
    def test_selected_compiler_publishes_qa_without_second_appendix_and_reuses_cache(
        self,
    ):
        import test_episode_document as fixtures

        from news_officer.daily_document import read_tree
        from news_officer.episode_document import SelectedEpisodeCompiler

        f = fixtures.SelectedDocuments()
        f.setUp()
        self.addCleanup(f.doCleanups)
        source = f.fixture
        value = sample()
        # The compiler fixture has one source block; all turn refs remain local.
        for speaker in value["speakers"]:
            speaker["evidence_ids"] = ["C0001"]
        value["core"][0]["evidence_ids"] = ["C0001"]
        for turn in value["chapters"][0]["turns"]:
            turn["evidence_ids"] = ["C0001"]
        value["proper_nouns"] = []
        source.writer.generate.side_effect = lambda _, value=value: value
        translation_writer = Mock()
        compiler = SelectedEpisodeCompiler(
            source.store,
            source.api,
            source.writer,
            start_date="2026-09-21",
            today=lambda: "2026-09-21",
            include_fulltext=True,
            transcript_writer=translation_writer,
            new_style=True,
        )
        compiler.initialize()
        compiler.enqueue_ready("2026-09-21")
        job = source.store.claim_next("document")
        result = compiler.publish(job)
        self.assertEqual(result["count"], 1)
        token = result["documents"][0]["url"].rsplit("/", 1)[-1]
        nodes = read_tree(source.api.blocks(token), token)
        content = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("QA 整理", content)
        self.assertIn("John Smith：", content)
        self.assertNotIn("完整中文文字稿", content)
        translation_writer.generate.assert_not_called()
        self.assertEqual(compiler.publish(job), result)
        source.writer.generate.assert_called_once()
        self.assertNotEqual(
            compiler.document_identity(source.record),
            f.compiler.document_identity(source.record),
        )

    def test_runtime_passes_config_flag_to_writer_and_compiler(self):
        import importlib
        import tempfile

        from news_officer import __main__

        # A read-only preview test imports this module while constructors are
        # patched. Refresh bindings after those patches have been restored.
        build_runtime = importlib.reload(__main__).build_runtime

        env = {
            "FEISHU_APP_ID": "id",
            "FEISHU_APP_SECRET": "secret",
            "OPENAI_API_KEY": "key",
            "NEWS_OFFICER_DAILY_DOCUMENT": "true",
        }
        with tempfile.TemporaryDirectory() as directory:
            env["NEWS_OFFICER_DB_PATH"] = str(Path(directory) / "state.sqlite")
            for enabled in (False, True):
                env["NEWS_OFFICER_DEEPREAD_NEW_STYLE"] = str(enabled).lower()
                with patch.dict(os.environ, env, clear=True):
                    runtime = build_runtime(Settings.from_env())
                self.assertEqual(runtime.document_compiler.new_style, enabled)
                self.assertEqual(runtime.document_compiler.writer.new_style, enabled)


class NewStyleMigrationTests(unittest.TestCase):
    def test_layout_upgrade_replaces_generated_content_and_protects_manual_edits(self):
        import test_chinese_transcript as fixtures

        for manual_edit in (False, True):
            with self.subTest(manual_edit=manual_edit):
                f = fixtures.LegacyMigration()
                f.setUp()
                self.addCleanup(f.doCleanups)
                compiler = f.c
                compiler.new_style = True
                value = sample()
                for speaker in value["speakers"]:
                    speaker["evidence_ids"] = ["C0001"]
                value["core"][0]["evidence_ids"] = ["C0001"]
                for turn in value["chapters"][0]["turns"]:
                    turn["evidence_ids"] = ["C0001"]
                value["proper_nouns"] = []
                compiler.writer.generate.side_effect = lambda _, value=value: value
                if manual_edit:
                    f.f.api.data[f.token][-1]["text"]["elements"][0]["text_run"][
                        "content"
                    ] = "人工编辑"
                    before = copy.deepcopy(f.f.api.data)
                    with self.assertRaisesRegex(Exception, "人工修改"):
                        compiler.publish(f.job)
                    self.assertEqual(f.f.api.data, before)
                else:
                    f.lost_delete = True
                    with self.assertRaises(TimeoutError):
                        compiler.publish(f.job)
                    result = compiler.publish(f.job)
                    content = json.dumps(f.f.api.data, ensure_ascii=False)
                    self.assertIn("QA 整理", content)
                    self.assertNotIn("归档原文", content)
                    self.assertNotIn("核心论点", content)
                    self.assertEqual(compiler.publish(f.job), result)
