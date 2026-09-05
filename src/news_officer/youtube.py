from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

import requests

from .models import Episode, Transcript

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml"
YOUTUBE_EXTRACTOR_ARGS = (
    "youtube:player_client=web_embedded;player_skip=webpage;skip=translated_subs"
)
VTT_CUE_RE = re.compile(
    r"(?m)^(?P<start>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})"
)


def run(*args: str, timeout_seconds: int = 180) -> str:
    return subprocess.check_output(
        args,
        text=True,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )


def is_youtube_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in YOUTUBE_HOSTS


def youtube_video_id(url: str) -> str | None:
    """Extract one canonical video ID from a public YouTube video URL."""

    if not is_youtube_url(url):
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in parts.path.split("/") if segment]
    candidate = ""
    if host == "youtu.be" and segments:
        candidate = segments[0]
    elif parts.path.rstrip("/") == "/watch":
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
    elif len(segments) == 2 and segments[0] in {"embed", "live", "shorts"}:
        candidate = segments[1]
    return candidate if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate) else None


def _published_at(data: dict) -> datetime | None:
    timestamp = data.get("timestamp") or data.get("release_timestamp")
    if timestamp:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=UTC)
        except (TypeError, ValueError, OSError):
            pass
    compact_date = data.get("upload_date") or data.get("release_date")
    if compact_date:
        try:
            return datetime.strptime(str(compact_date), "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def episode_from_metadata(data: dict, fallback_url: str) -> Episode:
    episode_id = str(data.get("id") or fallback_url)
    return Episode(
        id=episode_id,
        title=data.get("title") or "用户提交的播客",
        url=data.get("webpage_url") or data.get("original_url") or fallback_url,
        show=data.get("channel") or data.get("uploader") or "文字稿未明确",
        duration_seconds=float(data["duration"]) if data.get("duration") else None,
        duration_string=data.get("duration_string"),
        published_at=_published_at(data),
        metadata=data,
    )


def _oembed_metadata(url: str) -> Episode:
    """Read minimal official metadata when yt-dlp is blocked by the host network."""

    video_id = youtube_video_id(url)
    if video_id is None:
        raise ValueError("Expected a public YouTube video URL")
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    response = requests.get(
        f"{YOUTUBE_OEMBED_URL}?{urlencode({'url': canonical_url, 'format': 'json'})}",
        headers={"User-Agent": "NewsOfficerBot/0.2 (+transcript verification)"},
        timeout=20,
        allow_redirects=False,
    )
    response.raise_for_status()
    if response.status_code != 200 or len(response.content) > 1_000_000:
        raise ValueError("YouTube oEmbed returned an invalid response")
    final = urlsplit(str(getattr(response, "url", "") or YOUTUBE_OEMBED_URL))
    if final.scheme != "https" or (final.hostname or "").lower() not in {
        "youtube.com",
        "www.youtube.com",
    }:
        raise ValueError("YouTube oEmbed redirected outside the official origin")
    data = response.json()
    if not isinstance(data, dict) or data.get("type") != "video":
        raise ValueError("YouTube oEmbed did not return video metadata")
    author_name = str(data.get("author_name") or "").strip()
    return Episode(
        id=video_id,
        title=str(data.get("title") or "用户提交的播客").strip(),
        url=canonical_url,
        show=author_name or "文字稿未明确",
        metadata={
            **data,
            "webpage_url": canonical_url,
            "metadata_source": "YouTube oEmbed",
        },
    )


def video_metadata(url: str) -> Episode:
    if not is_youtube_url(url):
        raise ValueError("Only public YouTube URLs are accepted by this source")
    if youtube_video_id(url) is None:
        raise ValueError("Expected a public YouTube video URL")
    try:
        data = json.loads(
            run(
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--skip-download",
                "--ignore-no-formats-error",
                "--extractor-args",
                YOUTUBE_EXTRACTOR_ARGS,
                "--dump-single-json",
                url,
                timeout_seconds=90,
            )
        )
        return episode_from_metadata(data, url)
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return _oembed_metadata(url)


def normalize_channel_videos_url(channel: str) -> str:
    """Return the uploads tab for a supported YouTube channel URL."""
    if not is_youtube_url(channel):
        raise ValueError("Only public YouTube channel URLs are accepted")
    parts = urlsplit(channel)
    if (parts.hostname or "").lower() == "youtu.be":
        raise ValueError("A video URL cannot be used as a YouTube channel")
    segments = [segment for segment in parts.path.split("/") if segment]
    is_videos_tab = segments[-1:] == ["videos"] and (
        (len(segments) == 2 and segments[0].startswith("@"))
        or (len(segments) == 3 and segments[0] in {"channel", "c", "user"})
    )
    if is_videos_tab:
        return f"https://www.youtube.com/{'/'.join(segments)}"
    is_handle = len(segments) == 1 and segments[0].startswith("@")
    is_named_channel = (
        len(segments) == 2 and segments[0] in {"channel", "c", "user"}
    )
    if not (is_handle or is_named_channel):
        raise ValueError("Expected a YouTube channel root or /videos URL")
    return f"https://www.youtube.com/{'/'.join(segments)}/videos"


def _channel_id(channel_url: str) -> str | None:
    parts = urlsplit(normalize_channel_videos_url(channel_url))
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) == 3 and segments[0] == "channel":
        candidate = segments[1]
        if YOUTUBE_CHANNEL_ID_RE.fullmatch(candidate):
            return candidate
    return None


