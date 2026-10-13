"""Golden output was rendered with daily_report.py from 08a91ca^.

The fixture includes missing/zero/unparseable durations and trailing whitespace.
"""
import json
import unittest
from pathlib import Path

import test_combined_daily as fixtures

from news_officer.daily_archive import read_daily_digest
from news_officer.daily_report import (
    compact_paragraphs,
    episode_summary,
    normalize_duration,
    reader_episode,
    render_reader_daily,
)
from news_officer.models import DailyItem, Episode, Transcript


class Review52Tests(unittest.TestCase):
    setUp = fixtures.CombinedDailyTests.setUp
    tearDown = fixtures.CombinedDailyTests.tearDown

    def golden_items(self):
        golden = json.loads((Path(__file__).parent / 'fixtures/review52_flag_off.json').read_text())
        items = []
        for index, message in enumerate(golden['messages']):
            episode = Episode(str(index), '标题', f'https://example.test/{index}', 'Show',
                              duration_seconds=3600)
            record = self.store.save_verified_transcript(
                episode, Transcript('Complete source', 'official', episode.url, True))
            self.store.save_transcript_digest(episode.id, message,
                record.content_sha256, record.record_revision_sha256)
            items.append(DailyItem(episode, 'summarized', message,
                                   fixtures.attachment_for(record, message.strip())))
        return golden, items

    def test_flag_off_daily_and_archive_are_byte_identical(self):
        golden, items = self.golden_items()
        self.assertEqual(render_reader_daily(items, [], compact=False).encode(),
                         golden['daily'].encode())
        for reader_mode in (False, True):
            key = f'daily:golden-{reader_mode}'
            day = '2026-10-09' if reader_mode else '2026-10-08'
            self.store.enqueue(key, 'daily', {'scheduled_for': day + 'T08:30:00+08:00'})
            for item in items:
                self.store.save_job_result(key, item.episode.id, 'daily_item', item.to_persisted_dict())
            self.store.save_job_result(key, 'bundle', 'daily_bundle', {'reader_mode': reader_mode})
            self.store.mark_analysis_complete(key)
            self.assertEqual(read_daily_digest(self.store, day)['markdown'].encode(),
                             golden[f'archive_{reader_mode}'].encode())

    def test_unparseable_duration_is_preserved_in_compact_output(self):
        for value in ('约60分钟', '62 min'):
            with self.subTest(value=value):
                self.assertEqual(normalize_duration(value), value)
                item = DailyItem(Episode('x', '标题', 'https://example.test', 'Show',
                                         duration_string=value), 'summarized', '节目：Show\n观点。')
                self.assertIn('时长：' + value,
                              reader_episode(episode_summary(item, compact=True), compact=True))

    def test_zero_duration_is_missing(self):
        for value in (0, '0', '00:00', '0 分钟', '0秒', None, ''):
            with self.subTest(value=value):
                self.assertEqual(normalize_duration(value), '未提供')
        item = DailyItem(Episode('zero', '标题', 'https://example.test', 'Show',
                                 duration_seconds=0), 'summarized', '节目：Show\n观点。')
        self.assertIn('时长：未提供', render_reader_daily([item], [], compact=True))
        self.assertEqual(normalize_duration(3720), '62 分钟')

    def test_indivisible_sentence_is_not_duplicated(self):
        source = '节目：Show\n\n唯一观点。\n\n链接：https://example.test'
        result = compact_paragraphs(source)
        self.assertEqual(result.count('唯一观点。'), 1)
        self.assertEqual(reader_episode(source, compact=True).count('唯一观点。'), 1)

    def test_label_links_survive_paragraph_merging(self):
        for label in ('节目链接：', '收听链接：'):
            with self.subTest(label=label):
                source = ('节目：Show\n\n首句。\n\n次句。\n' + label
                          + 'https://example.test/listen\n\n末句。')
                self.assertIn(label + 'https://example.test/listen', compact_paragraphs(source))
                result = reader_episode(source, compact=True)
                self.assertNotIn(label, result)
                self.assertTrue(result.endswith('[收听节目](https://example.test/listen)'))
