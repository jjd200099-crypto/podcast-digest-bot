from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests

from .models import OutboxItem

BRAND_HEADER = "🎧 情报官｜每日播客情报"
FEISHU_API = "https://open.feishu.cn/open-apis"
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)]\((https?://[^\s)]+)\)")
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def brand_message(markdown: str) -> str:
    content = markdown.strip()
    if content.startswith(BRAND_HEADER):
        return content
    return f"{BRAND_HEADER}\n\n{content}"


def split_utf8(text: str, max_bytes: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for character in text:
        character_size = len(character.encode("utf-8"))
        if current and size + character_size > max_bytes:
            pieces.append("".join(current))
            current = []
            size = 0
        current.append(character)
        size += character_size
    if current:
        pieces.append("".join(current))
    return pieces


def split_message(markdown: str, max_bytes: int = 3500) -> list[str]:
    # Leave room for the visible part counter that is appended during delivery.
    content_limit = max(128, max_bytes - 64)
    chunks: list[str] = []
    current = ""
    for paragraph in markdown.split("\n\n"):
        for piece in split_utf8(paragraph, content_limit):
            candidate = (current + "\n\n" + piece).strip()
            if current and len(candidate.encode("utf-8")) > content_limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def idempotency_uuid(key: str, part: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"news-officer:{key}:{part}"))


def _post_elements(line: str) -> list[dict]:
    """Convert the small Markdown subset used by summaries into post elements."""

    heading = line.lstrip().startswith("#")
    clean = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
    clean = clean.replace("**", "").replace("__", "")
    if heading:
        clean = f"【{clean}】"
    elements: list[dict] = []
    position = 0
    for match in MARKDOWN_LINK_RE.finditer(clean):
        if match.start() > position:
            elements.append({"tag": "text", "text": clean[position : match.start()]})
        elements.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
        position = match.end()
    if position < len(clean):
        elements.append({"tag": "text", "text": clean[position:]})
    return elements or [{"tag": "text", "text": " "}]


def _post_content(markdown: str, title: str) -> str:
    paragraphs = [_post_elements(line) for line in markdown.splitlines()]
    return json.dumps(
        {"zh_cn": {"title": title, "content": paragraphs or [[{"tag": "text", "text": " "}]]}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def delivery_parts(markdown: str, idempotency_key: str) -> list[tuple[str, str, str]]:
    """Freeze branded, numbered content and its UUID before any network call."""

    body = markdown.strip()
    if body.startswith(BRAND_HEADER):
        body = body[len(BRAND_HEADER) :].lstrip()
    chunks = split_message(body) or ["（无内容）"]
    total = len(chunks)
    return [
        (
            "post",
            _post_content(
                chunk,
                BRAND_HEADER + (f"（第 {index}/{total} 段）" if total > 1 else ""),
            ),
            idempotency_uuid(idempotency_key, index),
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def _retry_delay(response: requests.Response, attempt: int) -> float:
    value = str(response.headers.get("Retry-After", "") or "").strip()
    if value:
        try:
            return max(0.0, min(120.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(
                    0.0,
                    min(120.0, (retry_at - datetime.now(UTC)).total_seconds()),
                )
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, 0.5 * (2**attempt))


class FeishuMessenger:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    @staticmethod
    def _check(response: requests.Response, operation: str) -> dict:
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Feishu {operation} error: {body}")
        return body

    def token(self) -> str:
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = None
            for attempt in range(4):
                response = requests.post(
                    f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=30,
                )
                if response.status_code not in RETRYABLE_HTTP_STATUSES:
                    break
                if attempt < 3:
                    time.sleep(_retry_delay(response, attempt))
            if response is None:  # pragma: no cover - loop always executes
                raise RuntimeError("Feishu token request did not run")
            body = self._check(response, "token")
            self._token = body["tenant_access_token"]
            # Feishu normally returns a two-hour token. Refresh a minute early.
            self._token_expires_at = time.monotonic() + max(
                60, int(body.get("expire", 7200)) - 60
            )
            return self._token

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._token = ""
            self._token_expires_at = 0.0

    def reply(self, markdown: str, message_id: str, idempotency_key: str) -> None:
        parts = delivery_parts(markdown, idempotency_key)
        for index, (msg_type, content, item_uuid) in enumerate(parts, start=1):
            self.deliver(
                OutboxItem(
                    id=0,
                    job_key="",
                    group_key=idempotency_key,
                    delivery_key=idempotency_key,
                    operation="reply",
                    target_id=message_id,
                    target_type="",
                    reply_in_thread=False,
                    part=index,
                    total_parts=len(parts),
                    msg_type=msg_type,
                    content=content,
                    uuid=item_uuid,
                )
            )

    def send(
        self, markdown: str, receive_id: str, receive_id_type: str, idempotency_key: str
    ) -> None:
        parts = delivery_parts(markdown, idempotency_key)
        for index, (msg_type, content, item_uuid) in enumerate(parts, start=1):
            self.deliver(
                OutboxItem(
                    id=0,
                    job_key="",
                    group_key=idempotency_key,
                    delivery_key=idempotency_key,
                    operation="send",
                    target_id=receive_id,
                    target_type=receive_id_type,
                    reply_in_thread=False,
                    part=index,
                    total_parts=len(parts),
                    msg_type=msg_type,
                    content=content,
                    uuid=item_uuid,
                )
            )

    def deliver(self, item: OutboxItem) -> None:
        """Deliver one already-frozen outbox item without re-splitting it."""

        payload = {
            "msg_type": item.msg_type,
            "content": item.content,
            "uuid": item.uuid,
        }
        if item.operation == "reply":
            url = f"{FEISHU_API}/im/v1/messages/{item.target_id}/reply"
            params = None
            if item.reply_in_thread:
                payload["reply_in_thread"] = True
            operation = "reply"
        elif item.operation == "send" and item.target_type:
            url = f"{FEISHU_API}/im/v1/messages"
            params = {"receive_id_type": item.target_type}
            payload["receive_id"] = item.target_id
            operation = "send"
        else:
            raise ValueError(f"Invalid outbox delivery operation: {item.operation}")

        refreshed = False
        response = None
        for attempt in range(5):
            headers = {
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json; charset=utf-8",
            }
            response = requests.post(
                url,
                params=params,
                headers=headers,
                json=payload,
                timeout=30,
            )
            if response.status_code == 401 and not refreshed:
                self._invalidate_token()
                refreshed = True
                continue
            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < 4:
                time.sleep(_retry_delay(response, attempt))
                continue
            self._check(response, operation)
            return
        if response is not None:  # pragma: no cover - final branch returns or raises
            self._check(response, operation)

    def broadcast(
        self,
        markdown: str,
        user_open_ids: Iterable[str],
        group_chat_ids: Iterable[str],
        idempotency_key: str,
    ) -> None:
        for user_open_id in user_open_ids:
            self.send(
                markdown,
                user_open_id,
                "open_id",
                f"{idempotency_key}:user:{user_open_id}",
            )
        for chat_id in group_chat_ids:
            self.send(markdown, chat_id, "chat_id", f"{idempotency_key}:chat:{chat_id}")
