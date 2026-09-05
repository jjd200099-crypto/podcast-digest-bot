from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .podcast import PodcastService

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

    def handle(self, text: str) -> PluginResponse: ...


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

    def handle(self, text: str) -> PluginResponse:
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

    def handle(self, text: str) -> PluginResponse:
        message = (
            "我是新闻官。你可以直接发送 YouTube 或已支持的播客官网链接；我会先取得并核验完整文字稿，"
            "再按投研会议纪要整理。没有完整文字稿时，我不会根据标题或简介猜测。\n\n"
            "我也会按设定时间主动推送每日播客情报。后续的搜索、订阅管理和其他研究功能，"
            "会作为独立插件接入，不需要改变这套飞书入口。"
        )
        return PluginResponse((message,))


class CommandRouter:
    def __init__(self, plugins: list[BotPlugin]):
        self.plugins = plugins

    def select(self, text: str) -> BotPlugin:
        for plugin in self.plugins:
            if plugin.matches(text):
                return plugin
        raise RuntimeError("A fallback plugin must always be registered")
