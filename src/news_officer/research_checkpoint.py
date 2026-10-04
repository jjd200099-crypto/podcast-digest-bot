"""Private, per-event SDK checkpoints on the existing persistent volume.

Only application state and SDK replay items are serialized, never owner objects,
clients, credentials, pickle, or instructions. Completion removes the checkpoint
in the same transaction that commits the answer. Tool writes remain idempotent.
"""

import json
from dataclasses import asdict, replace

from .library import LibraryDocument
from .models import Episode, TranscriptAttachment

FIELDS = (
    'warnings', 'tool_warnings', 'recent_queries', 'evidence', 'steps',
    'daily_reports', '_sequence', 'task', 'validation_errors',
)
SETS = ('catalog_evidence', 'body_evidence', 'discovered_episode_urls', 'ambiguous_episode_urls')


class ResearchContinuationPending(Exception):
    """A bounded segment stopped; the worker must resume, not publish a failure."""


def initialize_checkpoints(store):
    with store._connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS research_checkpoints (
            session TEXT NOT NULL, message_id TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(session,message_id))''')


def save_checkpoint(state, messages):
    values = {name: getattr(state, name) for name in FIELDS}
    values.update({name: list(getattr(state, name)) for name in SETS})
    values['read_coverage'] = {k: list(v) for k, v in state.read_coverage.items()}
    values['documents'] = None if state.documents is None else {k: asdict(v) for k, v in state.documents.items()}
    values['attachments'] = [v.to_persisted_dict() for v in state.attachments]
    values['discovered_episodes'] = {k: {'episode': v.to_persisted_dict(), 'metadata': v.metadata}
                                     for k, v in state.discovered_episodes.items()}
    values['diagnosed'] = getattr(state, 'diagnosed', False)
    values['episode_search_attempted'] = getattr(state, 'episode_search_attempted', False)
    values['document_requests'] = getattr(state, 'document_requests', {})
    payload = {'version': 1, 'messages': messages, 'state': values,
               'total_calls': getattr(state, 'total_model_calls', 0)}
    encoded = json.dumps(payload, ensure_ascii=False)
    with state.agent.store._connect() as db:
        db.execute('INSERT OR REPLACE INTO research_checkpoints VALUES (?,?,?)',
                   (state.key, state.message.message_id, encoded))


def restore_checkpoint(state):
    with state.agent.store._connect() as db:
        row = db.execute('SELECT payload_json FROM research_checkpoints WHERE session=? AND message_id=?',
                         (state.key, state.message.message_id)).fetchone()
    if not row:
        return None
    payload = json.loads(row[0])
    if payload['version'] != 1:
        raise ValueError('Unsupported research checkpoint version')
    values = payload['state']
    for name in FIELDS:
        setattr(state, name, values[name])
    for name in SETS:
        setattr(state, name, set(values[name]))
    state.read_coverage = {k: set(v) for k, v in values['read_coverage'].items()}
    state.documents = None if values['documents'] is None else {k: LibraryDocument(**v) for k, v in values['documents'].items()}
    state.attachments = [TranscriptAttachment.from_persisted_dict(v) for v in values['attachments']]
    state.discovered_episodes = {k: replace(Episode.from_persisted_dict(v['episode']), metadata=v['metadata'])
                                 for k, v in values['discovered_episodes'].items()}
    state.diagnosed = values['diagnosed']
    state.episode_search_attempted = values.get('episode_search_attempted', False)
    state.document_requests = values.get('document_requests', {})
    state.total_model_calls = payload['total_calls']
    return payload['messages']
