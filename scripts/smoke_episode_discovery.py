"""Live ambiguous-episode acceptance test; isolated DB/files, no Feishu sends.

Uses existing environment credentials without printing them. Podwise processing
credits are disabled: an unavailable full transcript is a failed acceptance,
not permission to invent a summary. Does not change production subscriptions.
"""

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from news_officer.__main__ import build_runtime
from news_officer.config import Settings
from news_officer.feishu import FeishuMessenger
from news_officer.models import IncomingMessage


def main():
    settings = Settings.from_env()
    with tempfile.TemporaryDirectory(prefix='episode-discovery-smoke-') as directory:
        root = Path(directory)
        settings = replace(settings, db_path=root / 'state.db',
                           podcast_memory_path=root / 'memory',
                           feeds_path=Path(__file__).resolve().parents[1] / 'feeds.json',
                           research_group_chat_ids=('isolated-test',),
                           podwise_auto_process=False)
        runtime = build_runtime(settings)
        runtime.store.initialize()
        agent = runtime.research_agent
        agent.initialize()
        message = IncomingMessage('town-discovery-smoke', 'isolated-test',
                                  '最近听说 Town 的 founder CEO 的播客不错，给我拉出来重点总结一下',
                                  'group', 'isolated-colleague')
        print('START: actual RSS + production model + full-text providers; isolated colleague group', flush=True)
        with patch.object(FeishuMessenger, 'deliver', side_effect=AssertionError('No external sends')), \
                patch.object(FeishuMessenger, 'send', side_effect=AssertionError('No external sends')), \
                patch.object(FeishuMessenger, 'reply', side_effect=AssertionError('No external sends')):
            answer = agent.handle(message.text, message)
        with runtime.store._connect() as db:
            steps = [dict(r) for r in db.execute('SELECT tool,status FROM research_steps ORDER BY step')]
        print(json.dumps({'steps': steps, 'answer': answer.messages,
                          'attachments': len(answer.attachments)}, ensure_ascii=False), flush=True)
        names = {s['tool'] for s in steps if s['status'] == 'ok'}
        assert {'search_episodes', 'analyze_podcast', 'read_document'} <= names
        assert 'Town' in '\n'.join(answer.messages)
        records = runtime.store.list_recent_transcripts(10)
        assert any('Founder of Town' in r.episode.title for r in records)
        for record in records:
            print(json.dumps({'verified_title': record.episode.title, 'source': record.transcript.source,
                              'source_url': record.transcript.source_url,
                              'chars': len(record.transcript.text)}, ensure_ascii=False), flush=True)
        assert not answer.attachments
        print('PASS: located company CEO interview and read full-text evidence; zero Feishu sends', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact provider exceptions containing credentials
        print('FAIL TYPE:', type(error).__name__, flush=True)
        raise SystemExit(1) from None
