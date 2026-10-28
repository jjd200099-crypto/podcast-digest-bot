import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.models import Episode, Transcript
from news_officer.podcast import FeedDiscoveryError, PodcastService, TranscriptResolver
from news_officer.store import Store


class RSSFirstSeenTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'db')
        self.store.initialize()
        self.feeds = self.root / 'feeds.json'
        self.feed = 'https://example.org/feed'
        self.write_feeds([self.feed])
        self.now = datetime(2026, 10, 17, 12, tzinfo=UTC)
        provider = Mock(fetch=Mock(return_value=Transcript('full text', 'official', '', True)))
        self.service = PodcastService(self.store, self.feeds, Mock(summarize=Mock(return_value='summary')),
                                      TranscriptResolver([provider]), lookback_hours=24,
                                      daily_rss_only=True)

    def write_feeds(self, urls):
        self.feeds.write_text(json.dumps({'sources': [
            {'name': url, 'type': 'rss', 'rss_url': url} for url in urls]}))

    def episode(self, key, days=10, **metadata):
        return Episode('rss:' + key, 'Interview ' + key, 'https://example.org/' + key, 'Show',
                       duration_seconds=1800, published_at=self.now - timedelta(days=days),
                       metadata={'rss_feed_url': self.feed, 'rss_guid': key,
                                 'audio_url': 'https://example.org/' + key + '.mp3', **metadata})

    def scan(self, episodes, now=None):
        with patch('news_officer.podcast.latest_rss_episodes', return_value=episodes):
            return self.service.discover_daily_candidates(now or self.now)

    def seed(self):
        self.assertEqual(self.scan([self.episode('seed')]), [])

    def test_first_run_matches_pubdate_only(self):
        episodes = [self.episode('old'), self.episode('recent', days=0)]
        result = self.scan(episodes)
        self.assertEqual([e.id for e in result], ['rss:recent'])
        self.assertFalse(any(e.metadata.get('first_seen_promoted') for e in result))
        observations = self.store.record_rss_first_seen(self.feed, episodes, self.now)
        self.assertEqual(observations, [(self.now, True), (self.now, True)])

    def test_new_old_item_promoted_then_expires(self):
        self.seed()
        episode = self.episode('new')
        [candidate] = self.scan([episode])
        self.assertTrue(candidate.metadata['first_seen_promoted'])
        self.assertEqual(candidate.metadata['rss_first_seen_at'], self.now.isoformat())
        self.assertEqual(self.scan([episode], self.now + timedelta(hours=25)), [])

    def test_identity_aliases_keep_earliest_and_seeded(self):
        self.seed()
        original = self.episode('new')
        self.scan([original])
        later = self.now + timedelta(hours=25)
        for changed in [replace(original, metadata={**original.metadata, 'audio_url': 'https://x.org/new'}),
                        replace(original, metadata={**original.metadata, 'rss_guid': 'changed'})]:
            self.assertEqual(self.scan([changed], later), [])
            [(first, seeded)] = self.store.record_rss_first_seen(self.feed, [changed], later)
            self.assertEqual(first, self.now)
            self.assertFalse(seeded)
        seeded = self.episode('seed', audio_url='https://x.org/changed')
        self.assertEqual(self.scan([seeded]), [])

    def test_max_age_and_future(self):
        self.seed()
        self.assertEqual(self.scan([self.episode('ancient', 46), self.episode('future', -1)]), [])

    def test_burst_limit_is_per_feed_and_allows_three(self):
        self.seed()
        candidates = self.scan([self.episode(str(i)) for i in range(3)])
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(e.metadata.get('first_seen_promoted') for e in candidates))

    def test_burst_guard_persists_suppression(self):
        self.seed()
        episodes = [self.episode(str(i)) for i in range(4)]
        with self.assertLogs('news_officer.store', level='WARNING') as logs:
            self.assertEqual(self.scan(episodes), [])
        self.assertIn(self.feed, logs.output[0])
        self.assertEqual(self.scan(episodes[:1]), [])
        self.assertTrue(all(row[1] for row in self.store.record_rss_first_seen(
            self.feed, episodes, self.now)))

    def test_failed_fetch_does_not_seed(self):
        with patch('news_officer.podcast.latest_rss_episodes', side_effect=TimeoutError('offline')), \
                self.assertRaises(FeedDiscoveryError):
            self.service.discover_daily_candidates(self.now)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rss_item_first_seen').fetchone()[0], 0)
        self.seed()

    def test_new_feed_is_seeded_independently(self):
        self.seed()
        added = 'https://example.org/added'
        self.write_feeds([self.feed, added])
        with patch('news_officer.podcast.latest_rss_episodes',
                   side_effect=lambda name, feed, **kwargs: [self.episode('seed' if feed == self.feed else 'added')]):
            self.assertEqual(self.service.discover_daily_candidates(self.now), [])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT seeded FROM rss_item_first_seen WHERE feed_key=?',
                                        (added,)).fetchone()[0], 1)

    def test_added_podcast_feeds_seed_history_without_backfill(self):
        self.seed()
        names = {"TBPN", "Decoder with Nilay Patel", "CNBC Squawk Pod"}
        added = [s for s in json.loads((ROOT / 'feeds.json').read_text())["sources"]
                 if s["name"] in names]
        self.assertEqual(len(added), 3)
        self.feeds.write_text(json.dumps({"sources": added}))

        def fetch(name, feed, **kwargs):
            return [replace(self.episode(name), title="AI interview", show=name,
                            metadata={"rss_feed_url": feed, "rss_guid": name,
                                      "audio_url": feed + "/episode.mp3"})]

        with patch('news_officer.podcast.latest_rss_episodes', side_effect=fetch):
            self.assertEqual(self.service.discover_daily_candidates(self.now), [])
            self.assertEqual(self.service.discover_daily_candidates(
                self.now + timedelta(hours=1)), [])
        with self.store._connect() as db:
            for source in added:
                with self.subTest(name=source["name"]):
                    rows = db.execute(
                        'SELECT seeded FROM rss_item_first_seen WHERE feed_key=?',
                        (source["rss_url"],)).fetchall()
                    self.assertTrue(rows)
                    self.assertTrue(all(row[0] == 1 for row in rows))

    def test_flag_off_does_not_record_or_promote(self):
        self.service.rss_first_seen = False
        self.assertEqual([e.id for e in self.scan([self.episode('old'), self.episode('recent', 0)])],
                         ['rss:recent'])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rss_item_first_seen').fetchone()[0], 0)

    def test_already_summarized_or_skipped_stays_skipped(self):
        self.seed()
        for state in ['summarized', 'skipped']:
            episode = self.episode(state)
            self.store.record_episode(episode, state)
            self.assertEqual(self.scan([episode]), [])

    def test_build_tracks_and_processes_promoted_item(self):
        self.seed()
        episode = self.episode('new')
        with patch('news_officer.podcast.latest_rss_episodes', return_value=[episode]), \
                patch.object(self.store, 'track_daily_candidates', wraps=self.store.track_daily_candidates) as track:
            result = self.service.build_daily(self.now)
        self.assertEqual([r.status for r in result], ['summarized'])
        self.assertEqual(track.call_args.args[0][0].id, episode.id)

    def test_promoted_backlog_can_retry_after_window_expires(self):
        self.seed()
        episode = self.episode('retry')
        [promoted] = self.scan([episode])
        self.store.track_daily_candidates([promoted])
        with self.store._connect() as db:
            db.execute("UPDATE daily_transcript_backlog SET next_check_at='2000-01-01'")
        results = self.service.build_pending()
        self.assertEqual([item.status for item in results], ['summarized'])

    def test_metadata_does_not_invalidate_archived_digest(self):
        episode = self.episode('archive')
        transcript = Transcript('complete transcript', 'official', '', True)
        old = self.store.save_verified_transcript(replace(episode, metadata={}), transcript)
        self.seed()
        [promoted] = self.scan([episode])
        updated = self.store.save_verified_transcript(promoted, transcript)
        self.assertEqual(old.record_revision_sha256, updated.record_revision_sha256)
        self.assertEqual(old.episode.to_persisted_dict(), updated.episode.to_persisted_dict())

    def test_candidate_cap_still_seeds_the_full_rss_feed(self):
        self.service.max_daily_candidates = 1
        with patch('news_officer.podcast.latest_rss_episodes',
                   return_value=[self.episode(str(i)) for i in range(40)]) as fetch:
            self.assertEqual(self.service.discover_daily_candidates(self.now), [])
        self.assertEqual(fetch.call_args.kwargs['limit'], 0)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rss_item_first_seen').fetchone()[0], 80)

    def test_promoted_items_beyond_30_cutoff_are_still_kept(self):
        self.service.max_daily_candidates = 10
        seeded = [self.episode(str(i)) for i in range(35)]
        self.assertEqual(self.scan(seeded), [])
        deep_item = self.episode('new-deep-item', days=20)

        candidates = self.scan([*seeded, deep_item])

        self.assertEqual([episode.id for episode in candidates], [deep_item.id])
        self.assertTrue(candidates[0].metadata['first_seen_promoted'])

    def test_atom_entries_do_not_enter_first_seen_store(self):
        atom = replace(self.episode('atom'), metadata={'rss_feed_url': self.feed})
        self.assertEqual(self.scan([atom]), [])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rss_item_first_seen').fetchone()[0], 0)

    def test_feed_and_enclosure_normalization(self):
        episode = self.episode('seed')
        self.store.record_rss_first_seen(self.feed + '/?utm_source=x', [episode], self.now)
        changed = replace(episode, metadata={**episode.metadata, 'rss_guid': 'changed',
                          'audio_url': episode.metadata['audio_url'] + '?utm_source=x'})
        self.assertEqual(self.store.record_rss_first_seen(self.feed, [changed],
                         self.now + timedelta(days=1)), [(self.now, True)])
