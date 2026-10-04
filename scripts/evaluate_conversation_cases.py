"""Opt-in LIVE model replay with synthetic transcripts and isolated state.

No Feishu connection, production DB, paid transcription or real document writes.
This measures conversational behavior, not factual accuracy of real podcasts.
"""

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from news_officer.models import Episode, IncomingMessage, Transcript
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import PodcastResearchAgent
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


def main():
    with tempfile.TemporaryDirectory(prefix='conversation-eval-') as directory:
        root = Path(directory)
        feeds = root / 'feeds.json'
        feeds.write_text('{"sources": []}')
        store = Store(root / 'state.sqlite3')
        store.initialize()
        episode = Episode('fixture:researcher', '合成测试：Noam Brown 多智能体访谈',
            'https://example.org/synthetic-evaluation', 'Synthetic evaluation, not a real episode',
            published_at=datetime.now(UTC))
        text = ('以下是合成验收材料，不是 Noam Brown 的真实发言，不可对外当作播客事实。\n'
            '主持人：为什么采用多智能体？\n测试嘉宾：为了并行处理独立子任务，减少串行等待。'
            '测试中十个工作者分别查询资料，然后一个汇总者合并结果。\n'
            '主持人：什么任务不合适？\n测试嘉宾：如果每一步都必须等待上一步结果，并行收益就很小。'
            '写小说要求统一叙事，合并十个不同版本可能更费事。\n'
            '主持人：有哪些限制？\n测试嘉宾：工作者会重复搜索，增加成本；错误可能在汇总中传播；'
            '统一评估很难，所以不能凭工作者数量断言系统优于人类团队。\n'
            '主持人：怎样改进？\n测试嘉宾：明确分工、保存来源、检查冲突；先测效果再增加规模。\n')
        store.save_verified_transcript(episode, Transcript(text, 'synthetic-test', episode.url, True))
        agent = PodcastResearchAgent(store, SourceRegistry(store, feeds), PodcastArchive(store),
            os.environ['OPENAI_API_KEY'], os.environ.get('OPENAI_MODEL', 'gpt-5.6-terra'),
            chats=('test-group',))
        agent.initialize()
        # Seed one previously delivered message without contacting Feishu.
        store.enqueue('daily:fixture', 'daily', {})
        store.ensure_outbox(job_key='daily:fixture', group_key='episode:' + episode.id, delivery_key='fixture',
            operation='send', target_id='test-group', target_type='chat_id', reply_in_thread=False,
            parts=[('text', '{"text":"synthetic summary"}', 'fixture')])
        with store._connect() as db:
            db.execute("UPDATE outbox SET status='sent',remote_message_id='quoted-fixture'")
        cases = [
            ('colleague_greeting', 'colleague-b', '你好，一句话告诉我你可以怎么帮我。', ''),
            ('quoted_reference', 'colleague-a', '这篇提到的并行有什么限制？请直接给三个要点，不要建文档。', 'quoted-fixture'),
            ('followup', 'colleague-a', '那为什么写小说不一定适合？一句话解释。', ''),
            ('preference', 'colleague-b', '以后我个人更喜欢简短的自然段，帮我记下这个偏好建议，先不要改全群规则。', ''),
            ('feature_request', 'colleague-b', '请登记每周比较不同嘉宾观点的周报需求，先不要实现。', ''),
            ('feature_status', 'colleague-b', '所以刚才那个周报功能已经上线了吗？', ''),
        ]
        results = []
        for name, sender, question, parent in cases:
            started = time.monotonic()
            message = IncomingMessage(name, 'test-group', question, 'group', sender, parent_message_id=parent)
            reply = agent.handle(question, message)
            answer = '\n'.join(reply.messages)
            with store._connect() as db:
                tools = [r[0] for r in db.execute('SELECT tool FROM research_steps WHERE message_id=?', (name,))]
            if name in {'quoted_reference', 'followup'}:
                passed = 'read_document' in tools and '并行' in answer and '请提供' not in answer
            elif name == 'preference':
                passed = 'record_editorial_feedback' in tools
            elif name == 'feature_request':
                passed = 'record_feature_request' in tools
            elif name == 'feature_status':
                passed = any(word in answer for word in ('没有上线', '尚未', '未上线', '还没', '待开发', '未实现'))
            else:
                passed = len(answer) >= 10 and '尚未获准' not in answer
            result = {'case': name, 'passed': passed, 'seconds': round(time.monotonic() - started),
                      'tools': tools, 'answer': answer}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        print(json.dumps({'passed': all(r['passed'] for r in results), 'cases': len(results),
                          'feishu_sends': 0, 'production_writes': 0, 'corpus': 'synthetic'}), flush=True)
        if not all(r['passed'] for r in results):
            raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 - no credential-bearing SDK errors
        print(json.dumps({'failed': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
