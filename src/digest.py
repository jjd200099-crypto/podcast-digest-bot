import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import requests
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state.json"
FEEDS_PATH = ROOT / "feeds.json"
USER_OPEN_ID = os.environ.get("FEISHU_USER_OPEN_ID", "ou_b3bfd8beda8f00996f6f2014da9432cf")
GROUP_CHAT_IDS = [
    chat_id.strip()
    for chat_id in os.environ.get("FEISHU_GROUP_CHAT_IDS", "").split(",")
    if chat_id.strip()
]
BRAND_HEADER = "📰 新闻官｜每日播客情报"
VTT_CUE_RE = re.compile(
    r"(?m)^(?P<start>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})"
)


def brand_message(markdown: str) -> str:
    """Add a stable user-facing identity without changing Feishu app permissions."""
    content = markdown.strip()
    if content.startswith(BRAND_HEADER):
        return content
    return f"{BRAND_HEADER}\n\n{content}"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def latest_videos(channel: str):
    raw = run("yt-dlp", "--flat-playlist", "--playlist-end", "4", "--dump-single-json", channel)
    data = json.loads(raw)
    for entry in data.get("entries") or []:
        if entry.get("id"):
            yield {
                "id": entry["id"],
                "title": entry.get("title", "Untitled"),
                "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry['id']}",
                "channel": entry.get("channel") or data.get("channel") or channel,
                "duration_seconds": entry.get("duration"),
                "duration_string": entry.get("duration_string"),
            }


def timestamp_seconds(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def vtt_covers_episode(vtt: str, duration_seconds: float) -> bool:
    """Conservatively reject caption tracks that do not span almost all of an episode."""
    cues = list(VTT_CUE_RE.finditer(vtt))
    if not cues or duration_seconds <= 0:
        return False
    intervals = sorted(
        (
            max(0.0, timestamp_seconds(cue.group("start"))),
            min(duration_seconds, timestamp_seconds(cue.group("end"))),
        )
        for cue in cues
    )
    intervals = [(start, end) for start, end in intervals if end > start]
    if not intervals:
        return False
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    first_start = merged[0][0]
    last_end = merged[-1][1]
    covered_seconds = sum(end - start for start, end in merged)
    gaps = [next_start - end for (_, end), (next_start, _) in zip(merged, merged[1:])]
    max_gap = max(gaps, default=0.0)
    allowed_gap = min(180.0, max(60.0, duration_seconds * 0.03))
    return (
        first_start <= min(180, duration_seconds * 0.10)
        and (duration_seconds - last_end) <= min(180, duration_seconds * 0.10)
        and (last_end - first_start) >= duration_seconds * 0.85
        and covered_seconds >= duration_seconds * 0.65
        and max_gap <= allowed_gap
    )


def fetch_full_subtitles(video: dict) -> tuple[str, str] | None:
    """Return clean full VTT transcript and duration, or None if captions are unavailable."""
    with tempfile.TemporaryDirectory() as temp_dir:
        target = str(Path(temp_dir) / "%(id)s.%(ext)s")
        try:
            duration_seconds = video.get("duration_seconds")
            duration_string = video.get("duration_string")
            if not duration_seconds:
                metadata = json.loads(
                    run("yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json", video["url"])
                )
                duration_seconds = metadata.get("duration")
                duration_string = metadata.get("duration_string")
            duration_seconds = float(duration_seconds)
            run(
                "yt-dlp", "--no-playlist", "--skip-download", "--write-subs", "--write-auto-subs",
                "--sub-langs", "en,en-US,en-orig", "--sub-format", "vtt", "-o", target, video["url"],
            )
        except (subprocess.CalledProcessError, TypeError, ValueError, json.JSONDecodeError):
            return None
        files = list(Path(temp_dir).glob("*.vtt"))
        if not files:
            return None
        # Prefer creator captions, but fall through to another complete English track if needed.
        tracks = sorted(files, key=lambda path: 0 if ".en.vtt" in path.name else 1)
        for path in tracks:
            vtt = path.read_text(errors="ignore")
            if not vtt_covers_episode(vtt, duration_seconds):
                continue
            lines, previous = [], ""
            for line in vtt.splitlines():
                if not line.strip() or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
                    continue
                clean = re.sub(r"<[^>]+>", "", line).strip()
                if clean and clean != previous:
                    lines.append(clean)
                    previous = clean
            transcript = "\n".join(lines)
            # A subtitle file that only contains an intro cannot qualify as a full transcript.
            minimum_chars = max(5_000, round(duration_seconds / 60) * 300)
            if len(transcript) >= minimum_chars:
                return transcript, duration_string or f"{round(duration_seconds / 60)} 分钟"
        return None


def summarize(video: dict, transcript: str, duration: str) -> str:
    prompt = f"""你是投资研究团队的播客编辑。请严格只依据以下完整英文文字稿，用中文生成一份高密度会议纪要。

节目：{video['title']}
频道/主播：{video['channel']}
链接：{video['url']}
时长：{duration}

要求：
1. 使用 Markdown；先给出标题、主播/嘉宾（若文字稿无法确认则写“文字稿未明确”）、链接和时长。
2. 按 4 到 6 个主题组织，输出 10 到 20 条编号洞察。删除广告、寒暄和重复。
3. 重点保留创业、AI、技术和投资相关的具体论点、数字、反共识判断及可执行启示。
4. 嘉宾预测、公司自述、未经审计的数据必须标明“嘉宾观点”“公司主张”或“模型估算”。不得补充文字稿外的事实。
5. 不要写“我无法确认”之类的过程性内容。

完整文字稿如下：
{transcript}
"""
    client = OpenAI()
    response = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
        input=prompt,
        store=False,
    )
    return response.output_text.strip()


