from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup, Tag

from .models import Episode, Transcript

SEQUOIA_HOST = "sequoiacap.com"
URL_RE = re.compile(r"https?://[^\s<>\"]+")
WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:[\N{RIGHT SINGLE QUOTATION MARK}'-][A-Za-z0-9]+)*"
)
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCKED_SELECTORS = (
    ".content-gate-obscure",
    "[data-testid='paywall']",
    "[class*='paywall']",
)
BLOCKED_MARKERS = (
    "[insert intro here]",
    "[insert transcript here]",
    "access the full transcript",
    "subscribe to continue reading",
    "sign in to read the rest",
    "log in or register",
    "this post is for paid subscribers",
    "transcript coming soon",
    "transcript to come",
)


def _safe_site_url(raw_url: str, *, episode_only: bool = False) -> str | None:
    """Return a canonical URL only when it stays on Sequoia's HTTPS origin."""

    cleaned = html.unescape(raw_url).strip().rstrip(".,，。;；:：)]}）】>'\"")
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").lower() != SEQUOIA_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
    ):
        return None
    path = parts.path or "/"
    if episode_only and not (
        path.startswith("/podcast/") and path.rstrip("/") != "/podcast"
    ):
        return None
    if path != "/":
        path = path.rstrip("/")
    netloc = SEQUOIA_HOST if port is None else f"{SEQUOIA_HOST}:443"
    return urlunsplit(("https", netloc, path, "", ""))


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", unquote(value)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _duration_seconds(episode: Episode) -> float | None:
    if episode.duration_seconds is not None:
        try:
            value = float(episode.duration_seconds)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    value = str(episode.duration_string or "").strip()
    if not value:
        return None
    try:
        parts = [float(part) for part in value.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        minutes, seconds = parts
        duration = minutes * 60 + seconds
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        duration = hours * 3600 + minutes * 60 + seconds
    else:
        return None
    return duration if duration > 0 else None


def _parse_iso_duration(value: str) -> float | None:
    match = ISO_DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    seconds = (
        int(match.group("days") or 0) * 86400
        + int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
        + float(match.group("seconds") or 0)
    )
    return seconds if seconds > 0 else None


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


class SequoiaOfficialTranscriptProvider:
    """Fetch and verify public episode transcripts hosted by Sequoia Capital."""

    name = "Sequoia official transcript"
    sitemap_url = "https://sequoiacap.com/sitemap.xml"
    minimum_words_per_minute = 70.0
    maximum_words_per_minute = 220.0

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._sitemap_cache: tuple[float, list[str]] | None = None

    def supports_url(self, url: str) -> bool:
        return _safe_site_url(url, episode_only=True) is not None

    def _get(self, url: str) -> requests.Response:
        current = _safe_site_url(url)
        if current is None:
            raise ValueError("Only HTTPS URLs on sequoiacap.com are allowed")
        for _ in range(4):
            response = requests.get(
                current,
                headers={"User-Agent": "NewsOfficerBot/0.2 (+transcript verification)"},
                timeout=self.timeout,
                allow_redirects=False,
            )
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                redirected = _safe_site_url(urljoin(current, location or ""))
                if location is None or redirected is None:
                    raise ValueError(
                        "Sequoia page redirected outside the allowed origin"
                    )
                current = redirected
                continue
            response.raise_for_status()
            final_url = getattr(response, "url", None)
            if (
                isinstance(final_url, str)
                and final_url
                and _safe_site_url(final_url) is None
            ):
                raise ValueError("Response URL is outside the allowed origin")
            if len(response.content) > 12_000_000:
                raise ValueError("Official transcript page is unexpectedly large")
            return response
        raise ValueError("Too many redirects while reading the Sequoia page")

    def _sitemap_episode_urls(self) -> list[str]:
        now = time.monotonic()
        if self._sitemap_cache and now - self._sitemap_cache[0] < 600:
            return self._sitemap_cache[1]
        root = ElementTree.fromstring(self._get(self.sitemap_url).content)
        urls: list[str] = []
        seen: set[str] = set()
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                continue
            safe_url = _safe_site_url(element.text.strip(), episode_only=True)
            if safe_url and safe_url not in seen:
                seen.add(safe_url)
                urls.append(safe_url)
        self._sitemap_cache = (now, urls)
        return urls

    @staticmethod
    def _title_score(episode_title: str, url: str) -> float:
        target = _normalized_title(episode_title)
        slug = _normalized_title(
            urlsplit(url).path.rsplit("/", 1)[-1].replace("-", " ")
        )
        if not target or not slug:
            return 0.0
        sequence_score = SequenceMatcher(None, target, slug).ratio()
        target_words = set(target.split())
        slug_words = set(slug.split())
        coverage = len(target_words & slug_words) / len(slug_words)
        return sequence_score * 0.65 + coverage * 0.35

    def _official_url(self, episode: Episode) -> str | None:
        direct = _safe_site_url(episode.url, episode_only=True)
        if direct:
            return direct
        description = str(episode.metadata.get("description") or "")
        for raw_url in URL_RE.findall(description):
            safe_url = _safe_site_url(raw_url, episode_only=True)
            if safe_url:
                return safe_url
        if "sequoia" not in episode.show.casefold():
            return None
        matches = sorted(
            (
                (self._title_score(episode.title, url), url)
                for url in self._sitemap_episode_urls()
            ),
            reverse=True,
        )
        if not matches or matches[0][0] < 0.62:
            return None
        if (
            len(matches) > 1
            and matches[0][0] < 0.90
            and matches[0][0] - matches[1][0] < 0.03
        ):
            return None
        return matches[0][1]

    @staticmethod
    def _speaker_labels(container: Tag) -> set[str]:
        speakers: set[str] = set()
        for paragraph in container.find_all("p"):
            label_tag = paragraph.find(["strong", "b"])
            if label_tag is None:
                continue
            label = label_tag.get_text(" ", strip=True)
            full_text = paragraph.get_text(" ", strip=True)
            name = label[:-1].strip() if label.endswith(":") else ""
            if (
                full_text.startswith(label)
                and 1 <= len(name) <= 80
                and len(name.split()) <= 10
                and re.search(r"[A-Za-z]", name)
                and not re.search(r"[.!?]", name)
            ):
                speakers.add(" ".join(name.casefold().split()))
        return speakers

    @classmethod
    def _extract_transcript(cls, soup: BeautifulSoup) -> tuple[str, set[str]] | None:
        container = soup.select_one("#podcast-transcript")
        if container is None:
            return None
        if any(soup.select_one(selector) is not None for selector in BLOCKED_SELECTORS):
            return None
        blocks: list[str] = []
        for block in container.find_all(["h2", "h3", "h4", "p", "li"]):
            text = block.get_text(" ", strip=True)
            if text:
                blocks.append(text)
        transcript = "\n".join(blocks).strip()
        lowered_page = soup.get_text(" ", strip=True).casefold()
        lowered_transcript = transcript.casefold()
        if not transcript or any(
            marker in lowered_transcript or marker in lowered_page
            for marker in BLOCKED_MARKERS
        ):
            return None
        speakers = cls._speaker_labels(container)
        return transcript, speakers

    @staticmethod
    def _metadata_from_page(url: str, soup: BeautifulSoup) -> Episode:
        title_meta = soup.select_one("meta[property='og:title']")
        title = str(title_meta.get("content") or "").strip() if title_meta else ""
        if not title:
            heading = soup.find("h1") or soup.find("title")
            title = heading.get_text(" ", strip=True) if heading else "Sequoia Podcast"
        title = re.sub(
            r"\s*[|\N{EN DASH}\N{EM DASH}-]\s*Sequoia Capital\s*$", "", title
        ).strip()

        description_meta = soup.select_one("meta[name='description']")
        description = (
            str(description_meta.get("content") or "").strip()
            if description_meta
            else ""
        )
        published_at = None
        published_meta = soup.select_one("meta[property='article:published_time']")
        if published_meta and published_meta.get("content"):
            published_at = _parse_datetime(str(published_meta["content"]))

        duration = None
        duration_meta = soup.select_one("meta[property='og:video:duration']")
        if duration_meta and duration_meta.get("content"):
            try:
                duration = float(str(duration_meta["content"]))
            except ValueError:
                pass
        if duration is None:
            for script in soup.find_all(
                "script", attrs={"type": "application/ld+json"}
            ):
                try:
                    data = json.loads(script.string or "")
                except (TypeError, json.JSONDecodeError):
                    continue
                records = data if isinstance(data, list) else [data]
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    if published_at is None and record.get("datePublished"):
                        published_at = _parse_datetime(str(record["datePublished"]))
                    if record.get("duration"):
                        duration = _parse_iso_duration(str(record["duration"]))
                    if duration is not None:
                        break
                if duration is not None:
                    break

        youtube_url = ""
        for iframe in soup.find_all("iframe", src=True):
            source = str(iframe.get("src") or "")
            match = re.search(
                r"^https://(?:www\.)?youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{6,20})",
                source,
            )
            if match:
                youtube_url = f"https://www.youtube.com/watch?v={match.group(1)}"
                break

        slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        return Episode(
            id=f"sequoia:{slug}",
            title=title,
            url=url,
            show="Sequoia Capital",
            duration_seconds=duration,
            published_at=published_at,
            metadata={
                "description": description,
                "official_url": url,
                "youtube_url": youtube_url,
            },
        )

    def episode_from_url(self, url: str) -> Episode | None:
        safe_url = _safe_site_url(url, episode_only=True)
        if safe_url is None:
            return None
        soup = BeautifulSoup(self._get(safe_url).content, "html.parser")
        return self._metadata_from_page(safe_url, soup)

    def fetch(self, episode: Episode) -> Transcript | None:
        official_url = self._official_url(episode)
        if official_url is None:
            return None
        soup = BeautifulSoup(self._get(official_url).content, "html.parser")
        extracted = self._extract_transcript(soup)
        if extracted is None:
            return None
        text, speakers = extracted
        if len(speakers) < 2:
            return None
        duration = _duration_seconds(episode)
        if duration is None:
            return None
        word_count = len(WORD_RE.findall(text))
        words_per_minute = word_count / (duration / 60.0)
        if not (
            self.minimum_words_per_minute
            <= words_per_minute
            <= self.maximum_words_per_minute
        ):
            return None
        return Transcript(
            text=text,
            source=self.name,
            source_url=official_url,
            verified_complete=True,
        )
