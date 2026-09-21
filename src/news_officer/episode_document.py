"""Selected episodes get independent documents; the morning digest is untouched."""

import hashlib
import json
import re
from datetime import date, timedelta
from types import SimpleNamespace

from .daily_document import DailyDocumentCompiler
from .models import IncomingMessage
from .shownotes import VERSION, note_identity, text_node


def stars(markdown):
    match = re.search(r'(?m)^推荐星级：([★☆]{5})', markdown)
    return match[1].count('★') if match else 0


class SelectedEpisodeCompiler(DailyDocumentCompiler):
    def __init__(self, *args, min_stars=3, request_authorizer=None, **kwargs):
        super().__init__(*args, **kwargs)
        if min_stars not in range(1, 6):
            raise ValueError('Document minimum stars must be 1–5')
        self.min_stars = min_stars
        self.request_authorizer = request_authorizer

    def request_allowed(self, payload):
        return bool(self.request_authorizer and self.request_authorizer(
            IncomingMessage(payload['message_id'], payload['chat_id'], '',
                            payload['chat_type'], payload['sender_open_id'])))

    def enqueue_request(self, reference, message):
        record = self.store.get_verified_transcript(reference)
        if not record or not record.transcript.verified_complete:
            raise ValueError('必须先取得并核验本期完整文字稿')
        payload = {'mode': 'requested_episode', 'episode_id': record.episode.id,
                   'message_id': message.message_id, 'chat_id': message.chat_id,
                   'chat_type': message.chat_type, 'sender_open_id': message.sender_open_id,
                   'reply_in_thread': bool(message.thread_id)}
        if not self.request_allowed(payload):
            raise ValueError('当前会话未获准创建播客文档')
        identity = hashlib.sha256((message.chat_id + ':' + message.message_id + ':' + record.episode.id).encode()).hexdigest()[:24]
        key = 'requested-document:' + identity
        self.store.enqueue(key, 'document', payload)
        return {'status': 'queued', 'job_key': key, 'episode_id': record.episode.id,
                'title': record.episode.title,
                'message': '已受理本期文档编译；完成并核对阅读权限后，会回复当前消息。尚未生成成品链接。'}

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
            requested = db.execute(
                "SELECT job_key,payload_json FROM jobs WHERE kind='document' AND status='failed' "
                "AND json_extract(payload_json,'$.mode')='requested_episode'").fetchall()
            pending = db.execute(
                "SELECT job_key,payload_json FROM jobs WHERE kind='document' AND status!='completed' "
                "AND json_extract(payload_json,'$.mode')='selected_episodes' "
                "ORDER BY created_at,rowid LIMIT 1").fetchone()
            jobs = db.execute("SELECT job_key FROM jobs WHERE kind='daily' AND analysis_complete=1 "
                              "AND status='completed' AND EXISTS (SELECT 1 FROM outbox o "
                              "WHERE o.job_key=jobs.job_key AND o.status='sent') ORDER BY job_key").fetchall()
        retried = sum(int(self.store.enqueue(r[0], 'document', json.loads(r[1]))) for r in requested
                      if self.request_allowed(json.loads(r[1])))
        if pending:
            return retried + int(self.store.enqueue(pending[0], 'document', json.loads(pending[1])))
        active, count = set(self.store.list_subscriptions()), retried
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
        requested = job.payload.get('mode') == 'requested_episode'
        if job.payload.get('mode') not in {'selected_episodes', 'requested_episode'}:
            return None
        if requested:
            if not self.request_allowed(job.payload):
                return None
            record = self.store.get_verified_transcript(job.payload['episode_id'])
            if not record or not record.transcript.verified_complete:
                raise ValueError('请求的完整文字稿已不可用')
            day = record.episode.published_at.isoformat()[:10] if record.episode.published_at else ''
            targets = [('open_id', job.payload['sender_open_id'])] if job.payload['chat_type'] == 'p2p' else [('chat_id', job.payload['chat_id'])]
            revision, records = note_identity(record), [(SimpleNamespace(message=''), record)]
        else:
            day = job.payload['day']
            date.fromisoformat(day)
            active = set(self.store.list_subscriptions())
            targets = [t for t in job.payload['targets'] if tuple(t) in active]
            revision, records, _ = self.snapshot(job.payload['base_job'])
        if not targets:
            return None
        bundle = self.store.get_job_result(job.key, 'episodes:content')
        if bundle is None:
            if not requested and revision != job.payload['revision']:
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
            if not requested and self.snapshot(job.payload['base_job'])[0] != revision:
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
                     document['revision'], '' if requested else job.key, document['key']))
                notified = db.execute('SELECT notification_job FROM daily_documents WHERE day=?', (document['key'],)).fetchone()[0]
            if requested or notified == job.key:
                links.append({'title': document['title'], 'stars': document['stars'],
                              'url': f'https://www.feishu.cn/docx/{token}'})
        result = {'title': '播客重点总结已整理成文档' if requested else f'{day} 重点播客精读',
                  'documents': links, 'count': len(links), 'notify': bool(links), 'targets': targets}
        if requested:
            result['reply_to'] = job.payload['message_id']
            result['reply_in_thread'] = job.payload['reply_in_thread']
        return result
