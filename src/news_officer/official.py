from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup, Tag

from .models import Episode, Transcript

URL_RE = re.compile(r"https?://[^\s<>\"]+")
TIMESTAMP_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
DWARKESH_HOSTS = {"dwarkesh.com", "www.dwarkesh.com"}


def _safe_canonical_url(raw_url: str, allowed_hosts: set[str]) -> str | None:
    cleaned = html.unescape(raw_url).rstrip(".,，。)]）'\"")
    parts = urlsplit(cleaned)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed_hosts:
        return None
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", ""))


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).lower()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _timestamp_seconds(value: str) -> int | None:
    match = TIMESTAMP_RE.search(value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))


class DwarkeshOfficialTranscriptProvider:
    """Reads the public, explicitly labelled transcript on dwarkesh.com."""

    name = "Dwarkesh official transcript"
    feed_url = "https://www.dwarkesh.com/feed"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._feed_cache: tuple[float, list[tuple[str, str]]] | None = None

    def supports_url(self, url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and (parts.hostname or "").lower() in DWARKESH_HOSTS
            and parts.path.startswith("/p/")
        )

    def _get(self, url: str) -> requests.Response:
        safe_url = _safe_canonical_url(url, DWARKESH_HOSTS)
        if safe_url is None:
            raise ValueError("Refusing a non-Dwarkesh transcript URL")
        response = requests.get(
            safe_url,
            headers={"User-Agent": "NewsOfficerBot/0.2 (+transcript verification)"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        if _safe_canonical_url(response.url, DWARKESH_HOSTS) is None:
            raise ValueError("Dwarkesh redirected outside the official domain")
        if len(response.content) > 12_000_000:
            raise ValueError("Official transcript page is unexpectedly large")
        return response

    def _feed_items(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        if self._feed_cache and now - self._feed_cache[0] < 600:
            return self._feed_cache[1]
        response = self._get(self.feed_url)
        root = ElementTree.fromstring(response.content)
        items: list[tuple[str, str]] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            safe_link = _safe_canonical_url(link, DWARKESH_HOSTS)
            if title and safe_link:
                items.append((title, safe_link))
        self._feed_cache = (now, items)
        return items

    def _official_url(self, episode: Episode) -> str | None:
        description = str(episode.metadata.get("description") or "")
        for raw_url in URL_RE.findall(description):
            safe_url = _safe_canonical_url(raw_url, DWARKESH_HOSTS)
            if safe_url and "/p/" in urlsplit(safe_url).path:
                return safe_url
        show = episode.show.lower()
        if "dwarkesh" not in show and "dwarkesh" not in episode.title.lower():
            return None
        target = _normalized_title(episode.title)
        matches = [
            (SequenceMatcher(None, target, _normalized_title(title)).ratio(), link)
            for title, link in self._feed_items()
        ]
        if not matches:
            return None
        score, link = max(matches)
        return link if score >= 0.55 else None

    @staticmethod
    def _metadata_from_page(url: str, soup: BeautifulSoup) -> Episode:
        title = "Dwarkesh Podcast"
        published_at = None
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            if data.get("@type") not in {"NewsArticle", "Article"}:
                continue
            title = str(data.get("headline") or title)
            if data.get("datePublished"):
                try:
                    published_at = datetime.fromisoformat(
                        str(data["datePublished"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            break
        episode_id = f"dwarkesh:{urlsplit(url).path.removeprefix('/p/')}"
        return Episode(
            id=episode_id,
            title=title,
            url=url,
            show="Dwarkesh Podcast",
            published_at=published_at,
        )

    def episode_from_url(self, url: str) -> Episode | None:
        safe_url = _safe_canonical_url(url, DWARKESH_HOSTS)
        if not safe_url or not self.supports_url(safe_url):
            return None
        soup = BeautifulSoup(self._get(safe_url).text, "html.parser")
        return self._metadata_from_page(safe_url, soup)

    @staticmethod
    def _extract_transcript(soup: BeautifulSoup) -> tuple[str, list[int]] | None:
        body = soup.select_one("div.body.markup") or soup.select_one(
            ".available-content"
        )
        if body is None:
            return None
        heading = next(
            (
                tag
                for tag in body.find_all(["h2", "h3"], recursive=True)
                if tag.get_text(" ", strip=True).strip().lower() == "transcript"
            ),
            None,
        )
        if heading is None or heading.parent is not body:
            return None
        lines: list[str] = []
        timestamps: list[int] = []
        for sibling in heading.find_next_siblings():
            if not isinstance(sibling, Tag):
                continue
            text = sibling.get_text(" ", strip=True)
            if not text:
                continue
            if sibling.name == "h2":
                break
            if text.lower() in {"ready for more?", "subscribe", "share"}:
                break
            if sibling.name not in {"h3", "h4", "p", "blockquote", "ul", "ol", "pre"}:
                continue
            if sibling.name in {"h3", "h4"}:
                timestamp = _timestamp_seconds(text)
                if timestamp is not None:
                    timestamps.append(timestamp)
            lines.append(text)
        transcript = "\n".join(lines).strip()
        blocked_markers = (
            "access the full transcript",
            "subscribe to continue reading",
            "sign in to read the rest",
        )
        if any(marker in transcript.lower() for marker in blocked_markers):
            return None
        return transcript, timestamps

    def fetch(self, episode: Episode) -> Transcript | None:
        official_url = (
            episode.url
            if self.supports_url(episode.url)
            else self._official_url(episode)
        )
        if not official_url:
            return None
        soup = BeautifulSoup(self._get(official_url).text, "html.parser")
        extracted = self._extract_transcript(soup)
        if extracted is None:
            return None
        text, timestamps = extracted
        duration = float(episode.duration_seconds or 0)
        minimum_chars = max(5_000, round(duration / 60) * 180) if duration else 5_000
        if len(text) < minimum_chars:
            return None
        if duration and len(timestamps) >= 2:
            if timestamps[0] > min(180, duration * 0.10):
                return None
            if duration - timestamps[-1] > max(900, duration * 0.20):
                return None
        return Transcript(
            text=text,
            source=self.name,
            source_url=official_url,
            verified_complete=True,
        )
