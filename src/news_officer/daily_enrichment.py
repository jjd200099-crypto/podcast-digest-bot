"""Independent episode documents, durable in-place enrichment of a daily receipt.

The original send/outbox is immutable. Edit intents are saved before PUT and
read back afterwards, so a lost response is recovered without sending again.
Only the single document worker writes these edits (including recovery jobs).
"""

import hashlib
import json
from datetime import date, timedelta

from .feishu import combined_delivery_parts
from .library import LibraryError
from .models import DailyItem

DOCUMENT_PLACEHOLDER = 'https://document.invalid/DAILY_DOCUMENT_LINK'


def enrichment_bundle(store, key):
    matches = [b for b in store.list_job_results(key, 'daily_bundle')
               if (b.get('enrichment') or {}).get('version') == 1]
    if len(matches) > 1:
        raise LibraryError('日报原消息不唯一，停止自动编辑')
    return matches[0] if matches else None


def enqueue_enrichment(store, today, *, audience_filter=None, compiler=None):
    """Resume each episode separately, only after the original send succeeded."""
    cutoff = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    with store._connect() as db:
        rows = db.execute("SELECT DISTINCT r.job_key FROM job_results r JOIN jobs j "
                          "ON j.job_key=r.job_key WHERE r.kind='daily_bundle' "
                          "AND j.analysis_complete=1 AND j.kind='daily' "
                          "AND json_extract(r.payload_json,'$.enrichment.version')=1 "
                          "AND json_extract(r.payload_json,'$.enrichment.day') BETWEEN ? AND ?",
                          (cutoff, today)).fetchall()
    count = 0
    for row in rows:
        base = row[0]
        bundle = enrichment_bundle(store, base)
        # Finish ALL original targets first. Never edit a message not yet sent.
        if not bundle or not store.outbox_group_sent(base, bundle['group_key']):
            continue
        targets = _audience(store, [(d['target_type'], d['target_id']) for d in bundle['deliveries']], audience_filter)
        if not targets:
            continue
        items = {i.episode.id: i for i in (DailyItem.from_persisted_dict(v)
                 for v in store.list_job_results(base, 'daily_item'))}
        for episode_id in bundle['enrichment']['eligible']:
            item = items.get(episode_id)
            if not item or not item.attachment:
                continue  # Old/incomplete provenance is never regenerated blindly.
            key = 'daily-document:' + hashlib.sha256(
                f'{base}:{episode_id}:{item.attachment.record_revision_sha256}'.encode()).hexdigest()[:28]
            with store._connect() as db:
                existing = db.execute('SELECT status FROM jobs WHERE job_key=?', (key,)).fetchone()
            if existing and existing[0] == 'completed':
                continue
            payload = {
                'mode': 'daily_episode', 'day': bundle['enrichment']['day'],
                'base_job': base, 'episode_id': episode_id, 'targets': targets,
                'record_revision': item.attachment.record_revision_sha256}
            if compiler and not compiler.retry_available(payload, today):
                # Persist the terminal job as well as the per-revision warning.
                store.enqueue(key, 'document', payload)
                compiler._stop_job(key)
                continue
            count += store.enqueue(key, 'document', payload)
        count += enqueue_followup(store, base, today, audience_filter=audience_filter)
    return count


def normalized_content(kind, content):
    value = json.loads(content)
    if kind == 'text':
        return {'text': value['text']}
    if kind != 'post':
        raise LibraryError('日报消息类型不支持编辑')
    value = value.get('zh_cn', value)
    # Readback adds content_v2 and empty style arrays. Retain all meaningful
    # fields/styles so human changes cannot be mistaken for our prior version.
    return {'title': value['title'], 'content': [
        [{k: v for k, v in node.items() if not (k == 'style' and v == [])}
         for node in line] for line in value['content']]}


def _content(bundle, documents, delivery):
    edition = bundle['enrichment']
    blocks = list(edition['prefix'])
    for section in edition['sections']:
        doc = documents.get(section['episode_id'])
        blocks.append(section['template'].replace(DOCUMENT_PLACEHOLDER, doc['url'])
                      if doc else section['body'])
    parts = combined_delivery_parts('\n\n'.join(blocks), delivery['key'], title=edition['title'])
    if len(parts) != 1 or parts[0][0] != delivery['parts'][0][0]:
        raise LibraryError('补充链接后超过原消息容量，保留原日报，不拆分发送')
    return parts[0][0], parts[0][1]


def _read_receipt(api, row):
    identity = row['remote_message_id']
    data = api.request('GET', f'/im/v1/messages/{identity}')
    items = data.get('items', [])
    if len(items) != 1:
        raise LibraryError('无法唯一回读日报原消息')
    message = items[0]
    sender = message.get('sender', {})
    if (message.get('message_id') != identity or message.get('deleted')
            or sender.get('sender_type') != 'app' or sender.get('id_type') != 'app_id'
            or sender.get('id') != api.messenger.app_id
            or message.get('msg_type') != row['msg_type']
            or (row['target_type'] == 'chat_id' and message.get('chat_id') != row['target_id'])):
        raise LibraryError('日报回执身份或会话不符，停止编辑')
    return normalized_content(row['msg_type'], message['body']['content'])


