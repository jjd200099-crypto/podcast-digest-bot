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
        return cls(
            feishu_app_id=os.environ["FEISHU_APP_ID"].strip(),
            feishu_app_secret=os.environ["FEISHU_APP_SECRET"].strip(),
            openai_api_key=os.environ["OPENAI_API_KEY"].strip(),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra").strip(),
            db_path=db_path,
            feeds_path=feeds_path,
            timezone=timezone,
            daily_time=_clock(os.environ.get("NEWS_OFFICER_DAILY_TIME", "08:30")),
            user_open_ids=user_ids,
            group_chat_ids=_csv(os.environ.get("FEISHU_GROUP_CHAT_IDS", "")),
            max_daily_summaries=max(
                1, int(os.environ.get("NEWS_OFFICER_MAX_SUMMARIES", "3"))
            ),
            max_daily_candidates=max(
                1, int(os.environ.get("NEWS_OFFICER_MAX_CANDIDATES", "16"))
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
        )
