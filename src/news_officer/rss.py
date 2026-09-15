from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from .models import Episode, Transcript

ITUNES_NAMESPACE = "http://www.itunes.com/dtds/podcast-1.0.dtd"
SUBSTACK_HOSTS = {
    "generalist.com",
    "lennysnewsletter.com",
    "www.generalist.com",
    "www.lennysnewsletter.com",
}
SUBSTACK_CDN_HOSTS = {"substackcdn.com", "www.substackcdn.com"}
TRUSTED_RSS_TRANSCRIPT_HOSTS = {"share.transistor.fm"}
MAX_FEED_BYTES = 20_000_000
MAX_TRANSCRIPT_BYTES = 25_000_000
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "by",
    "ceo",
    "episode",
    "for",
    "from",
    "full",
    "has",
    "how",
    "in",
    "interview",
    "of",
    "on",
    "podcast",
    "re",
    "show",
    "tech",
    "technology",
    "the",
    "to",
    "vc",
    "video",
    "we",
    "with",
    "why",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_duration(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        parts = [float(part) for part in value.split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        seconds = parts[0]
    elif len(parts) == 2:
        seconds = parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
    else:
        return None
    return seconds if seconds > 0 else None


def _parse_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_https_url(value: str) -> str | None:
    try:
        parts = urlsplit(html.unescape(value).strip())
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
    ):
        return None
    return parts.geturl()


def _assert_public_https_url(value: str) -> str:
    """Reject local/private destinations before every network request."""

    safe_url = _safe_https_url(value)
    if safe_url is None:
        raise ValueError("Only public HTTPS feed and transcript URLs are supported")
    hostname = (urlsplit(safe_url).hostname or "").rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        ".localhost"
    ):
        raise ValueError("Local feed and transcript hosts are not supported")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("Private feed and transcript addresses are not supported")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError("Unable to resolve feed or transcript host") from error
    if not addresses:
        raise ValueError("Unable to resolve feed or transcript host")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address[4][0])
        except (IndexError, ValueError):
            raise ValueError("Invalid feed or transcript address") from None
        if not resolved.is_global:
            raise ValueError("Private feed and transcript addresses are not supported")
    return safe_url


