"""Resumable Feishu daily editions, separate from timely chat delivery.

One document per day; later full-text arrivals append to that document. Frozen
content, idempotent block requests, prefix readback and explicit audience ACLs
prevent duplicate editions and links to incomplete/private drafts.
"""

import hashlib
import json
import re
import uuid
from datetime import date, timedelta

from .daily_report import ranked_daily_items
from .library import LibraryError
from .models import DailyItem
from .shownotes import INTRO, VERSION, note_identity, render_episode, text_node


def tree_signature(node):
    """Ignore server IDs/run boundaries; retain exact text and link targets.

    Feishu can split a literal text run into several runs during storage. Those
    storage boundaries are not content changes. Do not normalize whitespace or
    merge across different link destinations.
    """
    payload = next((v for k, v in node.items() if isinstance(v, dict) and k != 'children'), {})
    elements = []
    for element in payload.get('elements', []):
        run = element.get('text_run', {})
        text = run.get('content', '')
        link = run.get('text_element_style', {}).get('link', {}).get('url', '')
        if elements and elements[-1][1] == link:
            elements[-1] = (elements[-1][0] + text, link)
        else:
            elements.append((text, link))
    return node['block_type'], tuple(elements), tuple(tree_signature(c) for c in node.get('children', []))


def read_tree(blocks, token):
    mapping = {b['block_id']: b for b in blocks}
    if token not in mapping:
        raise LibraryError('文档回读缺少根节点')

    def expand(identity, visiting):
        if identity in visiting or identity not in mapping:
            raise LibraryError('文档块结构不完整')
        node = dict(mapping[identity])
        node['children'] = [expand(c, visiting | {identity}) for c in node.get('children', [])]
        return node

    return [expand(c, {token}) for c in mapping[token].get('children', [])]


def descendant_payload(nodes, start):
    flat = []

    def flatten(node):
        identity = 'b' + str(len(flat))
        block = {k: v for k, v in node.items() if k != 'children'}
        block['block_id'] = identity
        flat.append(block)
        if node.get('children'):
            block['children'] = [flatten(c) for c in node['children']]
        return identity

    roots = [flatten(n) for n in nodes]
    if len(flat) > 1000:
        raise ValueError('Document batch exceeds native block limit')
    return {'children_id': roots, 'descendants': flat, 'index': start}


