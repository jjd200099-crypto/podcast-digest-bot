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
from news_officer.deepread import (
    DeepRead,
    source_evidence,
    transcript_evidence,
    validate_deepread,
)
from news_officer.shownotes import (
    ShownotesWriter,
    note_identity,
    render_episode,
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
            id="episode-test",
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
    def correction(self, original, evidence_id="C0002"):
        return {
            "original": original, "corrected": "fixed",
            "reason": "ASR", "evidence_id": evidence_id,
        }

    def test_removed_generator_symbols_and_prompt_are_absent(self):
        from news_officer import shownotes
        for symbol in ('EpisodeNotes', 'validate_notes', 'chapter_outline',
                       'transcript_evidence', 'INSTRUCTIONS', 'WORKFLOW'):
            self.assertFalse(hasattr(shownotes, symbol), symbol)
        self.assertFalse((Path(shownotes.__file__).parent / 'prompts' / 'podcast_shownotes.md').exists())
        self.assertNotIn('deepread_new_style', Settings.__dataclass_fields__)

    def test_removed_switch_startup_guard(self):
        from news_officer.config import _warn_obsolete_deepread_variable
        _warn_obsolete_deepread_variable.cache_clear()
        env = {'FEISHU_APP_ID': 'id', 'FEISHU_APP_SECRET': 'secret', 'OPENAI_API_KEY': 'key'}
        with patch.dict(os.environ, env, clear=True):
            Settings.from_env()
            with patch.dict(os.environ, {'NEWS_OFFICER_DEEPREAD_NEW_STYLE': ' TrUe '}):
                with self.assertLogs('news_officer.config', level='WARNING') as logs:
                    Settings.from_env()
                self.assertEqual(len(logs.output), 1)
                self.assertIn('obsolete', logs.output[0])
                with self.assertNoLogs('news_officer.config', level='WARNING'):
                    Settings.from_env()
            for value in ('false', '', '1', 'yes'):
                with (patch.dict(os.environ, {'NEWS_OFFICER_DEEPREAD_NEW_STYLE': value}),
                      self.assertRaisesRegex(ValueError, 'has been removed')):
                    Settings.from_env()

    def test_correction_token_boundaries_and_cjk(self):
        from news_officer.deepread import _correction_matches
        for original, evidence, expected in (
            ('U.S.', 'because', False), ('us', 'business', False),
            ('U.S.', 'in u s today', True), ('U.S.', 'the U.S. market', True),
            ('“OpenAI”', 'using "openai" today', True),
            ('ｏｐｅｎａｉ， MVP', 'OpenAI  \n MVP', True),
            ('收入', '总收入增长', True), ('使用OpenAI', '正在使用OpenAI构建产品', True),
            ('使用us', '使用business', False),
        ):
            with self.subTest(original=original, evidence=evidence):
                self.assertEqual(_correction_matches(original, evidence), expected)

    def test_bad_correction_is_dropped_with_one_episode_warning(self):
        value = sample()
        good = [self.correction("OpenAI"), self.correction("MVP")]
        value["corrections"] = [good[0], self.correction("not in source"), good[1]]
        with self.assertLogs("news_officer.deepread", level="WARNING") as logs:
            result = validate_deepread(
                value, source_evidence(record().transcript.text), episode_id="ep-123"
            )
        self.assertEqual(result["corrections"], good)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("Dropping 1 unsupported ASR correction(s)", logs.output[0])
        self.assertIn("ep-123", logs.output[0])

    def test_correction_matches_normalized_source(self):
        value = sample()
        value["corrections"] = [self.correction('ｉ USE　“openai”，  for\n an MVP！')]
        evidence = source_evidence(record().transcript.text)
        evidence["C0002"]["text"] = 'John Smith: I use "OpenAI", for an MVP!'
        with self.assertNoLogs("news_officer.deepread", level="WARNING"):
            result = validate_deepread(value, evidence)
        self.assertEqual(result["corrections"], value["corrections"])

    def test_all_bad_corrections_allow_generation_without_retry(self):
        value = sample()
        value["corrections"] = [
            self.correction(original) for original in ["absent", "", "  ", "…！？"]
        ]
        client = Mock()
        client.responses.parse.return_value.output_parsed = DeepRead.model_validate(value)
        episode_record = record()
        episode_record.episode.id = "ep-all-bad"
        with self.assertLogs("news_officer.deepread", level="WARNING") as logs:
            result = ShownotesWriter(client, "model").generate(
                episode_record
            )
        self.assertEqual(result, sample())
        client.responses.parse.assert_called_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("Dropping 4 unsupported ASR correction(s)", logs.output[0])
        self.assertIn("ep-all-bad", logs.output[0])
        nodes, markdown = render_episode(episode_record, result)
        self.assertTrue(nodes)
        self.assertNotIn("订正", markdown)
        self.assertNotIn("纠错", markdown)

    def test_unknown_correction_evidence_still_raises(self):
        value = sample()
        value["corrections"] = [self.correction("", evidence_id="C9999")]
        with self.assertRaisesRegex(ValueError, "^Unknown deepread evidence: C9999$"):
            validate_deepread(value, source_evidence(record().transcript.text))


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


    def test_new_style_writer_raises_without_old_model_call_or_secret_log(self):
        for failure in (ValueError, TimeoutError, ConnectionError, RuntimeError):
            client = Mock()
            error = failure("secret-token-must-not-be-logged")
            with patch("news_officer.deepread.generate", side_effect=error), self.assertLogs(
                "news_officer.shownotes", level="WARNING"
            ) as logs, self.assertRaises(failure) as raised:
                ShownotesWriter(client, "model").generate(record())
            self.assertIs(raised.exception, error)
            client.responses.parse.assert_not_called()
            self.assertEqual(len(logs.output), 1)
            self.assertIn("episode episode-test; no old-style fallback", logs.output[0])
            self.assertNotIn("secret-token", logs.output[0])

    def test_enabled_writer_uses_new_prompt_and_complete_dialogue_schema(self):
        client = Mock()
        client.responses.parse.return_value.output_parsed = DeepRead.model_validate(
            sample()
        )
        self.assertEqual(
            ShownotesWriter(client, "model").generate(record()),
            sample(),
        )
        args = client.responses.parse.call_args.kwargs
        self.assertTrue(args["instructions"].startswith(NEW_INSTRUCTIONS))
        self.assertIs(args["text_format"], DeepRead)
        self.assertLessEqual(args["max_output_tokens"], 12000)
        self.assertFalse(args["store"])

    def test_native_heading_bullets_and_highlighted_named_speakers(self):
        nodes, md = render_episode(record(), sample())
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

    def test_proper_noun_absent_from_qa_is_dropped_without_changing_dialogue(self):
        self.assertIn("English 专有名词保留 English", NEW_INSTRUCTIONS)
        self.assertNotIn("王颢蓁", NEW_INSTRUCTIONS)
        self.assertNotIn("A/B", NEW_INSTRUCTIONS)
        self.assertNotIn("A-B", NEW_INSTRUCTIONS)
        self.assertNotIn("Wang", NEW_INSTRUCTIONS)
        value = sample()
        value["chapters"][0]["turns"][0]["paragraphs"] = ["你怎样构建产品？"]
        value["chapters"][0]["turns"][1]["paragraphs"] = ["我使用开放人工智能。"]
        with self.assertLogs("news_officer.deepread", level="WARNING") as logs:
            result = validate_deepread(
                value, transcript_evidence(record().transcript.text)
            )
        self.assertIn("OpenAI", logs.output[0])
        self.assertIn("missing from QA", logs.output[0])
        self.assertEqual(result, {**value, "proper_nouns": []})
        self.assertEqual(len(value["proper_nouns"]), 1)

    def test_proper_noun_canonical_is_normalized_against_qa(self):
        for canonical, qa in (
            ("OPENAI", "openai"),
            ("OpenAI, Inc.", "OPENAI inc"),
            ("OpenAI-Inc", "OpenAI—Inc"),
            ("OpenAI\t Inc", "OpenAI \n  Inc"),
        ):
            with self.subTest(canonical=canonical, qa=qa):
                value = sample()
                value["proper_nouns"][0]["canonical"] = canonical
                value["chapters"][0]["turns"][0]["paragraphs"] = [qa]
                self.assertEqual(
                    validate_deepread(
                        value, transcript_evidence(record().transcript.text)
                    ),
                    value,
                )

    def test_cjk_or_non_latin_canonical_still_raises(self):
        for canonical in ("开放人工智能", "OpenAI人工智能", "123", "---"):
            for appears_in_qa in (False, True):
                with self.subTest(canonical=canonical, appears_in_qa=appears_in_qa):
                    value = sample()
                    value["proper_nouns"][0]["canonical"] = canonical
                    if appears_in_qa:
                        value["chapters"][0]["turns"][0]["paragraphs"] = [canonical]
                    with self.assertRaisesRegex(ValueError, "proper noun"):
                        validate_deepread(
                            value, transcript_evidence(record().transcript.text)
                        )

    def test_proper_noun_case_whitespace_and_punctuation_are_normalized(self):
        for original, source in (
            ("OPENAI", "openai"),
            ("OpenAI, Inc.", "OPENAI inc"),
            ("OpenAI-Inc", "OpenAI—Inc"),
            ("OpenAI\t Inc", "OpenAI \n  Inc"),
        ):
            with self.subTest(original=original, source=source):
                value = sample()
                value["proper_nouns"][0]["original"] = original
                evidence = transcript_evidence(record().transcript.text)
                evidence["C0001"]["text"] = source
                self.assertEqual(validate_deepread(value, evidence), value)

    def test_unknown_proper_noun_is_dropped_without_changing_notes(self):
        value = sample()
        value["proper_nouns"].append(
            {
                "original": "Steve Jamison",
                "canonical": "Steve Jamison",
                "evidence_ids": ["C0001"],
            }
        )
        evidence = transcript_evidence(record().transcript.text)
        evidence["C0001"]["text"] += (
            Path(__file__).parent
            / "fixtures/youtube-captions-jellyfish-2026-10-13.txt"
        ).read_text(encoding="utf-8")
        with self.assertLogs("news_officer.deepread", level="WARNING") as logs:
            result = validate_deepread(value, evidence)
        self.assertIn("Steve Jamison", logs.output[0])
        self.assertEqual(result, sample())
        self.assertEqual(len(value["proper_nouns"]), 2)

    def test_proper_noun_must_match_cited_evidence(self):
        value = sample()
        evidence = transcript_evidence(record().transcript.text)
        evidence["C0001"]["text"] = "An unrelated question."
        with self.assertLogs("news_officer.deepread", level="WARNING"):
            result = validate_deepread(value, evidence)
        self.assertEqual(result, {**value, "proper_nouns": []})
        value["proper_nouns"][0]["evidence_ids"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "Unknown deepread evidence"):
            validate_deepread(value, evidence)

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
                generated = ShownotesWriter(client, "model").generate(
                    rec
                )
                self.assertEqual(generated, value)
                cached = json.loads(json.dumps(generated))
                self.assertIn(text, render_episode(rec, cached)[1])

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
    def test_new_style_rejects_old_notes_even_under_new_cache_identity(self):
        import test_episode_document as fixtures

        f = fixtures.SelectedDocuments()
        f.setUp()
        self.addCleanup(f.doCleanups)

        rec = f.fixture.record
        identity = note_identity(rec)
        old = {"participants": "legacy", "parts": []}
        with f.fixture.store._connect() as db:
            db.execute('INSERT INTO episode_shownotes VALUES (?,?,?)',
                       (identity, json.dumps(old), 'legacy'))
        with self.assertRaisesRegex(ValueError, 'refusing old-style notes'):
            f.compiler._notes(rec, '')
        self.assertEqual(f.fixture.api.creates, 0)
        self.assertFalse((f.compiler.root / (identity + '.md')).exists())

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
        compiler = SelectedEpisodeCompiler(
            source.store,
            source.api,
            source.writer,
            start_date="2026-09-21",
            today=lambda: "2026-09-21",
            include_fulltext=True,
        )
        compiler.initialize()
        compiler.enqueue_ready("2026-09-21")
        job = source.store.claim_next("document")
        with patch("news_officer.document_transcript.ChineseTranscriptWriter.generate") as translation:
            result = compiler.publish(job)
        translation.assert_not_called()
        self.assertEqual(result["count"], 1)
        token = result["documents"][0]["url"].rsplit("/", 1)[-1]
        nodes = read_tree(source.api.blocks(token), token)
        content = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("QA 整理", content)
        self.assertIn("John Smith：", content)
        self.assertNotIn("完整中文文字稿", content)
        self.assertFalse(hasattr(compiler, "transcript_writer"))
        self.assertEqual(compiler.publish(job), result)
        source.writer.generate.assert_called_once()
        self.assertEqual(
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
                include_fulltext=True,
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

    def test_failed_new_style_defers_then_retries_without_old_style(self):
        import test_episode_document as fixtures

        for enrichment in (False, True):
            for failure in (ValueError("invalid"), TimeoutError("timeout"),
                            ConnectionError("transport"), RuntimeError("unexpected")):
                with self.subTest(enrichment=enrichment, failure=type(failure)):
                    f = fixtures.SelectedDocuments()
                    f.setUp()
                    self.addCleanup(f.doCleanups)
                    client = Mock()
                    client.responses.parse.side_effect = failure

                    f.compiler.writer = ShownotesWriter(client, "model")
                    if enrichment:
                        f.fixture.store.save_job_result(f.fixture.base, "bundle", "daily_bundle", {
                            "enrichment": {"version": 1, "eligible": ["one"]},
                            "deliveries": [{"target_type": "chat_id", "target_id": "oc_test"}],
                        })
                        job = f.enrichment_job("one")
                    else:
                        job = f.job()
                    # Old jobs may have frozen a fallback result before this upgrade.
                    f.fixture.store.save_job_result(job.key, 'episodes:content',
                                                    'episode_documents', {'documents': []})
                    with self.assertLogs("news_officer.shownotes", level="WARNING") as logs:
                        if enrichment:
                            with self.assertRaisesRegex(ValueError, "Deepread chunk generation failed"):
                                f.compiler.publish(job)
                        else:
                            result = f.compiler.publish(job)
                            self.assertTrue(result["deferred"])
                            self.assertEqual(result["documents"], [])
                    self.assertEqual(len(logs.output), 1)
                    self.assertIn("episode one; no old-style fallback", logs.output[0])
                    self.assertEqual(f.fixture.api.creates, 0)
                    self.assertTrue(f.compiler.retry_available(job.payload, f.compiler.today()))
                    with f.fixture.store._connect() as db:
                        self.assertEqual(tuple(db.execute(
                            "SELECT attempts,status FROM episode_document_attempts"
                        ).fetchone()), (1, "pending"))
                    self.assertEqual(client.responses.parse.call_count, 3)
                    self.assertTrue(all(c.kwargs["text_format"] is DeepRead
                                        for c in client.responses.parse.call_args_list))
                    value = json.loads(json.dumps(sample()).replace("C0002", "C0001"))
                    value["proper_nouns"] = []
                    with patch("news_officer.deepread.generate", return_value=value):
                        result = f.compiler.publish(job)
                    self.assertEqual(result["count"], 1)
                    self.assertIn("QA 整理", json.dumps(f.fixture.api.data, ensure_ascii=False))
                    self.assertNotIn("核心论点", json.dumps(f.fixture.api.data, ensure_ascii=False))


class NewStyleMigrationTests(unittest.TestCase):
    def test_layout_upgrade_replaces_generated_content_and_protects_manual_edits(self):
        import test_chinese_transcript as fixtures

        for manual_edit in (False, True):
            with self.subTest(manual_edit=manual_edit):
                f = fixtures.LegacyMigration()
                f.setUp()
                self.addCleanup(f.doCleanups)
                compiler = f.c

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


class EvidenceNormalizationTests(unittest.TestCase):
    def test_quoted_ids_split_and_deduplicate_across_nested_fields(self):
        from news_officer.deepread import normalize_evidence_ids
        value = {'evidence_ids': ["C0062','C0063','C0070", 'C0063; C0071'],
                 'nested': [{'evidence_ids': ['C0001 / C0002', 'C0001']}]}
        normalized = normalize_evidence_ids(value)
        self.assertEqual(normalized['evidence_ids'], ['C0062', 'C0063', 'C0070', 'C0071'])
        self.assertEqual(normalized['nested'][0]['evidence_ids'], ['C0001', 'C0002'])

    def test_nonexistent_extracted_ids_still_rejected(self):
        value = sample()
        value['core'][0]['evidence_ids'] = ["C0001','C9999"]
        with self.assertRaisesRegex(ValueError, 'Unknown deepread evidence: C9999'):
            validate_deepread(value, source_evidence(record().transcript.text))

    def test_quoted_existing_ids_validate(self):
        value = sample()
        value['core'][0]['evidence_ids'] = ["C0001','C0002", 'C0001']
        result = validate_deepread(value, source_evidence(record().transcript.text))
        self.assertEqual(result['core'][0]['evidence_ids'], ['C0001', 'C0002'])


    def test_model_parsing_normalizes_before_core_reference_length_validation(self):
        value = sample()
        value['core'][0]['evidence_ids'] = ["C0062','C0063','C0070; C0071 C0072"]
        parsed = DeepRead.model_validate(value)
        self.assertEqual(parsed.core[0].evidence_ids,
                         ['C0062', 'C0063', 'C0070', 'C0071', 'C0072'])