def _response(url: str, *, timeout: int, max_bytes: int) -> requests.Response:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        safe_url = _assert_public_https_url(current_url)
        response = requests.get(
            safe_url,
            headers={"User-Agent": "NewsOfficerBot/0.3 (+official-rss)"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in REDIRECT_STATUSES:
            location = str(response.headers.get("Location") or "").strip()
            if not location or redirect_count >= MAX_REDIRECTS:
                response.close()
                raise ValueError("The source returned an invalid redirect chain")
            response.close()
            current_url = urljoin(safe_url, location)
            continue
        response.raise_for_status()
        if hasattr(response, "iter_content"):
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    response.close()
                    raise ValueError("The source response is unexpectedly large")
                chunks.append(chunk)
            # requests.Response.text/json continue to work after the bounded
            # streamed body is materialized here.
            response._content = b"".join(chunks)
            response._content_consumed = True
        elif len(response.content) > max_bytes:
            # Lightweight test doubles may not implement streaming.
            raise ValueError("The source response is unexpectedly large")
        return response
    raise ValueError("The source returned too many redirects")


def latest_rss_episodes(
    show_name: str,
    feed_url: str,
    *,
    limit: int = 4,
    timeout: int = 20,
) -> list[Episode]:
    """Discover dated podcast episodes from the publisher's RSS feed."""

    response = _response(feed_url, timeout=timeout, max_bytes=MAX_FEED_BYTES)
    root = ElementTree.fromstring(response.content)
    items = root.findall(".//item")
    episodes: list[Episode] = []
    for item in items[: max(1, limit)]:
        title = (item.findtext("title") or "").strip()
        published_at = _parse_date(item.findtext("pubDate") or "")
        link = _safe_https_url(item.findtext("link") or "")
        guid = (item.findtext("guid") or "").strip()
        if link is None:
            link = _safe_https_url(guid)
        duration_string = (
            item.findtext(f"{{{ITUNES_NAMESPACE}}}duration") or ""
        ).strip()
        description = ""
        for child in item:
            if _local_name(child.tag) in {"description", "encoded", "summary"}:
                candidate = " ".join(child.itertext()).strip()
                if len(candidate) > len(description):
                    description = candidate
        transcript_urls: list[dict[str, str]] = []
        audio_url = ""
        for child in item.iter():
            local_name = _local_name(child.tag)
            if local_name == "transcript":
                transcript_url = _safe_https_url(child.attrib.get("url", ""))
                if transcript_url:
                    transcript_urls.append(
                        {
                            "url": transcript_url,
                            "type": child.attrib.get("type", "text/plain"),
                            "language": child.attrib.get("language", ""),
                        }
                    )
            elif local_name == "enclosure" and not audio_url:
                audio_url = _safe_https_url(child.attrib.get("url", "")) or ""
        identity = guid or link or f"{title}|{published_at or ''}"
        if not title or not identity:
            continue
        episode_id = "rss:" + hashlib.sha256(
            f"{feed_url}|{identity}".encode()
        ).hexdigest()[:32]
        episodes.append(
            Episode(
                id=episode_id,
                title=title,
                url=link or _safe_https_url(feed_url) or feed_url,
                show=show_name,
                duration_seconds=_parse_duration(duration_string),
                duration_string=duration_string or None,
                published_at=published_at,
                metadata={
                    "audio_url": audio_url,
                    "description": BeautifulSoup(
                        html.unescape(description), "html.parser"
                    ).get_text(" ", strip=True),
                    "rss_feed_url": feed_url,
                    "rss_transcripts": transcript_urls,
                },
            )
        )
    if not episodes:
        raise ValueError("Official RSS feed contains no valid podcast episodes")
    return episodes


def _normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _title_words(value: str) -> set[str]:
    return {
        word
        for word in _normalized_title(value).split()
        if len(word) >= 2 and word not in TITLE_STOPWORDS
    }


def _title_score(first: str, second: str) -> tuple[float, int, float, float]:
    first_words = _title_words(first)
    second_words = _title_words(second)
    if not first_words or not second_words:
        return 0.0, 0, 0.0, 0.0
    overlap = len(first_words & second_words)
    containment = overlap / min(len(first_words), len(second_words))
    jaccard = overlap / len(first_words | second_words)
    score = containment * 0.65 + jaccard * 0.35
    return score, overlap, containment, jaccard


def _confident_episode_match(rss: Episode, youtube: Episode) -> float | None:
    """Return a high-confidence identity score, never a fuzzy topical guess."""

    title_score, overlap, containment, jaccard = _title_score(
        rss.title, youtube.title
    )
    rss_title = _normalized_title(rss.title)
    youtube_title = _normalized_title(youtube.title)
    exact = rss_title == youtube_title
    contained_title = (
        len(_title_words(rss.title)) >= 4
        and (rss_title in youtube_title or youtube_title in rss_title)
    )
    if not exact and not (
        overlap >= 4
        and containment >= 0.80
        and (jaccard >= 0.50 or contained_title)
    ):
        return None

    has_date_evidence = False
    if rss.published_at and youtube.published_at:
        date_delta = abs(
            (rss.published_at.astimezone(UTC) - youtube.published_at.astimezone(UTC))
            .total_seconds()
        )
        if date_delta > 48 * 3600:
            return None
        has_date_evidence = True
        title_score += 0.08
    elif not exact and not contained_title:
        return None

    has_duration_evidence = False
    if rss.duration_seconds and youtube.duration_seconds:
        duration_ratio = min(rss.duration_seconds, youtube.duration_seconds) / max(
            rss.duration_seconds, youtube.duration_seconds
        )
        if duration_ratio < 0.82:
            return None
        has_duration_evidence = True
        title_score += 0.08
    elif not exact and not (
        (has_date_evidence and overlap >= 4) or (contained_title and overlap >= 5)
    ):
        return None
    # Titles are not episode IDs. Even an exact title can be reused for a
    # recurring format such as "Emergency Pod", so require independent date or
    # duration evidence. Very short/generic titles require both.
    significant_word_count = min(
        len(_title_words(rss.title)), len(_title_words(youtube.title))
    )
    if not (has_date_evidence or has_duration_evidence):
        return None
    if significant_word_count <= 3 and not (
        has_date_evidence and has_duration_evidence
    ):
        return None
    return title_score + (0.10 if exact else 0.0)


def attach_youtube_fallbacks(
    rss_episodes: list[Episode], youtube_episodes: list[Episode]
) -> list[Episode]:
    """Attach only unique YouTube matches and preserve unmatched releases."""

    unused = set(range(len(youtube_episodes)))
    output: list[Episode] = []
    for episode in rss_episodes:
        matches = sorted(
            (match, index)
            for index in unused
            if (
                match := _confident_episode_match(
                    episode, youtube_episodes[index]
                )
            )
            is not None
        )
        unique_best = bool(matches) and (
            len(matches) == 1 or matches[-1][0] - matches[-2][0] >= 0.08
        )
        if unique_best:
            _score, index = matches[-1]
            youtube = youtube_episodes[index]
            unused.remove(index)
            output.append(
                replace(
                    episode,
                    # Existing deployments used the YouTube video ID as the
                    # durable dedupe key. Reusing it prevents migration re-sends.
                    id=youtube.id,
                    duration_seconds=(
                        episode.duration_seconds or youtube.duration_seconds
                    ),
                    duration_string=episode.duration_string or youtube.duration_string,
                    metadata={
                        **episode.metadata,
                        "youtube_url": youtube.url,
                        "youtube_title": youtube.title,
                    },
                )
            )
        else:
            output.append(episode)
    output.extend(youtube_episodes[index] for index in sorted(unused))
    return output


def _valid_plain_transcript(
    text: str,
    duration_seconds: float | None,
    *,
    require_text_boundaries: bool = False,
) -> bool:
    raw_text = text.strip()
    text = re.sub(r"\s+", " ", raw_text)
    if len(text) < 5_000 or not duration_seconds:
        return False
    minutes = duration_seconds / 60
    word_count = len(text.split())
    words_per_minute = word_count / minutes
    if word_count < max(900, minutes * 80) or not 80 <= words_per_minute <= 280:
        return False
    if not require_text_boundaries:
        return True
    opening = text[:2_500].casefold()
    closing = text[-3_000:].casefold()
    has_opening = any(
        marker in opening
        for marker in (
            "intro",
            "welcome",
            "this is ",
            "today we're",
            "today we are",
            "my guest",
            "our guest",
        )
    )
    has_closing = any(
        marker in closing
        for marker in (
            "see you next time",
            "see you all next time",
            "thanks for listening",
            "thank you for listening",
            "until next time",
            "that's all for today",
            "that is all for today",
        )
    )
    # A publisher-declared plain transcript has no timestamps. Require speaker
    # turns distributed across the full document so a synopsis with a copied
    # intro/outro cannot masquerade as the complete conversation.
    speaker_matches = list(
        re.finditer(r"(?m)^([A-Za-z][A-Za-z .'-]{0,40}):\s+", raw_text)
    )
    speakers = {match.group(1).strip().casefold() for match in speaker_matches}
    if len(speaker_matches) < 20 or len(speakers) < 2:
        return False
    covered_buckets = {
        min(9, int(match.start() * 10 / max(1, len(raw_text))))
        for match in speaker_matches
    }
    has_distributed_turns = len(covered_buckets) == 10
    return has_opening and has_closing and has_distributed_turns


def _strict_timeline_coverage(
    intervals: list[tuple[float, float]], duration_seconds: float
) -> bool:
    """Require transcription from the opening through the closing, with few gaps."""

    if not intervals or duration_seconds <= 0:
        return False
    clipped = sorted(
        (max(0.0, start), min(duration_seconds, end))
        for start, end in intervals
        if end > start and end > 0 and start < duration_seconds
    )
    if not clipped or clipped[0][0] > 30:
        return False
    if clipped[-1][1] < duration_seconds - max(45, duration_seconds * 0.01):
        return False
    covered = 0.0
    current_start, current_end = clipped[0]
    for start, end in clipped[1:]:
        gap = start - current_end
        if gap > 60:
            return False
        # Ordinary conversation contains silence. A short gap between adjacent
        # speech segments is not missing transcript coverage; a longer gap is.
        if gap <= 30:
            current_end = max(current_end, end)
        else:
            covered += current_end - current_start
            current_start, current_end = start, end
    covered += current_end - current_start
    return covered >= duration_seconds * 0.90


class RSSDeclaredTranscriptProvider:
    """Read a publisher-declared Podcasting 2.0 transcript URL."""

    name = "publisher RSS transcript"

    def __init__(
        self,
        timeout: int = 30,
        allowed_hosts: set[str] | frozenset[str] = TRUSTED_RSS_TRANSCRIPT_HOSTS,
    ):
        self.timeout = timeout
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}

    def supports_url(self, _url: str) -> bool:
        return False

    def fetch(self, episode: Episode) -> Transcript | None:
        for item in episode.metadata.get("rss_transcripts") or ():
            transcript_url = str(item.get("url") or "")
            media_type = str(item.get("type") or "text/plain").casefold()
            transcript_host = (urlsplit(transcript_url).hostname or "").casefold()
            if not transcript_url or media_type not in {
                "text/plain",
                "text/html",
                "application/xhtml+xml",
            } or transcript_host not in self.allowed_hosts:
                continue
            response = _response(
                transcript_url,
                timeout=self.timeout,
                max_bytes=MAX_TRANSCRIPT_BYTES,
            )
            if media_type == "text/plain":
                text = response.text.strip()
            else:
                text = BeautifulSoup(response.content, "html.parser").get_text(
                    "\n", strip=True
                )
            if _valid_plain_transcript(
                text,
                episode.duration_seconds,
                require_text_boundaries=True,
            ):
                return Transcript(
                    text=text,
                    source=self.name,
                    source_url=transcript_url,
                    verified_complete=True,
                    language=str(item.get("language") or "en"),
                )
        return None


class SubstackApprovedTranscriptProvider:
    """Read an explicitly approved, full Substack podcast transcription."""

    name = "publisher-approved Substack transcript"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def supports_url(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and (parts.hostname or "").lower() in SUBSTACK_HOSTS

    def _page_data(self, url: str) -> tuple[dict, str]:
        if not self.supports_url(url):
            raise ValueError("Unsupported Substack publisher URL")
        response = _response(url, timeout=self.timeout, max_bytes=MAX_FEED_BYTES)
        if (urlsplit(response.url).hostname or "").lower() not in SUBSTACK_HOSTS:
            raise ValueError("Publisher page redirected outside an allowed host")
        soup = BeautifulSoup(response.content, "html.parser")
        for script in soup.find_all("script"):
            script_text = script.string or script.get_text()
            match = re.search(
                r"window\._preloads\s*=\s*JSON\.parse\((\".*\")\)\s*;?\s*$",
                script_text,
                re.DOTALL,
            )
            if match:
                decoded = json.loads(json.loads(match.group(1)))
                if isinstance(decoded, dict):
                    return decoded, response.url
        raise ValueError("Publisher page has no readable transcript metadata")

    def episode_from_url(self, url: str) -> Episode | None:
        if not self.supports_url(url):
            return None
        data, resolved_url = self._page_data(url)
        post = data.get("post") or {}
        upload = post.get("podcastUpload") or {}
        published_at = None
        if post.get("post_date"):
            try:
                published_at = datetime.fromisoformat(
                    str(post["post_date"]).replace("Z", "+00:00")
                )
            except ValueError:
                pass
        return Episode(
            id=f"substack:{post.get('publication_id')}:{post.get('id')}",
            title=str(post.get("title") or "Podcast episode"),
            url=resolved_url,
            show=str((data.get("pub") or {}).get("name") or "Podcast"),
            duration_seconds=float(upload.get("duration") or 0) or None,
            published_at=published_at,
        )

    def fetch(self, episode: Episode) -> Transcript | None:
        if not self.supports_url(episode.url):
            return None
        data, resolved_url = self._page_data(episode.url)
        post = data.get("post") or {}
        upload = post.get("podcastUpload") or {}
        transcription = upload.get("transcription") or {}
        if (
            transcription.get("status") != "transcribed"
            or not transcription.get("approved_at")
        ):
            return None
        transcript_url = _safe_https_url(str(transcription.get("cdn_url") or ""))
        if (
            transcript_url is None
            or (urlsplit(transcript_url).hostname or "").lower()
            not in SUBSTACK_CDN_HOSTS
        ):
            return None
        response = _response(
            transcript_url,
            timeout=self.timeout,
            max_bytes=MAX_TRANSCRIPT_BYTES,
        )
        segments = response.json()
        if not isinstance(segments, list):
            return None
        duration = float(episode.duration_seconds or upload.get("duration") or 0)
        intervals: list[tuple[float, float]] = []
        lines: list[str] = []
        speaker_map = transcription.get("speaker_map") or {}
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            try:
                start = float(segment["start"])
                end = float(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = re.sub(r"\s+", " ", str(segment.get("text") or "")).strip()
            if not text or end <= start:
                continue
            intervals.append((start, end))
            speaker = str(speaker_map.get(segment.get("speaker"), "")).strip()
            lines.append(f"{speaker}: {text}" if speaker else text)
        transcript_text = "\n".join(lines)
        if (
            duration <= 0
            or not _strict_timeline_coverage(intervals, duration)
            or not _valid_plain_transcript(transcript_text, duration)
        ):
            return None
        return Transcript(
            text=transcript_text,
            source=self.name,
            source_url=resolved_url,
            verified_complete=True,
        )
