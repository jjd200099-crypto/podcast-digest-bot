"""Cloud RSS/Podwise smoke test: temporary DB, no LLM calls or Feishu sends."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from news_officer.config import Settings
from news_officer.podcast import PodcastService, load_youtube_sources
from news_officer.podwise import PodwiseTranscriptProvider
from news_officer.store import Store
from news_officer.youtube import YouTubeTranscriptProvider


def main():
    settings = Settings.from_env()
    assert settings.daily_rss_only, 'RSS-only mode is not enabled'
    assert settings.podwise_api_token, 'Podwise is not configured'
    sources = load_youtube_sources(settings.feeds_path)
    assert sources and all(source.rss_url for source in sources), 'A built-in source has no RSS'
    with tempfile.TemporaryDirectory(prefix='rss-smoke-') as temporary:
        store = Store(Path(temporary) / 'test.sqlite3')
        store.initialize()
        service = PodcastService(store, settings.feeds_path, Mock(), daily_rss_only=True,
                                 podwise_api_token=settings.podwise_api_token,
                                 lookback_hours=settings.lookback_hours)
        with patch('news_officer.podcast.latest_videos', side_effect=AssertionError('YouTube scan called')), \
                patch('news_officer.podcast.video_metadata', side_effect=AssertionError('YouTube metadata called')), \
                patch.object(YouTubeTranscriptProvider, 'fetch', side_effect=AssertionError('YouTube captions called')):
            episodes = service.discover_daily_candidates()
            assert not service._last_failed_feeds, 'RSS scan had failed feeds'
            assert all(e.published_at for e in episodes), 'RSS candidate has no publish date'
            assert not any(isinstance(p, YouTubeTranscriptProvider)
                           for p in service.daily_transcript_resolver.providers)
            print(json.dumps({'rss_sources': len(sources), 'dated_candidates': len(episodes),
                              'episodes': [{'title': e.title, 'published_at': e.published_at.isoformat()}
                                           for e in episodes]}, ensure_ascii=False), flush=True)
            provider = PodwiseTranscriptProvider(settings.podwise_api_token, timeout=20)
            # Exercise a real dated RSS asset against Podwise, without generating
            # summaries or altering the production episode/outbox state.
            verified = None
            for episode in episodes[:6]:
                transcript = provider.fetch(episode)
                if transcript and transcript.verified_complete:
                    verified = {'title': episode.title, 'characters': len(transcript.text),
                                'source_url': transcript.source_url}
                    break
            assert verified, 'No selected RSS fixture has a verified Podwise transcript yet'
            print(json.dumps({'podwise_verified': verified, 'passed': True,
                              'production_db_writes': False, 'feishu_sends': 0,
                              'youtube_calls': 0}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
