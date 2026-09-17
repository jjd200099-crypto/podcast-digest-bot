"""Opt-in live model smoke test; temporary DB, no Feishu message or source writes.

Run on the configured cloud host with PYTHONPATH=src. Requires existing cloud
credentials; prints only answers and never credentials. Not included in CI.
"""

import tempfile
from pathlib import Path

from news_officer.config import Settings
from news_officer.feishu import FeishuMessenger
from news_officer.library import FeishuLibraryAPI, PodcastLibrary
from news_officer.models import IncomingMessage
from news_officer.research_agent import PodcastResearchAgent
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


def main():
    settings = Settings.from_env()
    messenger = FeishuMessenger(settings.feishu_app_id, settings.feishu_app_secret)
    with tempfile.TemporaryDirectory(prefix="podcast-agent-smoke-") as temporary:
        store = Store(Path(temporary) / "state.sqlite3")
        store.initialize()
        registry = SourceRegistry(store, settings.feeds_path)
        library = PodcastLibrary(store, FeishuLibraryAPI(messenger), "")
        agent = PodcastResearchAgent(
            store,
            registry,
            library,
            settings.openai_api_key,
            settings.openai_model,
            users=("smoke-user",),
        )
        agent.initialize()
        for index, question in enumerate(
            (
                "你在监听哪些播客？",
                "这些里面有 Acquired 吗？",
                "帮我追踪纽约时报 The New York Times 的 Hard Fork 播客。",
                "确认添加",
            )
        ):
            message = IncomingMessage(
                f"smoke-{index}", "smoke-chat", question, "p2p", "smoke-user"
            )
            result = agent.handle(question, message)
            print(question, flush=True)
            print(result.messages[0], flush=True)
        reopened = SourceRegistry(Store(store.path), settings.feeds_path)
        added = [s for s in reopened.list() if s.get("origin") == "conversation"]
        assert any(s["name"] == "Hard Fork" for s in added), (
            "Source addition did not complete"
        )
        print(
            "PASS: 新建 Registry 从临时 SQLite 读到了 Hard Fork；生产订阅未改动。",
            flush=True,
        )


if __name__ == "__main__":
    main()
