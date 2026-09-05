from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .models import IncomingMessage
from .podcast import PodcastService
from .store import Store

URL_RE = re.compile(r"https?://[^\s<>]+")
MENTION_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub(" ", text)).strip()


def first_url(text: str) -> str | None:
    match = URL_RE.search(clean_text(text))
    return match.group(0).rstrip(".,，。)]）") if match else None


@dataclass(frozen=True)
class PluginResponse:
    messages: tuple[str, ...]


class BotPlugin(Protocol):
    name: str

    def matches(self, text: str) -> bool: ...

    def acknowledgement(self, text: str) -> str | None: ...

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse: ...


class PodcastPlugin:
    name = "podcast"

    def __init__(self, service: PodcastService):
        self.service = service

    def matches(self, text: str) -> bool:
        return first_url(text) is not None

    def acknowledgement(self, text: str) -> str | None:
        url = first_url(text)
        if url and self.service.supports_url(url):
            return "收到。我会先寻找并核验完整文字稿；只有确认完整后才整理，通常需要几分钟。"
        return None

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        url = first_url(text)
        if url is None:
            return PluginResponse(("没有识别到可分析的链接。",))
        return PluginResponse((self.service.analyze_url(url).message,))


class HelpPlugin:
    name = "help"

    def matches(self, text: str) -> bool:
        return True

    def acknowledgement(self, text: str) -> str | None:
        return None

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        message = (
            "我是新闻官。可用指令：\n\n"
            "- 订阅：私聊中为你订阅日报；群聊中 @我，可为本群订阅。\n"
            "- 退订：取消当前私聊或当前群的日报。\n"
            "- 帮助：查看这份说明。\n\n"
            "你也可以直接发送 YouTube 或已支持的播客官网链接。我会先取得并核验完整文字稿，"
            "再按投研会议纪要整理；没有完整文字稿时，不会根据标题或简介猜测。"
        )
        return PluginResponse((message,))


class SubscriptionPlugin:
    name = "subscriptions"
    COMMANDS = frozenset({"订阅", "退订"})

    def __init__(self, store: Store):
        self.store = store

    def matches(self, text: str) -> bool:
        return clean_text(text) in self.COMMANDS

    def acknowledgement(self, text: str) -> str | None:
        return None

    @staticmethod
    def _target(message: IncomingMessage) -> tuple[str, str, str] | None:
        chat_type = message.chat_type.strip().lower()
        if chat_type == "p2p" and message.sender_open_id:
            return "open_id", message.sender_open_id, "你"
        if chat_type in {"group", "topic"} and message.chat_id:
            return "chat_id", message.chat_id, "本群"
        return None

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        target = self._target(message)
        if target is None:
            return PluginResponse(
                ("无法确认当前对话类型，请在与新闻官的私聊中发送，或在群聊中 @新闻官。",)
            )
        target_type, target_id, label = target
        command = clean_text(text)
        if command == "订阅":
            changed = self.store.add_subscription(target_type, target_id)
            if changed:
                return PluginResponse(
                    (f"订阅成功。新闻官会按设定时间向{label}发送每日播客情报。",)
                )
            return PluginResponse((f"{label}已经订阅每日播客情报。",))

        changed = self.store.remove_subscription(target_type, target_id)
        if changed:
            return PluginResponse((f"退订成功。新闻官将不再向{label}发送每日播客情报。",))
        return PluginResponse((f"{label}当前没有订阅每日播客情报。",))


class CommandRouter:
    def __init__(self, plugins: list[BotPlugin]):
        self.plugins = plugins

    def select(self, text: str) -> BotPlugin:
        for plugin in self.plugins:
            if plugin.matches(text):
                return plugin
        raise RuntimeError("A fallback plugin must always be registered")
