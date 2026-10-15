import json
import unittest
from pathlib import Path

# Audio RSS URLs from dac0a5949bc770cec9202b450264fc7b94cc0eab.
# Keep this snapshot independent of git history at test time.
AUDIO_RSS_SNAPSHOT = {
    'SemiAnalysis Weekly': 'https://anchor.fm/s/10fbee758/podcast/rss',
    '20VC': 'https://rss.libsyn.com/shows/61840/destinations/240976.xml',
    'BG2': 'https://anchor.fm/s/f06c2370/podcast/rss',
    'All-In Podcast': 'https://allinchamathjason.libsyn.com/rss',
    'Big Technology Podcast': 'https://feeds.megaphone.fm/LI3617121267',
    'Goldman Sachs Exchanges — Tech/AI': 'https://feeds.megaphone.fm/GLD9218176758',
    'Invest Like the Best': 'https://feeds.megaphone.fm/CLS2859450455',
    'Sequoia Capital — Training Data': 'https://feeds.megaphone.fm/trainingdata',
    'David Senra': 'https://feeds.megaphone.fm/david-senra',
    'Acquired': 'https://feeds.transistor.fm/acquired',
    'Dwarkesh Podcast': 'https://api.substack.com/feed/podcast/69345.rss',
    'a16z Show': 'https://feeds.simplecast.com/JGE3yC0V',
    "Lenny's Podcast": 'https://api.substack.com/feed/podcast/10845.rss',
    'The Generalist': 'https://api.substack.com/feed/podcast/15764.rss',
}

YOUTUBE_ONLY_CHANNELS = {
    'Anthropic', 'AI Engineer', 'Andrej Karpathy', 'Stripe',
    'Founders You Should Know', 'Index Ventures',
}


def configured_sources():
    return json.loads(
        (Path(__file__).resolve().parents[1] / 'feeds.json').read_text(encoding='utf-8')
    )['sources']


class FeedsAudioRSSPreservedTests(unittest.TestCase):
    def test_audio_rss_urls_preserved(self):
        sources = configured_sources()
        for name, rss_url in AUDIO_RSS_SNAPSHOT.items():
            with self.subTest(name=name):
                matches = [source for source in sources if source['name'] == name]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]['rss_url'], rss_url)
                self.assertNotIn('youtube.com/feeds', matches[0]['rss_url'])

    def test_youtube_video_rss_only_for_channels_without_original_audio_rss(self):
        for source in configured_sources():
            if 'videos.xml' in source.get('rss_url', ''):
                with self.subTest(name=source['name']):
                    self.assertTrue(
                        source['name'] in YOUTUBE_ONLY_CHANNELS
                        or source['name'] not in AUDIO_RSS_SNAPSHOT
                    )
