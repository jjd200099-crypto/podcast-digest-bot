"""Session-scoped diagnostics; never expose raw provider errors or other users."""

import json

from .response_quality import completeness_error


def request_status(store, session: str) -> dict:
    with store._connect() as db:
        rows = db.execute("""SELECT j.job_key,j.status,j.attempts,j.created_at,j.updated_at,j.payload_json,
            t.question,t.answer,s.audit_json FROM jobs j
            LEFT JOIN research_turns t ON j.job_key='message:' || t.message_id AND t.session=?
            LEFT JOIN research_run_state s ON s.message_id=t.message_id AND s.session=t.session
            WHERE j.kind='message' AND j.session_key=? ORDER BY j.created_at DESC LIMIT 8""",
                          (session, session)).fetchall()
        records = []
        for row in rows:
            audit = json.loads(row['audit_json']) if row['audit_json'] else {}
            delivery = [dict(r) for r in db.execute("""SELECT status,count(*) AS parts
                FROM outbox WHERE job_key=? GROUP BY status""", (row['job_key'],))]
            records.append({
                'question': row['question'] or str(json.loads(row['payload_json']).get('text', ''))[:1000],
                'answer': row['answer'][:4000] if row['answer'] else None,
                'answer_preview_truncated': bool(row['answer'] and len(row['answer']) > 4000),
                'processing_status': row['status'], 'attempts': row['attempts'],
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'delivery': delivery, 'research_outcome': audit.get('outcome', 'not_recorded'),
                'audit_recorded': bool(row['audit_json']),
                'validation_errors': audit.get('validation_errors'),
                'completeness_issue': completeness_error(row['answer']) if row['answer'] else None,
            })
    return {'scope': 'Only this conversation and sender', 'requests': records,
            'limits': 'Delivery success is not task success. No audit or delivery records means unknown, not no errors. An incomplete stored answer proves the defect existed before Feishu display; it cannot be only client display truncation. Missing historical raw model output cannot prove why generation stopped. This tool cannot inspect deployments, code, credentials or other conversations, and does not repair code.'}
