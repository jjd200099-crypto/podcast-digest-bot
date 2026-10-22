#!/usr/bin/env python3
import json, os, re, sys
from datetime import datetime, timedelta, timezone
import requests

ZONE = timezone(timedelta(hours=8))
day_s = os.environ.get("WATCH_DATE", "").strip() or datetime.now(ZONE).date().isoformat()
day = datetime.strptime(day_s, "%Y-%m-%d").date()
chat = os.environ["NEWS_OFFICER_WATCH_CHAT_ID"].strip()
app_id = os.environ["FEISHU_APP_ID"].strip()
secret = os.environ["FEISHU_APP_SECRET"].strip()
start = datetime.combine(day, datetime.strptime("08:00", "%H:%M").time(), ZONE)
s = requests.Session()
tok = s.post(
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
    json={"app_id": app_id, "app_secret": secret},
    timeout=30,
).json()
token = tok.get("tenant_access_token")
if not token:
    print(json.dumps({"status": "check_failed", "error": "token", "raw_code": tok.get("code")}, ensure_ascii=False))
    sys.exit(1)
s.headers["Authorization"] = f"Bearer {token}"
params = {
    "container_id_type": "chat",
    "container_id": chat,
    "start_time": str(int(start.timestamp())),
    "sort_type": "ByCreateTimeAsc",
    "page_size": 50,
    "card_msg_content_type": "raw_card_content",
}
items = []
for _ in range(20):
    r = s.get("https://open.feishu.cn/open-apis/im/v1/messages", params=params, timeout=30).json()
    if r.get("code") != 0:
        print(json.dumps({"status": "check_failed", "error": r.get("msg"), "code": r.get("code")}, ensure_ascii=False))
        sys.exit(1)
    data = r.get("data") or {}
    items.extend(data.get("items") or [])
    if not data.get("has_more"):
        break
    params["page_token"] = data.get("page_token")

def texts(v):
    if isinstance(v, str):
        yield v
        if v.lstrip().startswith(("{", "[")):
            try:
                yield from texts(json.loads(v))
            except Exception:
                return
    elif isinstance(v, dict):
        for x in v.values():
            yield from texts(x)
    elif isinstance(v, list):
        for x in v:
            yield from texts(x)

rows = []
for m in items:
    try:
        ct = int(m.get("create_time", "0")) / 1000
    except Exception:
        ct = 0
    tstr = datetime.fromtimestamp(ct, ZONE).strftime("%H:%M:%S")
    sender = m.get("sender") or {}
    try:
        body = json.loads((m.get("body") or {}).get("content") or "{}")
    except Exception:
        body = {"raw": (m.get("body") or {}).get("content")}
    blob = "\n".join(texts(body))
    rows.append({
        "message_id": m.get("message_id"),
        "time": tstr,
        "sender_type": sender.get("sender_type"),
        "sender_id": sender.get("id"),
        "msg_type": m.get("msg_type"),
        "updated": bool(m.get("update_time") and m.get("update_time") != m.get("create_time")),
        "title_hits": re.findall(r"🎧 播客精选 · [\d-]+|情报官日报｜[\d-]+｜\d+ 期", blob)[:5],
        "has_deepread": bool(re.search(r"精读和完整中文对谈|精读", blob)),
        "has_listen": bool(re.search(r"收听", blob)),
        "has_release_notes": bool(re.search(r"功能更新|更新说明|版本更新", blob)),
        "episode_titles": re.findall(r"【[^】]{4,80}】", blob)[:12],
        "docx_links": len(re.findall(r"feishu\.cn/(?:docx|wiki|docs)", blob)),
        "preview": blob[:500].replace("\n", " / "),
    })

dailies = [
    r for r in rows
    if r["sender_type"] == "app" and r["sender_id"] == app_id and (
        any(("播客精选 · " + day.isoformat()) in h or ("情报官日报｜" + day.isoformat()) in h for h in r["title_hits"])
        or (("播客精选 · " + day.isoformat()) in r["preview"])
    )
]
if not dailies:
    dailies = [
        r for r in rows
        if r["sender_type"] == "app" and r["sender_id"] == app_id
        and day.isoformat() in r["preview"]
        and ("播客精选" in r["preview"] or "情报官日报" in r["preview"])
    ]

status = "ok"
notes = []
if len(dailies) == 0:
    status, notes = "fail", ["no_daily"]
elif len(dailies) > 1:
    status, notes = "fail", ["multiple_dailies"]
else:
    d = dailies[0]
    if not d["episode_titles"]:
        notes.append("no_episode_titles_parsed")
    if not d["has_deepread"]:
        notes.append("deepread_missing_maybe_pending")
        status = "pending"
    if not d["has_listen"]:
        notes.append("listen_missing")
        status = "fail"
    if d["has_release_notes"]:
        notes.append("has_release_notes")
        status = "fail"

out = {
    "date": day.isoformat(),
    "status": status,
    "msg_count_after_08": len(rows),
    "daily_count": len(dailies),
    "notes": notes,
    "messages": [
        {k: r[k] for k in ("message_id", "time", "sender_type", "msg_type", "updated", "title_hits", "has_deepread", "has_listen", "has_release_notes", "episode_titles", "docx_links")}
        for r in rows
    ],
    "daily": (
        {k: dailies[0][k] for k in ("message_id", "time", "updated", "title_hits", "has_deepread", "has_listen", "has_release_notes", "episode_titles", "docx_links", "preview")}
        if dailies else None
    ),
}
print(json.dumps(out, ensure_ascii=False, indent=2))
sys.exit(0 if status in ("ok", "pending") else 1)
