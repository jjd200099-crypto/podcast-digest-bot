"""Live research replay on a disposable SQLite backup, with no Feishu transport.

PYTHONPATH=src python scripts/replay_qa.py --db state.db --question '问题'
Only read tools are exposed; credentials come from OPENAI_API_KEY / OPENAI_MODEL.
"""
import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from news_officer.agent_runtime import run_research
from news_officer.models import IncomingMessage
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import (
    INSTRUCTIONS,
    TOOLS,
    PodcastResearchAgent,
    ResearchTools,
)
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store

READ_TOOLS = {'list_documents', 'search_library', 'read_document', 'set_research_task'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--question', required=True)
    parser.add_argument('--model', default=os.environ.get('OPENAI_MODEL', 'gpt-5.4'))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='qa-replay-') as directory:
        copied = Path(directory) / 'replay.sqlite3'
        # SQLite backup includes a consistent snapshot of committed WAL contents.
        uri = args.db.resolve().as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as source, sqlite3.connect(copied) as target:
            source.backup(target)
        store = Store(copied)
        store.initialize()
        library = PodcastArchive(store)
        owner = PodcastResearchAgent(store, SourceRegistry(store, Path('feeds.json')),
                                     library, os.environ['OPENAI_API_KEY'], args.model)
        owner.initialize()
        message = IncomingMessage('replay', 'replay', args.question, 'p2p', 'replay')
        state = ResearchTools(owner, 'replay', message)
        # No messenger or podcast service is constructed; mutations are unavailable.
        owner.registry = SimpleNamespace()
        trace = []
        execute = state.execute

        def traced_execute(name, arguments):
            result = execute(name, arguments)
            trace.append({'tool': name, 'arguments': arguments,
                          'result_chars': len(json.dumps(result, ensure_ascii=False))})
            return result

        state.execute = traced_execute
        answer = asyncio.run(run_research(
            owner, state, [{'role': 'user', 'content': args.question}], INSTRUCTIONS,
            [tool for tool in TOOLS if tool['name'] in READ_TOOLS]))
        print(json.dumps({'answer': answer, 'tools': trace, 'steps': state.steps, 'audit': state.audit()},
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
