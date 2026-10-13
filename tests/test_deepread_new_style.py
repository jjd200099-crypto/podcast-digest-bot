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
from news_officer.deepread import DeepRead, source_evidence, validate_deepread
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
    def test_source_turns_merge_cues_losslessly_without_inventing_speakers(self):
        text = "[00:01] Jane Doe: Why?\n[00:02] Jane Doe: Tell me more.\n[00:03] John Smith: I use OpenAI.\n[00:04] John Smith: Here is how.\n[00:05] Unlabelled material."
        evidence = source_evidence(text)
        self.assertEqual(len(evidence), 3)
        self.assertEqual("".join(x["text"] for x in evidence.values()), text)
        self.assertEqual(evidence["C0001"]["time"], "00:01")
        self.assertEqual(evidence["C0002"]["time"], "00:03")
        self.assertEqual(
            source_evidence("unlabelled source"),
            transcript_evidence("unlabelled source"),
        )

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
        self.assertTrue(args["instructions"].startswith(NEW_INSTRUCTIONS))
        self.assertIs(args["text_format"], DeepRead)
        self.assertLessEqual(args["max_output_tokens"], 12000)
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
        self.assertNotIn("A-B", NEW_INSTRUCTIONS)
        self.assertNotIn("Wang", NEW_INSTRUCTIONS)
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
            lambda v: v["chapters"][0]["turns"][0].update(paragraphs=["【A/B】"]),
            lambda v: v["chapters"][0]["turns"][0].update(paragraphs=["[A-B]"]),
            lambda v: v["chapters"][0]["turns"][0].update(paragraphs=["Wang method"]),
            lambda v: v["chapters"][0]["turns"][0].update(paragraphs=["用A-B工作流"]),
            lambda v: v["chapters"][0]["turns"][0].update(paragraphs=["用A/B方法"]),
            lambda v: v["core"][0].update(evidence_ids=["missing"]),
        ):
            value = copy.deepcopy(sample())
            mutate(value)
            with self.assertRaises(ValueError):
                validate_deepread(value, transcript_evidence(record().transcript.text))

    def test_normal_english_in_titles_bodies_and_cached_render(self):
        for text in (
            "data-backed",
            "ultra-bullish",
            "Alpha-Beta",
            "A/B testing",
            "A-B testing",
            "A-B 测试",
        ):
            with self.subTest(text=text):
                rec, value = record(), sample()
                rec.episode.title = text
                value["chapters"][0]["turns"][1]["paragraphs"].append(text)
                client = Mock()
                client.responses.parse.return_value.output_parsed = (
                    DeepRead.model_validate(value)
                )
                generated = ShownotesWriter(client, "model", new_style=True).generate(
                    rec
                )
                self.assertEqual(generated, value)
                cached = json.loads(json.dumps(generated))
                self.assertIn(text, render_episode(rec, cached, new_style=True)[1])

    def test_chunking_and_per_chunk_validation_and_transport_retries(self):
        from news_officer.deepread import generate

        rec = record()
        rec.transcript.text = "\n".join(
            f"[00:{i:02d}] Speaker{i % 2}: source {i}" for i in range(12)
        )
        attempts = {}
        consolidation_attempts = []
        client = Mock()

        def respond(**kwargs):
            from news_officer.deepread import Consolidation

            if kwargs["text_format"] is Consolidation:
                data = json.loads(kwargs["input"])
                consolidation_attempts.append(data)
                if len(consolidation_attempts) == 1:
                    raise TimeoutError("consolidation timeout")
                return SimpleNamespace(
                    output_parsed=Consolidation.model_validate(
                        {
                            "core": [
                                {
                                    "text": f"**整期主题{i}：**内容",
                                    "evidence_ids": ["C0001"],
                                }
                                for i in range(10)
                            ],
                            "chapters": [
                                {"title": f"主题{i}", "start_turn": i * 2}
                                for i in range(5)
                            ],
                        }
                    )
                )
            batch = json.loads(kwargs["input"])["transcript"]
            key = next(iter(batch))
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] == 1:
                if key == "C0001":
                    raise TimeoutError("timeout")
                if key == "C0006":
                    raise ConnectionError("transport")
            value = {
                "speakers": [
                    {"name": "Narrator", "role": "other", "evidence_ids": list(batch)}
                ],
                "core": [{"text": "**主题：**内容", "evidence_ids": [key]}],
                "chapters": [
                    {
                        "title": "章节",
                        "turns": [
                            {
                                "speaker": "Narrator",
                                "paragraphs": ["完整内容"],
                                "evidence_ids": [ref],
                            }
                            for ref in batch
                        ],
                    }
                ],
                "proper_nouns": [],
                "exclusions": [],
                "corrections": [],
            }
            if key == "C0011" and attempts[key] == 1:
                value["chapters"][0]["turns"].pop()
            self.assertLessEqual(len(batch), 5)
            self.assertLessEqual(kwargs["max_output_tokens"], 12000)
            return SimpleNamespace(output_parsed=DeepRead.model_validate(value))

        client.responses.parse.side_effect = respond
        value = generate(client, "model", rec)
        self.assertEqual(attempts, {"C0001": 2, "C0006": 2, "C0011": 2})
        self.assertEqual(len(consolidation_attempts), 2)
        self.assertEqual(len(value["chapters"]), 5)
        self.assertEqual(len(value["core"]), 10)
        self.assertEqual(
            [turn for chapter in value["chapters"] for turn in chapter["turns"]],
            [
                {k: v for k, v in turn.items() if k != "index"}
                for turn in consolidation_attempts[-1]["turns"]
            ],
        )
        self.assertEqual(len(value["speakers"]), 1)
        validate_deepread(value, source_evidence(rec.transcript.text))

    def test_consolidation_minimums_scale_with_source_length(self):
        from news_officer.deepread import consolidation_minimums

        for length, expected in (
            (100, (4, 2)),
            (6_000, (4, 2)),
            (18_000, (7, 3)),
            (30_000, (10, 5)),
            (100_000, (10, 5)),
        ):
            with self.subTest(length=length):
                evidence = {
                    "C0001": {"text": "x" * (length // 2)},
                    "C0002": {"text": "x" * (length - length // 2)},
                }
                self.assertEqual(consolidation_minimums(evidence), expected)

    def test_consolidation_short_and_long_validation_and_prompts(self):
        from news_officer.deepread import consolidate

        for length, core_count, chapter_count in (
            (6_000, 4, 2),
            (18_000, 7, 3),
            (30_000, 10, 5),
        ):
            evidence = source_evidence(record().transcript.text)
            evidence["C0002"]["text"] += "x" * (
                length - sum(len(body["text"]) for body in evidence.values())
            )
            merged = sample()
            turns = merged["chapters"][0]["turns"]
            turns.extend(copy.deepcopy(turns[-1]) for _ in range(3))
            valid = {
                "core": merged["core"] * core_count,
                "chapters": [
                    {"title": f"主题{i}", "start_turn": i} for i in range(chapter_count)
                ],
            }
            outputs = [valid]
            for field in ("core", "chapters"):
                invalid = copy.deepcopy(valid)
                invalid[field].pop()
                outputs.append(invalid)
                excessive = copy.deepcopy(valid)
                excessive[field] = excessive[field][:1] * (
                    19 if field == "core" else 11
                )
                outputs.append(excessive)
            for output in outputs:
                with self.subTest(length=length, output=output):
                    client = Mock()
                    client.responses.parse.return_value.output_parsed.model_dump.return_value = output
                    if output == valid:
                        result = consolidate(client, "model", merged, evidence)
                        self.assertEqual(len(result["core"]), core_count)
                        self.assertEqual(len(result["chapters"]), chapter_count)
                        self.assertEqual(
                            [t for c in result["chapters"] for t in c["turns"]],
                            turns,
                        )
                        self.assertEqual(client.responses.parse.call_count, 1)
                    else:
                        with self.assertRaisesRegex(ValueError, "consolidation failed"):
                            consolidate(client, "model", merged, evidence)
                        self.assertEqual(client.responses.parse.call_count, 3)
                    for call in client.responses.parse.call_args_list:
                        instructions = call.kwargs["instructions"]
                        self.assertIn(f"{core_count}–18条", instructions)
                        self.assertIn(f"{chapter_count}–10章", instructions)

    def test_consolidation_failure_retries_and_raises(self):
        from news_officer.deepread import Consolidation, consolidate

        invalid_boundaries = Consolidation.model_validate(
            {
                "core": sample()["core"] * 10,
                "chapters": [{"title": "重复边界", "start_turn": 0}] * 5,
            }
        )
        for output in (
            None,
            Mock(model_dump=lambda: {"core": [], "chapters": []}),
            invalid_boundaries,
        ):
            client = Mock()
            client.responses.parse.return_value.output_parsed = output
            with self.assertRaisesRegex(ValueError, "consolidation failed"):
                consolidate(
                    client, "model", sample(), source_evidence(record().transcript.text)
                )
            self.assertEqual(client.responses.parse.call_count, 3)

    def test_batch_character_budget(self):
        from news_officer.deepread import BATCH_CHARACTERS

        self.assertEqual(BATCH_CHARACTERS, 3000)

    def test_long_single_turn_is_bounded_and_lossless(self):
        text = "[00:01] Host: " + "long source " * 2000
        evidence = source_evidence(text)
        self.assertGreater(len(evidence), 5)
        self.assertTrue(all(len(body["text"]) <= 2400 for body in evidence.values()))
        self.assertEqual("".join(body["text"] for body in evidence.values()), text)

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

        # Simulate deployment with an edited prompt, including module reload
        # and a fresh compiler so this catches a hash computed at import time.
        import importlib
        from news_officer import deepread

        source.store.complete(job.key)
        identity = compiler.document_identity(source.record)
        revision = compiler.snapshot(source.base)[0]
        before = copy.deepcopy(source.api.data)
        instructions = deepread.INSTRUCTIONS
        module_state = deepread.__dict__.copy()
        try:
            with patch.object(Path, "read_text", return_value=instructions + "\nPrompt edit"):
                importlib.reload(deepread)
            self.assertNotEqual(deepread.INSTRUCTIONS, instructions)
            restarted = SelectedEpisodeCompiler(
                source.store, source.api, source.writer,
                start_date="2026-09-21", today=lambda: "2026-09-21",
                include_fulltext=True, new_style=True,
            )
            restarted.initialize()
            self.assertEqual(restarted.document_identity(source.record), identity)
            self.assertEqual(restarted.snapshot(source.base)[0], revision)
            self.assertEqual(restarted.enqueue_ready("2026-09-21"), 0)
            self.assertIsNone(source.store.claim_next("document"))
            restarted._notes(source.record, "")
            source.writer.generate.assert_called_once()
            self.assertEqual(source.api.data, before)
        finally:
            deepread.__dict__.clear()
            deepread.__dict__.update(module_state)

    def test_failed_new_style_publishes_old_style_in_normal_and_enrichment_modes(self):
        import test_episode_document as fixtures
        from test_shownotes import ShownotesValidation

        for enrichment in (False, True):
            for failure in (
                ValueError("invalid"),
                TimeoutError("timeout"),
                ConnectionError("transport"),
                RuntimeError("unexpected"),
            ):
                with self.subTest(enrichment=enrichment, failure=type(failure)):
                    f = fixtures.SelectedDocuments()
                    f.setUp()
                    self.addCleanup(f.doCleanups)
                    old_fixture = ShownotesValidation()
                    old_fixture.setUp()
                    old = old_fixture.value
                    client = Mock()

                    def respond(failure=failure, old=old, **kwargs):
                        if kwargs["text_format"] is DeepRead:
                            if isinstance(failure, ValueError):
                                return SimpleNamespace(output_parsed=None)
                            raise failure
                        return SimpleNamespace(
                            output_parsed=EpisodeNotes.model_validate(old)
                        )

                    client.responses.parse.side_effect = respond
                    f.compiler.new_style = True
                    f.compiler.writer = ShownotesWriter(client, "model", new_style=True)
                    if enrichment:
                        f.fixture.store.save_job_result(
                            f.fixture.base,
                            "bundle",
                            "daily_bundle",
                            {
                                "enrichment": {"version": 1, "eligible": ["one"]},
                                "deliveries": [
                                    {"target_type": "chat_id", "target_id": "oc_test"}
                                ],
                            },
                        )
                        job = f.enrichment_job("one")
                    else:
                        job = f.job()
                    with self.assertLogs(
                        "news_officer.shownotes", level="WARNING"
                    ) as logs:
                        result = f.compiler.publish(job)
                    self.assertEqual(result["count"], 1)
                    self.assertIn("falling back", logs.output[0])
                    self.assertIn(
                        "核心论点", json.dumps(f.fixture.api.data, ensure_ascii=False)
                    )
                    self.assertEqual(
                        sum(
                            call.kwargs["text_format"] is DeepRead
                            for call in client.responses.parse.call_args_list
                        ),
                        3,
                    )
                    self.assertEqual(f.compiler.publish(job), result)

    def test_existing_document_fallback_preserves_fulltext_and_does_not_cache(self):
        import test_document_transcript as fixtures

        from news_officer.models import Job

        f = fixtures.FulltextDocuments()
        f.setUp()
        self.addCleanup(f.doCleanups)
        compiler = f.compiler
        first_job = f.case.job()
        first = compiler.publish(first_job)
        f.f.store.complete(first_job.key)
        before = copy.deepcopy(f.f.api.data)
        old = f.f.writer.generate(f.f.record)
        client = Mock()
        client.responses.parse.return_value.output_parsed = EpisodeNotes.model_validate(
            old
        )
        compiler.writer = ShownotesWriter(client, "model", new_style=True)
        failing_generate = self.enterContext(
            patch(
                "news_officer.deepread.generate",
                side_effect=ValueError("new style failed"),
            )
        )
        compiler.new_style = True
        compiler.request_authorizer = lambda _: True
        job = Job(
            "fallback-existing",
            "document",
            {
                "mode": "requested_episode",
                "episode_id": f.f.record.episode.id,
                "message_id": "test",
                "chat_id": "test",
                "chat_type": "p2p",
                "sender_open_id": "owner",
                "reply_in_thread": False,
            },
            1,
        )
        f.f.store.enqueue(job.key, job.kind, job.payload)
        result = compiler.publish(job)
        self.assertEqual(result["documents"][0]["url"], first["documents"][0]["url"])
        self.assertEqual(f.f.api.data, before)
        self.assertTrue(f.appendix(first["documents"][0]["url"].rsplit("/", 1)[-1]))
        with f.f.store._connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT * FROM episode_shownotes WHERE identity=?",
                    (note_identity(f.f.record, new_style=True),),
                ).fetchone()
            )
        self.assertFalse(
            (
                compiler.root / (note_identity(f.f.record, new_style=True) + ".md")
            ).exists()
        )
        calls = failing_generate.call_count
        compiler._notes(f.f.record, "", fulltext_attached=True)
        compiler._notes(f.f.record, "", fulltext_attached=True)
        self.assertEqual(failing_generate.call_count, calls + 2)

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