def _latest_videos_from_feed(channel: str, playlist_end: int) -> list[Episode]:
    """Use YouTube's official Atom feed when channel extraction is unavailable."""

    channel_id = _channel_id(channel)
    if channel_id is None:
        raise ValueError("Official YouTube feed fallback requires a channel ID URL")
    response = requests.get(
        f"{YOUTUBE_FEED_URL}?{urlencode({'channel_id': channel_id})}",
        headers={"User-Agent": "NewsOfficerBot/0.2 (+feed discovery)"},
        timeout=20,
        allow_redirects=False,
    )
    response.raise_for_status()
    if response.status_code != 200 or len(response.content) > 2_000_000:
        raise ValueError("YouTube feed returned an invalid response")
    final = urlsplit(str(getattr(response, "url", "") or YOUTUBE_FEED_URL))
    if final.scheme != "https" or (final.hostname or "").lower() not in {
        "youtube.com",
        "www.youtube.com",
    }:
        raise ValueError("YouTube feed redirected outside the official origin")
    root = ElementTree.fromstring(response.content)
    namespace = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    returned_channel_id = (
        root.findtext("yt:channelId", default="", namespaces=namespace) or ""
    ).strip()
    if returned_channel_id not in {channel_id, channel_id.removeprefix("UC")}:
        raise ValueError("YouTube feed channel identity is missing or mismatched")
    show = (root.findtext("atom:title", default="", namespaces=namespace) or "").strip()
    episodes: list[Episode] = []
    for entry in root.findall("atom:entry", namespace)[:playlist_end]:
        entry_channel_id = (
            entry.findtext("yt:channelId", default="", namespaces=namespace) or ""
        ).strip()
        if entry_channel_id and entry_channel_id != channel_id:
            continue
        video_id = (entry.findtext("yt:videoId", default="", namespaces=namespace) or "").strip()
        if not YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
            continue
        title = (entry.findtext("atom:title", default="", namespaces=namespace) or "").strip()
        author = (
            entry.findtext("atom:author/atom:name", default="", namespaces=namespace)
            or show
        ).strip()
        published = (
            entry.findtext("atom:published", default="", namespaces=namespace) or ""
        ).strip()
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            published_at = None
        if published_at is not None and published_at.tzinfo is None:
            published_at = None
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        episodes.append(
            Episode(
                id=video_id,
                title=title or "用户提交的播客",
                url=canonical_url,
                show=author or "文字稿未明确",
                published_at=published_at,
                metadata={
                    "channel_id": channel_id,
                    "webpage_url": canonical_url,
                    "metadata_source": "YouTube Atom feed",
                },
            )
        )
    if not episodes:
        raise ValueError("YouTube feed contains no valid video entries")
    return episodes


def latest_videos(channel: str, playlist_end: int = 4) -> list[Episode]:
    channel_videos_url = normalize_channel_videos_url(channel)
    # The official Atom feed is smaller, deterministic and does not need a
    # browser-like YouTube player request. All configured feeds use channel IDs.
    feed_error: Exception | None = None
    if _channel_id(channel_videos_url) is not None:
        try:
            return _latest_videos_from_feed(channel_videos_url, playlist_end)
        except (ElementTree.ParseError, requests.RequestException, ValueError) as error:
            feed_error = error
    try:
        data = json.loads(
            run(
                sys.executable,
                "-m",
                "yt_dlp",
                "--flat-playlist",
                "--playlist-end",
                str(playlist_end),
                "--dump-single-json",
                channel_videos_url,
                timeout_seconds=90,
            )
        )
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        if feed_error is not None:
            raise feed_error from error
        return _latest_videos_from_feed(channel_videos_url, playlist_end)
    episodes: list[Episode] = []
    for entry in data.get("entries") or []:
        if not entry or not YOUTUBE_VIDEO_ID_RE.fullmatch(str(entry.get("id") or "")):
            continue
        normalized = dict(entry)
        normalized.setdefault("channel", data.get("channel") or channel)
        normalized.setdefault(
            "webpage_url", f"https://www.youtube.com/watch?v={entry['id']}"
        )
        episodes.append(episode_from_metadata(normalized, normalized["webpage_url"]))
    return episodes


