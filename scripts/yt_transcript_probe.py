"""Read-only probe: python scripts/yt_transcript_probe.py VIDEO_ID."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from news_officer.models import Episode
from news_officer.youtube import (
    YOUTUBE_VIDEO_ID_RE,
    YouTubeTranscriptProvider,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video_id')
    args = parser.parse_args()
    if not YOUTUBE_VIDEO_ID_RE.fullmatch(args.video_id):
        parser.error('Expected an 11-character YouTube video ID')
    episode = Episode(args.video_id, 'Transcript probe',
                      f'https://www.youtube.com/watch?v={args.video_id}', 'YouTube')
    try:
        transcript = YouTubeTranscriptProvider().fetch(episode)
        if transcript is None:
            print('error: no complete transcript available')
            return 1
        print(f'transcript length: {len(transcript.text)}')
        return 0
    except Exception as error:  # noqa: BLE001 - transcript errors are logged and skipped
        print(f'error: {type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
