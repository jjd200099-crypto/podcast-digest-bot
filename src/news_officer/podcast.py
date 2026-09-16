from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import requests

from .colossus import ColossusOfficialTranscriptProvider
from .founders import DavidSenraOfficialTranscriptProvider
from .models import (
    AnalysisResult,
    DailyItem,
    Episode,
    StoredTranscript,
    Transcript,
    TranscriptAttachment,
)
from .official import DwarkeshOfficialTranscriptProvider
from .rss import (
    RSSDeclaredTranscriptProvider,
    SubstackApprovedTranscriptProvider,
    attach_youtube_fallbacks,
    latest_rss_episodes,
)
from .sequoia import SequoiaOfficialTranscriptProvider
from .store import Store
from .summarizer import SummaryFormatError
from .transcript_view import RENDERER_VERSION, render_readable_transcript
from .youtube import (
    YouTubeTranscriptProvider,
    is_youtube_url,
    latest_videos,
    video_metadata,
)

logger = logging.getLogger(__name__)


def _readable_attachment(
    record: StoredTranscript, digest_markdown: str
) -> TranscriptAttachment:
    filename, content = render_readable_transcript(
        record, digest_markdown=digest_markdown
    )
    return TranscriptAttachment.from_rendered(
        record,
        digest_markdown=digest_markdown,
        renderer_version=RENDERER_VERSION,
        filename=filename,
        content=content,
    )


class TranscriptLookupError(RuntimeError):
    """No provider succeeded because at least one usable source failed transiently."""


class FeedDiscoveryError(RuntimeError):
    """The daily source scan was not healthy enough to call the result empty."""


class TranscriptProvider(Protocol):
    name: str

    def fetch(self, episode: Episode) -> Transcript | None: ...


class Summarizer(Protocol):
    def summarize(self, episode: Episode, transcript: Transcript) -> str: ...


class TranscriptResolver:
    """Try official transcript adapters first, then captions or other fallbacks."""

    def __init__(self, providers: Sequence[TranscriptProvider]):
        self.providers = providers

    def fetch(self, episode: Episode) -> Transcript | None:
        errors: list[tuple[str, Exception]] = []
        for provider in self.providers:
            try:
                transcript = provider.fetch(episode)
            except Exception as error:  # noqa: BLE001 - one source must not block fallbacks
                errors.append((provider.name, error))
                logger.warning(
                    "Transcript provider %s failed for %s: %s",
                    provider.name,
                    episode.url,
                    error,
                )
                continue
            if transcript and transcript.verified_complete:
                return transcript
        if errors:
            sources = ", ".join(name for name, _error in errors)
            raise TranscriptLookupError(
                f"Transcript lookup had transient source errors: {sources}"
            ) from errors[-1][1]
        return None

    def supports_url(self, url: str) -> bool:
        return any(
            bool(getattr(provider, "supports_url", lambda _url: False)(url))
            for provider in self.providers
        )

    def episode_from_url(self, url: str) -> Episode | None:
        errors: list[tuple[str, Exception]] = []
        for provider in self.providers:
            factory = getattr(provider, "episode_from_url", None)
            if factory:
                try:
                    episode = factory(url)
                except Exception as error:  # noqa: BLE001 - preserve other fallbacks
                    errors.append((provider.name, error))
                    logger.warning(
                        "Episode metadata provider %s failed for %s: %s",
                        provider.name,
                        url,
                        error,
                    )
                    continue
                if episode:
                    return episode
        if errors:
            sources = ", ".join(name for name, _error in errors)
            raise TranscriptLookupError(
                f"Episode metadata lookup had transient source errors: {sources}"
            ) from errors[-1][1]
        return None


@dataclass(frozen=True)
class YouTubeFeedSource:
    url: str
    include_keywords: tuple[str, ...] = ()
    scan_depth: int = 4
    name: str = ""
    rss_url: str = ""
    priority: str = "B"

    def accepts(self, episode: Episode) -> bool:
        if not self.include_keywords:
            return True
        title = episode.title.casefold()
        return any(
            re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", title)
            is not None
            for keyword in self.include_keywords
        )


