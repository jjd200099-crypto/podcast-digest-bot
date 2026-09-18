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


def main():
    logging.basicConfig(level=logging.WARNING)
    settings = Settings.from_env()
    proof_dir = Path(tempfile.mkdtemp(prefix="agent-replay-proof-"))
    print(json.dumps({"proof_dir": str(proof_dir)}), flush=True)
    with tempfile.TemporaryDirectory(prefix="agent-continuity-") as directory:
        root = Path(directory)
        cloned = root / "state.sqlite3"
        with sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True) as src, sqlite3.connect(cloned) as dst:
            src.backup(dst)
        memory = root / "memory"
        shutil.copytree(settings.podcast_memory_path, memory)
        runtime = build_runtime(replace(settings, db_path=cloned, podcast_memory_path=memory))
        runtime.store.initialize()
        agent = runtime.research_agent
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
        for label, sender, question, parent in cases:
            if len(sys.argv) > 1 and label not in sys.argv[1:]:
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
            assert audit["outcome"] == "completed", audit
            assert "http" in answer
            assert "已完成的订阅变更" not in answer
            if label != "brief_followup":
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
