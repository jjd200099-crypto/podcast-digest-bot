"""Feishu folder is the corpus of record; SQLite tracks resumable publication."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .feishu import FEISHU_API
from .transcript_view import render_readable_transcript


class LibraryError(RuntimeError):
    pass


class FeishuLibraryAPI:
    def __init__(self, messenger):
        self.messenger = messenger
        self._lock = threading.Lock()

    def request(self, method, path, *, params=None, data=None):
        # Bound concurrency and rate across archive and interactive reads.
        with self._lock:
            for attempt in range(4):
                response = requests.request(
                    method,
                    FEISHU_API + path,
                    headers={"Authorization": "Bearer " + self.messenger.token()},
                    params=params,
                    json=data,
                    timeout=30,
                )
                body = response.json()
                if body.get("code") == 0 and response.ok:
                    time.sleep(0.35)
                    return body.get("data", {})
                if body.get("code") == 99991400 or response.status_code == 429:
                    time.sleep(2**attempt)
                    continue
                # Do not print API response objects: they may contain authentication data.
                scopes = [
                    v.get("subject", "")
                    for v in body.get("error", {}).get("permission_violations", [])
                ]
                suffix = ("，缺少权限：" + ", ".join(scopes)) if scopes else ""
                raise LibraryError(
                    f"飞书接口错误 {body.get('code', response.status_code)}{suffix}"
                )
            raise LibraryError("飞书接口限流，请稍后重试")

    def pages(self, path, key, params):
        result, seen = [], set()
        params = dict(params)
        while True:
            data = self.request("GET", path, params=params)
            result.extend(data.get(key, []))
            if not data.get("has_more"):
                return result
            token = data.get("next_page_token") or data.get("page_token")
            if not token or token in seen:
                raise LibraryError("飞书分页不完整，本次不使用部分清单")
            seen.add(token)
            params["page_token"] = token

    def files(self, folder):
        if not folder:
            raise LibraryError("未配置播客资料库文件夹")
        return self.pages(
            "/drive/v1/files", "files", {"folder_token": folder, "page_size": 200}
        )

    def blocks(self, token):
        return self.pages(
            f"/docx/v1/documents/{token}/blocks", "items", {"page_size": 500}
        )

    def text(self, token):
        return self.request("GET", f"/docx/v1/documents/{token}/raw_content")["content"]

    def create(self, folder, title):
        return self.request(
            "POST",
            "/docx/v1/documents",
            data={
                "folder_token": folder,
                "title": title,
            },
        )["document"]["document_id"]

    def append(self, token, blocks, start, identity):
        return self.request(
            "POST",
            f"/docx/v1/documents/{token}/blocks/{token}/children",
            params={
                "client_token": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{identity}:{start}")
                )
            },
            data={"children": blocks, "index": start},
        )


def inline_elements(text):
    """Restricted Markdown emitted by our renderer; never interpret HTML or fetch images."""
    parts, cursor = [], 0
    for match in re.finditer(
        r"\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?://[^\s)]+)\)", text
    ):
        if match.start() > cursor:
            parts.append({"text_run": {"content": text[cursor : match.start()]}})
        if match.group(1):
            parts.append(
                {
                    "text_run": {
                        "content": match.group(1),
                        "text_element_style": {"bold": True},
                    }
                }
            )
        else:
            parts.append(
                {
                    "text_run": {
                        "content": match.group(2),
                        "text_element_style": {
                            "link": {"url": quote(match.group(3), safe="")}
                        },
                    }
                }
            )
        cursor = match.end()
    if cursor < len(text):
        parts.append({"text_run": {"content": text[cursor:]}})
    return parts


def markdown_blocks(markdown):
    blocks = []
    for line in markdown.splitlines():
        line = line.rstrip()
        if not line or line.startswith("# "):
            continue
        kind, number = "text", 2
        heading = re.match(r"^(#{2,4}) (.+)$", line)
        if heading:
            level = len(heading[1])
            kind, number, line = f"heading{level}", level + 2, heading[2]
        elif line.startswith("> "):
            kind, number, line = "quote", 15, line[2:]
        elif line.startswith("- "):
            kind, number, line = "bullet", 12, line[2:]
        # Each text element is comfortably below the API's text-size limit.
        for offset in range(0, len(line), 1500):
            blocks.append(
                {
                    "block_type": number,
                    kind: {"elements": inline_elements(line[offset : offset + 1500])},
                }
            )
    return blocks


def block_signature(block):
    number = block["block_type"]
    key = {2: "text", 12: "bullet", 15: "quote"}.get(number, f"heading{number - 2}")
    return number, "".join(
        e.get("text_run", {}).get("content", "")
        for e in block.get(key, {}).get("elements", [])
    )


@dataclass(frozen=True)
class LibraryDocument:
    token: str
    title: str
    url: str
    text: str


class PodcastLibrary:
    def __init__(self, store, api, folder):
        self.store, self.api, self.folder = store, api, folder
        self._archive_lock = threading.Lock()

    def initialize(self):
        with self.store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS library_publications (
                identity TEXT PRIMARY KEY, folder TEXT NOT NULL, episode_id TEXT NOT NULL,
                title TEXT NOT NULL, blocks_json TEXT NOT NULL, document_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending')""")

    def archive_pending(self):
        """All archived transcripts, not just the ten most recent. Never overwrite human edits."""
        if not self.folder:
            return []
        with self._archive_lock:
            with self.store._connect() as db:
                ids = [
                    r[0]
                    for r in db.execute("SELECT episode_id FROM episode_transcripts")
                ]
            documents = self.api.files(self.folder)
            completed = []
            for episode_id in ids:
                record = self.store.get_verified_transcript(episode_id)
                if record is None:
                    continue
                digest = self.store.get_transcript_digest(episode_id)
                _, rendered = render_readable_transcript(record, digest_markdown=digest)
                identity = hashlib.sha256(
                    (self.folder + record.record_revision_sha256).encode()
                ).hexdigest()
                date = (
                    record.episode.published_at.date().isoformat()
                    if record.episode.published_at
                    else "日期未知"
                )
                title = (
                    f"{date}｜{record.episode.show}｜{record.episode.title}"[:730]
                    + f" [{identity[:12]}]"
                )
                with self.store._connect() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO library_publications(identity,folder,episode_id,title,blocks_json) VALUES (?,?,?,?,?)",
                        (
                            identity,
                            self.folder,
                            episode_id,
                            title,
                            json.dumps(
                                markdown_blocks(rendered.decode()), ensure_ascii=False
                            ),
                        ),
                    )
                    row = dict(
                        db.execute(
                            "SELECT * FROM library_publications WHERE identity=?",
                            (identity,),
                        ).fetchone()
                    )
                if row["status"] == "complete":
                    # A deliberately moved/deleted document must not be resurrected.
                    continue
                blocks = json.loads(row["blocks_json"])
                token = row["document_id"]
                if not token:
                    matches = [
                        d["token"]
                        for d in documents
                        if d["name"] == row["title"] and d["type"] == "docx"
                    ]
                    if len(matches) > 1:
                        raise LibraryError("发现重复归档文档，请先人工确认")
                    token = (
                        matches[0]
                        if matches
                        else self.api.create(self.folder, row["title"])
                    )
                    with self.store._connect() as db:
                        db.execute(
                            "UPDATE library_publications SET document_id=? WHERE identity=?",
                            (token, identity),
                        )
                existing = [
                    b
                    for b in self.api.blocks(token)
                    if b.get("parent_id") == token and b["block_type"] != 1
                ]
                expected = [block_signature(b) for b in blocks]
                actual = [block_signature(b) for b in existing]
                if len(actual) > len(expected) or actual != expected[: len(actual)]:
                    raise LibraryError("归档中的文档已被修改，已停止写入以保护人工内容")
                for start in range(len(existing), len(blocks), 50):
                    self.api.append(token, blocks[start : start + 50], start, identity)
                # Verify every paragraph before claiming completion or including it in Q&A.
                actual = [
                    block_signature(b)
                    for b in self.api.blocks(token)
                    if b.get("parent_id") == token and b["block_type"] != 1
                ]
                if actual != expected:
                    raise LibraryError("文档回读与归档内容不一致，未标记完成")
                with self.store._connect() as db:
                    db.execute(
                        "UPDATE library_publications SET status='complete' WHERE identity=?",
                        (identity,),
                    )
                completed.append(token)
            return completed

    def snapshot(self):
        """Fetch current folder membership and text; never use removed or stale documents."""
        if not self.folder:
            raise LibraryError("资料库尚未配置，暂不能检索飞书文档")
        pending, visited, docs, warnings = [self.folder], set(), [], []
        while pending:
            folder = pending.pop()
            if folder in visited:
                continue
            visited.add(folder)
            for item in self.api.files(folder):
                token = item["token"]
                if item["type"] == "folder":
                    pending.append(token)
                    continue
                if item["type"] != "docx":
                    warnings.append(f"{item['name']}：目前仅索引飞书新版文档正文")
                    continue
                # Read publication status after listing: an archive worker may
                # have created a doc meanwhile, or lost its create response.
                with self.store._connect() as db:
                    incomplete = db.execute(
                        "SELECT 1 FROM library_publications WHERE status!='complete' "
                        "AND (document_id=? OR (folder=? AND title=?)) LIMIT 1",
                        (token, self.folder, item["name"]),
                    ).fetchone()
                if incomplete:
                    warnings.append(f"{item['name']}：归档尚未完成，本次跳过")
                    continue
                try:
                    text = self.api.text(token)
                except LibraryError:
                    warnings.append(f"{item['name']}：本次读取失败，不使用旧缓存")
                    continue
                docs.append(
                    LibraryDocument(
                        token,
                        item["name"],
                        item.get("url") or f"https://feishu.cn/docx/{token}",
                        text,
                    )
                )
        return docs, warnings
