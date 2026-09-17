"""Read existing Podwise transcripts; never create a paid processing job."""

from __future__ import annotations

import itertools
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

import requests

from .models import Episode, Transcript
from .rss import _strict_timeline_coverage, _valid_plain_transcript

API_BASE = "https://app.podwise.ai/api/open/v1"
MAX_BYTES = 4_000_000


class PodwiseAPIError(RuntimeError):
    """Credential-free diagnostic; do not include response bodies or requests."""


def _url_key(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    if host == "youtu.be":
        return "youtube:" + parsed.path.strip("/")
    if host == "youtube.com" and parsed.path == "/watch":
        return "youtube:" + parse_qs(parsed.query).get("v", [""])[0]
    # Only ignore tracking parameters, not identifiers in publisher query URLs.
    query = sorted(
        (key, tuple(values))
        for key, values in parse_qs(parsed.query).items()
        if not key.startswith("utm_") and key not in {"fbclid", "gclid"}
    )
    return f"{host}{parsed.path.rstrip('/')}?{query}"


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _matches(episode: Episode, item: dict) -> bool:
    link = _url_key(str(item.get("link") or ""))
    known = {
        _url_key(episode.url),
        _url_key(str(episode.metadata.get("youtube_url") or "")),
        _url_key(str(episode.metadata.get("audio_url") or "")),
    }
    if link and link in known:
        return True
    # No fuzzy title-only matching: many channels publish clips of the same guest.
    if not episode.show or not episode.published_at or not episode.duration_seconds:
        return False
    published = _number(item.get("publishTime"))
    duration = _number(item.get("duration"))
    return bool(
        _normalized(episode.title) == _normalized(str(item.get("title") or ""))
        and _normalized(episode.show) == _normalized(str(item.get("podcastName") or ""))
        and published is not None
        and abs(published - episode.published_at.timestamp()) <= 86400
        and duration is not None
        and abs(duration - episode.duration_seconds)
        <= max(30, episode.duration_seconds * 0.05)
    )


def _timestamp(segment: dict) -> float | None:
    parts = str(segment.get("time") or "").split(":")
    if len(parts) not in {2, 3} or not all(
        re.fullmatch(r"\d+", part) for part in parts
    ):
        return None
    if any(int(part) >= 60 for part in parts[1:]):
        return None
    return float(
        sum(int(part) * 60**index for index, part in enumerate(reversed(parts)))
    )


def _timing_scale(segments: list[dict]) -> float | None:
    """Infer seconds/ms from the independent human-readable timestamps.

    Live Podwise transcripts return millisecond start/end numbers, while some
    exports use seconds. Never infer the unit from magnitude alone.
    """
    anchors = []
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        timestamp = _timestamp(segment)
        if timestamp is None:
            return None
        if segment.get("start") is not None:
            numeric = _number(segment["start"])
            if numeric is None:
                return None
            anchors.append((numeric, timestamp))
    if not anchors:
        return 1.0 if all(s.get("end") is None for s in segments) else None
    scales = [
        scale
        for scale in (1.0, 0.001)
        if all(
            abs(numeric * scale - timestamp) <= 1.01 for numeric, timestamp in anchors
        )
    ]
    return scales[0] if len(scales) == 1 else None


def _start(segment: dict, scale: float = 1.0) -> float | None:
    if segment.get("start") is None:
        return _timestamp(segment)
    numeric = _number(segment["start"])
    return numeric * scale if numeric is not None else None


class PodwiseTranscriptProvider:
    name = "Podwise verified transcript"

    def __init__(self, token: str, timeout: int = 30):
        self._token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            with requests.get(
                API_BASE + path,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code == 404:
                    return None
                if response.status_code != 200:
                    # Never print the response body, Authorization, or token.
                    raise PodwiseAPIError(f"Podwise HTTP {response.status_code}")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise PodwiseAPIError("Podwise response exceeded size limit")
                data = json.loads(body)
        except (requests.RequestException, ValueError):
            raise PodwiseAPIError("Podwise network or response error") from None
        if not isinstance(data, dict) or data.get("success") is not True:
            raise PodwiseAPIError("Podwise unsuccessful response")
        return data

    def fetch(self, episode: Episode) -> Transcript | None:
        if not self._token:
            return None
        search = self._get(
            "/episodes/search", {"q": episode.title[:300], "hitsPerPage": 30}
        )
        rows = search.get("result", []) if search else []
        if not isinstance(rows, list):
            raise PodwiseAPIError("Podwise invalid search result")
        matches = {
            item["seq"]: item
            for item in rows
            if isinstance(item, dict)
            and type(item.get("seq")) is int
            and item["seq"] > 0
            and item.get("transcribed") is True
            and _matches(episode, item)
        }
        if len(matches) != 1:
            return None
        seq, match = next(iter(matches.items()))
        if match.get("transcribed") is not True:
            return None
        path = f"/episodes/{seq}/transcripts"
        data = self._get(path)
        if not data:
            return None
        meta, segments = data.get("episode"), data.get("result")
        if not isinstance(meta, dict) or not isinstance(segments, list) or not segments:
            return None
        if (
            meta.get("seq") != seq
            or meta.get("transcribed") is not True
            or not _matches(episode, meta)
        ):
            return None
        duration = _number(meta.get("duration"))
        if not duration or duration <= 0:
            return None
        if episode.duration_seconds and abs(duration - episode.duration_seconds) > max(
            60, episode.duration_seconds * 0.05
        ):
            return None
        scale = _timing_scale(segments)
        if scale is None:
            return None
        # Feed duration and ASR audio can differ slightly (e.g. 14 seconds in a
        # 72-minute live fixture). Keep the original full-coverage requirement;
        # permit only a small closing overrun, never a materially longer asset.
        timing_tolerance = max(30, duration * 0.01)
        lines, starts, intervals = [], [], []
        for segment in segments:
            if not isinstance(segment, dict):
                return None
            content = str(segment.get("content") or "").strip()
            start, end = _start(segment, scale), _number(segment.get("end"))
            if end is not None:
                end *= scale
            if (
                not content
                or start is None
                or start < 0
                or start > duration + timing_tolerance
            ):
                return None
            if starts and start < starts[-1]:
                return None
            starts.append(start)
            if end is not None:
                if end <= start or end > duration + timing_tolerance:
                    return None
                intervals.append((start, end))
            speaker = str(segment.get("speaker") or "").strip()
            lines.append(
                f"[{int(start // 60):02d}:{int(start % 60):02d}] {speaker + ': ' if speaker else ''}{content}"
            )
        if len(intervals) == len(segments):
            complete = _strict_timeline_coverage(intervals, duration)
        else:
            # The API also returns timestamp-only segments. Require dense starts
            # through the final 45 seconds; never invent missing end timestamps.
            complete = (
                starts[0] <= 30
                and starts[-1] >= duration - 45
                and all(0 < b - a <= 60 for a, b in itertools.pairwise(starts))
            )
        text = "\n".join(lines)
        # Density is checked on content only, without timestamp/speaker padding.
        raw = "\n".join(str(segment["content"]) for segment in segments)
        if not complete or not _valid_plain_transcript(raw, duration):
            return None
        return Transcript(
            text=text,
            source=self.name,
            source_url=API_BASE + path,
            verified_complete=True,
            language=str(meta.get("language") or "en"),
        )
