from __future__ import annotations

import asyncio
import logging

from .agent import AgentIntentResolver
from .config import Settings
from .feishu import FeishuMessenger
from .library import FeishuLibraryAPI, PodcastLibrary
from .podcast import PodcastService
from .podcast_archive import PodcastArchive
from .qa import TranscriptQAService
from .research_agent import PodcastResearchAgent
from .router import (
    CommandRouter,
    HelpPlugin,
    PodcastPlugin,
    SubscriptionPlugin,
    TranscriptInteractionPlugin,
)
from .runtime import NewsOfficerRuntime
from .source_registry import SourceRegistry
from .store import Store
from .summarizer import TranscriptSummarizer


def build_runtime(settings: Settings) -> NewsOfficerRuntime:
    store = Store(settings.db_path)
    messenger = FeishuMessenger(settings.feishu_app_id, settings.feishu_app_secret)
    registry = (
        SourceRegistry(store, settings.feeds_path)
        if settings.research_agent_enabled
        else None
    )
    summarizer = TranscriptSummarizer(settings.openai_api_key, settings.openai_model)
    qa = TranscriptQAService(settings.openai_api_key, settings.openai_model)
    intent_resolver = AgentIntentResolver(
        settings.openai_api_key, settings.openai_model
    )
    podcast = PodcastService(
        store=store,
        feeds_path=settings.feeds_path,
        summarizer=summarizer,
        lookback_hours=settings.lookback_hours,
        max_daily_candidates=settings.max_daily_candidates,
        max_daily_summaries=settings.max_daily_summaries,
        source_registry=registry,
    )
    plugins = [SubscriptionPlugin(store)]
    research = None
    if settings.research_agent_enabled:
        library = (
            PodcastArchive(store)
            if settings.knowledge_mode == "podcast_archive"
            else PodcastLibrary(
                store, FeishuLibraryAPI(messenger), settings.library_folder_token
            )
        )
        research = PodcastResearchAgent(
            store,
            registry,
            library,
            settings.openai_api_key,
            settings.openai_model,
            users=settings.research_user_open_ids,
            chats=settings.research_group_chat_ids,
            podcast_service=podcast,
        )
        plugins.append(research)
    router = CommandRouter(
        plugins
        + [
            PodcastPlugin(podcast),
            TranscriptInteractionPlugin(store, qa, intent_resolver),
            HelpPlugin(),
        ]
    )
    return NewsOfficerRuntime(
        settings, store, messenger, router, podcast, research_agent=research
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(build_runtime(Settings.from_env()).run())


if __name__ == "__main__":
    main()
