from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .colossus import ColossusOfficialTranscriptProvider
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
        for provider in self.providers:
            factory = getattr(provider, "episode_from_url", None)
            if factory:
                episode = factory(url)
                if episode:
                    return episode
        return None


def load_youtube_feeds(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    # Backward compatible with the existing deployed feeds.json.
    if isinstance(data.get("youtube_channels"), list):
        return [str(url) for url in data["youtube_channels"] if str(url).strip()]
    channels: list[str] = []
    for source in data.get("sources") or []:
        if (
            source.get("enabled", True)
            and source.get("type") == "youtube"
            and source.get("url")
        ):
            channels.append(str(source["url"]))
    return channels


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

    def analyze_url(self, url: str) -> AnalysisResult:
        if not self.supports_url(url):
            return AnalysisResult(
                status="unsupported",
                message=(
                    "目前可直接分析公开 YouTube，以及 Dwarkesh、Sequoia 和 "
                    "Invest Like the Best/Colossus 的官方节目页。"
                ),
            )
        episode = (
            video_metadata(url)
            if is_youtube_url(url)
            else self.transcript_resolver.episode_from_url(url)
        )
        if episode is None:
            return AnalysisResult(
                status="unsupported",
                message="未能读取这个官方播客页面。请检查链接是否公开可访问。",
            )
        youtube_url = str(episode.metadata.get("youtube_url") or "")
        if episode.duration_seconds is None and is_youtube_url(youtube_url):
            youtube_episode = video_metadata(youtube_url)
            episode = replace(
                episode,
                duration_seconds=youtube_episode.duration_seconds,
                duration_string=youtube_episode.duration_string,
                metadata={**youtube_episode.metadata, **episode.metadata},
            )
        transcript = self.transcript_resolver.fetch(episode)
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
        channels = load_youtube_feeds(self.feeds_path)
        for channel in channels:
            try:
                groups.append(latest_videos(channel))
            except Exception as error:  # noqa: BLE001 - one feed must not block the digest
                failed_feeds.append(channel)
                logger.warning("Skipping unavailable feed %s: %s", channel, error)
        unique: dict[str, Episode] = {}
        for episode in _interleave(groups):
            if (
                episode.id not in unique
                and self.store.should_review_episode(episode.id)
            ):
                unique[episode.id] = episode
        successful_feeds = len(channels) - len(failed_feeds)
        if failed_feeds and (
            not unique or len(failed_feeds) >= successful_feeds
        ):
            raise FeedDiscoveryError(
                "The source scan was unhealthy: "
                f"{len(failed_feeds)} of {len(channels)} feed(s) failed"
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
                    episode = video_metadata(episode.url)
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
