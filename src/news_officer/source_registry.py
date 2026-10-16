"""Durable, validated podcast subscriptions shared by discovery and the agent."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from .rss import MAX_FEED_BYTES, _response, latest_rss_episodes
from .source_matching import normalize_rss_url


class SourceRegistry:
    def __init__(self, store, feeds_path: Path):
        self.store = store
        self.feeds_path = feeds_path

    def initialize(self):
        with self.store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS agent_sources (
                id TEXT PRIMARY KEY, source_json TEXT NOT NULL,
                added_by TEXT NOT NULL, added_at TEXT NOT NULL)""")

    def list(self) -> list[dict]:
        configured = json.loads(self.feeds_path.read_text())
        sources = configured.get("sources", [])
        if "youtube_channels" in configured:
            sources = [
                {"name": u, "type": "youtube", "url": u}
                for u in configured["youtube_channels"]
            ]
        result = [
            dict(s, origin="configured") for s in sources if s.get("enabled", True) and s.get("type") != "text"
        ]
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT source_json FROM agent_sources ORDER BY added_at, id"
            ).fetchall()
        result.extend(dict(json.loads(r[0]), origin="conversation") for r in rows)
        return result

    def find(self, query: str) -> list[dict]:
        """Name discovery only; a returned RSS must still pass validation."""
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
            raise ValueError("请提供简短的播客名称")
        response = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": query.strip(),
                "entity": "podcast",
                "limit": 8,
            },
            timeout=20,
        )
        response.raise_for_status()
        return [
            {
                "name": x.get("collectionName", ""),
                "publisher": x.get("artistName", ""),
                "rss_url": x["feedUrl"],
                "page_url": x.get("collectionViewUrl", ""),
            }
            for x in response.json().get("results", [])
            if x.get("feedUrl")
        ]

    def validate(self, url: str) -> dict:
        # Reuses the existing redirect-by-redirect public HTTPS checks and size cap.
        response = _response(url, timeout=20, max_bytes=MAX_FEED_BYTES)
        root = ElementTree.fromstring(response.content)
        channel = root.find("channel")
        if channel is None or not channel.findall("item"):
            raise ValueError("该地址不是含节目的 RSS，请提供播客 RSS 地址或节目名称")
        title = (channel.findtext("title") or "").strip()
        if not title or not any(
            x.find("enclosure") is not None for x in channel.findall("item")
        ):
            raise ValueError("该 RSS 缺少节目名称或音频条目")
        return {
            "name": title[:200],
            "type": "rss",
            "rss_url": url,
            "url": "",
            "priority": "B",
            "scan_depth": 30,
            "enabled": True,
        }

    def add(self, source: dict, actor: str) -> dict:
        url = source["rss_url"]
        # Only accept the canonical source object returned by validation, not arbitrary config.
        source = self.validate(url)
        for existing in self.list():
            if normalize_rss_url(existing.get("rss_url", "")) == normalize_rss_url(url):
                return {"status": "already_tracked", "source": existing}
        identity = hashlib.sha256(url.encode()).hexdigest()
        with self.store._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO agent_sources VALUES (?, ?, ?, ?)",
                (
                    identity,
                    json.dumps(source, ensure_ascii=False),
                    actor,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return {
            "status": "added",
            "source": source,
            "note": "已加入云端每日检查；取得完整文字稿后才摘要。",
        }

    def recent(self, days: int, name: str = "") -> dict:
        if type(days) is not int or not 1 <= days <= 31:
            raise ValueError("时间范围须为 1–31 天")
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=days)
        result, failures, checked = [], [], []
        fetch_statuses = []
        for source in self.list():
            if name and name.casefold() not in source["name"].casefold():
                continue
            checked.append(source["name"])
            feed = source.get("rss_url")
            if not feed:
                failures.append(
                    {
                        "source": source["name"],
                        "reason": "该源没有 RSS，尚未检查视频更新",
                    }
                )
                continue
            try:
                episodes = latest_rss_episodes(source["name"], feed, limit=100)
                fetch_statuses.append({
                    "source_name": source["name"], "url": feed,
                    "source_type": source.get("type", "rss"), "status": "ok", "reason": ""})
                for ep in episodes:
                    if ep.published_at and cutoff <= ep.published_at <= now:
                        keywords = source.get("include_keywords", [])
                        if keywords and not any(
                            k.casefold() in ep.title.casefold() for k in keywords
                        ):
                            continue
                        result.append(
                            {
                                "title": ep.title,
                                "show": ep.show,
                                "url": ep.url,
                                "published_at": ep.published_at.isoformat(),
                                "_episode": ep.to_persisted_dict(),
                                "_metadata": ep.metadata,
                            }
                        )
                if len(episodes) >= 100 and all(
                    ep.published_at is None or ep.published_at >= cutoff
                    for ep in episodes
                ):
                    failures.append(
                        {
                            "source": source["name"],
                            "reason": "超过单源 100 条扫描上限，可能不完整",
                        }
                    )
            except Exception as error:  # noqa: BLE001 - one unavailable feed must not hide other updates
                response = getattr(error, "response", None)
                reason = f"HTTP {response.status_code}" if response is not None else str(error)
                fetch_statuses.append({
                    "source_name": source["name"], "url": feed,
                    "source_type": source.get("type", "rss"), "status": "failed", "reason": reason})
                failures.append(
                    {
                        "source": source["name"],
                        "reason": "RSS 获取失败，不能视作没有更新",
                    }
                )
        if fetch_statuses:
            self.store.save_fetch_status(now.isoformat(), fetch_statuses)
        return {
            "from": cutoff.isoformat(),
            "to": now.isoformat(),
            "checked": checked,
            "episodes": sorted(result, key=lambda x: x["published_at"], reverse=True),
            "failures": failures,
            "note": "更新目录来自 RSS 元数据，不是内容摘要或完整文字稿证明。",
        }

    def search_episodes(self, query: str, days: int = 90, show: str = "") -> dict:
        """Find episodes, not shows, without changing subscriptions or the daily window.

        Match publisher metadata before truncating results. A low-frequency guest
        must not disappear behind the newest 60 episodes across all feeds.
        """
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
            raise ValueError("Use a short company, guest or topic query")
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("Search window must be 1–365 days")

        def normalize(value):
            return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                           if not unicodedata.combining(c))

        words = list(dict.fromkeys(re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', normalize(query))))
        if not words:
            raise ValueError("Query needs searchable terms")
        patterns = [re.compile(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])') for w in words]
        sources = [s for s in self.list() if not show or show.casefold() in s['name'].casefold()]
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=days)

        def scan(source):
            if not source.get('rss_url'):
                return [], {'source': source['name'], 'reason': '未配置 RSS，未检查'}, False, None
            try:
                episodes = latest_rss_episodes(source['name'], source['rss_url'], limit=100)
            except Exception as error:  # noqa: BLE001 - never confuse provider failure with no match
                response = getattr(error, 'response', None)
                reason = f'HTTP {response.status_code}' if response is not None else str(error)
                return [], {'source': source['name'], 'reason': 'RSS 获取失败，不能视作没有匹配节目'}, False, reason
            matches = []
            for ep in episodes:
                if ep.published_at and not cutoff <= ep.published_at <= now:
                    continue
                title = normalize(ep.title)
                description = normalize(str(ep.metadata.get('description', '')))
                title_hits = sum(bool(p.search(title)) for p in patterns)
                hits = sum(bool(p.search(title + ' ' + description)) for p in patterns)
                if hits != len(patterns):
                    continue
                matches.append({
                    'title': ep.title, 'show': ep.show, 'url': ep.url,
                    'published_at': ep.published_at.isoformat() if ep.published_at else None,
                    'duration_seconds': ep.duration_seconds,
                    'description': str(ep.metadata.get('description', ''))[:1600],
                    'match_basis': 'publisher_title' if title_hits == hits else 'publisher_description',
                    '_score': title_hits * 4 + hits,
                    '_episode': ep.to_persisted_dict(), '_metadata': ep.metadata,
                })
            return matches, None, len(episodes) >= 100, None

        entries, failures, capped = [], [], []
        fetch_statuses = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for source, (matches, failure, limited, fetch_error) in zip(sources, pool.map(scan, sources)):
                if source.get("rss_url"):
                    fetch_statuses.append({
                        "source_name": source["name"], "url": source["rss_url"],
                        "source_type": source.get("type", "rss"),
                        "status": "failed" if fetch_error is not None else "ok",
                        "reason": fetch_error or ""})
                entries.extend(matches)
                if failure:
                    failures.append(failure)
                if limited:
                    capped.append(source['name'])
        if fetch_statuses:
            self.store.save_fetch_status(now.isoformat(), fetch_statuses)
        entries.sort(key=lambda x: (x['_score'], x['published_at'] or ''), reverse=True)
        unique = {entry['_episode']['id']: entry for entry in entries}
        selected = list(unique.values())[:12]
        for entry in selected:
            entry.pop('_score', None)
        return {'query': query, 'days': days, 'checked': [s['name'] for s in sources],
                'episodes': selected, 'total_matches': len(unique), 'failures': failures,
                'scan_limit_sources': capped,
                'note': '仅检索已追踪 RSS，每源至多100期。元数据用于定位，不是全文；未命中不代表全网不存在。'}


def valid_feed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname) and not parts.username
    except ValueError:
        return False