class DailyDocumentCompiler:
    def __init__(self, store, api, writer, *, folder='', start_date='', root=None):
        self.store, self.api, self.writer = store, api, writer
        self.folder, self.start_date = folder, start_date
        self.root = root or store.path.parent / 'daily-shownotes'

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS daily_documents (
                    day TEXT PRIMARY KEY, title TEXT NOT NULL, document_id TEXT NOT NULL DEFAULT '',
                    creation_state TEXT NOT NULL DEFAULT 'new', nodes_json TEXT NOT NULL DEFAULT '[]',
                    entries_json TEXT NOT NULL DEFAULT '[]', revision TEXT NOT NULL DEFAULT '',
                    notification_job TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS episode_shownotes (
                    identity TEXT PRIMARY KEY, notes_json TEXT NOT NULL, markdown TEXT NOT NULL);
            ''')

    def snapshot(self, base_job):
        items = ranked_daily_items([DailyItem.from_persisted_dict(v)
                                   for v in self.store.list_job_results(base_job, 'daily_item')])
        records, pending = [], []
        for item in items:
            if item.status in {'outside_window', 'unverified_date'}:
                continue
            record = self.store.get_verified_transcript(item.episode.id)
            if record:
                records.append((item, record))
            else:
                pending.append({'title': item.episode.title, 'status': item.status})
        manifest = {'records': [(r.episode.id, r.record_revision_sha256) for _, r in records],
                    'pending': pending, 'version': VERSION}
        revision = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        return revision, records, pending

    def enqueue_ready(self, today):
        day = date.fromisoformat(today)
        oldest = (day - timedelta(days=7)).isoformat()
        cutoff = max(self.start_date or oldest, oldest)
        with self.store._connect() as db:
            jobs = db.execute("SELECT job_key FROM jobs WHERE kind='daily' AND analysis_complete=1 ORDER BY job_key").fetchall()
        count = 0
        active = set(self.store.list_subscriptions())
        for row in jobs:
            match = re.fullmatch(r'daily:(\d{4}-\d{2}-\d{2})', row[0])
            if not match or not cutoff <= match[1] <= today:
                continue
            targets = self.store.get_job_result(row[0], 'daily:recipients') or {}
            audience = [t for t in targets.get('targets', []) if tuple(t) in active]
            if not audience:
                continue
            # Finish a frozen edition before appending its successor, even if
            # it is waiting for retry. Otherwise an older retry could collide
            # with newer content already written to the same document.
            with self.store._connect() as db:
                outstanding = db.execute(
                    "SELECT job_key,payload_json FROM jobs WHERE kind='document' "
                    "AND status!='completed' AND json_extract(payload_json,'$.day')=? "
                    "ORDER BY created_at,rowid LIMIT 1", (match[1],)).fetchone()
            if outstanding:
                count += self.store.enqueue(outstanding[0], 'document', json.loads(outstanding[1]))
                continue
            revision, _, _ = self.snapshot(row[0])
            count += self.store.enqueue(f'daily-document:{match[1]}:{revision[:20]}', 'document',
                                        {'day': match[1], 'base_job': row[0], 'revision': revision,
                                         'targets': audience})
        return count

    def _notes(self, record, digest, *, fulltext_attached=False):
        identity = note_identity(record)
        with self.store._connect() as db:
            row = db.execute('SELECT notes_json FROM episode_shownotes WHERE identity=?', (identity,)).fetchone()
        notes = json.loads(row[0]) if row else self.writer.generate(record)
        nodes, markdown = render_episode(record, notes, digest, fulltext_attached=fulltext_attached)
        if not row:
            with self.store._connect() as db:
                db.execute('INSERT OR IGNORE INTO episode_shownotes VALUES (?,?,?)',
                           (identity, json.dumps(notes, ensure_ascii=False), markdown))
        # Generated application artifacts, never raw secret configuration or source deletion.
        (self.root / (identity + '.md')).write_text(markdown, encoding='utf-8')
        return nodes

    def _document(self, day, title):
        with self.store._connect() as db:
            db.execute('INSERT OR IGNORE INTO daily_documents(day,title) VALUES (?,?)', (day, title))
            row = dict(db.execute('SELECT * FROM daily_documents WHERE day=?', (day,)).fetchone())
        if row['document_id']:
            return row
        # A create timeout has no server idempotency key. Reconcile the exact
        # title, and never blindly create another document after uncertainty.
        params = {'page_size': 200}
        if self.folder:
            params['folder_token'] = self.folder
        files = self.api.pages('/drive/v1/files', 'files', params)
        found = [f['token'] for f in files if f.get('name') == row['title'] and f.get('type') == 'docx']
        if len(found) > 1:
            raise LibraryError('存在同名日报文档，需人工确认，不创建副本')
        if found:
            token = found[0]
        elif row['creation_state'] == 'creating':
            raise LibraryError('创建日报的回执尚未确认，已停止重复创建，需核对云空间')
        else:
            with self.store._connect() as db:
                db.execute("UPDATE daily_documents SET creation_state='creating' WHERE day=?", (day,))
            token = self.api.create(self.folder, row['title'])
        with self.store._connect() as db:
            db.execute("UPDATE daily_documents SET document_id=?,creation_state='created' WHERE day=?", (token, day))
        return {**row, 'document_id': token, 'creation_state': 'created'}

    def _write(self, token, nodes):
        actual = read_tree(self.api.blocks(token), token)
        expected = [tree_signature(n) for n in nodes]
        signatures = [tree_signature(n) for n in actual]
        if signatures != expected[:len(signatures)] or len(signatures) > len(expected):
            raise LibraryError('日报文档存在人工修改或未知内容，停止覆盖以保护内容')
        for start in range(len(actual), len(nodes), 30):
            payload = descendant_payload(nodes[start:start + 30], start)
            digest = hashlib.sha256((token + json.dumps(payload, sort_keys=True)).encode()).digest()[:16]
            self.api.request('POST', f'/docx/v1/documents/{token}/blocks/{token}/descendant',
                             params={'client_token': str(uuid.UUID(bytes=digest, version=4))}, data=payload)
        actual = read_tree(self.api.blocks(token), token)
        if [tree_signature(n) for n in actual] != expected:
            raise LibraryError('日报写入回读不一致，暂不推送文档链接')

    def publish(self, job):
        day = job.payload['day']
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('Invalid edition date')
        active = set(self.store.list_subscriptions())
        targets = [t for t in job.payload['targets'] if tuple(t) in active]
        if not targets:
            return None
        bundle = self.store.get_job_result(job.key, 'document:content')
        revision, records, pending = self.snapshot(job.payload['base_job'])
        if revision != job.payload['revision'] and bundle is None:
            # A newly queued revision will compile current sources instead.
            return None
        title = f'情报官播客精读｜{day}'
        # Freeze generated nodes before writing. Retry does not re-run the model.
        row = self._document(day, title)
        if bundle is None:
            nodes, entries = json.loads(row['nodes_json']), json.loads(row['entries_json'])
            if not nodes:
                nodes = [text_node(INTRO, 'quote')]
            new = [(item, record) for item, record in records if note_identity(record) not in entries]
            heading = '本日节目' if not entries else '补充精读（全文已补齐或版本更新）'
            nodes.append(text_node(heading, 'heading1'))
            nodes.append(text_node(f'本版新增 {len(new)} 期精读；本日共有 {len(records)} 期已取得完整文字稿。'))
            for item, record in new:
                nodes.extend(self._notes(record, self.store.get_transcript_digest(record.episode.id) or item.message))
                entries.append(note_identity(record))
            if pending:
                nodes.append(text_node('本版暂未编译', 'heading2'))
                nodes.extend(text_node(f'《{p["title"]}》：未取得已核验全文，不据简介编造；补齐后追加到本文。', 'bullet') for p in pending)
            elif not records:
                nodes.append(text_node('本日扫描窗口内暂无可编译的完整播客文字稿。'))
            # Freeze a validated source revision before any content write. Once
            # writing starts, finish this immutable edition on retry; a newer
            # queued revision appends an explicit correction/addendum later.
            if self.snapshot(job.payload['base_job'])[0] != revision:
                return None
            bundle = self.store.save_job_result(job.key, 'document:content', 'document_content',
                                               {'nodes': nodes, 'entries': entries, 'revision': revision,
                                                'count': len(records)})
        token = row['document_id']
        self._write(token, bundle['nodes'])
        for kind, identity in targets:
            self.api.request('POST', f'/drive/v1/permissions/{token}/members',
                             params={'type': 'docx', 'need_notification': False},
                             data={'member_type': 'openchat' if kind == 'chat_id' else 'openid',
                                   'member_id': identity, 'perm': 'view',
                                   'type': 'chat' if kind == 'chat_id' else 'user'})
        with self.store._connect() as db:
            db.execute('UPDATE daily_documents SET nodes_json=?,entries_json=?,revision=?, '
                       "notification_job=CASE WHEN notification_job='' THEN ? ELSE notification_job END WHERE day=?",
                       (json.dumps(bundle['nodes'], ensure_ascii=False), json.dumps(bundle['entries']), bundle['revision'], job.key, day))
            notify = db.execute('SELECT notification_job FROM daily_documents WHERE day=?', (day,)).fetchone()[0]
        return {'title': title, 'url': f'https://www.feishu.cn/docx/{token}', 'count': bundle['count'],
                'notify': notify == job.key, 'targets': targets}
