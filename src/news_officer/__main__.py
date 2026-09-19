from __future__ import annotations

import asyncio
import logging
import os
import threading

from .agent import AgentIntentResolver
from .config import Settings
from .editorial import EditorialPolicy
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
from .tone_advisor import ToneAdvisor


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
        podwise_api_token=settings.podwise_api_token,
        daily_rss_only=settings.daily_rss_only,
        editorial_policy=EditorialPolicy(summarizer.client, settings.openai_model, store,
                                        settings.research_focus_path) if settings.editorial_enabled else None,
    )
    plugins = [SubscriptionPlugin(store)]
    research = None
    if settings.research_agent_enabled:
        library = (
            PodcastArchive(store, settings.podcast_memory_path)
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
            backend=settings.agent_backend,
            hermes_python=settings.hermes_python,
            tone_advisor=ToneAdvisor(settings.deepseek_api_key,
                enabled=settings.tone_advisor_enabled, model=settings.tone_advisor_model),
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


def run_service(runtime, *, shutdown_grace=30) -> None:
    # asyncio cancellation cannot stop a blocked synchronous tool thread. Arm
    # a process-level deadline once shutdown begins, so the cloud supervisor
    # can restart even if Python waits for an executor or channel indefinitely.
    deadline = None

    def begin_shutdown():
        nonlocal deadline
        if deadline is None:
            deadline = threading.Timer(shutdown_grace, os._exit, args=(1,))
            deadline.daemon = True
            deadline.start()

    runtime.on_shutdown = begin_shutdown
    try:
        asyncio.run(runtime.run())
    finally:
        if deadline is not None:
            deadline.cancel()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_service(build_runtime(Settings.from_env()))


if __name__ == "__main__":
    main()
