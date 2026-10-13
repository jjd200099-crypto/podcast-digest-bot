"""Selected episodes get independent documents; the morning digest is untouched."""

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .daily_document import DailyDocumentCompiler, read_tree, tree_signature
from .document_transcript import fulltext_identity, render_fulltext
from .library import LibraryError
from .models import IncomingMessage
from .shownotes import note_identity, text_node
from .titles import normalize_deepread_title

logger = logging.getLogger(__name__)


def stars(markdown):
    match = re.search(r'(?m)^推荐星级：([★☆]{5})', markdown)
    return match[1].count('★') if match else 0


class DocumentRetryStopped(ValueError):
    """A document revision exhausted its durable budget or same-day deadline."""


class SelectedEpisodeCompiler(DailyDocumentCompiler):
    def __init__(self, *args, min_stars=4, request_authorizer=None, max_attempts=3,
                 timezone=None, today=None, include_fulltext=False, transcript_writer=None, **kwargs):
        super().__init__(*args, **kwargs)
        if min_stars not in range(1, 6):
            raise ValueError('Document minimum stars must be 1–5')
        self.min_stars = min_stars
        self.request_authorizer = request_authorizer
        self.include_fulltext = include_fulltext
        self.transcript_writer = transcript_writer
        self.max_attempts = max_attempts
        self.today = today or (lambda: datetime.now(timezone or ZoneInfo('Asia/Shanghai')).date().isoformat())

    def _migrate_attempt(self, record):
        # The pre-merge notes-only revision used note_identity directly. Keep
        # its durable budget when adopting main's composite document identity.
        if self.include_fulltext or self.new_style:
            return
        identity = self.document_identity(record)
        with self.store._connect() as db:
            db.execute('UPDATE episode_document_attempts SET revision=? WHERE episode_id=? AND revision=? '
                       'AND NOT EXISTS (SELECT 1 FROM episode_document_attempts WHERE episode_id=? AND revision=?)',
                       (identity, record.episode.id, note_identity(record, new_style=self.new_style), record.episode.id, identity))

    def _attempt_available(self, record, today):
        self._migrate_attempt(record)
        with self.store._connect() as db:
            row = db.execute('SELECT * FROM episode_document_attempts WHERE episode_id=? AND revision=?',
                             (record.episode.id, self.document_identity(record))).fetchone()
            if row and row['status'] != 'completed' and (
                    row['day'] != today or row['attempts'] >= self.max_attempts):
                db.execute("UPDATE episode_document_attempts SET status='failed' WHERE episode_id=? AND revision=?",
                           (record.episode.id, self.document_identity(record)))
                return False
        return not row or row['status'] != 'failed'

    def retry_available(self, payload, today):
        if payload.get('mode') == 'requested_episode':
            record = self.store.get_verified_transcript(payload['episode_id'])
            return bool(record and self._attempt_available(record, today))
        base = payload['base_job']
        day = payload.get('day') or payload['scheduled_for'][:10]
        _, records, _ = self.snapshot(base)
        if payload.get('mode') == 'daily_episode':
            records = [(i, r) for i, r in records if r.episode.id == payload['episode_id']]
        for _, record in records:
            self._migrate_attempt(record)
        if day != today:
            with self.store._connect() as db:
                for _, record in records:
                    pair = (record.episode.id, self.document_identity(record))
                    db.execute('INSERT OR IGNORE INTO episode_document_attempts(episode_id,revision,day) VALUES (?,?,?)',
                               (*pair, day))
                    db.execute("UPDATE episode_document_attempts SET status='failed' WHERE episode_id=? AND revision=? AND status!='completed'", pair)
            return False
        available = [self._attempt_available(record, today) for _, record in records]
        with self.store._connect() as db:
            states = [db.execute('SELECT status FROM episode_document_attempts WHERE episode_id=? AND revision=?',
                                 (record.episode.id, self.document_identity(record))).fetchone()
                      for _, record in records]
        unfinished = any(ok and (not state or state['status'] != 'completed')
                         for ok, state in zip(available, states, strict=True))
        return day == today and (all(available) or unfinished)

    def _claim_attempt(self, record, day, claimed):
        identity = self.document_identity(record)
        pair = (record.episode.id, identity)
        if pair in claimed:
            return
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO episode_document_attempts(episode_id,revision,day) VALUES (?,?,?)',
                       (*pair, day))
            row = db.execute('SELECT * FROM episode_document_attempts WHERE episode_id=? AND revision=?', pair).fetchone()
            if row['status'] == 'completed':
                return  # A verified publication can be replayed without regenerating it.
            stopped = row['status'] == 'failed' or row['day'] != self.today() or row['attempts'] >= self.max_attempts
            if stopped:
                db.execute("UPDATE episode_document_attempts SET status='failed' WHERE episode_id=? AND revision=?", pair)
            else:
                db.execute('UPDATE episode_document_attempts SET attempts=attempts+1 WHERE episode_id=? AND revision=?', pair)
        if stopped:
            raise DocumentRetryStopped('Episode document retry budget or deadline reached')
        claimed.add(pair)

    def _finish_attempts(self, claimed):
        with self.store._connect() as db:
            for pair in claimed:
                db.execute("UPDATE episode_document_attempts SET status=CASE WHEN attempts>=? THEN 'failed' "
                           "ELSE 'pending' END WHERE episode_id=? AND revision=? AND status!='completed'",
                           (self.max_attempts, *pair))

    def _stop_job(self, key):
        self.store.fail(key, 'DocumentRetryStopped', 1, max_attempts=1)

    def _replace_legacy_appendix(self, token, document):
        base = document.get('replacement_base')
        if not base:
            return
        # Optimistic revision prevents deleting a range changed after readback.
        meta = self.api.request('GET', f'/docx/v1/documents/{token}')
        revision = meta['document']['revision_id']
        actual = [tree_signature(n) for n in read_tree(self.api.blocks(token), token)]
        expected = [tree_signature(n) for n in document['nodes']]
        if actual == expected[:len(actual)] and len(actual) <= len(expected):
            return  # Already removed; resume an interrupted append normally.
        if actual != [tree_signature(n) for n in base]:
            raise LibraryError('旧附录或精读存在人工修改，停止替换以保护内容')
        start = document['replacement_start']
        # Frozen base is the recoverable backup. A layout upgrade replaces all
        # generated content; a legacy appendix upgrade replaces only its tail.
        self.api.request('DELETE', f'/docx/v1/documents/{token}/blocks/{token}/children/batch_delete',
                         params={'document_revision_id': revision},
                         data={'start_index': start, 'end_index': len(base)})

    def document_identity(self, record):
        components = [note_identity(record, new_style=self.new_style)]
        if self.include_fulltext and not self.new_style:
            components.append(fulltext_identity(record))
        return hashlib.sha256(json.dumps(components).encode()).hexdigest()

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
        manifest = [(r.episode.id, self.document_identity(r)) for _, r in selected]
        revision = hashlib.sha256(json.dumps([self.notes_version, self.min_stars, manifest]).encode()).hexdigest()
        return revision, selected, []

    def enqueue_ready(self, today, *, requested_only=False):
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
        retried = 0
        for row in requested:
            payload = json.loads(row[1])
            if not self.retry_available(payload, today):
                self._stop_job(row[0])
                continue
            if self.request_allowed(payload):
                retried += int(self.store.enqueue(row[0], 'document', payload))
        if requested_only:
            return retried
        if pending:
            if self.retry_available(json.loads(pending[1]), today):
                return retried + int(self.store.enqueue(pending[0], 'document', json.loads(pending[1])))
            self._stop_job(pending[0])
        count = retried
        for row in jobs:
            if self.store.get_job_result(row[0], 'daily:inline-documents') is not None:
                continue
            match = re.fullmatch(r'daily:(\d{4}-\d{2}-\d{2})', row[0])
            if not match or not cutoff <= match[1] <= today:
                continue
            audience = self._audience(
                (self.store.get_job_result(row[0], 'daily:recipients') or {}).get('targets', []))
            if not audience:
                continue
            revision, selected, _ = self.snapshot(row[0])
            payload = {'mode': 'selected_episodes', 'day': match[1], 'base_job': row[0]}
            if selected and self.retry_available(payload, today):
                count += self.store.enqueue(f'episode-documents:{match[1]}:{revision[:20]}', 'document',
                    {'mode': 'selected_episodes', 'day': match[1], 'base_job': row[0],
                     'revision': revision, 'targets': audience})
                if count:
                    return count
        return count

    def publish(self, job):
        claimed = set()
        try:
            return self._publish(job, claimed)
        except Exception as error:
            safe_error = RuntimeError(type(error).__name__)
            logger.warning('Episode document compilation deferred',
                           exc_info=(RuntimeError, safe_error, error.__traceback__))
            self._finish_attempts(claimed)
            if claimed and not self.retry_available(job.payload, self.today()):
                raise DocumentRetryStopped('Episode document retry budget or deadline reached') from None
            raise
        finally:
            self._finish_attempts(claimed)

    def _publish(self, job, claimed):
        # A deployment may inherit a pending combined-edition job. Retire that
        # obsolete workflow without deleting or rewriting its historical doc.
        requested = job.payload.get('mode') == 'requested_episode'
        enrichment = job.payload.get('mode') == 'daily_episode'
        if job.payload.get('mode') not in {'selected_episodes', 'requested_episode', 'daily_episode'}:
            return None
        if requested:
            if not self.request_allowed(job.payload):
                return None
            record = self.store.get_verified_transcript(job.payload['episode_id'])
            if not record or not record.transcript.verified_complete:
                raise ValueError('请求的完整文字稿已不可用')
            day = record.episode.published_at.isoformat()[:10] if record.episode.published_at else ''
            targets = [('open_id', job.payload['sender_open_id'])] if job.payload['chat_type'] == 'p2p' else [('chat_id', job.payload['chat_id'])]
            revision, records = self.document_identity(record), [(SimpleNamespace(message=''), record)]
        else:
            day = job.payload['day']
            date.fromisoformat(day)
            targets = self._audience(job.payload['targets'])
            revision, records, _ = self.snapshot(job.payload['base_job'])
            if enrichment:
                from .daily_enrichment import enrichment_bundle
                edition = enrichment_bundle(self.store, job.payload['base_job'])
                if not edition or job.payload['episode_id'] not in edition['enrichment']['eligible']:
                    return None
                audience = {(d['target_type'], d['target_id']) for d in edition['deliveries']}
                targets = [t for t in targets if tuple(t) in audience]
                records = [(i, r) for i, r in records if r.episode.id == job.payload['episode_id']]
                if len(records) != 1:
                    raise LibraryError('日报文档对应的已核验原稿不可用')
                if records[0][1].record_revision_sha256 != job.payload['record_revision']:
                    raise LibraryError('日报文档原稿版本已变化，停止替换旧日报')
                revision = self.document_identity(records[0][1])
        if not targets:
            return None
        if not self.retry_available(job.payload, self.today()):
            raise DocumentRetryStopped('Document retry budget or deadline reached')
        deferred = False
        bundle = self.store.get_job_result(job.key, 'episodes:content')
        if bundle is None:
            if not requested and not enrichment and revision != job.payload['revision']:
                return None
            documents = []
            for item, record in records:
                if not self._attempt_available(record, self.today()):
                    deferred = True
                    continue
                self._claim_attempt(record, self.today() if requested else day, claimed)
                identity = note_identity(record, new_style=self.new_style)
                key = 'episode:' + record.episode.id
                published = record.episode.published_at.isoformat()[:10] if record.episode.published_at else day
                title = normalize_deepread_title(f'{record.episode.title}｜{record.episode.show}｜{published}')
                digest = self.store.get_transcript_digest(record.episode.id) or item.message
                with self.store._connect() as db:
                    previous = db.execute('SELECT nodes_json,entries_json FROM daily_documents WHERE day=?', (key,)).fetchone()
                entries = json.loads(previous['entries_json']) if previous else []
                nodes = json.loads(previous['nodes_json']) if previous else []
                replacement = {}
                episode_nodes = None
                if self.new_style and identity not in entries:
                    try:
                        episode_nodes, generated_new_style = self._notes(
                            record, digest, fulltext_attached=self.include_fulltext and bool(nodes),
                            return_style=True)
                    except Exception:
                        if requested or enrichment:
                            raise
                        logger.warning('Episode document validation failed; deferred', exc_info=True)
                        deferred = True
                        continue
                    if nodes and not generated_new_style:
                        # Keep the existing notes and full-text appendix intact.
                        documents.append({'key': key, 'title': title, 'nodes': nodes,
                                          'entries': entries, 'revision': self.document_identity(record),
                                          'stars': stars(digest)})
                        continue
                    if nodes:
                        replacement = {'replacement_base': nodes, 'replacement_start': 0}
                        nodes, entries = [], []
                if self.include_fulltext and not self.new_style and fulltext_identity(record) not in entries:
                    legacy = [i for i, n in enumerate(nodes) if ''.join(
                        e.get('text_run', {}).get('content', '') for e in n.get('heading2', {}).get('elements', [])
                    ).startswith(('完整文字稿（归档原文', '完整中文文字稿', '完整对谈实录'))]
                    if legacy:
                        start = legacy[0]
                        if len(legacy) != 1 or any(n['block_type'] != 2 for n in nodes[start + 1:]):
                            raise LibraryError('旧全文不是独立的纯文本尾部，需人工核对')
                        replacement = {'replacement_base': nodes, 'replacement_start': start}
                        nodes = nodes[:start]
                        entries = [e for e in entries if not e.startswith('fulltext:')]
                if identity not in entries:
                    if nodes:
                        nodes.append(text_node('补充精读（原稿版本更新）', 'heading1'))
                    try:
                        if episode_nodes is None:
                            episode_nodes = self._notes(record, digest, fulltext_attached=self.include_fulltext)
                    except Exception:
                        if requested or enrichment:
                            raise
                        logger.warning('Episode document validation failed; deferred', exc_info=True)
                        deferred = True
                        continue
                    nodes.extend(episode_nodes)
                    if not self.new_style or generated_new_style:
                        entries.append(identity)
                if self.include_fulltext and not self.new_style:
                    appendix_id = fulltext_identity(record)
                    if appendix_id not in entries:
                        if self.transcript_writer is None:
                            raise ValueError('未配置中文全文翻译器，停止发布原文替代品')
                        translation = self.transcript_writer.generate(record)
                        nodes.extend(render_fulltext(record, translation))
                        (self.root / (appendix_id.replace(':', '-') + '.zh.json')).write_text(
                            json.dumps(translation, ensure_ascii=False), encoding='utf-8')
                        entries.append(appendix_id)
                    # Store a lossless local export alongside the derived notes.
                    (self.root / (appendix_id.replace(':', '-') + '.txt')).write_text(
                        record.transcript.text, encoding='utf-8')
                documents.append({'key': key, 'title': title, 'nodes': nodes, 'entries': entries,
                                  'revision': self.document_identity(record), 'stars': stars(digest), **replacement})
            if enrichment:
                current = self.store.get_verified_transcript(job.payload['episode_id'])
                if current is None or self.document_identity(current) != revision:
                    raise LibraryError('日报文档原稿版本在编译期间发生变化')
            elif not requested and self.snapshot(job.payload['base_job'])[0] != revision:
                return None
            bundle = {'documents': documents}
            if not deferred:
                bundle = self.store.save_job_result(job.key, 'episodes:content', 'episode_documents', bundle)
        if not requested:
            targets = self._audience(targets)
        links = []
        for document in bundle['documents']:
            # An older pending batch must not bypass a newly raised threshold.
            if not requested and document['stars'] < self.min_stars:
                continue
            if not self.include_fulltext and any(e.startswith('fulltext:') for e in document['entries']):
                raise ValueError('全文发布授权已关闭，停止写入冻结的全文文档')
            if any('完整文字稿（归档原文' in str(n.get('heading2', {})) for n in document['nodes']):
                raise ValueError('旧任务冻结了未翻译原文，请使用新的中文全文任务')
            record = next(r for _, r in records if r.episode.id == document['key'].removeprefix('episode:'))
            self._claim_attempt(record, self.today() if requested else day, claimed)
            row = self._document(document['key'], document['title'])
            token = row['document_id']
            self._replace_legacy_appendix(token, document)
            self._write(token, document['nodes'])
            self._verify_speaker_labels(token, document['nodes'])
            for kind, identity in targets:
                if not requested and not self._audience([(kind, identity)]):
                    continue
                self.api.request('POST', f'/drive/v1/permissions/{token}/members',
                    params={'type': 'docx', 'need_notification': False},
                    data={'member_type': 'openchat' if kind == 'chat_id' else 'openid',
                          'member_id': identity, 'perm': 'view', 'type': 'chat' if kind == 'chat_id' else 'user'})
            with self.store._connect() as db:
                db.execute('UPDATE daily_documents SET nodes_json=?,entries_json=?,revision=?, '
                    "notification_job=CASE WHEN notification_job='' THEN ? ELSE notification_job END WHERE day=?",
                    (json.dumps(document['nodes'], ensure_ascii=False), json.dumps(document['entries']),
                     document['revision'], '' if requested else job.key, document['key']))
                db.execute("UPDATE episode_document_attempts SET status='completed' WHERE episode_id=? AND revision=?",
                           (record.episode.id, document['revision']))
                notified = db.execute('SELECT notification_job FROM daily_documents WHERE day=?', (document['key'],)).fetchone()[0]
            if requested or enrichment or job.kind == 'daily' or notified == job.key:
                links.append({'title': document['title'], 'stars': document['stars'],
                              'episode_id': document['key'].removeprefix('episode:'),
                              'url': f'https://www.feishu.cn/docx/{token}'})
        if not requested:
            targets = self._audience(targets)
        result = {'title': '播客重点总结已整理成文档' if requested else f'{day} 重点播客精读',
                  'documents': links, 'count': len(links), 'notify': bool(links), 'targets': targets}
        if deferred:
            result['deferred'] = True
        if requested:
            result['reply_to'] = job.payload['message_id']
            result['reply_in_thread'] = job.payload['reply_in_thread']
        return result

    def _verify_speaker_labels(self, token, expected):
        """Feishu readback must retain the reference's bold speaker labels."""
        start = next((i for i, node in enumerate(expected) if ''.join(
            e.get('text_run', {}).get('content', '') for e in node.get('heading2', {}).get('elements', [])
        ) == '完整对谈实录'), None)
        if start is None:
            return
        actual = read_tree(self.api.blocks(token), token)
        for original, remote in zip(expected[start + 1:], actual[start + 1:], strict=True):
            first = original.get('text', {}).get('elements', [{}])[0].get('text_run', {})
            if not first.get('text_element_style', {}).get('bold'):
                continue
            remaining = first['content']
            for element in remote.get('text', {}).get('elements', []):
                run = element.get('text_run', {})
                text = run.get('content', '')[:len(remaining)]
                if text and (not run.get('text_element_style', {}).get('bold') or not remaining.startswith(text)):
                    raise LibraryError('对谈实录姓名加粗回读不符，暂不交付链接')
                remaining = remaining[len(text):]
                if not remaining:
                    break
            if remaining:
                raise LibraryError('对谈实录姓名回读缺失，暂不交付链接')
