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
from .models import AnalysisResult, DailyItem, Episode, Transcript
from .official import DwarkeshOfficialTranscriptProvider
from .sequoia import SequoiaOfficialTranscriptProvider
from .store import Store
from .youtube import (
    YouTubeTranscriptProvider,
    is_youtube_url,
    latest_videos,
    video_metadata,
)

logger = logging.getLogger(__name__)


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
        if (
            source.get("enabled", True)
            and source.get("type") == "youtube"
            and source.get("url")
        ):
            keywords = tuple(
                str(keyword).strip()
                for keyword in source.get("include_keywords") or ()
                if str(keyword).strip()
            )
            try:
                scan_depth = int(source.get("scan_depth", 4))
            except (TypeError, ValueError):
                scan_depth = 4
            sources.append(
                YouTubeFeedSource(
                    str(source["url"]),
                    keywords,
                    min(50, max(1, scan_depth)),
                )
            )
    return sources


def load_youtube_feeds(path: Path) -> list[str]:
    """Compatibility helper for callers that only need source URLs."""

    return [source.url for source in load_youtube_sources(path)]


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

    def supports_url(self, url: str) -> bool:
        return is_youtube_url(url) or self.transcript_resolver.supports_url(url)

    @staticmethod
    def _merge_episode(discovered: Episode, enriched: Episode) -> Episode:
        """Keep trustworthy feed fields when a fallback only returns partial metadata."""

        return Episode(
            id=enriched.id or discovered.id,
            title=enriched.title or discovered.title,
            url=enriched.url or discovered.url,
            show=enriched.show or discovered.show,
            duration_seconds=(
                enriched.duration_seconds
                if enriched.duration_seconds is not None
                else discovered.duration_seconds
            ),
            duration_string=enriched.duration_string or discovered.duration_string,
            published_at=enriched.published_at or discovered.published_at,
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
                    "目前可直接分析公开 YouTube，以及 David Senra/Founders、"
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
        return AnalysisResult(
            status="summarized",
            episode=episode,
            message=self.summarizer.summarize(episode, transcript),
        )

    def discover_daily_candidates(self) -> list[Episode]:
        groups: list[list[Episode]] = []
        failed_feeds: list[str] = []
        sources = load_youtube_sources(self.feeds_path)
        for source in sources:
            try:
                groups.append(
                    [
                        episode
                        for episode in latest_videos(
                            source.url, playlist_end=source.scan_depth
                        )
                        if source.accepts(episode)
                    ]
                )
            except Exception as error:  # noqa: BLE001 - one feed must not block the digest
                failed_feeds.append(source.url)
                logger.warning("Skipping unavailable feed %s: %s", source.url, error)
        unique: dict[str, Episode] = {}
        for episode in _interleave(groups):
            if (
                episode.id not in unique
                and self.store.should_review_episode(episode.id)
            ):
                unique[episode.id] = episode
        successful_feeds = len(sources) - len(failed_feeds)
        if failed_feeds and (
            not unique or len(failed_feeds) >= successful_feeds
        ):
            raise FeedDiscoveryError(
                "The source scan was unhealthy: "
                f"{len(failed_feeds)} of {len(sources)} feed(s) failed"
            )
        return list(unique.values())[: self.max_daily_candidates]

    def build_daily(self, now: datetime | None = None) -> list[DailyItem]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now.astimezone(UTC) - timedelta(hours=self.lookback_hours)
        results: list[DailyItem] = []
        summary_count = 0
        for discovered in self.discover_daily_candidates():
            episode = discovered
            try:
                # Flat playlist metadata often omits dates and duration. Enrich only
                # the bounded, unseen candidate set instead of every channel item.
                if episode.published_at is None or episode.duration_seconds is None:
                    episode = self._merge_episode(
                        episode, video_metadata(episode.url)
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
                summary = self.summarizer.summarize(episode, transcript)
                results.append(DailyItem(episode, "summarized", summary))
                summary_count += 1
                if summary_count >= self.max_daily_summaries:
                    break
            except Exception as error:
                logger.exception("Podcast analysis failed for %s", episode.url)
                results.append(DailyItem(episode, "failed", str(error)))
        return results
