import json
import os
import re
import sys
from urllib.parse import urlsplit

from digest import fetch_full_subtitles, reply_feishu, run, summarize


URL_RE = re.compile(r"https?://[^\s<>]+")
MENTION_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE)
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def clean_request(text: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub(" ", text)).strip()


def video_metadata(url: str) -> dict:
    raw = run("yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json", url)
    data = json.loads(raw)
    return {
        "id": data.get("id") or url,
        "title": data.get("title") or "用户提交的播客",
        "url": data.get("webpage_url") or data.get("original_url") or url,
        "channel": data.get("channel") or data.get("uploader") or "文字稿未明确",
        "duration_seconds": data.get("duration"),
        "duration_string": data.get("duration_string"),
    }


def help_text() -> str:
    return (
        "你可以直接发一条 YouTube 或播客链接，我会先寻找完整文字稿；"
        "只有取得完整、可核验的文字稿时才生成中文会议纪要。\n\n"
        "如果文字稿不完整或受限，我会明确回复“未取得完整文字稿，本次不摘要”。"
    )


def build_reply(request_text: str) -> str:
    text = clean_request(request_text)
    match = URL_RE.search(text)
    if not match:
        return help_text()

    url = match.group(0).rstrip(".,，。)]）")
    if (urlsplit(url).hostname or "").lower() not in ALLOWED_HOSTS:
        return "目前交互分析只接受公开 YouTube 链接；其他播客源仍由每日任务按完整文字稿规则处理。"
    video = video_metadata(url)
    result = fetch_full_subtitles(video)
    if not result:
        return (
            f"节目：{video['title']}\n"
            f"链接：{video['url']}\n\n"
            "未取得完整文字稿，本次不摘要。"
        )
    transcript, duration = result
    return summarize(video, transcript, duration)


def is_supported_link_request(request_text: str) -> bool:
    match = URL_RE.search(clean_request(request_text))
    if not match:
        return False
    return (urlsplit(match.group(0).rstrip(".,，。)]）")).hostname or "").lower() in ALLOWED_HOSTS


def main() -> None:
    message_id = os.environ.get("FEISHU_MESSAGE_ID", "").strip()
    request_text = os.environ.get("FEISHU_REQUEST_TEXT", "")
    if not message_id.startswith("om_"):
        raise RuntimeError("FEISHU_MESSAGE_ID is missing or invalid")
    if not request_text.strip():
        raise RuntimeError("FEISHU_REQUEST_TEXT is empty")

    if is_supported_link_request(request_text):
        reply_feishu(
            "收到。我正在寻找并核验完整文字稿；只有确认文字稿完整后才会整理，通常需要几分钟。",
            message_id,
            f"{message_id}-ack",
        )
    try:
        reply = build_reply(request_text)
    except Exception as error:
        print(f"Request processing failed: {error}", file=sys.stderr)
        reply = "处理失败，请稍后重试，或换一个包含公开完整文字稿的链接。"
    reply_feishu(reply, message_id, message_id)


if __name__ == "__main__":
    main()
