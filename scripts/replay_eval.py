"""Read-only production Q&A regression replay; no messaging transports."""
import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import tempfile
from pathlib import Path
from types import SimpleNamespace

from news_officer.agent_runtime import dialogue_input, run_research
from news_officer.models import IncomingMessage
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import (
    INSTRUCTIONS,
    TOOLS,
    PodcastResearchAgent,
    ResearchTools,
    conversation_goal,
)
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store

READ_TOOLS = {'list_documents', 'search_library', 'read_document', 'set_research_task'}


def passes(answer, case):
    text = answer.casefold()
    return (all(any(term.casefold() in text for term in group) for group in case['must_include'])
            and not any(term.casefold() in text for term in case['must_not_include']))


def percentile(values, quantile):
    values = sorted(values)
    index = (len(values) - 1) * quantile
    lo = int(index)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)


async def evaluate(args, store):
    cases = json.loads(args.cases.read_text())
    semaphore = asyncio.Semaphore(args.concurrency)

    async def replay(case, run):
        async with semaphore:
            owner = PodcastResearchAgent(store, SourceRegistry(store, Path('feeds.json')),
                                         PodcastArchive(store), os.environ['OPENAI_API_KEY'], args.model)
            owner.initialize()
            owner.registry = SimpleNamespace()
            history, turns = [], []
            for index, text in enumerate(case['messages']):
                message = IncomingMessage(f'eval-{case["id"]}-{run}-{index}', 'eval', text, 'p2p', 'eval')
                state = ResearchTools(owner, 'eval', message)
                state.conversation_goal = conversation_goal(history, text)
                answer = await run_research(owner, state, dialogue_input(history, text), INSTRUCTIONS,
                                            [t for t in TOOLS if t['name'] in READ_TOOLS])
                turns.append({'question': text, 'answer': answer, **state.engine_metrics})
                history.append({'question': text, 'answer': answer})
            return {'case': case['id'], 'run': run, 'passed': passes(answer, case), 'turns': turns}

    results = await asyncio.gather(*(replay(case, run) for case in cases for run in range(args.runs)))
    summaries = []
    for case in cases:
        rows = [r for r in results if r['case'] == case['id']]
        turns = [t for r in rows for t in r['turns']]
        latencies = [t['elapsed_seconds'] for t in turns]
        summaries.append({'case': case['id'], 'pass_rate': statistics.mean(r['passed'] for r in rows),
                          'mean_latency': statistics.mean(latencies), 'p50_latency': percentile(latencies, .5),
                          'p90_latency': percentile(latencies, .9),
                          **{'mean_' + key: statistics.mean(t[key] for t in turns) for key in
                             ('input_tokens', 'cached_input_tokens', 'output_tokens', 'total_model_calls')},
                          'failing_answers': [r for r in rows if not r['passed']]})
    return {'cases': summaries, 'questions': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--model', default=os.environ.get('OPENAI_MODEL', 'gpt-5.4'))
    parser.add_argument('--cases', type=Path, default=Path(__file__).resolve().parents[1] / 'tests/fixtures/qa_regression_cases.json')
    args = parser.parse_args()
    if args.runs < 1 or args.concurrency < 1:
        parser.error('runs and concurrency must be positive')
    with tempfile.TemporaryDirectory(prefix='qa-eval-') as directory:
        copied = Path(directory) / 'eval.sqlite3'
        with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(copied) as target:
            source.backup(target)
        store = Store(copied)
        store.initialize()
        print(json.dumps(asyncio.run(evaluate(args, store)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
