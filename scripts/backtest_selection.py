"""Read-only historical selection audit; provider calls require explicit --rescore."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from news_officer.editorial import Assessment, EditorialPolicy, decide
from news_officer.models import Episode, Transcript


class NoCache:
    """Do not write reviews or reuse historical model judgements."""

    def get_editorial_review(self, *_):
        return None

    def save_editorial_review(self, *_):
        pass


def historical_rows(db, start, end):
    """Use archived daily items plus reviews made during that daily's execution.

    Capped/filtered episodes may have reviews but no daily_item. Reviews are
    assigned by their timestamp to the daily job's creation/update interval.
    This is historical reconstruction, not a replay of missing discovery data.
    """
    for job in db.execute("SELECT * FROM jobs WHERE kind='daily' ORDER BY created_at,job_key"):
        payload = json.loads(job['payload_json'])
        if payload.get('prepare_only'):
            continue
        scheduled = datetime.fromisoformat(payload.get('scheduled_for') or job['created_at'])
        day = scheduled.astimezone(ZoneInfo('Asia/Shanghai')).date()
        if not start <= day <= end:
            continue
        episodes = {}
        for row in db.execute("SELECT payload_json FROM job_results WHERE job_key=? AND kind='daily_item' ORDER BY result_key", (job['job_key'],)):
            item = json.loads(row[0])
            episode = Episode.from_persisted_dict(item['episode'])
            episodes[episode.id] = episode
        for row in db.execute('SELECT DISTINCT t.* FROM editorial_reviews r JOIN episode_transcripts t USING(episode_id) WHERE r.created_at>=? AND r.created_at<=? ORDER BY r.created_at,r.episode_id', (job['created_at'], job['updated_at'])):
            episodes.setdefault(row['episode_id'], Episode(row['episode_id'], row['title'], row['episode_url'], row['show_name']))
        for episode in episodes.values():
            review = db.execute('SELECT decision_json FROM editorial_reviews WHERE episode_id=? AND created_at<=? ORDER BY created_at DESC,cache_key LIMIT 1', (episode.id, job['updated_at'])).fetchone()
            transcript = db.execute('SELECT transcript_text,source,source_url FROM episode_transcripts WHERE episode_id=?', (episode.id,)).fetchone()
            yield day.isoformat(), episode, json.loads(review[0]) if review else None, transcript


def print_backtest(db, start, end, policies=None):
    print('date\tepisode\told stars\trelevance\tdensity\tpriority\twithout floor\tfloor raised\told rescore\tnew stars\tin/out\treason')
    for day, episode, stored, transcript in historical_rows(db, start, end):
        value = (stored or {}).get('assessment', {})
        relevance, density = value.get('relevance', {}), value.get('density', {})
        without_floor = '?'
        if stored and transcript:
            raw = {key: value[key] for key in ('relevance', 'density', 'reason')}
            raw['relevance'] = {**raw['relevance'], 'priority': 'none'}
            try:
                without_floor = decide(Assessment.model_validate(raw), transcript['transcript_text'])['stars']
            except ValueError:
                pass
        old, new = None, None
        reason = value.get('reason', 'No stored review')
        if policies and transcript:
            full = Transcript(transcript['transcript_text'], transcript['source'], transcript['source_url'], True)
            try:
                old = policies[0].assess(episode, full)
                new = policies[1].assess(episode, full)
                reason = new['assessment']['reason']
            except ValueError as error:
                reason = str(error)
        stars = (stored or {}).get('stars', '?')
        selected = (new or stored or {}).get('selected')
        fields = [day, episode.title, stars, relevance.get('score', '?'), density.get('score', '?'), relevance.get('priority', '?'), without_floor,
                  stars > without_floor if isinstance(stars, int) and isinstance(without_floor, int) else '?',
                  (old or {}).get('stars', '-'), (new or {}).get('stars', '-'),
                  'in' if selected else 'out' if selected is False else '?', reason]
        print('\t'.join(str(field).replace('\t', ' ').replace('\n', ' ') for field in fields))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('start', type=date.fromisoformat)
    parser.add_argument('end', type=date.fromisoformat)
    parser.add_argument('--rescore', action='store_true', help='Send archived transcripts to the configured LLM (paid network calls)')
    parser.add_argument('--model', default=os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini'))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error('start must not be after end')
    policies = None
    if args.rescore:
        from openai import OpenAI
        client = OpenAI()
        policies = [EditorialPolicy(client, args.model, NoCache(), strict=strict) for strict in (False, True)]
    with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        print_backtest(db, args.start, args.end, policies)


if __name__ == '__main__':
    main()
