"""Opt-in real-provider comparison on empty temporary state; no Feishu sends."""

import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from news_officer.__main__ import build_runtime
from news_officer.config import Settings
from news_officer.models import IncomingMessage
from news_officer.response_quality import completeness_error


def main():
    settings = Settings.from_env()
    assert settings.deepseek_api_key, 'DeepSeek credential missing'
    assert settings.research_group_chat_ids, 'No configured research group'
    for enabled in (False, True):
        with tempfile.TemporaryDirectory(prefix='expression-smoke-') as directory:
            root = Path(directory)
            feeds = root / 'feeds.json'
            feeds.write_text('[]')
            runtime = build_runtime(replace(settings, db_path=root / 'state.sqlite3',
                podcast_memory_path=root / 'memory', feeds_path=feeds,
                tone_advisor_enabled=enabled))
            runtime.store.initialize()
            agent = runtime.research_agent
            agent.initialize()
            captured = []
            original = agent.tone_advisor.advise

            async def capture(question, history, original=original, captured=captured):
                started = time.monotonic()
                advice = await original(question, history)
                captured.append({'advice': advice, 'seconds': round(time.monotonic() - started, 2)})
                return advice

            agent.tone_advisor.advise = capture
            questions = [
                '用一个日常例子解释，Agent 和普通聊天机器人有什么区别？',
                '还是有点绕，讲人话，两三句话就好。',
                '那如果给团队用，应该怎么测它靠不靠谱？展开说，给出至少六个具体测试场景。不要查播客，也不要登记新需求。',
            ]
            for index, question in enumerate(questions):
                message = IncomingMessage(f'expression-smoke-{enabled}-{index}',
                    settings.research_group_chat_ids[0], question, 'group', 'expression-smoke')
                started = time.monotonic()
                reply = agent.handle(question, message)
                answer = reply.messages[0]
                assert completeness_error(answer) is None
                assert not reply.attachments
                assert captured and (not enabled or captured[-1]['advice'] is not None), 'Advisor fell back'
                print(json.dumps({'advisor_enabled': enabled, 'case': index,
                    'seconds': round(time.monotonic() - started, 2),
                    'advisor': captured[-1], 'answer': answer}, ensure_ascii=False), flush=True)
    print(json.dumps({'passed': True, 'production_db_writes': False, 'feishu_sends': 0}), flush=True)


if __name__ == '__main__':
    main()
