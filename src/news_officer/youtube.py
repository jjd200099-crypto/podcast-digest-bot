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
from urllib.parse import urlsplit

from .models import Episode, Transcript

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
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


def video_metadata(url: str) -> Episode:
    if not is_youtube_url(url):
        raise ValueError("Only public YouTube URLs are accepted by this source")
    data = json.loads(
        run(
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            url,
            timeout_seconds=90,
        )
    )
    return episode_from_metadata(data, url)


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


def latest_videos(channel: str, playlist_end: int = 4) -> list[Episode]:
    channel_videos_url = normalize_channel_videos_url(channel)
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
