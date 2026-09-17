"""Durable, validated podcast subscriptions shared by discovery and the agent."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from .rss import MAX_FEED_BYTES, _response, latest_rss_episodes


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
            dict(s, origin="configured") for s in sources if s.get("enabled", True)
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
            if existing.get("rss_url", "").rstrip("/") == url.rstrip("/"):
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
            except Exception:  # noqa: BLE001 - one unavailable feed must not hide other updates
                failures.append(
                    {
                        "source": source["name"],
                        "reason": "RSS 获取失败，不能视作没有更新",
                    }
                )
        return {
            "from": cutoff.isoformat(),
            "to": now.isoformat(),
            "checked": checked,
            "episodes": sorted(result, key=lambda x: x["published_at"], reverse=True),
            "failures": failures,
            "note": "更新目录来自 RSS 元数据，不是内容摘要或完整文字稿证明。",
        }


def valid_feed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname) and not parts.username
    except ValueError:
        return False
