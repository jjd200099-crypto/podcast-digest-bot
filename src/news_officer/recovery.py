"""Operator-authorized outage recovery. Preview by default; never run a second worker."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta

import requests

from .config import Settings
from .delivery_watchdog import CheckError, api
from .models import IncomingMessage
from .store import Store


def recover_daily(store, settings, day, *, execute=False, now=None):
    now = now or datetime.now(UTC)
    today = now.astimezone(settings.timezone).date()
    if not 1 <= (today - day).days <= 7:
        raise ValueError('Recovery must name a past day within seven days')
    if not store.has_subscriptions():
        raise ValueError('No active daily subscribers')
    when = datetime.combine(day, settings.daily_time, settings.timezone)
    key = f'daily:{day.isoformat()}'
    status = store.job_status(key)
    if status is not None:
        return {'date': str(day), 'state': status, 'enqueued': False}
    inserted = execute and store.enqueue(key, 'daily', {
        'scheduled_for': when.isoformat(), 'recovery_window_end': when.isoformat()})
    return {'date': str(day), 'state': 'queued' if inserted else 'preview', 'enqueued': bool(inserted)}


def history(session, kind, container, start=None, end=None):
    params = {'container_id_type': kind, 'container_id': container,
              'sort_type': 'ByCreateTimeAsc', 'page_size': 50}
    if kind == 'chat':
        params.update(start_time=str(int(start.timestamp())), end_time=str(int(end.timestamp())))
    rows = []
    for _ in range(20):
        data = api(session, 'GET', '/im/v1/messages', params=params).get('data')
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise CheckError('Invalid message history')
        rows.extend(data['items'])
        if not data.get('has_more'):
            return rows
        token = data.get('page_token')
        if not isinstance(token, str) or not token or token == params.get('page_token'):
            raise CheckError('Invalid history pagination')
        params['page_token'] = token
    raise CheckError('History scan capped; nothing replayed')


def incoming_from_history(message, chat_id, bot_open_id, start, end):
    if message.get('deleted'):
        return None
    sender = message.get('sender') or {}
    if sender.get('sender_type') != 'user' or sender.get('id_type') != 'open_id':
        return None
    times = []
    for field in ('create_time', 'update_time'):
        try:
            times.append(float(message.get(field, '')) / 1000)
        except (ValueError, TypeError):
            pass
    if not any(start.timestamp() <= value <= end.timestamp() for value in times):
        return None
    bot_mentions = []
    for mention in message.get('mentions') or []:
        identity = mention.get('id')
        identity = identity.get('open_id') if isinstance(identity, dict) else identity
        if identity == bot_open_id:
            bot_mentions.append(mention)
    if not bot_mentions:
        return None
    try:
        content = json.loads((message.get('body') or {}).get('content', '{}'))
    except (TypeError, ValueError):
        return None
    if message.get('msg_type') == 'text':
        text = content.get('text', '')
    elif message.get('msg_type') == 'post':
        post = content.get('zh_cn') or content.get('en_us') or content
        text = '\n'.join([str(post.get('title') or ''), *[
            ''.join(str(element.get('text') or element.get('href') or '') for element in row
                    if element.get('tag') in {'text', 'a'}) for row in post.get('content', [])]])
    else:
        return None
    if not isinstance(text, str):
        return None
    for mention in bot_mentions:
        if mention.get('key'):
            text = text.replace(mention['key'], '')
    text = text.strip()
    message_id, sender_id = message.get('message_id'), sender.get('id')
    if not text or not isinstance(message_id, str) or not message_id.startswith('om_') or not sender_id:
        return None
    return IncomingMessage(message_id, chat_id, text[:10000], 'group', sender_id,
        str(message.get('thread_id') or ''), str(message.get('parent_id') or message.get('root_id') or ''))


def recover_mentions(store, settings, chat_id, start, end, *, execute=False, now=None):
    now = now or datetime.now(UTC)
    if chat_id not in settings.research_group_chat_ids:
        raise ValueError('Chat is outside the existing research allowlist')
    if start.tzinfo is None or end.tzinfo is None or not now - timedelta(days=7) <= start < end <= now:
        raise ValueError('Recovery requires a bounded past window within seven days')
    session = requests.Session()
    try:
        auth = api(session, 'POST', '/auth/v3/tenant_access_token/internal',
                   json={'app_id': settings.feishu_app_id, 'app_secret': settings.feishu_app_secret})
        token = auth.get('tenant_access_token')
        if not isinstance(token, str) or not token:
            raise CheckError('Bot authentication unavailable')
        session.headers['Authorization'] = 'Bearer ' + token
        info = api(session, 'GET', '/bot/v3/info')
        bot_id = (info.get('bot') or {}).get('open_id')
        if not isinstance(bot_id, str) or not bot_id.startswith('ou_'):
            raise CheckError('Cannot verify bot identity')
        # Include recently created older roots to find outage replies in threads.
        roots = history(session, 'chat', chat_id, start - timedelta(days=7), end)
        threads = {m['thread_id'] for m in roots if m.get('thread_id')}
        if len(threads) > 100:
            raise CheckError('Thread scan capped; nothing replayed')
        rows = list(roots)
        for thread in sorted(threads):
            rows.extend(history(session, 'thread', thread))
    finally:
        session.close()
    candidates = {}
    def created(row):
        try:
            return int(row.get('create_time', 0))
        except (TypeError, ValueError):
            return 0
    rows.sort(key=created)
    for row in rows:
        incoming = incoming_from_history(row, chat_id, bot_id, start, end)
        if incoming:
            candidates.setdefault(incoming.message_id, incoming)
    queued, existing, failed = 0, 0, []
    for incoming in candidates.values():
        key = f'message:{incoming.message_id}'
        status = store.job_status(key)
        if status is not None:
            existing += 1
            if status == 'failed':
                failed.append(incoming.message_id)
            continue  # Never resurrect old answered/failed requests blindly.
        if execute:
            queued += store.enqueue(key, 'message', incoming.__dict__)
    return {'matched_mentions': len(candidates), 'existing_jobs': existing, 'queued': queued,
            'failed_message_ids_for_review': failed, 'threads_checked': len(threads),
            'older_threads_not_scanned': True, 'preview': not execute}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    daily = sub.add_parser('daily')
    daily.add_argument('--date', required=True)
    mentions = sub.add_parser('mentions')
    for name in ('chat', 'start', 'end'):
        mentions.add_argument('--' + name, required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings.db_path)
    try:
        if args.command == 'daily':
            result = recover_daily(store, settings, date.fromisoformat(args.date), execute=args.execute)
        else:
            result = recover_mentions(store, settings, args.chat, datetime.fromisoformat(args.start),
                                      datetime.fromisoformat(args.end), execute=args.execute)
    except (CheckError, ValueError) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
