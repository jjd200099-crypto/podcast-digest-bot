"""Selected episodes get independent documents; the morning digest is untouched."""

import hashlib
import json
import re
from datetime import date, timedelta

from .daily_document import DailyDocumentCompiler
from .shownotes import VERSION, note_identity, text_node


def stars(markdown):
    match = re.search(r'(?m)^推荐星级：([★☆]{5})', markdown)
    return match[1].count('★') if match else 0


class SelectedEpisodeCompiler(DailyDocumentCompiler):
    def __init__(self, *args, min_stars=4, **kwargs):
        super().__init__(*args, **kwargs)
        if min_stars not in range(1, 6):
            raise ValueError('Document minimum stars must be 1–5')
        self.min_stars = min_stars

    def snapshot(self, base_job):
        _, records, _ = super().snapshot(base_job)
        selected = []
        for item, record in records:
            digest = self.store.get_transcript_digest(record.episode.id) or item.message
            if stars(digest) >= self.min_stars:
                selected.append((item, record))
        selected.sort(key=lambda pair: -stars(self.store.get_transcript_digest(pair[1].episode.id) or pair[0].message))
        manifest = [(r.episode.id, note_identity(r)) for _, r in selected]
        revision = hashlib.sha256(json.dumps([VERSION, self.min_stars, manifest]).encode()).hexdigest()
        return revision, selected, []

    def enqueue_ready(self, today):
        oldest = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
        cutoff = max(self.start_date or oldest, oldest)
        with self.store._connect() as db:
            pending = db.execute(
                "SELECT job_key,payload_json FROM jobs WHERE kind='document' AND status!='completed' "
                "AND json_extract(payload_json,'$.mode')='selected_episodes' "
                "ORDER BY created_at,rowid LIMIT 1").fetchone()
            jobs = db.execute("SELECT job_key FROM jobs WHERE kind='daily' AND analysis_complete=1 ORDER BY job_key").fetchall()
        if pending:
            return int(self.store.enqueue(pending[0], 'document', json.loads(pending[1])))
        active, count = set(self.store.list_subscriptions()), 0
        for row in jobs:
            match = re.fullmatch(r'daily:(\d{4}-\d{2}-\d{2})', row[0])
            if not match or not cutoff <= match[1] <= today:
                continue
            audience = [t for t in (self.store.get_job_result(row[0], 'daily:recipients') or {}).get('targets', [])
                        if tuple(t) in active]
            if not audience:
                continue
            revision, selected, _ = self.snapshot(row[0])
            if selected:
                count += self.store.enqueue(f'episode-documents:{match[1]}:{revision[:20]}', 'document',
                    {'mode': 'selected_episodes', 'day': match[1], 'base_job': row[0],
                     'revision': revision, 'targets': audience})
                if count:
                    return count
        return count

    def publish(self, job):
        # A deployment may inherit a pending combined-edition job. Retire that
        # obsolete workflow without deleting or rewriting its historical doc.
        if job.payload.get('mode') != 'selected_episodes':
            return None
        day = job.payload['day']
        date.fromisoformat(day)
        active = set(self.store.list_subscriptions())
        targets = [t for t in job.payload['targets'] if tuple(t) in active]
        if not targets:
            return None
        bundle = self.store.get_job_result(job.key, 'episodes:content')
        revision, records, _ = self.snapshot(job.payload['base_job'])
        if bundle is None:
            if revision != job.payload['revision']:
                return None
            documents = []
            for item, record in records:
                identity = note_identity(record)
                key = 'episode:' + record.episode.id
                published = record.episode.published_at.isoformat()[:10] if record.episode.published_at else day
                title = f'{record.episode.title}｜{record.episode.show}｜{published}'
                digest = self.store.get_transcript_digest(record.episode.id) or item.message
                with self.store._connect() as db:
                    previous = db.execute('SELECT nodes_json,entries_json FROM daily_documents WHERE day=?', (key,)).fetchone()
                entries = json.loads(previous['entries_json']) if previous else []
                nodes = json.loads(previous['nodes_json']) if previous else []
                if identity not in entries:
                    if nodes:
                        nodes.append(text_node('补充精读（原稿版本更新）', 'heading1'))
                    nodes.extend(self._notes(record, digest))
                    entries.append(identity)
                documents.append({'key': key, 'title': title, 'nodes': nodes, 'entries': entries,
                                  'revision': identity, 'stars': stars(digest)})
            if self.snapshot(job.payload['base_job'])[0] != revision:
                return None
            bundle = self.store.save_job_result(job.key, 'episodes:content', 'episode_documents',
                                                {'documents': documents})
        links = []
        for document in bundle['documents']:
            row = self._document(document['key'], document['title'])
            token = row['document_id']
            self._write(token, document['nodes'])
            for kind, identity in targets:
                self.api.request('POST', f'/drive/v1/permissions/{token}/members',
                    params={'type': 'docx', 'need_notification': False},
                    data={'member_type': 'openchat' if kind == 'chat_id' else 'openid',
                          'member_id': identity, 'perm': 'view', 'type': 'chat' if kind == 'chat_id' else 'user'})
            with self.store._connect() as db:
                db.execute('UPDATE daily_documents SET nodes_json=?,entries_json=?,revision=?, '
                    "notification_job=CASE WHEN notification_job='' THEN ? ELSE notification_job END WHERE day=?",
                    (json.dumps(document['nodes'], ensure_ascii=False), json.dumps(document['entries']),
                     document['revision'], job.key, document['key']))
                notified = db.execute('SELECT notification_job FROM daily_documents WHERE day=?', (document['key'],)).fetchone()[0]
            if notified == job.key:
                links.append({'title': document['title'], 'stars': document['stars'],
                              'url': f'https://www.feishu.cn/docx/{token}'})
        return {'title': f'{day} 重点播客精读', 'documents': links, 'count': len(links),
                'notify': bool(links), 'targets': targets}