def load_youtube_sources(path: Path) -> list[YouTubeFeedSource]:
    data = json.loads(path.read_text())
    # Backward compatible with the existing deployed feeds.json.
    if isinstance(data.get("youtube_channels"), list):
        return [
            YouTubeFeedSource(str(url))
            for url in data["youtube_channels"]
            if str(url).strip()
        ]
    sources: list[YouTubeFeedSource] = []
    for source in data.get("sources") or []:
        if source.get("enabled", True) and source.get("type") in {
            "youtube",
            "rss",
        } and (source.get("url") or source.get("rss_url")):
            keywords = tuple(
                str(keyword).strip()
                for keyword in source.get("include_keywords") or ()
                if str(keyword).strip()
            )
            try:
                scan_depth = int(source.get("scan_depth", 4))
            except (TypeError, ValueError):
                scan_depth = 4
            priority = str(source.get("priority") or "B").strip().upper()
            if priority not in {"A", "B"}:
                priority = "B"
            sources.append(
                YouTubeFeedSource(
                    url=str(source.get("url") or ""),
                    include_keywords=keywords,
                    scan_depth=min(50, max(1, scan_depth)),
                    name=str(source.get("name") or ""),
                    rss_url=str(source.get("rss_url") or ""),
                    priority=priority,
                )
            )
    return sources


def load_youtube_feeds(path: Path) -> list[str]:
    """Compatibility helper for callers that only need source URLs."""

    return [source.url for source in load_youtube_sources(path) if source.url]


def _interleave(groups: Iterable[Sequence[Episode]]) -> list[Episode]:
    """Round-robin feeds so an early channel cannot crowd out every later source."""
    pending = [list(group) for group in groups]
    output: list[Episode] = []
    index = 0
    while any(index < len(group) for group in pending):
        for group in pending:
            if index < len(group):
                output.append(group[index])
        index += 1
    return output


def _weighted_priority_merge(
    priority_a: Sequence[Episode], priority_b: Sequence[Episode]
) -> list[Episode]:
    """Merge two tiers at a 2:1 ratio so B can never be starved by A."""

    output: list[Episode] = []
    a_index = 0
    b_index = 0
    while a_index < len(priority_a) or b_index < len(priority_b):
        for _ in range(2):
            if a_index < len(priority_a):
                output.append(priority_a[a_index])
                a_index += 1
        if b_index < len(priority_b):
            output.append(priority_b[b_index])
            b_index += 1
        if a_index >= len(priority_a) and b_index < len(priority_b):
            output.extend(priority_b[b_index:])
            break
    return output


def _rotate_source_groups(
    groups: list[list[Episode]], *, day_number: int
) -> list[list[Episode]]:
    """Deterministically rotate first consideration across calendar days."""

    if len(groups) < 2:
        return groups
    offset = day_number % len(groups)
    return [*groups[offset:], *groups[:offset]]


def _rss_with_unmatched_youtube(
    rss_episodes: list[Episode], youtube_episodes: list[Episode]
) -> list[Episode]:
    """Keep merged RSS episodes and every unconsumed YouTube release."""

    episodes = attach_youtube_fallbacks(rss_episodes, youtube_episodes)
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(
        episodes,
        key=lambda episode: episode.published_at or oldest,
        reverse=True,
    )


def _episode_dedupe_keys(episode: Episode) -> set[str]:
    """Build conservative cross-source identities for syndicated episodes."""

    keys = {f"id:{episode.id}"}
    feed_url = str(episode.metadata.get("rss_feed_url") or "").strip().rstrip("/")
    for value in (
        episode.metadata.get("audio_url"),
        episode.metadata.get("youtube_url"),
        episode.url,
    ):
        normalized_url = str(value or "").strip()
        if normalized_url and normalized_url.rstrip("/") != feed_url:
            keys.add(f"url:{normalized_url}")
    normalized_title = " ".join(
        re.findall(r"[a-z0-9]+", episode.title.casefold())
    )
    if episode.published_at and len(normalized_title) >= 12:
        published_day = episode.published_at.astimezone(UTC).date().isoformat()
        keys.add(f"title-day:{normalized_title}:{published_day}")
    return keys


