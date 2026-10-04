"""Live model routing check in an isolated DB; never sends Feishu messages/docs.

Reads one verified public podcast from the configured archive, invokes the
production Agent with a colleague request, and checks the resulting document job.
Credentials stay inside the existing environment; only a compact receipt prints.
"""

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from news_officer.config import Settings
from news_officer.episode_document import SelectedEpisodeCompiler
from news_officer.models import Episode, IncomingMessage, Transcript
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import PodcastResearchAgent
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('episode_id')
    args = parser.parse_args()
    settings = Settings.from_env()
    with sqlite3.connect(f'file:{settings.db_path}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM episode_transcripts WHERE episode_id=?', (args.episode_id,)).fetchone()
    if row is None:
        raise ValueError('Verified archived episode required')
    episode = Episode(row['episode_id'], row['title'], row['episode_url'], row['show_name'],
                      row['duration_seconds'], published_at=datetime.fromisoformat(row['published_at']))
    transcript = Transcript(row['transcript_text'], row['source'], row['source_url'], True)
    with tempfile.TemporaryDirectory(prefix='document-agent-smoke-') as root:
        store = Store(Path(root) / 'test.sqlite3')
        store.initialize()
        record = store.save_verified_transcript(episode, transcript)
        agent = PodcastResearchAgent(store, SourceRegistry(store, settings.feeds_path),
                                     PodcastArchive(store, Path(root) / 'memory'),
                                     settings.openai_api_key, settings.openai_model,
                                     chats=('test-team',))
        agent.initialize()
        compiler = SelectedEpisodeCompiler(store, None, None, request_authorizer=agent.allowed)
        compiler.initialize()
        agent.document_compiler = compiler
        question = f'@情报官 帮我重点总结这期播客：{episode.title}（编号 {record.reference}）。'
        message = IncomingMessage('test-request', 'test-team', question, 'group', 'test-colleague')
        answer = agent.handle(question, message)
        with store._connect() as db:
            jobs = db.execute("SELECT payload_json FROM jobs WHERE kind='document'").fetchall()
            steps = [r[0] for r in db.execute('SELECT tool FROM research_steps ORDER BY step')]
        assert len(jobs) == 1, 'Agent must create exactly one document job'
        payload = json.loads(jobs[0][0])
        assert payload['episode_id'] == episode.id and payload['chat_id'] == message.chat_id
        assert payload['message_id'] == message.message_id
        print(json.dumps({'ok': True, 'model': settings.openai_model, 'tools': steps,
                          'document_jobs': len(jobs), 'reply_bound_to_request': True,
                          'acknowledgement': answer.messages,
                          'external_writes': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