def timestamp_seconds(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def intervals_cover_episode(
    intervals: list[tuple[float, float]], duration_seconds: float
) -> bool:
    if not intervals or duration_seconds <= 0:
        return False
    intervals = sorted(intervals)
    intervals = [(start, end) for start, end in intervals if end > start]
    if not intervals:
        return False
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    first_start = merged[0][0]
    last_end = merged[-1][1]
    covered_seconds = sum(end - start for start, end in merged)
    gaps = [next_start - end for (_, end), (next_start, _) in pairwise(merged)]
    max_gap = max(gaps, default=0.0)
    allowed_gap = min(180.0, max(60.0, duration_seconds * 0.03))
    return (
        first_start <= min(180, duration_seconds * 0.10)
        and (duration_seconds - last_end) <= min(180, duration_seconds * 0.10)
        and (last_end - first_start) >= duration_seconds * 0.85
        and covered_seconds >= duration_seconds * 0.65
        and max_gap <= allowed_gap
    )


def vtt_covers_episode(vtt: str, duration_seconds: float) -> bool:
    cues = list(VTT_CUE_RE.finditer(vtt))
    return intervals_cover_episode(
        [
            (
                max(0.0, timestamp_seconds(cue.group("start"))),
                min(duration_seconds, timestamp_seconds(cue.group("end"))),
            )
            for cue in cues
        ],
        duration_seconds,
    )


def _merge_caption_chunks(chunks: list[str]) -> str:
    words: list[str] = []
    for chunk in chunks:
        incoming = re.sub(r"\s+", " ", chunk).strip().split()
        if not incoming:
            continue
        overlap = 0
        limit = min(len(words), len(incoming), 30)
        for count in range(limit, 0, -1):
            if words[-count:] == incoming[:count]:
                overlap = count
                break
        words.extend(incoming[overlap:])
    return " ".join(words)


def _clean_vtt(vtt: str) -> str:
    chunks: list[str] = []
    for line in vtt.splitlines():
        if (
            not line.strip()
            or "-->" in line
            or line.startswith(("WEBVTT", "Kind:", "Language:"))
        ):
            continue
        clean = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if clean:
            chunks.append(clean)
    return _merge_caption_chunks(chunks)


def _parse_json3(raw: str, duration_seconds: float) -> str | None:
    data = json.loads(raw)
    chunks: list[str] = []
    intervals: list[tuple[float, float]] = []
    for event in data.get("events") or []:
        try:
            start = max(0.0, float(event["tStartMs"]) / 1000)
            end = min(
                duration_seconds,
                start + float(event.get("dDurationMs") or 0) / 1000,
            )
        except (KeyError, TypeError, ValueError):
            continue
        text = "".join(
            str(segment.get("utf8") or "") for segment in event.get("segs") or []
        )
        text = html.unescape(text).replace("\n", " ").strip()
        if text:
            chunks.append(text)
            intervals.append((start, end))
    if not intervals_cover_episode(intervals, duration_seconds):
        return None
    return _merge_caption_chunks(chunks)


class YouTubeTranscriptProvider:
    name = "YouTube captions"

    def fetch(self, episode: Episode) -> Transcript | None:
        if not is_youtube_url(episode.url):
            return None
        with tempfile.TemporaryDirectory() as temp_dir:
            target = str(Path(temp_dir) / "%(id)s.%(ext)s")
            try:
                current = episode
                if not current.duration_seconds:
                    current = video_metadata(episode.url)
                duration_seconds = float(current.duration_seconds or 0)
                if duration_seconds <= 0:
                    return None
                run(
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--no-playlist",
                    "--skip-download",
                    "--write-subs",
                    "--write-auto-subs",
                    "--ignore-no-formats-error",
                    "--extractor-args",
                    YOUTUBE_EXTRACTOR_ARGS,
                    "--sub-langs",
                    "en,en-US,en-orig",
                    "--sub-format",
                    "json3/vtt",
                    "--sleep-subtitles",
                    "1",
                    "-o",
                    target,
                    episode.url,
                )
            except (TypeError, ValueError):
                return None
            files = [
                *Path(temp_dir).glob("*.json3"),
                *Path(temp_dir).glob("*.vtt"),
            ]
            tracks = sorted(
                files,
                key=lambda path: (
                    0 if path.suffix == ".json3" else 1,
                    0 if ".en." in path.name else 1,
                ),
            )
            for path in tracks:
                raw = path.read_text(errors="ignore")
                if path.suffix == ".json3":
                    text = _parse_json3(raw, duration_seconds)
                else:
                    text = (
                        _clean_vtt(raw)
                        if vtt_covers_episode(raw, duration_seconds)
                        else None
                    )
                if not text:
                    continue
                minimum_chars = max(5_000, round(duration_seconds / 60) * 300)
                words_per_minute = len(text.split()) / (duration_seconds / 60)
                if len(text) >= minimum_chars and 60 <= words_per_minute <= 260:
                    return Transcript(
                        text=text,
                        source=self.name,
                        source_url=episode.url,
                        verified_complete=True,
                    )
            return None
