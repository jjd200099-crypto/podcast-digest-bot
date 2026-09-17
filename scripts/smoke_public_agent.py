"""Live production-code test with temporary state; no sends or Drive access."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from news_officer.config import Settings
from news_officer.library import FeishuLibraryAPI
from news_officer.models import IncomingMessage
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import PodcastResearchAgent
from news_officer.source_registry import SourceRegistry
from news_officer.store import Store


def main():
    settings = Settings.from_env()
    assert settings.research_agent_enabled
    assert settings.knowledge_mode == "podcast_archive"
    records = Store(settings.db_path).list_recent_transcripts(50)
    records = [
        r for r in records if r.episode.title in {"Luca Ferrari", "The Home Depot"}
    ]
    assert len(records) == 2
    with tempfile.TemporaryDirectory(prefix="public-agent-test-") as directory:
        store = Store(Path(directory) / "state.db")
        store.initialize()
        for record in records:
            store.save_verified_transcript(record.episode, record.transcript)
        agent = PodcastResearchAgent(
            store,
            SourceRegistry(store, settings.feeds_path),
            PodcastArchive(store),
            settings.openai_api_key,
            settings.openai_model,
            users=("smoke",),
        )
        agent.initialize()
        with patch.object(
            FeishuLibraryAPI,
            "request",
            side_effect=AssertionError("Drive must not be called"),
        ) as drive:
            for i, question in enumerate(
                (
                    "Luca Ferrari 对收购的主要观点是什么？请给三条并附出处。",
                    "那和 Home Depot 的经营方式有什么异同？请引用这两期原文。",
                    "把刚才 Luca Ferrari 那期的完整文字稿发我。",
                    "你能读取我们组织里的飞书文档吗？",
                )
            ):
                message = IncomingMessage(
                    f"public-smoke-{i}", "smoke", question, "p2p", "smoke"
                )
                result = agent.handle(question, message)
                print(question, flush=True)
                print(result.messages[0], flush=True)
                if i == 0:
                    assert "davidsenra" in result.messages[0]
                if i == 1:
                    for record in records:
                        assert record.transcript.source_url in result.messages[0]
                if i == 2:
                    assert len(result.attachments) == 1
                    assert result.attachment_episode_ids == tuple(
                        a.episode_id for a in result.attachments
                    )
                    assert agent.handle(question, message) == result
                    print(
                        "PASS: downloadable transcript descriptor + replay", flush=True
                    )
                if i == 3:
                    assert any(
                        s in result.messages[0]
                        for s in (
                            "未接通",
                            "不能",
                            "无法",
                            "暂未",
                            "不可以",
                            "目前不",
                            "没有",
                        )
                    )
            drive.assert_not_called()
    print("PASS: actual public archive + live SDK; no Feishu messages sent", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact credential-bearing SDK errors
        print("FAIL TYPE:", type(error).__name__, flush=True)
        raise SystemExit(1) from None