def _transcript_preference(episode: Episode, resolver: TranscriptResolver) -> int:
    """Prefer representations that expose a first-party complete transcript."""

    score = 0
    if episode.metadata.get("rss_transcripts"):
        score += 40
    if resolver.supports_url(episode.url):
        score += 30
    if episode.metadata.get("youtube_url") or is_youtube_url(episode.url):
        score += 20
    if episode.duration_seconds:
        score += 2
    if episode.published_at:
        score += 1
    return score


class PodcastService:
    def __init__(
        self,
        store: Store,
        feeds_path: Path,
        summarizer: Summarizer,
        transcript_resolver: TranscriptResolver | None = None,
        lookback_hours: int = 72,
        max_daily_candidates: int = 16,
        max_daily_summaries: int = 3,
    ):
        self.store = store
        self.feeds_path = feeds_path
        self.summarizer = summarizer
        self.transcript_resolver = transcript_resolver or TranscriptResolver(
            [
                RSSDeclaredTranscriptProvider(),
                SubstackApprovedTranscriptProvider(),
                DavidSenraOfficialTranscriptProvider(),
                DwarkeshOfficialTranscriptProvider(),
                SequoiaOfficialTranscriptProvider(),
                ColossusOfficialTranscriptProvider(),
                YouTubeTranscriptProvider(),
            ]
        )
        self.lookback_hours = lookback_hours
        self.max_daily_candidates = max_daily_candidates
        self.max_daily_summaries = max_daily_summaries
        self._last_failed_feeds: tuple[str, ...] = ()

    def supports_url(self, url: str) -> bool:
        return is_youtube_url(url) or self.transcript_resolver.supports_url(url)

    @staticmethod
    def _merge_episode(discovered: Episode, enriched: Episode) -> Episode:
        """Keep trustworthy feed fields when a fallback only returns partial metadata."""

        rss_discovered = bool(discovered.metadata.get("rss_feed_url"))
        return Episode(
            id=discovered.id if rss_discovered else enriched.id or discovered.id,
            title=discovered.title if rss_discovered else enriched.title or discovered.title,
            url=discovered.url if rss_discovered else enriched.url or discovered.url,
            show=discovered.show if rss_discovered else enriched.show or discovered.show,
            duration_seconds=(
                discovered.duration_seconds
                if rss_discovered and discovered.duration_seconds is not None
                else enriched.duration_seconds
                if enriched.duration_seconds is not None
                else discovered.duration_seconds
            ),
            duration_string=(
                discovered.duration_string
                if rss_discovered and discovered.duration_string
                else enriched.duration_string or discovered.duration_string
            ),
            published_at=(
                discovered.published_at
                if rss_discovered and discovered.published_at is not None
                else enriched.published_at or discovered.published_at
            ),
            metadata={**discovered.metadata, **enriched.metadata},
        )

    @staticmethod
    def _source_error(episode: Episode | None = None) -> AnalysisResult:
        prefix = ""
        if episode is not None:
            prefix = f"节目：{episode.title}\n链接：{episode.url}\n\n"
        return AnalysisResult(
            status="source_error",
            episode=episode,
            message=(
                f"{prefix}来源暂时不可访问。未取得完整文字稿，本次不摘要。"
                "请稍后重新发送这个链接。"
            ),
        )

    def analyze_url(self, url: str) -> AnalysisResult:
        if not self.supports_url(url):
            return AnalysisResult(
                status="unsupported",
                message=(
                    "目前可直接分析公开 YouTube，以及 David Senra、"
                    "Dwarkesh、Sequoia 和 Invest Like the Best/Colossus 的官方节目页。"
                ),
            )
        try:
            episode = (
                video_metadata(url)
                if is_youtube_url(url)
                else self.transcript_resolver.episode_from_url(url)
            )
        except (
            requests.RequestException,
            subprocess.SubprocessError,
            TranscriptLookupError,
            ValueError,
        ):
            logger.warning("Unable to read episode metadata for %s", url, exc_info=True)
            return self._source_error()
        if episode is None:
            return AnalysisResult(
                status="unsupported",
                message="未能读取这个官方播客页面。请检查链接是否公开可访问。",
            )
        youtube_url = str(episode.metadata.get("youtube_url") or "")
        if episode.duration_seconds is None and is_youtube_url(youtube_url):
            try:
                youtube_episode = video_metadata(youtube_url)
            except (requests.RequestException, subprocess.SubprocessError, ValueError):
                youtube_episode = None
            if youtube_episode is not None:
                episode = replace(
                    episode,
                    duration_seconds=youtube_episode.duration_seconds,
                    duration_string=youtube_episode.duration_string,
                    metadata={**youtube_episode.metadata, **episode.metadata},
                )
        try:
            transcript = self.transcript_resolver.fetch(episode)
        except TranscriptLookupError:
            return self._source_error(episode)
        if transcript is None:
            return AnalysisResult(
                status="no_transcript",
                episode=episode,
                message=(
                    f"节目：{episode.title}\n"
                    f"链接：{episode.url}\n\n"
                    "未取得完整文字稿，本次不摘要。"
                ),
            )
        # Archive the verified source before any model call. A formatting or
        # delivery failure must never discard the only copy available for Q&A.
        stored = self.store.save_verified_transcript(episode, transcript)
        try:
            summary_candidate = self.summarizer.summarize(episode, transcript)
        except SummaryFormatError:
            return AnalysisResult(
                status="summary_format_error",
                episode=episode,
                message=(
                    f"节目：{episode.title}\n链接：{episode.url}\n\n"
                    "本次摘要没有稳定收敛为 10 条精选要点，因此没有发送不合格结果。"
                    "请稍后重新发送这个链接。"
                ),
            )
        summary = self.store.save_transcript_digest(
            episode.id,
            summary_candidate,
            stored.content_sha256,
            stored.record_revision_sha256,
        )
        return AnalysisResult(
            status="summarized",
            episode=episode,
            message=summary,
            attachment=_readable_attachment(stored, summary),
        )

    def discover_daily_candidates(
        self, now: datetime | None = None
    ) -> list[Episode]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now.astimezone(UTC) - timedelta(hours=self.lookback_hours)
        groups_by_priority: dict[str, list[list[Episode]]] = {"A": [], "B": []}
        failed_sources: list[str] = []
        sources = load_youtube_sources(self.feeds_path)
        for source in sources:
            youtube_episodes: list[Episode] = []
            rss_episodes: list[Episode] = []
            channel_with_results = 0
            if source.url:
                try:
                    discovered_youtube = latest_videos(
                        source.url, playlist_end=source.scan_depth
                    )
                    if discovered_youtube:
                        channel_with_results += 1
                    youtube_episodes = [
                        episode
                        for episode in discovered_youtube
                        if source.accepts(episode)
                    ]
                except Exception as error:  # noqa: BLE001 - isolate source failures
                    logger.warning(
                        "Optional YouTube discovery failed for %s: %s",
                        source.name or source.url,
                        error,
                    )
            if source.rss_url:
                try:
                    discovered_rss = latest_rss_episodes(
                        source.name or source.url,
                        source.rss_url,
                        limit=source.scan_depth,
                    )
                    if discovered_rss:
                        channel_with_results += 1
                    rss_episodes = [
                        episode
                        for episode in discovered_rss
                        if source.accepts(episode)
                    ]
                except Exception as error:  # noqa: BLE001 - isolate source failures
                    logger.warning(
                        "RSS discovery failed for %s: %s",
                        source.name or source.rss_url,
                        error,
                    )
            if rss_episodes:
                source_episodes = _rss_with_unmatched_youtube(
                    rss_episodes, youtube_episodes
                )
            elif youtube_episodes:
                source_episodes = youtube_episodes
            else:
                source_episodes = []
            if source_episodes:
                groups_by_priority[source.priority].append(
                    [
                        replace(
                            episode,
                            metadata={
                                **episode.metadata,
                                "source_priority": source.priority,
                            },
                        )
                        for episode in source_episodes
                    ]
                )
            if channel_with_results == 0:
                failed_sources.append(
                    source.name or source.rss_url or source.url
                )
                logger.warning(
                    "All discovery channels returned no usable feed for %s",
                    failed_sources[-1],
                )

        new_groups: dict[str, list[list[Episode]]] = {"A": [], "B": []}
        retry_groups: dict[str, list[list[Episode]]] = {"A": [], "B": []}
        all_episodes = [
            episode
            for priority_groups in groups_by_priority.values()
            for source_group in priority_groups
            for episode in source_group
        ]
        identity_members: dict[str, list[Episode]] = {}
        preferred_by_identity: dict[str, Episode] = {}
        for episode in all_episodes:
            for identity in _episode_dedupe_keys(episode):
                identity_members.setdefault(identity, []).append(episode)
                preferred = preferred_by_identity.get(identity)
                if preferred is None or _transcript_preference(
                    episode, self.transcript_resolver
                ) > _transcript_preference(preferred, self.transcript_resolver):
                    preferred_by_identity[identity] = episode

        seen: set[str] = set()
        for priority in ("A", "B"):
            for source_group in groups_by_priority[priority]:
                source_new: list[Episode] = []
                source_retry: list[Episode] = []
                for episode in source_group:
                    published_at = episode.published_at
                    if (
                        published_at is not None
                        and published_at.tzinfo is not None
                        and published_at.astimezone(UTC) < cutoff
                    ):
                        continue
                    identity_keys = _episode_dedupe_keys(episode)
                    if any(
                        preferred_by_identity[identity] is not episode
                        for identity in identity_keys
                    ):
                        continue
                    if seen & identity_keys:
                        continue
                    aliases = {
                        member.id
                        for identity in identity_keys
                        for member in identity_members.get(identity, ())
                    }
                    alias_states = {
                        alias: self.store.episode_review_state(alias)
                        for alias in aliases
                    }
                    # A final or cooling-down alias means this canonical episode
                    # must not be resent under a different syndicated ID.
                    if any(
                        state is None and self.store.has_episode(alias)
                        for alias, state in alias_states.items()
                    ):
                        continue
                    if any(state == "retry" for state in alias_states.values()):
                        review_state = "retry"
                    else:
                        review_state = "new"
                    seen.update(identity_keys)
                    if review_state == "new":
                        source_new.append(episode)
                    elif review_state == "retry":
                        source_retry.append(episode)
                if source_new:
                    new_groups[priority].append(source_new)
                if source_retry:
                    retry_groups[priority].append(source_retry)

        candidates: list[Episode] = []
        # Fresh episodes beat retries globally. Give every configured source a
        # first slot (A before B), then spend remaining capacity on backlog by
        # tier. This preserves priority without starving all B sources whenever
        # the eight high-frequency A feeds each publish multiple episodes.
        for grouped in (new_groups, retry_groups):
            rotated = {
                priority: _rotate_source_groups(
                    grouped[priority],
                    day_number=now.date().toordinal(),
                )
                for priority in ("A", "B")
            }
            first = {
                priority: [
                    source_group[0]
                    for source_group in rotated[priority]
                    if source_group
                ]
                for priority in ("A", "B")
            }
            candidates.extend(_weighted_priority_merge(first["A"], first["B"]))
            rest = {
                priority: _interleave(
                    [source_group[1:] for source_group in rotated[priority]]
                )
                for priority in ("A", "B")
            }
            candidates.extend(_weighted_priority_merge(rest["A"], rest["B"]))

        successful_sources = len(sources) - len(failed_sources)
        self._last_failed_feeds = tuple(failed_sources)
        if failed_sources and (
            not candidates or len(failed_sources) >= successful_sources
        ):
            raise FeedDiscoveryError(
                "The source scan was unhealthy: "
                f"{len(failed_sources)} of {len(sources)} source(s) failed"
            )
        # New releases take precedence over older no-transcript/date retries.
        # Otherwise a backlog from a temporarily blocked provider can consume
        # the bounded candidate budget and starve newly published episodes.
        return candidates[: self.max_daily_candidates]

    def build_daily(self, now: datetime | None = None) -> list[DailyItem]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now.astimezone(UTC) - timedelta(hours=self.lookback_hours)
        candidates = self.discover_daily_candidates(now)
        results: list[DailyItem] = [
            DailyItem(
                Episode(
                    id=f"source-failure:{source_url}",
                    title="节目源扫描失败",
                    url=source_url,
                    show="订阅源",
                ),
                "failed",
                "feed discovery failed",
            )
            for source_url in self._last_failed_feeds
        ]
        summary_count = 0
        priority_b_summaries = 0
        reserve_b_slot = self.max_daily_summaries >= 2
        priority_a_soft_cap = self.max_daily_summaries - int(reserve_b_slot)
        deferred_priority_a: list[
            tuple[Episode, Transcript, StoredTranscript]
        ] = []
        for discovered in candidates:
            episode = discovered
            try:
                # Flat playlist metadata often omits dates and duration. Enrich only
                # the bounded, unseen candidate set instead of every channel item.
                youtube_url = (
                    episode.url
                    if is_youtube_url(episode.url)
                    else str(episode.metadata.get("youtube_url") or "")
                )
                if (
                    is_youtube_url(youtube_url)
                    and (
                        episode.published_at is None
                        or episode.duration_seconds is None
                    )
                ):
                    episode = self._merge_episode(
                        episode, video_metadata(youtube_url)
                    )
                if episode.published_at is None:
                    results.append(DailyItem(episode, "unverified_date"))
                    continue
                if episode.published_at.astimezone(UTC) < cutoff:
                    results.append(DailyItem(episode, "outside_window"))
                    continue
                transcript = self.transcript_resolver.fetch(episode)
                if transcript is None:
                    results.append(DailyItem(episode, "no_transcript"))
                    continue
                stored = self.store.save_verified_transcript(episode, transcript)
                source_priority = str(
                    episode.metadata.get("source_priority") or "B"
                ).upper()
                if (
                    reserve_b_slot
                    and source_priority == "A"
                    and summary_count >= priority_a_soft_cap
                    and priority_b_summaries == 0
                ):
                    deferred_priority_a.append(
                        (episode, transcript, stored)
                    )
                    continue
                summary_candidate = self.summarizer.summarize(episode, transcript)
                summary = self.store.save_transcript_digest(
                    episode.id,
                    summary_candidate,
                    stored.content_sha256,
                    stored.record_revision_sha256,
                )
                results.append(
                    DailyItem(
                        episode,
                        "summarized",
                        summary,
                        _readable_attachment(stored, summary),
                    )
                )
                summary_count += 1
                if source_priority == "B":
                    priority_b_summaries += 1
                if summary_count >= self.max_daily_summaries:
                    break
            except SummaryFormatError as error:
                logger.warning("Podcast summary format failed for %s: %s", episode.url, error)
                results.append(DailyItem(episode, "summary_format_error", str(error)))
            except Exception as error:
                logger.exception("Podcast analysis failed for %s", episode.url)
                results.append(DailyItem(episode, "failed", str(error)))
        for episode, transcript, stored in deferred_priority_a:
            if summary_count >= self.max_daily_summaries:
                break
            try:
                summary_candidate = self.summarizer.summarize(episode, transcript)
                summary = self.store.save_transcript_digest(
                    episode.id,
                    summary_candidate,
                    stored.content_sha256,
                    stored.record_revision_sha256,
                )
            except SummaryFormatError as error:
                logger.warning(
                    "Deferred podcast summary format failed for %s: %s",
                    episode.url,
                    error,
                )
                results.append(DailyItem(episode, "summary_format_error", str(error)))
                continue
            except Exception as error:
                logger.exception("Deferred podcast analysis failed for %s", episode.url)
                results.append(DailyItem(episode, "failed", str(error)))
                continue
            results.append(
                DailyItem(
                    episode,
                    "summarized",
                    summary,
                    _readable_attachment(stored, summary),
                )
            )
            summary_count += 1
        return results
