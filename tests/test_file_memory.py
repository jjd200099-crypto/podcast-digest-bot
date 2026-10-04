import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_officer.file_memory import PodcastFileMemory
from news_officer.models import Episode, Transcript
from news_officer.podcast_archive import PodcastArchive
from news_officer.store import Store


class FileMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "state.db")
        self.store.initialize()
        self.root = self.directory / "podcast-memory"
        self.memory = PodcastFileMemory(self.store, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, identity="one", text="Guest: Original words.\n保留数字 123 和语义限定。\n"):
        return self.store.save_verified_transcript(
            Episode(identity, "A good episode", "https://example.test/episode", "Show",
                    published_at=datetime(2026, 9, 17, tzinfo=UTC)),
            Transcript(text, "official", "https://example.test/transcript", True),
        )

    def entry(self, reference):
        return json.loads((self.root / "index.json").read_text())["episodes"][reference]

    def test_raw_text_exact_and_no_private_dialogue_exported(self):
        record = self.record()
        self.store.save_conversation_context("private", pending_question="PRIVATE_MARKER")
        self.assertEqual(self.memory.sync(), [record.reference])
        raw = self.root / self.entry(record.reference)["directory"] / "transcript.txt"
        self.assertEqual(raw.read_bytes(), record.transcript.text.encode())
        self.assertNotIn("PRIVATE_MARKER", "".join(p.read_text() for p in self.root.rglob("*") if p.is_file()))
        docs, warnings = self.memory.snapshot()
        self.assertEqual(docs[0].text, record.transcript.text)
        self.assertFalse(warnings)

    def test_restart_is_idempotent_and_keeps_index_stable(self):
        self.record()
        self.memory.sync()
        before = (self.root / "index.json").read_bytes()
        reopened = PodcastFileMemory(Store(self.store.path), self.root)
        self.assertEqual(reopened.sync(), [])
        self.assertEqual((self.root / "index.json").read_bytes(), before)
        self.assertEqual(len(reopened.snapshot()[0]), 1)

    def test_summary_is_separate_and_old_revision_survives(self):
        record = self.record()
        self.memory.sync()
        old = self.entry(record.reference)["directory"]
        summary = "推荐理由：一个例子。\n1. 一条要点。\n推荐星级：★★★★☆"
        self.store.save_transcript_digest(record.episode.id, summary,
                                         record.content_sha256, record.record_revision_sha256)
        self.memory.sync()
        new = self.entry(record.reference)["directory"]
        self.assertNotEqual(old, new)
        self.assertTrue((self.root / old / "transcript.txt").exists())
        self.assertEqual((self.root / new / "summary.md").read_text(), summary)
        self.assertEqual(self.memory.snapshot()[0][0].text, record.transcript.text)

    def test_updated_transcript_has_new_version_and_no_stale_summary(self):
        record = self.record()
        self.store.save_transcript_digest(record.episode.id, "old summary",
                                         record.content_sha256, record.record_revision_sha256)
        self.memory.sync()
        old = self.entry(record.reference)["directory"]
        updated = self.record(text="Changed verified full transcript")
        self.memory.sync()
        new = self.entry(record.reference)["directory"]
        self.assertNotEqual(new, old)
        self.assertFalse((self.root / new / "summary.md").exists())
        self.assertEqual(self.memory.snapshot()[0][0].text, updated.transcript.text)
        self.assertTrue((self.root / old / "summary.md").exists())

    def test_corrupt_file_is_not_silently_overwritten_or_used(self):
        record = self.record()
        self.memory.sync()
        path = self.root / self.entry(record.reference)["directory"] / "transcript.txt"
        path.write_text("Unverified modification")
        docs, warnings = self.memory.snapshot()
        self.assertEqual(docs, [])
        self.assertTrue(warnings)
        self.assertEqual(path.read_text(), "Unverified modification")

    def test_corrupt_episode_does_not_block_other_files(self):
        first, second = self.record(), self.record("two")
        self.memory.sync()
        path = self.root / self.entry(first.reference)["directory"] / "metadata.json"
        path.write_text("changed metadata")
        docs, warnings = self.memory.snapshot()
        self.assertEqual([d.token for d in docs], [second.reference])
        self.assertTrue(warnings)

    def test_every_archived_episode_is_searchable_including_older_than_200(self):
        oldest = self.record("oldest")
        for index in range(201):
            self.record(str(index))
        docs, warnings = PodcastArchive(self.store, self.root).snapshot()
        self.assertEqual(len(docs), 202)
        self.assertIn(oldest.reference, {d.token for d in docs})
        self.assertFalse(warnings)

    def test_unverified_transcript_rejected_before_files(self):
        record = self.record()
        with self.assertRaises(ValueError):
            self.store.save_verified_transcript(record.episode, replace(record.transcript, verified_complete=False))

    def test_database_hash_corruption_is_not_exported(self):
        self.record()
        with self.store._connect() as db:
            db.execute("UPDATE episode_transcripts SET transcript_text='changed'")
        docs, warnings = self.memory.snapshot()
        self.assertFalse(docs)
        self.assertTrue(warnings)

    def test_titles_cannot_escape_root(self):
        record = self.record()
        self.store.save_verified_transcript(
            replace(record.episode, title="../../outside\\x", show="../../sneaky"), record.transcript)
        self.memory.sync()
        (self.root / self.entry(record.reference)["directory"]).resolve().relative_to(self.root.resolve())
        self.assertFalse((self.directory / "outside").exists())

    def test_symlink_file_cannot_read_or_overwrite_outside_file(self):
        record = self.record()
        self.memory.sync()
        outside = self.directory / "outside.txt"
        outside.write_text("private outside file")
        path = self.root / self.entry(record.reference)["directory"] / "transcript.txt"
        path.unlink()
        path.symlink_to(outside)
        docs, warnings = self.memory.snapshot()
        self.assertFalse(docs)
        self.assertTrue(warnings)
        self.assertEqual(outside.read_text(), "private outside file")

    def test_root_symlink_is_rejected(self):
        target = self.directory / "actual"
        target.mkdir()
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            PodcastFileMemory(self.store, self.root)

    def test_unrelated_existing_index_is_not_overwritten(self):
        self.root.mkdir()
        path = self.root / "index.json"
        path.write_text('{"my_manual_notes": true}')
        self.record()
        with self.assertRaises(ValueError):
            self.memory.sync()
        self.assertEqual(path.read_text(), '{"my_manual_notes": true}')

    def test_root_must_be_a_dedicated_directory(self):
        with self.assertRaises(ValueError):
            PodcastFileMemory(self.store, self.directory)

    def test_interrupted_file_write_not_indexed_and_retry_recovers(self):
        import os
        self.record()
        original = os.replace
        def fail_metadata(source, target):
            if str(target).endswith("metadata.json"):
                raise OSError("simulated interrupted write")
            return original(source, target)
        with patch("news_officer.file_memory.os.replace", side_effect=fail_metadata):
            self.memory.sync()
        self.assertEqual(json.loads((self.root / "index.json").read_text())["episodes"], {})
        self.assertFalse(list(self.root.rglob(".pending-*")))
        self.assertEqual(len(self.memory.snapshot()[0]), 1)


if __name__ == "__main__":
    unittest.main()
