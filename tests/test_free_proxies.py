import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_officer import free_proxies as fp
from news_officer.models import Episode
from news_officer.youtube import YouTubeTranscriptProvider


class FreeProxyTests(unittest.TestCase):
    def setUp(self):
        fp._WORKING.clear()
        fp._SPENT.clear()
        self.config = fp.ProxyConfig(True, (), 2, 1, 3, 1)

    def test_env_defaults_and_overrides(self):
        with patch.dict("os.environ", {}, clear=True):
            config = fp.ProxyConfig.from_env()
            self.assertEqual(
                (config.workers, config.timeout, config.candidates, config.budget),
                (32, 8, 300, 120),
            )
            self.assertTrue(config.enabled)
        with patch.dict(
            "os.environ",
            {
                "YOUTUBE_FREE_PROXY_ENABLED": "false",
                "YOUTUBE_FREE_PROXY_LISTS": " a , b ",
                "YOUTUBE_FREE_PROXY_WORKERS": "4",
                "YOUTUBE_FREE_PROXY_REQUEST_TIMEOUT": "nan",
                "YOUTUBE_FREE_PROXY_MAX_CANDIDATES": "bad",
                "YOUTUBE_FREE_PROXY_TOTAL_BUDGET": "5",
            },
            clear=True,
        ):
            config = fp.ProxyConfig.from_env()
            self.assertFalse(config.enabled)
            self.assertEqual(config.lists, ("a", "b"))
            self.assertEqual(
                (config.workers, config.timeout, config.candidates, config.budget),
                (4, 8, 300, 5),
            )

    def test_first_success_returns_without_waiting_for_loser(self):
        release = threading.Event()
        started = threading.Event()

        def fetch(proxy, timeout, deadline):
            if proxy == "slow":
                started.set()
                release.wait(2)
                return None
            self.assertTrue(started.wait(1))
            return "transcript"

        try:
            start = time.monotonic()
            self.assertEqual(
                fp._race(["slow", "winner", "queued"], fetch, self.config, start + 1),
                "transcript",
            )
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(fp._WORKING, ["winner"])
        finally:
            release.set()

    def test_definitive_error_stops_rotation(self):
        fetch = Mock(side_effect=fp.TranscriptsDisabled("video"))
        config = fp.ProxyConfig(True, (), 1, 1, 3, 1)
        with self.assertRaises(fp.TranscriptsDisabled):
            fp._race(["one", "two"], fetch, config, time.monotonic() + 1)
        self.assertEqual(fetch.call_count, 1)

    def test_proxy_errors_continue_and_candidate_cap(self):
        fetch = Mock(side_effect=RuntimeError("blocked"))
        with (
            patch.object(fp.ProxyConfig, "from_env", return_value=self.config),
            patch.object(fp, "_load_candidates", return_value=["a", "b", "c", "d"]),
        ):
            self.assertIsNone(fp.fetch_with_free_proxies(fetch))
        self.assertEqual(fetch.call_count, 3)

    def test_budget_exhaustion_does_not_wait_for_running_request(self):
        release = threading.Event()
        config = fp.ProxyConfig(True, (), 1, 1, 3, 0.03)
        try:
            start = time.monotonic()
            self.assertIsNone(
                fp._race(
                    ["a", "b"],
                    lambda *args: release.wait(2) or None,
                    config,
                    start + config.budget,
                )
            )
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            release.set()

    def test_remembered_proxy_is_tried_before_list_fetch(self):
        fp._WORKING.append("remembered")
        fetch = Mock(return_value="transcript")
        with (
            patch.object(fp.ProxyConfig, "from_env", return_value=self.config),
            patch.object(fp, "_load_candidates") as load,
        ):
            self.assertEqual(fp.fetch_with_free_proxies(fetch), "transcript")
            load.assert_not_called()
        self.assertEqual(fetch.call_args.args[0], "remembered")

    def test_disabled_does_no_work(self):
        with (
            patch.dict("os.environ", {"YOUTUBE_FREE_PROXY_ENABLED": "false"}),
            patch.object(fp, "_load_candidates") as load,
        ):
            self.assertIsNone(fp.fetch_with_free_proxies(Mock()))
            load.assert_not_called()

    def test_list_parsing_socks_and_deduplication(self):
        response = Mock(text="1.2.3.4:80\ninvalid\n1.2.3.4:80\n")
        config = fp.ProxyConfig(True, ("https://example/socks5.txt",), 1, 1, 3, 1)
        with patch.object(fp.TimeoutSession, "get", return_value=response):
            self.assertEqual(
                fp._load_candidates(config, time.monotonic() + 1),
                ["socks5://1.2.3.4:80"],
            )

    def test_private_local_and_metadata_proxies_are_filtered(self):
        blocked = ['10.0.0.1:80', '172.16.0.1:80', '192.168.1.1:80',
                   '127.0.0.1:80', '0.0.0.0:80', '169.254.169.254:80',
                   '169.254.1.1:80', 'localhost:80', 'local.example:80',
                   '[::1]:80', '[fe80::1]:80', '[fc00::1]:80',
                   '[::ffff:127.0.0.1]:80', '1.2.3.4:invalid']
        response = Mock(text='\n'.join(blocked + ['8.8.8.8:80']))
        config = fp.ProxyConfig(True, ('https://example/http.txt',), 1, 1, 30, 1)
        with patch.object(fp.TimeoutSession, 'get', return_value=response):
            self.assertEqual(fp._load_candidates(config, time.monotonic() + 1), ['http://8.8.8.8:80'])

    def test_validation_failure_stops_proxy_rotation(self):
        fetch = Mock(side_effect=fp.CaptionValidationFailed('incomplete'))
        config = fp.ProxyConfig(True, (), 1, 1, 3, 1)
        with self.assertRaises(fp.CaptionValidationFailed):
            fp._race(['one', 'two'], fetch, config, time.monotonic() + 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fp._WORKING, [])

    def test_session_timeout_and_tls(self):
        with patch("requests.Session.request") as request:
            fp.TimeoutSession(8).get("https://example", verify=False)
            self.assertEqual(request.call_args.kwargs["timeout"], 8)
            self.assertTrue(request.call_args.kwargs["verify"])