def feishu_token() -> str:
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": os.environ["FEISHU_APP_ID"], "app_secret": os.environ["FEISHU_APP_SECRET"]},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Feishu token error: {body}")
    return body["tenant_access_token"]


def split_utf8(text: str, max_bytes: int) -> list[str]:
    pieces, current, size = [], [], 0
    for character in text:
        character_size = len(character.encode("utf-8"))
        if current and size + character_size > max_bytes:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += character_size
    if current:
        pieces.append("".join(current))
    return pieces


def split_feishu_message(markdown: str, max_bytes: int = 3500) -> list[str]:
    # Prefer paragraph boundaries, but also protect against a single oversized paragraph.
    chunks, current = [], ""
    for paragraph in markdown.split("\n\n"):
        for piece in split_utf8(paragraph, max_bytes):
            candidate = (current + "\n\n" + piece).strip()
            if current and len(candidate.encode("utf-8")) > max_bytes:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def check_feishu_response(response: requests.Response, operation: str) -> None:
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Feishu {operation} error: {body}")


def feishu_uuid(idempotency_key: str, part: int) -> str:
    """Return a stable API-safe UUID without exposing or lengthening the source message ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"news-officer:{idempotency_key}:{part}"))


def send_feishu_to(markdown: str, receive_id: str, receive_id_type: str, token: str) -> None:
    chunks = split_feishu_message(markdown)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    for i, chunk in enumerate(chunks, start=1):
        suffix = f"\n\n（第 {i}/{len(chunks)} 段）" if len(chunks) > 1 else ""
        payload = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": chunk + suffix}, ensure_ascii=False)}
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers=headers, json=payload, timeout=30,
        )
        check_feishu_response(response, "send")


def reply_feishu(markdown: str, message_id: str, idempotency_key: str) -> None:
    """Reply to the source message with a deterministic UUID for retry idempotency."""
    token = feishu_token()
    chunks = split_feishu_message(brand_message(markdown))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    for i, chunk in enumerate(chunks, start=1):
        suffix = f"\n\n（第 {i}/{len(chunks)} 段）" if len(chunks) > 1 else ""
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": chunk + suffix}, ensure_ascii=False),
            "uuid": feishu_uuid(idempotency_key, i),
        }
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers=headers,
            json=payload,
            timeout=30,
        )
        check_feishu_response(response, "reply")


def send_feishu(markdown: str) -> None:
    markdown = brand_message(markdown)
    token = feishu_token()
    send_feishu_to(markdown, USER_OPEN_ID, "open_id", token)
    for chat_id in GROUP_CHAT_IDS:
        send_feishu_to(markdown, chat_id, "chat_id", token)


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured")
    state = json.loads(STATE_PATH.read_text())
    seen = set(state.get("seen_video_ids", []))
    feeds = json.loads(FEEDS_PATH.read_text())
    candidates = []
    for channel in feeds["youtube_channels"]:
        try:
            candidates.extend(v for v in latest_videos(channel) if v["id"] not in seen)
        except Exception as error:
            print(f"Skipping unavailable channel {channel}: {error}", file=sys.stderr)
    # Avoid duplicate uploads and cap daily API spending.
    unique = {v["id"]: v for v in candidates}
    reviewed_ids = []
    summary_count = 0
    for video in list(unique.values())[:12]:
        try:
            result = fetch_full_subtitles(video)
        except Exception as error:
            print(f"Transcript check failed; skipped {video['title']}: {error}", file=sys.stderr)
            reviewed_ids.append(video["id"])
            continue
        if not result:
            print(f"No complete transcript; skipped: {video['title']}")
            reviewed_ids.append(video["id"])
            continue
        transcript, duration = result
        digest = summarize(video, transcript, duration)
        send_feishu(digest)
        reviewed_ids.append(video["id"])
        summary_count += 1
        print(f"Sent: {video['title']}")
        if summary_count >= 3:
            break
    if summary_count == 0:
        send_feishu("今日无可摘要的关键播客更新。")
    # Mark reviewed videos so the same upload is never sent twice.
    state["seen_video_ids"] = (state.get("seen_video_ids", []) + reviewed_ids)[-500:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
