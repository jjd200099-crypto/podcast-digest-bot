"""Real-model worker simulation with a FAKE outbound transport and empty test DB.

Exercises actual inbound callback, concurrent workers, ordered follow-ups,
multipart delivery and an ambiguous send failure. Never connects to Feishu or
uses the production DB. Not proof of the real Feishu event subscription itself.
"""

import asyncio
import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from news_officer.__main__ import build_runtime
from news_officer.config import Settings


class TestTransport:
    def __init__(self):
        self.delivered = {}
        self.attempts = []
        self.injected = False

    def deliver(self, item):
        self.attempts.append(item.uuid)
        self.delivered.setdefault(item.uuid, item)
        if item.target_id == 'runtime-slow' and item.group_key.startswith('message:result') and not self.injected:
            self.injected = True
            raise ConnectionError('simulated lost response after accepted send')
        return 'simulation-' + item.uuid


async def main():
    settings = Settings.from_env()
    proof = Path(tempfile.mkdtemp(prefix='agent-runtime-proof-'))
    print(json.dumps({'proof_dir': str(proof)}), flush=True)
    with tempfile.TemporaryDirectory(prefix='agent-runtime-test-') as temp:
        root = Path(temp)
        feeds = root / 'feeds.json'
        shutil.copy2(settings.feeds_path, feeds)
        instance = build_runtime(replace(settings, db_path=root / 'state.db', podcast_memory_path=root / 'memory',
            feeds_path=feeds, knowledge_mode='podcast_archive'))
        instance.store.initialize()
        instance.research_agent.initialize()
        instance.messenger = transport = TestTransport()
        instance._main_loop = asyncio.get_running_loop()
        chat = settings.research_group_chat_ids[0]
        cases = [
            ('runtime-slow', 'alice', '请设计团队研究助理的验收方案，列出至少十二种场景，每种说明操作步骤和通过标准。总计至少1200中文字。直接给完整方案，不是播客研究，不需要查资料。'),
            ('runtime-followup', 'alice', '把你刚才写的方案压缩成100字以内的执行建议。'),
            ('runtime-fast', 'bob', '你好，用一句话告诉我你能帮忙做什么。'),
        ]
        for identifier, sender, question in cases:
            await instance._on_message(SimpleNamespace(message_id=identifier, chat_id=chat, body_text=question,
                chat_type='group', sender_id=sender, raw={}))
        workers = [asyncio.create_task(instance._worker('message')) for _ in range(4)]
        try:
            deadline = asyncio.get_running_loop().time() + 240
            while True:
                with instance.store._connect() as db:
                    jobs = [dict(r) for r in db.execute('SELECT job_key,status,attempts,created_at,updated_at FROM jobs ORDER BY created_at')]
                if all(j['status'] in {'completed', 'failed'} for j in jobs):
                    break
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError('Runtime replay exceeded 240 seconds')
                await asyncio.sleep(0.5)
            assert all(j['status'] == 'completed' for j in jobs), jobs
            by_key = {j['job_key']: j for j in jobs}
            assert by_key['message:runtime-fast']['updated_at'] < by_key['message:runtime-slow']['updated_at'], jobs
            assert by_key['message:runtime-slow']['updated_at'] < by_key['message:runtime-followup']['updated_at'], jobs
            with instance.store._connect() as db:
                turns = [dict(r) for r in db.execute('SELECT message_id,question,answer FROM research_turns ORDER BY id')]
                deliveries = [dict(r) for r in db.execute('SELECT job_key,group_key,part,total_parts,status,uuid FROM outbox ORDER BY id')]
            answers = {r['message_id']: r['answer'] for r in turns}
            assert len(answers['runtime-slow']) >= 1200
            assert len(answers['runtime-followup']) < 350
            assert transport.injected
            assert len(turns) == 3, 'Retry repeated model execution/answer commit'
            assert all(d['status'] == 'sent' for d in deliveries)
            assert len(transport.delivered) == len(deliveries), 'Transport UUIDs were not reused'
            result = {'passed': True, 'jobs': jobs, 'turns': turns, 'deliveries': deliveries,
                      'send_attempts': len(transport.attempts), 'unique_deliveries': len(transport.delivered),
                      'feishu_sends': 0, 'production_writes': False}
            (proof / 'result.json').write_text(json.dumps(result, ensure_ascii=False))
            print(json.dumps({k: v for k, v in result.items() if k not in {'turns', 'deliveries'}}, ensure_ascii=False), flush=True)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
