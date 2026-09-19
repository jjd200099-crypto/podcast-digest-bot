"""Trusted transport context, distinct from untrusted source/document content.

Only a message actually delivered into this chat can supply quoted context.
Never resolve a globally known message ID without checking its audience first.
"""

import json


def initialize_context(store):
    with store._connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS research_run_state (
            session TEXT NOT NULL, message_id TEXT NOT NULL,
            task_json TEXT NOT NULL, audit_json TEXT NOT NULL,
            PRIMARY KEY(session, message_id))""")


def previous_task(store, session):
    with store._connect() as db:
        row = db.execute("""SELECT s.task_json,s.audit_json FROM research_run_state s
            JOIN research_turns t ON t.session=s.session AND t.message_id=s.message_id
            WHERE s.session=? ORDER BY t.id DESC LIMIT 1""", (session,)).fetchone()
    task = json.loads(row[0]) if row else None
    if task:
        task["outcome"] = json.loads(row[1]).get("outcome", "pending")
    return task


def quoted_context(store, message):
    if not message.parent_message_id:
        return None
    with store._connect() as db:
        row = db.execute("""SELECT o.*, j.payload_json FROM outbox o
            JOIN jobs j ON j.job_key=o.job_key
            WHERE o.remote_message_id=? AND o.status='sent' ORDER BY o.id DESC LIMIT 1""",
                         (message.parent_message_id,)).fetchone()
        if row is None:
            return None
        original = json.loads(row["payload_json"])
        if row["operation"] == "send":
            allowed = (
                row["target_type"] == "chat_id" and row["target_id"] == message.chat_id
            ) or (
                message.chat_type == "p2p" and row["target_type"] == "open_id"
                and row["target_id"] == message.sender_open_id
            )
        else:
            allowed = original.get("chat_id") == message.chat_id
        if not allowed:
            return None
        context = {"message_id": message.parent_message_id}
        if row["group_key"].startswith("daily:bundle:"):
            bundle = store.get_job_result(row["job_key"], row["group_key"])
            if bundle:
                episodes = []
                for episode_id in bundle["episode_ids"]:
                    record = store.get_verified_transcript(episode_id)
                    if record:
                        episodes.append({"document_id": record.reference,
                                         "title": record.episode.title,
                                         "url": record.episode.url, "show": record.episode.show})
                context["episodes"] = episodes
                if len(episodes) == 1:
                    context["episode"] = episodes[0]
            return context
        if row["group_key"].startswith("episode:"):
            episode_id = row["group_key"][len("episode:"):]
            record = store.get_verified_transcript(episode_id)
            if record:
                context["episode"] = {
                    "document_id": record.reference, "title": record.episode.title,
                    "url": record.episode.url, "show": record.episode.show,
                }
            return context
        # The ONE quoted bot answer is visible in this chat. Never copy the
        # original speaker's other turns, proposals or private session state.
        if row["job_key"].startswith("message:"):
            turn = db.execute("""SELECT t.question,t.answer,s.task_json
                FROM research_turns t LEFT JOIN research_run_state s
                ON s.session=t.session AND s.message_id=t.message_id
                WHERE t.message_id=? LIMIT 1""",
                              (original.get("message_id", ""),)).fetchone()
            if turn:
                context.update(question=turn["question"], answer=turn["answer"][:12000])
                if turn["task_json"]:
                    context["task"] = json.loads(turn["task_json"])
        return context