class ApiChainTests(unittest.TestCase):
    def setUp(self):
        self.episode = Episode(
            "kG8AoExkX40",
            "long video",
            "https://youtu.be/kG8AoExkX40",
            "show",
            duration_seconds=1800,
        )

    def test_incomplete_primary_does_not_rotate_proxies(self):
        snippets = [SimpleNamespace(text='short', start=0, duration=10)]
        with patch('youtube_transcript_api.YouTubeTranscriptApi', return_value=Mock(fetch=Mock(return_value=snippets))), \
                patch('news_officer.youtube.fetch_with_free_proxies') as rotate:
            self.assertIsNone(YouTubeTranscriptProvider()._fetch_api(self.episode))
        rotate.assert_not_called()

    def test_incomplete_proxy_stops_before_loading_more_candidates(self):
        snippets = [SimpleNamespace(text='short', start=0, duration=10)]
        fp._WORKING[:] = ['http://8.8.8.8:80']
        with patch('youtube_transcript_api.YouTubeTranscriptApi', side_effect=[
                Mock(fetch=Mock(side_effect=RuntimeError('blocked'))),
                Mock(fetch=Mock(return_value=snippets))]) as api, \
                patch.object(fp, '_load_candidates') as load:
            self.assertIsNone(YouTubeTranscriptProvider()._fetch_api(self.episode))
        self.assertEqual(api.call_count, 2)
        load.assert_not_called()

    def test_primary_then_rotation_then_ytdlp(self):
        order = []
        with (
            patch(
                "youtube_transcript_api.YouTubeTranscriptApi",
                side_effect=lambda **kwargs: (
                    order.append("primary")
                    or Mock(fetch=Mock(side_effect=RuntimeError()))
                ),
            ),
            patch(
                "news_officer.youtube.fetch_with_free_proxies",
                side_effect=lambda fetch: order.append("rotation"),
            ),
            patch.object(
                YouTubeTranscriptProvider,
                "_fetch_captions",
                side_effect=lambda episode: order.append("ytdlp"),
            ),
        ):
            self.assertIsNone(YouTubeTranscriptProvider().fetch(self.episode))
        self.assertEqual(order, ["primary", "rotation", "ytdlp"])

    def test_definitive_primary_skips_rotation_but_falls_through(self):
        with (
            patch(
                "youtube_transcript_api.YouTubeTranscriptApi",
                return_value=Mock(
                    fetch=Mock(side_effect=fp.TranscriptsDisabled("video"))
                ),
            ),
            patch("news_officer.youtube.fetch_with_free_proxies") as rotate,
            patch.object(
                YouTubeTranscriptProvider, "_fetch_captions", return_value="captions"
            ),
        ):
            self.assertEqual(
                YouTubeTranscriptProvider().fetch(self.episode), "captions"
            )
            rotate.assert_not_called()

    def test_configured_primary_proxy_and_rotation_success(self):
        with (
            patch.dict(
                "os.environ",
                {"YOUTUBE_TRANSCRIPT_PROXY_URL": "http://user:secret@example:80"},
            ),
            patch(
                "youtube_transcript_api.YouTubeTranscriptApi",
                return_value=Mock(fetch=Mock(side_effect=RuntimeError())),
            ) as api,
            patch(
                "news_officer.youtube.fetch_with_free_proxies",
                return_value="transcript",
            ),
            patch.object(YouTubeTranscriptProvider, "_fetch_captions") as captions,
        ):
            self.assertEqual(
                YouTubeTranscriptProvider().fetch(self.episode), "transcript"
            )
            self.assertEqual(
                api.call_args.kwargs["proxy_config"].http_url,
                "http://user:secret@example:80",
            )
            captions.assert_not_called()

    def test_valid_primary_returns_before_rotation_and_prefers_english(self):
        snippets = Mock()
        snippets.language_code = "en"
        snippets.__iter__ = Mock(
            side_effect=lambda: iter(
                [
                    SimpleNamespace(
                        text=" ".join(f"word{i}_{j}" for j in range(20)),
                        start=i * 18,
                        duration=18,
                    )
                    for i in range(100)
                ]
            )
        )
        with (
            patch(
                "youtube_transcript_api.YouTubeTranscriptApi",
                return_value=Mock(fetch=Mock(return_value=snippets)),
            ) as api,
            patch("news_officer.youtube.fetch_with_free_proxies") as rotate,
            patch.object(YouTubeTranscriptProvider, "_fetch_captions") as captions,
        ):
            result = YouTubeTranscriptProvider().fetch(self.episode)
            self.assertTrue(result.verified_complete)
            self.assertEqual(result.language, "en")
            api.return_value.fetch.assert_called_once_with(
                "kG8AoExkX40", languages=["en", "en-US"]
            )
            rotate.assert_not_called()
            captions.assert_not_called()


class InferredDurationTests(unittest.TestCase):
    def _run(self, total_seconds):
        snippets = [
            SimpleNamespace(text=" ".join(["word"] * 150), start=float(i * 60), duration=60.0)
            for i in range(int(total_seconds // 60))
        ]
        fetched = type("F", (list,), {"language_code": "en"})(snippets)
        episode = Episode(
            id="abcdefghijk", title="Talk", url="https://www.youtube.com/watch?v=abcdefghijk",
            show="AI Engineer",
        )
        with (
            patch("news_officer.youtube.video_metadata", return_value=episode),
            patch("news_officer.youtube.youtube_skip_reason", return_value=None),
            patch("youtube_transcript_api.YouTubeTranscriptApi", return_value=Mock(fetch=Mock(return_value=fetched))),
        ):
            return YouTubeTranscriptProvider()._fetch_api(episode)

    def test_unknown_duration_inferred_from_captions(self):
        self.assertIsNotNone(self._run(30 * 60))

    def test_unknown_duration_short_video_rejected(self):
        self.assertIsNone(self._run(10 * 60))
