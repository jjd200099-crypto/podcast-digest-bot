from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _clock(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        raise ValueError(
            "NEWS_OFFICER_DAILY_TIME must use HH:MM, for example 08:30"
        ) from None


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    openai_api_key: str
    openai_model: str
    db_path: Path
    feeds_path: Path
    timezone: ZoneInfo
    daily_time: time
    user_open_ids: tuple[str, ...]
    group_chat_ids: tuple[str, ...]
    max_daily_summaries: int
    max_daily_candidates: int
    lookback_hours: int
    manual_digest_id: str
    research_agent_enabled: bool = False
    library_folder_token: str = ""
    research_user_open_ids: tuple[str, ...] = ()
    research_group_chat_ids: tuple[str, ...] = ()
    knowledge_mode: str = "feishu_folder"
    podwise_api_token: str = ""
    daily_transcript_attachments: bool = False
    daily_combined_message: bool = True
    podcast_memory_path: Path | None = None
    agent_backend: str = "agents_sdk"
    hermes_python: str = ""
    message_workers: int = 4
    health_port: int = 8080
    tone_advisor_enabled: bool = False
    deepseek_api_key: str = ''
    tone_advisor_model: str = 'deepseek-flash'
    editorial_enabled: bool = True
    research_focus_path: Path | None = None
    daily_rss_only: bool = True
    podwise_auto_process: bool = False
    daily_document_enabled: bool = False
    daily_document_start_date: str = ''
    daily_document_folder: str = ''

    @classmethod
    def from_env(cls) -> Settings:
        missing = [
            name
            for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "OPENAI_API_KEY")
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        db_path = Path(
            os.environ.get("NEWS_OFFICER_DB_PATH", "/data/news-officer.sqlite3")
        )
        memory_path = Path(os.environ.get(
            "NEWS_OFFICER_MEMORY_PATH", str(db_path.parent / "podcast-memory")
        ))
        if os.environ.get("RAILWAY_DEPLOYMENT_ID"):
            mount_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").rstrip("/")
            if mount_path != "/data":
                raise RuntimeError(
                    "Railway must attach a persistent volume at /data before startup"
                )
            try:
                db_path.resolve().relative_to(Path("/data").resolve())
            except ValueError:
                raise RuntimeError(
                    "NEWS_OFFICER_DB_PATH must live under the Railway /data volume"
                ) from None
            try:
                memory_path.resolve().relative_to(Path("/data").resolve())
            except ValueError:
                raise RuntimeError("NEWS_OFFICER_MEMORY_PATH must live under the Railway /data volume") from None
        feeds_path = Path(
            os.environ.get("NEWS_OFFICER_FEEDS_PATH", str(ROOT / "feeds.json"))
        )
        timezone_name = os.environ.get("NEWS_OFFICER_TIMEZONE", "Asia/Shanghai")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            raise ValueError(
                f"Unknown NEWS_OFFICER_TIMEZONE: {timezone_name}"
            ) from None

        user_ids = _csv(os.environ.get("FEISHU_USER_OPEN_IDS", ""))
        legacy_user_id = os.environ.get("FEISHU_USER_OPEN_ID", "").strip()
        if legacy_user_id and legacy_user_id not in user_ids:
            user_ids += (legacy_user_id,)

        manual_digest_id = os.environ.get("NEWS_OFFICER_MANUAL_DIGEST_ID", "").strip()
        if manual_digest_id and not re.fullmatch(
            r"[A-Za-z0-9._-]{1,80}", manual_digest_id
        ):
            raise ValueError(
                "NEWS_OFFICER_MANUAL_DIGEST_ID must contain only letters, "
                "numbers, dot, underscore, or hyphen"
            )

        knowledge_mode = os.environ.get(
            "NEWS_OFFICER_KNOWLEDGE_MODE", "feishu_folder"
        ).strip()
        if knowledge_mode not in {"feishu_folder", "podcast_archive"}:
            raise ValueError("Invalid NEWS_OFFICER_KNOWLEDGE_MODE")
        backend = os.environ.get("NEWS_OFFICER_AGENT_BACKEND", "agents_sdk").strip()
        if backend not in {"agents_sdk", "hermes"}:
            raise ValueError("Invalid NEWS_OFFICER_AGENT_BACKEND")
        hermes_python = os.environ.get("NEWS_OFFICER_HERMES_PYTHON", "").strip()
        if backend == "hermes" and not Path(hermes_python).is_absolute():
            raise ValueError("Hermes requires an absolute NEWS_OFFICER_HERMES_PYTHON")
        return cls(
            podwise_auto_process=os.environ.get('PODWISE_AUTO_PROCESS', 'false').lower() == 'true',
            daily_rss_only=os.environ.get('NEWS_OFFICER_DAILY_RSS_ONLY', 'true').lower() == 'true',
            editorial_enabled=os.environ.get('NEWS_OFFICER_EDITORIAL_FILTER', 'true').lower() == 'true',
            research_focus_path=Path(os.environ['NEWS_OFFICER_RESEARCH_FOCUS_PATH'])
                if os.environ.get('NEWS_OFFICER_RESEARCH_FOCUS_PATH') else None,
            tone_advisor_enabled=os.environ.get('NEWS_OFFICER_TONE_ADVISOR', 'false').lower() == 'true',
            deepseek_api_key=os.environ.get('DEEPSEEK_API_KEY', '').strip(),
            tone_advisor_model=os.environ.get('NEWS_OFFICER_TONE_MODEL', 'deepseek-flash').strip(),
            health_port=int(os.environ.get('PORT', '8080')),
            message_workers=max(1, min(16, int(os.environ.get("NEWS_OFFICER_MESSAGE_WORKERS", "4")))),
            agent_backend=backend,
            hermes_python=hermes_python,
            feishu_app_id=os.environ["FEISHU_APP_ID"].strip(),
            feishu_app_secret=os.environ["FEISHU_APP_SECRET"].strip(),
            openai_api_key=os.environ["OPENAI_API_KEY"].strip(),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra").strip(),
            db_path=db_path,
            podcast_memory_path=memory_path,
            feeds_path=feeds_path,
            timezone=timezone,
            daily_time=_clock(os.environ.get("NEWS_OFFICER_DAILY_TIME", "08:30")),
            user_open_ids=user_ids,
            group_chat_ids=_csv(os.environ.get("FEISHU_GROUP_CHAT_IDS", "")),
            max_daily_summaries=max(
                0, int(os.environ.get("NEWS_OFFICER_MAX_SUMMARIES", "0"))
            ),
            max_daily_candidates=max(
                0, int(os.environ.get("NEWS_OFFICER_MAX_CANDIDATES", "0"))
            ),
            lookback_hours=max(
                1, int(os.environ.get("NEWS_OFFICER_LOOKBACK_HOURS", "72"))
            ),
            manual_digest_id=manual_digest_id,
            research_agent_enabled=os.environ.get(
                "NEWS_OFFICER_RESEARCH_AGENT", ""
            ).lower()
            == "true",
            library_folder_token=os.environ.get(
                "NEWS_OFFICER_LIBRARY_FOLDER", ""
            ).strip(),
            research_user_open_ids=_csv(
                os.environ.get("NEWS_OFFICER_RESEARCH_USERS", "")
            ),
            research_group_chat_ids=_csv(
                os.environ.get("NEWS_OFFICER_RESEARCH_CHATS", "")
            ),
            knowledge_mode=knowledge_mode,
            podwise_api_token=os.environ.get("PODWISE_API_TOKEN", "").strip(),
            daily_combined_message=os.environ.get("NEWS_OFFICER_DAILY_COMBINED_MESSAGE", "true").lower() == "true",
            daily_transcript_attachments=os.environ.get(
                "NEWS_OFFICER_DAILY_TRANSCRIPT_ATTACHMENTS", "false"
            ).lower() == "true",
            daily_document_enabled=os.environ.get('NEWS_OFFICER_DAILY_DOCUMENT', 'false').lower() == 'true',
            daily_document_start_date=os.environ.get('NEWS_OFFICER_DAILY_DOCUMENT_START_DATE', ''),
            daily_document_folder=os.environ.get('NEWS_OFFICER_DAILY_DOCUMENT_FOLDER', ''),
        )
