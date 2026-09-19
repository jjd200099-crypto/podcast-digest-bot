"""Read verified Podwise transcripts; opt-in daily processing uses a durable ledger."""

from __future__ import annotations

import itertools
import json
import math
import re
from datetime import timedelta
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


def _title_core(value: str) -> str:
    # Edition numbers and a trailing guest list can differ across syndications.
    value = re.sub(r'^\s*(?:ep\.?|episode)\s*\d+\s*[-:–—]?\s*', '', value, flags=re.IGNORECASE)
    return _normalized(value.split('|', 1)[0])


def _show_key(value: str) -> str:
    name = _normalized(value).removeprefix('the ')
    return {'semianalysis weekly': 'semianalysis'}.get(name, name)


def _cross_version_matches(episode: Episode, item: dict) -> bool:
    """Not fuzzy matching: exact long title core + publisher + near-identical date.

    Final acceptance additionally requires independent RSS duration, complete
    timeline coverage, and a completed Podwise status for the alternate asset.
    """
    if not episode.metadata.get('rss_feed_url') or not episode.published_at or not episode.duration_seconds:
        return False
    core = _title_core(episode.title)
    published = _number(item.get('publishTime'))
    duration = _number(item.get('duration'))
    return bool(
        len(core.split()) >= 8
        and core == _title_core(str(item.get('title') or ''))
        and _show_key(episode.show) == _show_key(str(item.get('podcastName') or ''))
        and published is not None
        and abs(published - episode.published_at.timestamp()) <= 3600
        and (duration is None or abs(duration - episode.duration_seconds) <= max(30, episode.duration_seconds * .05))
    )


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

    def __init__(self, token: str, timeout: int = 30, *, processing_store=None, auto_process: bool = False):
        self._token = token
        self.timeout = timeout
        self.processing_store = processing_store
        self.auto_process = auto_process
        self.diagnostics: dict[str, str] = {}

    def _process(self, seq: int) -> dict:
        # No POST retries or redirects: after an uncertain result, poll status.
        try:
            response = requests.post(API_BASE + f'/episodes/{seq}/process',
                headers={'Authorization': f'Bearer {self._token}'},
                timeout=self.timeout, allow_redirects=False)
            if response.status_code != 200:
                raise PodwiseAPIError(f'Podwise processing HTTP {response.status_code}')
            data = response.json()
        except (requests.RequestException, ValueError):
            raise PodwiseAPIError('Podwise processing response uncertain') from None
        if not isinstance(data, dict) or data.get('success') is not True:
            raise PodwiseAPIError('Podwise processing response uncertain')
        return data

    def _ensure_processing(self, episode: Episode, matches: dict) -> bool | None:
        if len(matches) != 1:
            self.diagnostics[episode.id] = 'Podwise 未找到可唯一核验的同一期记录，待复查匹配。'
            return
        seq = next(iter(matches))
        data = self._get(f'/episodes/{seq}/status')
        status = (data or {}).get('result', {}).get('status')
        if status in {'done', 'waiting', 'processing'}:
            self.diagnostics[episode.id] = ('Podwise 已完成处理，但全文校验未通过，待复核。' if status == 'done'
                else 'Podwise 已在排队或转写中，完成后补充摘要。')
            return status == 'done'
        if status == 'failed':
            self.diagnostics[episode.id] = 'Podwise 转写失败，需复核后重试；未重复扣取转写额度。'
            return
        if status != 'not_requested' or not self.auto_process or self.processing_store is None:
            self.diagnostics[episode.id] = 'Podwise 已收录但尚未启动转写。'
            return
        if not episode.metadata.get('rss_feed_url'):
            return  # Automatic processing is restricted to tracked publisher RSS.
        account = (self._get('/me') or {}).get('result', {})
        credits = account.get('credits', {})
        available = (credits.get('publicEpisodeAiProcessing') == -1
                     or credits.get('aiProcessing') == -1
                     or (_number(credits.get('aiProcessing')) or 0) +
                        (_number(credits.get('aiProcessingAddOn')) or 0) > 0)
        if account.get('plan') not in {'Pro', 'Enterprise'} or not available:
            self.diagnostics[episode.id] = 'Podwise 转写额度不足或套餐不支持，待补足额度；不会自动购买。'
            return
        if not self.processing_store.reserve_podwise_processing(seq):
            self.diagnostics[episode.id] = '转写请求已有记录但尚未确认完成，待复核；不会重复提交扣费。'
            return
        try:
            response = self._process(seq)
            state = response.get('result', {}).get('status')
            if state not in {'done', 'waiting', 'processing'}:
                raise PodwiseAPIError('Unexpected processing state')
            self.processing_store.set_podwise_processing_state(seq, state)
            self.diagnostics[episode.id] = '已向 Podwise 提交转写，完成后补充摘要。'
            return state == 'done'
        except PodwiseAPIError:
            self.processing_store.set_podwise_processing_state(seq, 'uncertain')
            self.diagnostics[episode.id] = '转写提交结果未确认，待查询处理状态；不会重复提交扣费。'

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
        self.diagnostics.pop(episode.id, None)
        if not self._token:
            return None
        search = self._get(
            "/episodes/search", {"q": episode.title[:300], "hitsPerPage": 30}
        )
        rows = search.get("result", []) if search else []
        if not isinstance(rows, list):
            raise PodwiseAPIError("Podwise invalid search result")
        has_exact = any(isinstance(r, dict) and _matches(episode, r) for r in rows)
        if (not rows or (episode.metadata.get('rss_feed_url') and not has_exact)) and episode.show and episode.published_at:
            # A long compound title can miss the search index. Read the show's
            # dated episode catalog too; final asset matching remains strict.
            shows = self._get("/podcasts/search", {"q": episode.show, "hitsPerPage": 3})
            shows = shows.get("result", []) if shows else []
            if not isinstance(shows, list):
                raise PodwiseAPIError("Podwise invalid podcast search result")
            for show in shows[:3]:
                show_seq = show.get("seq") if isinstance(show, dict) else None
                if type(show_seq) is not int or show_seq <= 0:
                    continue
                catalog = self._get(f"/podcasts/{show_seq}/episodes", {
                    "date": (episode.published_at + timedelta(days=1)).date().isoformat(),
                    "days": 3,
                })
                entries = catalog.get("result", []) if catalog else []
                if not isinstance(entries, list):
                    raise PodwiseAPIError("Podwise invalid episode catalog")
                rows.extend(entries)
        exact_assets = {
            item['seq']: item for item in rows
            if isinstance(item, dict) and type(item.get('seq')) is int
            and item['seq'] > 0 and _matches(episode, item)
        }
        # Try already-processed syndications before spending a processing credit.
        if episode.metadata.get('rss_feed_url') and not any(r.get('transcribed') is True for r in exact_assets.values()):
            core = _title_core(episode.title)
            if len(core.split()) >= 8:
                extra = self._get('/episodes/search', {'q': core[:180], 'hitsPerPage': 30})
                entries = (extra or {}).get('result', [])
                if not isinstance(entries, list):
                    raise PodwiseAPIError('Podwise invalid search result')
                rows.extend(entries)
                exact_assets.update({r['seq']: r for r in entries if isinstance(r, dict)
                    and type(r.get('seq')) is int and r['seq'] > 0 and _matches(episode, r)})
        matches = {
            item["seq"]: item
            for item in rows
            if isinstance(item, dict)
            and type(item.get("seq")) is int
            and item["seq"] > 0
            and item.get("transcribed") is True
            and (_matches(episode, item) or _cross_version_matches(episode, item))
        }
        # A publisher RSS item and its attached YouTube fallback may both be
        # transcribed. The enclosure is the authoritative asset for this RSS
        # episode; do not discard it because a second video version exists.
        audio_key = _url_key(str(episode.metadata.get("audio_url") or ""))
        audio_matches = {
            seq: item for seq, item in matches.items()
            if audio_key and _url_key(str(item.get("link") or "")) == audio_key
        }
        if audio_matches:
            matches = audio_matches
        if len(matches) != 1:
            if not matches and episode.metadata.get('rss_feed_url'):
                # Search indexing can lag behind completed processing. Consult
                # status and read the verified transcript endpoint directly.
                if self._ensure_processing(episode, exact_assets):
                    matches = {seq: {**row, 'transcribed': True} for seq, row in exact_assets.items()}
            else:
                self.diagnostics[episode.id] = 'Podwise 同期记录存在歧义，需核验具体版本。'
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
            or not (_matches(episode, meta) or _cross_version_matches(episode, meta))
        ):
            return None
        if not _matches(episode, meta):
            status = self._get(f'/episodes/{seq}/status')
            result = (status or {}).get('result', {})
            if result.get('status') != 'done' or result.get('progress') != 100:
                return None
        duration = _number(meta.get("duration"))
        if not duration and episode.metadata.get('rss_feed_url'):
            # Identity was already independently verified above. A publisher
            # duration can validate full coverage when Podwise omits this field;
            # never derive expected duration from the transcript's own last cue.
            duration = _number(episode.duration_seconds)
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
        ends = [_number(s.get("end")) for s in segments]
        if all(end is not None for end in ends):
            final_end = max(ends) * scale
            if final_end > duration + timing_tolerance:
                # Dynamic-ad renditions can outlast RSS metadata. Require the
                # provider's independent completed-job status, a small <=5%
                # discrepancy, and full timeline coverage of the *longer* text.
                # Do not simply clip away the extra tail to pass validation.
                if final_end > duration * 1.05:
                    return None
                status = self._get(f"/episodes/{seq}/status")
                result = status.get("result", {}) if status else {}
                if result.get("status") != "done" or result.get("progress") != 100:
                    return None
                duration = final_end
                timing_tolerance = 0
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
