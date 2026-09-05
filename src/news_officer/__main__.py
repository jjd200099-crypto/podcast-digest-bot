from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .feishu import FeishuMessenger
from .podcast import PodcastService
from .router import CommandRouter, HelpPlugin, PodcastPlugin
from .runtime import NewsOfficerRuntime
from .store import Store
from .summarizer import TranscriptSummarizer


def build_runtime(settings: Settings) -> NewsOfficerRuntime:
    store = Store(settings.db_path)
    messenger = FeishuMessenger(settings.feishu_app_id, settings.feishu_app_secret)
    summarizer = TranscriptSummarizer(settings.openai_api_key, settings.openai_model)
    podcast = PodcastService(
        store=store,
        feeds_path=settings.feeds_path,
        summarizer=summarizer,
        lookback_hours=settings.lookback_hours,
        max_daily_candidates=settings.max_daily_candidates,
        max_daily_summaries=settings.max_daily_summaries,
    )
    router = CommandRouter([PodcastPlugin(podcast), HelpPlugin()])
    return NewsOfficerRuntime(settings, store, messenger, router, podcast)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(build_runtime(Settings.from_env()).run())


if __name__ == "__main__":
    main()
