import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.podcast import load_youtube_sources


class CuratedSourcesTests(unittest.TestCase):
    def test_cheeky_pint_source_loads(self):
        feeds_path = ROOT / "feeds.json"
        sources = json.loads(feeds_path.read_text())["sources"]
        configured = [source for source in sources if source["name"] == "Cheeky Pint"]
        self.assertEqual(configured, [{
            "name": "Cheeky Pint",
            "type": "rss",
            "priority": "A",
            "url": "",
            "rss_url": "https://cheekypint.substack.com/feed",
            "enabled": True,
        }])
        self.assertIs(configured[0]["enabled"], True)

        loaded = [source for source in load_youtube_sources(feeds_path)
                  if source.name == "Cheeky Pint"]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].source_type, "rss")
        self.assertEqual(loaded[0].priority, "A")
        self.assertEqual(loaded[0].url, "")
        self.assertEqual(loaded[0].rss_url, "https://cheekypint.substack.com/feed")

    def test_only_podcast_generalist_source_is_configured(self):
        feeds_text = (ROOT / "feeds.json").read_text(encoding="utf-8")
        sources = json.loads(feeds_text)["sources"]
        generalist_sources = [source for source in sources
                              if "generalist" in source["name"].casefold()]
        self.assertEqual(generalist_sources, [{
            "name": "The Generalist",
            "type": "youtube",
            "priority": "B",
            "url": "https://www.youtube.com/channel/UC5xRQOIT8cfumDr2HaKNd4g/videos",
            "rss_url": "https://api.substack.com/feed/podcast/15764.rss",
        }])
        self.assertNotIn("UCwV9z8IGmbUQUb5D8KSv3mQ", feeds_text)

    def test_curated_youtube_sources_load(self):
        expected = {
            "Anthropic": "UCrDwWp7EBBv4NwvScIpBDOA",
            "AI Engineer": "UCLKPca3kwwd-B59HNr-_lvA",
            "Andrej Karpathy": "UCXUPKJO5MZQN11PqgIvyuvQ",
            "Stripe": "UCM1guA1E-RHLO2OyfQPOkEQ",
            "Founders You Should Know": "UC7tPLStIIpO7a9x9ERMXrdg",
            "Index Ventures": "UCFMxDWNYefVV3XmCdCBCFjQ",
        }
        feeds_path = ROOT / "feeds.json"
        sources = json.loads(feeds_path.read_text())["sources"]
        names = [source["name"] for source in sources]
        self.assertEqual(len(names), len(set(names)))
        configured = {source["name"]: source for source in sources}
        loaded = {source.name: source for source in load_youtube_sources(feeds_path)}
        for name, channel_id in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, configured)
                self.assertIn(name, loaded)
                source = configured[name]
                self.assertEqual(source["type"], "youtube")
                self.assertEqual(source["priority"], "B")
                self.assertIn("rss_url", source)
                self.assertEqual(source["url"],
                                 f"https://www.youtube.com/channel/{channel_id}/videos")
                self.assertRegex(source["url"],
                                 r"\Ahttps://www\.youtube\.com/channel/UC[A-Za-z0-9_-]{22}/videos\Z")
                self.assertEqual(loaded[name].url, source["url"])
                self.assertEqual(loaded[name].priority, "B")

    def test_curated_rss_sources_load(self):
        expected_urls = {
            "https://feeds.megaphone.fm/nopriors",
            "https://api.substack.com/feed/podcast/1084089.rss",
            "https://anchor.fm/s/f2ee4948/podcast/rss",
            "https://feeds.simplecast.com/dOSE_bdP",
            "https://api.substack.com/feed/podcast/458709.rss",
            "https://anchor.fm/s/e231a4ec/podcast/rss",
            "https://anchor.fm/s/1156f1c04/podcast/rss",
            "https://api.substack.com/feed/podcast/320996.rss",
            "https://feeds.simplecast.com/JT6pbPkg",
            "https://madrona.libsyn.com/rss",
        }
        feeds_path = ROOT / "feeds.json"
        configured = {
            source["rss_url"]: source
            for source in json.loads(feeds_path.read_text())["sources"]
            if source.get("rss_url") in expected_urls
        }
        loaded = {
            source.rss_url: source for source in load_youtube_sources(feeds_path)
            if source.rss_url in expected_urls
        }
        self.assertEqual(set(configured), expected_urls)
        self.assertEqual(set(loaded), expected_urls)
        for rss_url in expected_urls:
            with self.subTest(rss_url=rss_url):
                self.assertEqual(configured[rss_url]["type"], "rss")
                self.assertIs(configured[rss_url]["enabled"], True)
                self.assertEqual(configured[rss_url]["priority"], "B")
                self.assertEqual(configured[rss_url]["url"], "")
                self.assertEqual(loaded[rss_url].url, "")
                self.assertEqual(loaded[rss_url].priority, "B")
                self.assertEqual(loaded[rss_url].name, configured[rss_url]["name"])

    def test_feeds_have_no_duplicate_rss_urls(self):
        sources = json.loads((ROOT / "feeds.json").read_text())["sources"]
        rss_urls = [source["rss_url"].strip().rstrip("/")
                    for source in sources if source.get("rss_url")]
        self.assertEqual(len(rss_urls), len(set(rss_urls)))
