"""Independent receipt check, runnable on GitHub even when Railway is stopped.

Read-only by default. --notify sends one private, credential-free failure notice.
No conversation text, source material or tokens are logged or persisted.
"""

import argparse
import json
import os
import re
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE = 'https://open.feishu.cn/open-apis'
ZONE = ZoneInfo('Asia/Shanghai')


class CheckError(RuntimeError):
    pass


def api(session, method, path, **kwargs):
    for attempt in range(3):
        try:
            response = session.request(method, BASE + path, timeout=30, allow_redirects=False, **kwargs)
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise CheckError('Feishu network unavailable') from None
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        if response.status_code != 200:
            raise CheckError(f'Feishu HTTP {response.status_code}')
        try:
            body = response.json()
        except ValueError:
            raise CheckError('Feishu invalid response') from None
        if not isinstance(body, dict) or body.get('code') != 0:
            code = body.get('code') if isinstance(body, dict) else None
            raise CheckError(f'Feishu API code {code if isinstance(code, int) else "unknown"}')
        return body
    raise CheckError('Feishu retries exhausted')  # pragma: no cover


def text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_values(item)


def is_daily_receipt(message, app_id, day, start, end):
    sender = message.get('sender') or {}
    if (message.get('deleted') or sender.get('sender_type') != 'app'
            or sender.get('id_type') != 'app_id' or sender.get('id') != app_id):
        return False
    try:
        created = int(message.get('create_time', '')) / 1000
        body = json.loads((message.get('body') or {}).get('content', '{}'))
    except (ValueError, TypeError):
        return False
    if not start <= created <= end:
        return False
    marker = (rf'^(?:情报官日报｜{re.escape(day.isoformat())}｜\d+ 期'
              rf'|🎧 播客精选 · {re.escape(day.isoformat())})(?:\s|$)')
    return any(re.match(marker, value.strip().lstrip('#* ')) for value in text_values(body))


def check_receipt(session, app_id, chat_id, day, now):
    start = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=8, minute=30)
    end = min(now, start.replace(hour=0, minute=0) + timedelta(days=1))
    if now < start + timedelta(minutes=47):
        return 'not_due'
    params = {'container_id_type': 'chat', 'container_id': chat_id,
              'start_time': str(int(start.timestamp())), 'end_time': str(int(end.timestamp())),
              'sort_type': 'ByCreateTimeDesc', 'page_size': 50}
    for _ in range(20):
        data = api(session, 'GET', '/im/v1/messages', params=params).get('data')
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise CheckError('Feishu history response invalid')
        for message in data['items']:
            if isinstance(message, dict) and is_daily_receipt(message, app_id, day, start.timestamp(), end.timestamp()):
                return 'delivered'
        if not data.get('has_more'):
            return 'missing'
        cursor = data.get('page_token')
        if not isinstance(cursor, str) or not cursor or cursor == params.get('page_token'):
            raise CheckError('Feishu pagination invalid')
        params['page_token'] = cursor
    raise CheckError('History scan limit reached; delivery unknown')


def run_check(app_id, secret, chat_id, owner_id='', *, day=None, now=None, notify=False):
    now = now or datetime.now(UTC)
    day = day or now.astimezone(ZONE).date()
    if notify and day != now.astimezone(ZONE).date():
        raise ValueError('Historical checks cannot send alerts')
    session = requests.Session()
    result = {'date': day.isoformat(), 'status': 'check_failed', 'alert_sent': False}
    try:
        token = api(session, 'POST', '/auth/v3/tenant_access_token/internal',
                    json={'app_id': app_id, 'app_secret': secret}).get('tenant_access_token')
        if not isinstance(token, str) or not token:
            raise CheckError('Feishu token unavailable')
        session.headers['Authorization'] = 'Bearer ' + token
        try:
            result['status'] = check_receipt(session, app_id, chat_id, day, now)
        except CheckError as error:
            result['error'] = str(error)
        if notify and result['status'] in {'missing', 'check_failed'}:
            if not owner_id:
                raise CheckError('Alert recipient not configured')
            text = ('今日日报尚未确认送达，请检查云端服务。' if result['status'] == 'missing'
                    else '日报送达检查失败，无法确认送达，请检查云端服务与飞书读取权限。')
            text += f'\n日期：{day.isoformat()}。这是 GitHub 独立检查，不代表机器人已恢复。'
            receipt = api(session, 'POST', '/im/v1/messages', params={'receive_id_type': 'open_id'},
                json={'receive_id': owner_id, 'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False),
                      'uuid': str(uuid.uuid5(uuid.NAMESPACE_URL, f'news-officer-watchdog:{chat_id}:{day}'))})
            if not (receipt.get('data') or {}).get('message_id'):
                raise CheckError('Alert receipt missing')
            result['alert_sent'] = True
    except CheckError as error:
        result['error'] = str(error)
    finally:
        session.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args()
    values = [os.getenv(key, '').strip() for key in (
        'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'NEWS_OFFICER_WATCH_CHAT_ID', 'NEWS_OFFICER_ALERT_OPEN_ID')]
    if not all(values[:3]):
        print(json.dumps({'status': 'check_failed', 'error': 'Watchdog configuration missing'}))
        return 1
    requested = os.getenv('WATCH_DATE', '').strip()
    try:
        result = run_check(*values, day=date.fromisoformat(requested) if requested else None, notify=args.notify)
    except ValueError:
        result = {'status': 'check_failed', 'error': 'Invalid date or historical alert requested'}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in {'delivered', 'not_due'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
