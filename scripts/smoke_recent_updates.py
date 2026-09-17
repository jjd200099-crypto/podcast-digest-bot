"""Opt-in live-model/RSS test for the user's seven-day query.

Uses a temporary database; never sends Feishu messages or writes production
subscriptions. A transcript-catalog read fails the test, even if later recovered.
"""

import copy
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from news_officer.config import Settings
from news_officer.feishu import FeishuMessenger
from news_officer.library import FeishuLibraryAPI, PodcastLibrary
from news_officer.models import IncomingMessage
from news_officer.research_agent import PodcastResearchAgent
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


def main():
    settings = Settings.from_env()
    with tempfile.TemporaryDirectory(prefix="podcast-recent-smoke-") as directory:
        store = Store(Path(directory) / "state.sqlite3")
        store.initialize()
        registry = SourceRegistry(store, settings.feeds_path)
        library = PodcastLibrary(
            store,
            FeishuLibraryAPI(
                FeishuMessenger(settings.feishu_app_id, settings.feishu_app_secret)
            ),
            "",
        )
        agent = PodcastResearchAgent(
            store,
            registry,
            library,
            settings.openai_api_key,
            settings.openai_model,
            users=("smoke",),
        )
        agent.initialize()
        observed = []
        original = registry.recent

        def recent(days, name=""):
            print("RECENT ARGS:", days, repr(name), flush=True)
            result = original(days, name)
            # The tool removes episodes when preparing its evidence payload.
            observed.append((days, copy.deepcopy(result)))
            print(
                "FEEDS:",
                len(result["checked"]),
                "EPISODES:",
                len(result["episodes"]),
                "FAILURES:",
                len(result["failures"]),
                flush=True,
            )
            return result

        question = "告诉我过去一周更新了什么播客"
        with (
            patch.object(registry, "recent", side_effect=recent),
            patch.object(library, "snapshot", side_effect=AssertionError) as snapshot,
        ):
            reply = agent.handle(
                question,
                IncomingMessage("recent-smoke", "smoke", question, "p2p", "smoke"),
            )
        print("ANSWER:", reply.messages[0], flush=True)
        snapshot.assert_not_called()
        assert observed and all(days == 7 for days, _ in observed)
        expected_sources = {source["name"] for source in registry.list()}
        assert expected_sources
        assert any(
            set(result["checked"]) == expected_sources for _, result in observed
        ), "The request was for all tracked podcasts, not a nonexistent show filter"
        for _, result in observed:
            for episode in result["episodes"]:
                assert (
                    datetime.fromisoformat(result["from"])
                    <= datetime.fromisoformat(episode["published_at"])
                    <= datetime.fromisoformat(result["to"])
                )
            if result["episodes"]:
                for episode in result["episodes"][:60]:
                    title = (
                        episode["title"]
                        .replace("[", "［")
                        .replace("]", "］")
                        .replace("\n", " ")
                    )
                    assert f"[{title}]({episode['url']})" in reply.messages[0], (
                        "Returned episode was omitted or its title rewritten"
                    )
        print(
            "PASS: dated live RSS query; no transcript catalog or production writes",
            flush=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact SDK authentication metadata
        print("FAIL TYPE:", type(error).__name__, flush=True)
        raise SystemExit(1) from None
