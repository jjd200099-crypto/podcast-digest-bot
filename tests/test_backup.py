import sqlite3
import tempfile
import unittest
from pathlib import Path

from news_officer.backup import create_backup, verify_backup
from news_officer.models import Episode, Transcript
from news_officer.store import Store


class BackupTests(unittest.TestCase):
    def test_snapshot_restores_sources_and_detects_tampering_without_live_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'live.sqlite3')
            store.initialize()
            episode = Episode('one', 'Test', 'https://example.org', 'Show')
            store.save_verified_transcript(episode, Transcript('complete original', 'test', episode.url, True))
            destination = root / 'backup'
            result = create_backup(store.path, root / 'memory', destination)
            self.assertTrue(result['verified'])
            self.assertFalse(result['offsite'])
            self.assertTrue(verify_backup(destination)['verified'])
            restored = Store(destination / 'state.sqlite3')
            self.assertEqual(restored.get_verified_transcript('one').transcript.text, 'complete original')
            self.assertTrue((destination / 'podcast-memory/index.json').exists())
            with self.assertRaises(ValueError):
                create_backup(store.path, root / 'memory', destination)
            with sqlite3.connect(destination / 'state.sqlite3') as db:
                db.execute("UPDATE episode_transcripts SET transcript_text='tampered'")
            with self.assertRaises(ValueError):
                verify_backup(destination)
            self.assertEqual(store.get_verified_transcript('one').transcript.text, 'complete original')

    def test_rejects_live_tree_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                create_backup(root / 'db', root / 'memory', root / 'memory/nested-backup')
