"""Live, read-only discovery acceptance: no follows, paid processing or Feishu sends."""

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from news_officer.editorial import EditorialPolicy
from news_officer.podcast import PodcastService
from news_officer.podwise_discovery import PodwiseDiscovery
from news_officer.store import Store
from news_officer.summarizer import TranscriptSummarizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--assess-limit', type=int, default=0)
    args = parser.parse_args()
    token = os.environ.get('PODWISE_API_TOKEN', '')
    discovery = PodwiseDiscovery(token, pages=1, popular_limit=20, catalog_limit=30)
    print('Starting read-only Podwise discovery; no messages, follows or processing requests.', flush=True)
    result = discovery.discover(datetime.now(UTC), args.hours)
    print(json.dumps({'notice': result.notice, 'candidates': len(result.episodes),
                      'successful_requests': result.successful_requests, 'failed_requests': result.failed_requests,
                      'samples': [{'title': e.title, 'show': e.show, 'url': e.url,
                                   'published_at': e.published_at.isoformat()}
                                  for e in result.episodes[:8]]}, ensure_ascii=False), flush=True)
    if not result.successful_requests:
        raise RuntimeError('No successful Podwise reads')
    if not args.assess_limit:
        return
    with tempfile.TemporaryDirectory(prefix='podwise-discovery-smoke-') as directory:
        store = Store(Path(directory) / 'state.db')
        store.initialize()
        summarizer = TranscriptSummarizer(os.environ['OPENAI_API_KEY'], os.environ.get('OPENAI_MODEL', 'gpt-5.6-terra'))
        policy = EditorialPolicy(summarizer.client, os.environ.get('OPENAI_MODEL', 'gpt-5.6-terra'), store)
        service = PodcastService(store, Path(__file__).resolve().parents[1] / 'feeds.json', summarizer,
                                 podwise_api_token=token, daily_rss_only=True, editorial_policy=policy)
        count = 0
        for episode in result.episodes[:10]:
            transcript = service.daily_transcript_resolver.fetch(episode)
            if not transcript:
                print(json.dumps({'title': episode.title, 'status': 'fulltext_unavailable'}, ensure_ascii=False), flush=True)
                continue
            decision = policy.assess(episode, transcript)
            print(json.dumps({'title': episode.title, 'show': episode.show, 'verified_chars': len(transcript.text),
                              'selected': decision['selected'], 'priority': decision['assessment']['relevance']['priority'],
                              'internal_rating': decision['stars'], 'reason': decision['assessment']['reason']},
                             ensure_ascii=False), flush=True)
            count += 1
            if count >= args.assess_limit:
                break
        print(json.dumps({'assessed': count, 'feishu_sends': 0, 'paid_processing': 0}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact credentials and provider payloads
        print('Discovery smoke failed:', type(error).__name__, flush=True)
        raise SystemExit(1) from None
