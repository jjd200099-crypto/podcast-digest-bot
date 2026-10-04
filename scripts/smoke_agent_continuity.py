"""Opt-in real-model replay on a cloned production DB/files. NO Feishu sends.

Checks the reported quoted-summary request and clarification -> guest-name
follow-up, plus a subsequent brief question. Credentials remain on the host.
"""

import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from news_officer.__main__ import build_runtime
from news_officer.config import Settings
from news_officer.models import IncomingMessage
from news_officer.qa import chunk_transcript
from news_officer.response_quality import completeness_error


def main():
    logging.basicConfig(level=logging.WARNING)
    settings = Settings.from_env()
    proof_dir = Path(tempfile.mkdtemp(prefix="agent-replay-proof-"))
    print(json.dumps({"proof_dir": str(proof_dir), "backend": settings.agent_backend,
                      "model": settings.openai_model}), flush=True)
    with tempfile.TemporaryDirectory(prefix="agent-continuity-") as directory:
        root = Path(directory)
        cloned = root / "state.sqlite3"
        with sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True) as src, sqlite3.connect(cloned) as dst:
            src.backup(dst)
        memory = root / "memory"
        shutil.copytree(settings.podcast_memory_path, memory)
        feeds = root / "feeds.json"
        shutil.copy2(settings.feeds_path, feeds)
        runtime = build_runtime(replace(settings, db_path=cloned, podcast_memory_path=memory, feeds_path=feeds))
        runtime.store.initialize()
        agent = runtime.research_agent
        agent.pilot_progress = lambda event: print(json.dumps({"progress": event}), flush=True)
        agent.initialize()
        chat = settings.research_group_chat_ids[0]
        with runtime.store._connect() as db:
            record = db.execute("SELECT episode_id,reference,transcript_text,source_url FROM episode_transcripts WHERE lower(title) LIKE '%noam brown%' LIMIT 1").fetchone()
            assert record, "No verified Noam Brown transcript"
            quote = db.execute("SELECT remote_message_id FROM outbox WHERE group_key=? AND target_id=? AND status='sent' AND remote_message_id!='' LIMIT 1",
                               ("episode:" + record["episode_id"], chat)).fetchone()
            assert quote, "No delivered Noam Brown summary to quote"
            # Reproduce the original unresolved two-turn request in this clone.
            db.execute("INSERT INTO research_turns(session,message_id,question,answer) VALUES (?,?,?,?)",
                       (f"group:{chat}:smoke-clarification", "old-clarification",
                        "这篇很好，能不能为我做更完整详细的版本", "可以，你是想把哪一期扩展为详细版？"))
        # RSS/YouTube may have different IDs/titles for the same official
        # transcript. Verify canonical source identity, not the first DB row ID.
        with runtime.store._connect() as db:
            equivalents = {r["reference"]: len(chunk_transcript(r["transcript_text"]))
                           for r in db.execute("SELECT reference,transcript_text FROM episode_transcripts WHERE source_url=?", (record["source_url"],))}
        cases = [
            ("quoted_detail", "smoke-quote", "这篇很好，能不能为我做更完整详细的版本", quote[0]),
            ("guest_followup", "smoke-clarification", "noam brown", ""),
            ("brief_followup", "smoke-clarification", "他对 Agent 群体协作的关键限制是什么？用三句话说清楚。", ""),
        ]
        conversations = '--conversations' in sys.argv
        if conversations:
            cases = [
                ('greeting', 'smoke-chat', '你好啊', ''),
                ('capabilities', 'smoke-chat', '你有什么功能？请具体说清楚，不要只给开头。', ''),
                ('general_question', 'smoke-chat', '用一个例子解释 Agent 和普通聊天机器人的区别。', ''),
                ('rewrite', 'smoke-chat', '帮我把这句话写通顺：这个东西他的回答不完整然后希望帮我们改进。只给改写结果。', ''),
                ('long_conversation', 'smoke-chat', '请帮我设计团队使用播客研究机器人的验收方案，按场景、操作、预期结果写，至少八个场景。直接给完整方案，不是播客观点，不需要查文字稿。', ''),
                ('self_diagnosis', 'smoke-diagnosis', '你也太笨了，自己检查一下错误，为什么你说话会截断？', ''),
                ('feature_request', 'smoke-features', '我希望新增每周对比不同嘉宾观点的周报功能，请登记这个功能需求，先不要修改任何代码或推送设置。', ''),
                ('feature_followup', 'smoke-features', '所以这个功能现在已经上线了吗？', ''),
                ('colleague', 'smoke-colleague', '你好，我是同事，请告诉我可以怎么向你提问。', ''),
            ]
            historical = IncomingMessage('smoke-broken-answer', chat, '检查错误', 'group', 'smoke-diagnosis')
            runtime.store.enqueue('message:' + historical.message_id, 'message', historical.__dict__)
            with runtime.store._connect() as db:
                db.execute('INSERT INTO research_turns(session,message_id,question,answer) VALUES (?,?,?,?)',
                           (historical.conversation_key, historical.message_id, historical.text, '你说得对。回看这段对话，主要错误有：'))
            runtime.store.complete('message:' + historical.message_id)
        extended = "--extended" in sys.argv
        if extended:
            with runtime.store._connect() as db:
                other = db.execute("SELECT reference FROM episode_transcripts WHERE source_url != ? LIMIT 1", (record["source_url"],)).fetchone()
            assert other, "Need two distinct transcripts for comparison"
            cases += [
                ("cross_episode", "smoke-comparison",
                 f"比较文档 {record['reference']} 与 {other['reference']}：各提一条最核心且有原文依据的观点，再指出两期讨论重点的不同。简短回答。", ""),
                ("source_proposal", "smoke-source", "请新增追踪 Practical AI 播客，RSS 是 https://changelog.com/practicalai/feed。先给我确认。", ""),
                ("source_confirmation", "smoke-source", "确认添加", ""),
            ]
        for label, sender, question, parent in cases:
            selected = [a for a in sys.argv[1:] if not a.startswith("--")]
            if selected and label not in selected:
                continue
            print(json.dumps({"started": label}, ensure_ascii=False), flush=True)
            msg = IncomingMessage("continuity-" + label, chat, question, "group", sender,
                                  parent_message_id=parent)
            reply = agent.handle(question, msg)
            answer = reply.messages[0]
            with runtime.store._connect() as db:
                row = db.execute("SELECT task_json,audit_json FROM research_run_state WHERE message_id=?",
                                 (msg.message_id,)).fetchone()
                steps = [dict(r) for r in db.execute("SELECT tool,status FROM research_steps WHERE message_id=? ORDER BY step", (msg.message_id,))]
            task, audit = json.loads(row[0]), json.loads(row[1])
            proof = {"case": label, "answer": answer, "task": task,
                     "audit": audit, "steps": steps, "chars": len(answer)}
            (proof_dir / (label + ".json")).write_text(json.dumps(proof, ensure_ascii=False))
            print(json.dumps({k: v for k, v in proof.items() if k != "answer"}, ensure_ascii=False), flush=True)
            assert not reply.attachments
            assert completeness_error(answer) is None
            if conversations:
                assert audit['outcome'] not in {'budget_exhausted', 'validation_failed'}
                if label == 'self_diagnosis':
                    assert any(s['tool'] == 'get_request_status' for s in steps)
                    assert '主要错误有' in answer or '开头' in answer or '半句' in answer or '冒号' in answer
                if label == 'feature_request':
                    assert any(s['tool'] == 'record_feature_request' for s in steps)
                if label == 'long_conversation':
                    assert len(answer) > 600
                if label == 'general_question':
                    assert len(answer) > 60
                continue
            if label.startswith("source_"):
                with runtime.store._connect() as db:
                    proposal = db.execute("SELECT status FROM source_proposals WHERE session=? ORDER BY rowid DESC LIMIT 1", (f"group:{chat}:smoke-source",)).fetchone()
                assert proposal, "No source proposal created"
                assert proposal[0] == ("pending" if label == "source_proposal" else "applied"), proposal[0]
                continue
            assert audit["outcome"] == "completed", audit
            assert "http" in answer
            assert "已完成的订阅变更" not in answer
            if label == "cross_episode":
                assert len(task["document_ids"]) >= 2, "Comparison did not select both documents"
            elif label != "brief_followup":
                assert task["format"] == "detailed"
                assert task["document_ids"] and set(task["document_ids"]) <= equivalents.keys()
                assert all(audit["coverage"].get(t) == equivalents[t] for t in task["document_ids"])
                assert len(answer) >= 1600, "Detailed request produced a short reply"
                assert "##" in answer, "No thematic structure"
            else:
                assert len(answer) < 1600, "Current short-answer request did not override old detail task"
        print(json.dumps({"passed": True, "production_writes": False, "feishu_sends": 0}), flush=True)


if __name__ == "__main__":
    main()