def update_daily_message(store, api, base, *, audience_filter=None):
    bundle = enrichment_bundle(store, base)
    if not bundle:
        raise LibraryError('日报没有可编辑的冻结版本')
    ready = store.list_job_results(base, 'daily_document_ready')
    for delivery in bundle['deliveries']:
        target = (delivery['target_type'], delivery['target_id'])
        if not _audience(store, [target], audience_filter):
            continue
        documents = {d['episode_id']: d for result in ready if target in
                     {tuple(t) for t in result['targets']} for d in result['documents']
                     if d['episode_id'] in bundle['enrichment']['eligible']}
        if not documents:
            continue
        with store._connect() as db:
            rows = db.execute("SELECT * FROM outbox WHERE job_key=? AND delivery_key=?",
                              (base, delivery['key'])).fetchall()
        if len(rows) != 1 or rows[0]['status'] != 'sent' or not rows[0]['remote_message_id']:
            raise LibraryError('日报原消息尚未确认送达，稍后重试链接更新')
        row = rows[0]
        kind, content = _content(bundle, documents, delivery)
        desired = normalized_content(kind, content)
        remote = _read_receipt(api, row)
        intents = [v for v in store.list_job_results(base, 'daily_edit_intent')
                   if v['message_id'] == row['remote_message_id']]
        if remote != desired:
            known = [normalized_content(row['msg_type'], row['content'])]
            known.extend(normalized_content(v['msg_type'], v['content']) for v in intents)
            if remote not in known:
                raise LibraryError('日报存在外部修改，保留人工内容，不覆盖或重发')
            # Feishu allows 20 edits. Reserve the last slot for all remaining
            # documents rather than consuming it on another partial update.
            if len(intents) >= 19 and set(documents) != set(bundle['enrichment']['eligible']):
                return
            identity = hashlib.sha256((row['remote_message_id'] + content).encode()).hexdigest()
            existing = store.get_job_result(base, 'daily:edit:' + identity)
            if len(intents) >= 20 and existing is None:
                raise LibraryError('日报已用完安全编辑预算，保留已发送消息')
            store.save_job_result(base, 'daily:edit:' + identity, 'daily_edit_intent', {
                'message_id': row['remote_message_id'], 'msg_type': kind, 'content': content})
            if not _audience(store, [target], audience_filter):
                continue
            api.request('PUT', f"/im/v1/messages/{row['remote_message_id']}",
                        data={'msg_type': kind, 'content': content})
            if _read_receipt(api, row) != desired:
                raise LibraryError('日报链接更新回读不符，暂不标记完成')
        digest = hashlib.sha256((row['remote_message_id'] + content).encode()).hexdigest()
        store.save_job_result(base, 'daily:edit-receipt:' + digest, 'daily_edit_receipt', {
            'message_id': row['remote_message_id'], 'episode_ids': sorted(documents)})


def publish_enrichment(store, compiler, job):
    base = job.payload['base_job']
    saved = store.get_job_result(base, 'daily:document:' + job.payload['episode_id'])
    if saved is None:
        result = compiler.publish(job)
        if result is None:
            # All intended recipients unsubscribed: no new publication needed.
            if not compiler._audience(job.payload['targets']):
                return
            raise LibraryError('日报文档未完成，保留任务重试')
        if {d['episode_id'] for d in result['documents']} != {job.payload['episode_id']}:
            raise LibraryError('日报文档结果未包含指定节目')
        store.save_job_result(base, 'daily:document:' + job.payload['episode_id'],
                              'daily_document_ready', result)
    update_daily_message(store, compiler.api, base, audience_filter=compiler._audience)


def _audience(store, targets, audience_filter):
    if audience_filter is not None:
        return [tuple(t) for t in audience_filter(targets)]
    active = set(store.list_subscriptions())
    return [tuple(t) for t in targets if tuple(t) in active]


def enqueue_followup(store, base, today, *, audience_filter=None):
    """One links-only receipt after all same-day episode jobs finish or fail."""
    bundle = enrichment_bundle(store, base)
    if not bundle or bundle['enrichment']['day'] != today:
        return 0
    if not store.outbox_group_sent(base, bundle['group_key']):
        return 0
    ready = store.list_job_results(base, 'daily_document_ready')
    documents = {d['episode_id'] for r in ready for d in r['documents']}
    eligible = set(bundle['enrichment']['eligible'])
    if not documents & eligible:
        return 0
    with store._connect() as db:
        failed = {row[0] for row in db.execute(
            "SELECT json_extract(payload_json,'$.episode_id') FROM jobs WHERE kind='document' "
            "AND status='failed' AND json_extract(payload_json,'$.mode')='daily_episode' "
            "AND json_extract(payload_json,'$.base_job')=?", (base,))}
    if not eligible <= documents | failed:
        return 0
    targets = _audience(store, [(d['target_type'], d['target_id']) for d in bundle['deliveries']], audience_filter)
    deliveries = []
    for target in targets:
        links = {d['episode_id']: d for r in ready if target in {tuple(t) for t in r['targets']}
                 for d in r['documents'] if d['episode_id'] in eligible}
        if links:
            content = '\n\n'.join(
                f"[{d['title'].replace('[', '（').replace(']', '）')}]({d['url']})"
                for _, d in sorted(links.items()))
            deliveries.append({'target': target, 'content': content})
    if not deliveries:
        return 0
    return int(store.enqueue(f'daily-document-followup:{today}', 'document', {
        'mode': 'daily_document_followup', 'day': today, 'base_job': base,
        'deliveries': deliveries}))
