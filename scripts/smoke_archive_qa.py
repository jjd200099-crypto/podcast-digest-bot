"""Opt-in model/retrieval smoke test using real archived transcripts.

Feishu is explicitly replaced by a corpus fixture; this is NOT a Feishu folder
integration test. Production SQLite is read only. All turns use a temporary DB.
No messages are sent and no subscriptions or documents are modified.
"""

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import news_officer.research_agent as agent_module
from news_officer.config import Settings
from news_officer.library import LibraryDocument
from news_officer.models import IncomingMessage
from news_officer.research_agent import PodcastResearchAgent, ResearchTools
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


class CorpusFixture:
    def __init__(self, records, app_id):
        self.api = SimpleNamespace(messenger=SimpleNamespace(app_id=app_id))
        self.docs = [
            LibraryDocument(
                r.reference, r.episode.title, r.transcript.source_url, r.transcript.text
            )
            for r in records
        ]

    def initialize(self):
        pass

    def snapshot(self):
        return self.docs, []


def main():
    settings = Settings.from_env()
    records = Store(settings.db_path).list_recent_transcripts(50)
    records = [
        r for r in records if r.episode.title in {"Luca Ferrari", "The Home Depot"}
    ]
    assert len(records) == 2, "Required real transcript fixtures are unavailable"
    print(
        "TEST BOUNDARY: real transcripts + live model, simulated folder adapter; no Feishu writes",
        flush=True,
    )
    calls = []

    class ObservedTools(ResearchTools):
        def execute(self, name, args):
            calls.append(name)
            print("TOOL:", name, flush=True)
            result = super().execute(name, args)
            print("TOOL DONE:", name, flush=True)
            return result

    with tempfile.TemporaryDirectory(prefix="podcast-qa-smoke-") as temporary:
        store = Store(Path(temporary) / "state.sqlite3")
        store.initialize()
        registry = SourceRegistry(store, settings.feeds_path)
        agent = PodcastResearchAgent(
            store,
            registry,
            CorpusFixture(records, settings.feishu_app_id),
            settings.openai_api_key,
            settings.openai_model,
            users=("smoke",),
        )
        agent.initialize()
        with patch.object(agent_module, "ResearchTools", ObservedTools):
            for index, question in enumerate(
                (
                    "资料库里 Luca Ferrari 如何解释 Bending Spoons 的收购策略？请给三个简短要点。",
                    "那和 Home Depot 的经营方式有什么异同？请基于这两期各自的内容比较，不要只凭公司常识。",
                )
            ):
                before = len(calls)
                started = time.monotonic()
                print("QUESTION START:", question, flush=True)
                reply = agent.handle(
                    question,
                    IncomingMessage(str(index), "smoke", question, "p2p", "smoke"),
                )
                assert any(
                    n in {"search_library", "read_document"} for n in calls[before:]
                ), "Model did not read evidence"
                print("QUESTION:", question, flush=True)
                print(reply.messages[0], flush=True)
                assert "https://" in reply.messages[0], "No source citation returned"
                required = (
                    records
                    if index
                    else [r for r in records if r.episode.title == "Luca Ferrari"]
                )
                for record in required:
                    assert record.transcript.source_url in reply.messages[0], (
                        "Missing required source citation: " + record.episode.title
                    )
                print("ELAPSED SECONDS:", round(time.monotonic() - started), flush=True)
    print(
        "PASS: tool-based live-model retrieval returned cited answers; Feishu integration still unverified",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact SDK authentication metadata
        # Never print SDK exceptions which could expose authentication metadata.
        print("FAIL TYPE:", type(error).__name__, flush=True)
        raise SystemExit(1) from None
