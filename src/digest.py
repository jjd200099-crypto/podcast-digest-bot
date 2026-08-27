import json
import os
import re
import subprocess
import sys
import tempfile
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
            }


def fetch_full_subtitles(video: dict) -> tuple[str, str] | None:
    """Return clean full VTT transcript and duration, or None if captions are unavailable."""
    with tempfile.TemporaryDirectory() as temp_dir:
        target = str(Path(temp_dir) / "%(id)s.%(ext)s")
        try:
            metadata = run("yt-dlp", "--skip-download", "--print", "%(duration_string)s", video["url"]).strip()
            run(
                "yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
                "--sub-langs", "en,en-US,en-orig", "--sub-format", "vtt", "-o", target, video["url"],
            )
        except subprocess.CalledProcessError:
            return None
        files = list(Path(temp_dir).glob("*.vtt"))
        if not files:
            return None
        # Prefer creator captions if both creator and automatic tracks exist.
        vtt = next((p for p in files if ".en.vtt" in p.name), files[0]).read_text(errors="ignore")
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
        if len(transcript) < 5_000:
            return None
        return transcript, metadata or "时长未知"


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


def send_feishu_to(markdown: str, receive_id: str, receive_id_type: str, token: str) -> None:
    # Feishu post messages have a practical size cap; split only at paragraph boundaries.
    chunks, current = [], ""
    for paragraph in markdown.split("\n\n"):
        candidate = (current + "\n\n" + paragraph).strip()
        if current and len(candidate.encode("utf-8")) > 3500:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    for i, chunk in enumerate(chunks, start=1):
        suffix = f"\n\n（第 {i}/{len(chunks)} 段）" if len(chunks) > 1 else ""
        payload = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": chunk + suffix}, ensure_ascii=False)}
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers=headers, json=payload, timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Feishu send error: {body}")


def send_feishu(markdown: str) -> None:
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
    sent_ids = []
    for video in list(unique.values())[:3]:
        result = fetch_full_subtitles(video)
        if not result:
            print(f"No complete transcript; skipped: {video['title']}")
            sent_ids.append(video["id"])
            continue
        transcript, duration = result
        digest = summarize(video, transcript, duration)
        send_feishu(digest)
        sent_ids.append(video["id"])
        print(f"Sent: {video['title']}")
    # Mark reviewed videos so the same upload is never sent twice.
    state["seen_video_ids"] = (state.get("seen_video_ids", []) + sent_ids)[-500:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
