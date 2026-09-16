from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .models import IncomingMessage, StoredTranscript
from .podcast import PodcastService
from .qa import QAFormatError, TranscriptQAService, TranscriptTooLongError
from .store import Store

URL_RE = re.compile(r"https?://[^\s<>]+")
MENTION_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE)
REFERENCE_RE = re.compile(
    r"(?<![0-9a-f])(?:ep:|#)?([0-9a-f]{8})(?![0-9a-f])",
    re.IGNORECASE,
)
CHOICE_RE = re.compile(r"^选\s*(\d{1,2})$")
QUESTION_CUES = (
    "观点",
    "看法",
    "怎么看",
    "如何看",
    "怎么说",
    "为什么",
    "为何",
    "整理",
    "总结",
    "梳理",
    "提炼",
    "这期",
    "播客",
)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub(" ", text)).strip()


def first_url(text: str) -> str | None:
    match = URL_RE.search(clean_text(text))
    return match.group(0).rstrip(".,，。)]）") if match else None


def conversation_key(message: IncomingMessage) -> str:
    if message.thread_id:
        return (
            f"thread:{message.chat_id}:{message.thread_id}:"
            f"{message.sender_open_id or 'anonymous'}"
        )
    if message.chat_type.strip().lower() == "p2p":
        return f"p2p:{message.sender_open_id or message.chat_id}"
    return f"group:{message.chat_id}:{message.sender_open_id or 'anonymous'}"


@dataclass(frozen=True)
class PluginResponse:
    messages: tuple[str, ...]
    attachment_episode_ids: tuple[str, ...] = ()
    context_episode_id: str = ""


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
        result = self.service.analyze_url(url)
        if result.status != "summarized" or result.episode is None:
            return PluginResponse((result.message,))
        self.service.store.save_conversation_context(
            conversation_key(message), episode_id=result.episode.id
        )
        return PluginResponse(
            (result.message,),
            attachment_episode_ids=(result.episode.id,),
            context_episode_id=result.episode.id,
        )


class TranscriptInteractionPlugin:
    name = "transcript_qa"

    def __init__(self, store: Store, qa: TranscriptQAService):
        self.store = store
        self.qa = qa

    def matches(self, text: str) -> bool:
        value = clean_text(text)
        return bool(
            value == "最近播客"
            or value.startswith(("文字稿", "问 "))
            or CHOICE_RE.fullmatch(value)
            or any(cue in value for cue in QUESTION_CUES)
        )

    def acknowledgement(self, text: str) -> str | None:
        value = clean_text(text)
        if value == "最近播客" or value.startswith("文字稿") or CHOICE_RE.fullmatch(value):
            return None
        return "收到。我会只基于已归档且核验完整的文字稿回答，不补充节目外的信息。"

    @staticmethod
    def _format_episode(record: StoredTranscript, number: int | None = None) -> str:
        date = (
            record.episode.published_at.date().isoformat()
            if record.episode.published_at
            else "日期未知"
        )
        prefix = f"{number}. " if number is not None else ""
        return (
            f"{prefix}[{record.reference}] {record.episode.title}｜"
            f"{record.episode.show}｜{date}"
        )

    def _recent_message(self, key: str) -> str:
        records = self.store.list_recent_transcripts(10)
        if not records:
            return "还没有已归档的完整文字稿。请先发送一个支持的播客或 YouTube 链接。"
        self.store.save_recent_transcript_snapshot(
            key, tuple(record.episode.id for record in records)
        )
        lines = ["最近可问答的播客："]
        lines.extend(
            self._format_episode(record, number)
            for number, record in enumerate(records, start=1)
        )
        lines.append("\n用法：`文字稿 编号`，或 `问 编号 你的问题`。也可以直接说“整理某某的观点”。")
        return "\n".join(lines)

    @staticmethod
    def _search_term(value: str, *, transcript_command: bool) -> str:
        if transcript_command:
            return value[len("文字稿") :].strip()

        patterns = (
            r"(?:整理|总结|梳理|提炼)\s*([^，。？！:：]{2,50}?)(?:的|在[^，。？！]{0,16}?中的?|对|关于)(?:核心)?(?:观点|看法|判断)",
            r"([\u4e00-\u9fff]{2,8})(?:怎么看|如何看|认为|的观点|的看法)",
            r"^(?:问\s+)?([A-Za-z][A-Za-z0-9'.-]*(?:\s+[A-Za-z][A-Za-z0-9'.-]*){0,3})\s+(?:怎么看|如何看|认为|的观点|的看法)",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                term = match.group(1).strip(" ：:，,。.!！?？")
                generic_terms = {
                    "ai",
                    "podcast",
                    "agent",
                    "the",
                    "他",
                    "他们",
                    "她",
                    "她们",
                    "嘉宾",
                    "主持人",
                }
                if (
                    term.lower() not in generic_terms
                    and not term.startswith(("为什么", "为何", "怎么", "如何"))
                ):
                    return term
        return ""

    def _resolve_records(
        self,
        value: str,
        *,
        transcript_command: bool,
        recent_episode_ids: tuple[str, ...] = (),
    ) -> tuple[list[StoredTranscript], bool]:
        reference = REFERENCE_RE.search(value)
        if reference:
            record = self.store.get_verified_transcript(reference.group(1).lower())
            return ([record] if record else []), True

        # The numbered recent list is also accepted directly: 文字稿 2 / 问 2 ...
        command_number = re.match(r"^(?:文字稿|问)\s*(\d{1,2})(?:\s|$)", value)
        if command_number:
            index = int(command_number.group(1)) - 1
            if recent_episode_ids:
                if not 0 <= index < len(recent_episode_ids):
                    return [], True
                record = self.store.get_verified_transcript(recent_episode_ids[index])
                return ([record] if record else []), True
            recent = self.store.list_recent_transcripts(10)
            return ([recent[index]] if 0 <= index < len(recent) else []), True

        term = self._search_term(value, transcript_command=transcript_command)
        if not term:
            return [], False
        records = self.store.search_verified_transcripts(term, 5)
        title_matches = [
            record
            for record in records
            if term.casefold() in record.episode.title.casefold()
        ]
        return (title_matches if title_matches else records), True

    def _select_or_defer(
        self,
        records: list[StoredTranscript],
        *,
        key: str,
        question: str,
        action: str,
    ) -> PluginResponse | StoredTranscript | None:
        if not records:
            return None
        if len(records) == 1:
            return records[0]
        self.store.save_conversation_context(
            key,
            pending_episode_ids=tuple(record.episode.id for record in records),
            pending_question=question,
            pending_action=action,
        )
        lines = ["找到多期可能相关的节目，请回复“选 1”这类指令："]
        lines.extend(
            self._format_episode(record, number)
            for number, record in enumerate(records, start=1)
        )
        return PluginResponse(("\n".join(lines),))

    def _perform(
        self,
        record: StoredTranscript,
        *,
        question: str,
        action: str,
        key: str,
    ) -> PluginResponse:
        self.store.save_conversation_context(key, episode_id=record.episode.id)
        if action == "transcript":
            return PluginResponse(
                (
                    (
                        f"已附上完整文字稿：{record.episode.title}\n"
                        f"文字稿来源：{record.transcript.source_url}"
                    ),
                ),
                attachment_episode_ids=(record.episode.id,),
                context_episode_id=record.episode.id,
            )
        try:
            answer = self.qa.answer(record, question)
        except TranscriptTooLongError:
            answer = "这期文字稿过长，当前无法在不截断全文的前提下可靠回答。"
        except QAFormatError:
            answer = "这次回答没有通过文字稿引用校验，因此没有发送可能失真的内容。请换一种问法再试。"
        except ValueError:
            answer = "这个问题为空或过长（最多 500 字），请简化并明确问题后再试。"
        return PluginResponse(
            (answer,), context_episode_id=record.episode.id
        )

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        value = clean_text(text)
        key = conversation_key(message)
        if value == "最近播客":
            return PluginResponse((self._recent_message(key),))

        context = self.store.get_conversation_context(key) or {}
        choice = CHOICE_RE.fullmatch(value)
        if choice:
            pending_ids = tuple(context.get("pending_episode_ids") or ())
            index = int(choice.group(1)) - 1
            if not 0 <= index < len(pending_ids):
                return PluginResponse(("没有对应的待选节目，请重新发送问题或输入“最近播客”。",))
            record = self.store.get_verified_transcript(pending_ids[index])
            if record is None:
                return PluginResponse(("这期节目的完整文字稿已不可用，请重新选择。",))
            return self._perform(
                record,
                question=str(context.get("pending_question") or ""),
                action=str(context.get("pending_action") or "qa"),
                key=key,
            )

        transcript_command = value.startswith("文字稿")
        action = "transcript" if transcript_command else "qa"
        question = value

        # A direct reply to a digest is the strongest episode signal and does
        # not require any message-history permission.
        episode_id = self.store.episode_for_remote_message(message.parent_message_id)
        record = self.store.get_verified_transcript(episode_id or "")

        if record is None:
            explicit_records, selector_attempted = self._resolve_records(
                value,
                transcript_command=transcript_command,
                recent_episode_ids=tuple(context.get("recent_episode_ids") or ()),
            )
            if explicit_records:
                selected = self._select_or_defer(
                    explicit_records,
                    key=key,
                    question=question,
                    action=action,
                )
                if isinstance(selected, PluginResponse):
                    return selected
                record = selected
            elif selector_attempted:
                return PluginResponse(
                    ("没有找到对应的已归档完整文字稿。请输入“最近播客”查看可用节目。",)
                )

        if record is None and context.get("episode_id"):
            record = self.store.get_verified_transcript(str(context["episode_id"]))

        if record is None:
            if transcript_command and value == "文字稿":
                return PluginResponse((self._recent_message(key),))
            return PluginResponse(
                (
                    (
                        "无法唯一确认你指的是哪期节目。请带上节目名、嘉宾名或 8 位编号；"
                        "也可以先输入“最近播客”。"
                    ),
                )
            )
        return self._perform(
            record, question=question, action=action, key=key
        )


class HelpPlugin:
    name = "help"

    def matches(self, text: str) -> bool:
        return True

    def acknowledgement(self, text: str) -> str | None:
        return None

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        message = (
            "我是情报官。可用指令：\n\n"
            "- 订阅：私聊中为你订阅日报；群聊中 @我，可为本群订阅。\n"
            "- 退订：取消当前私聊或当前群的日报。\n"
            "- 帮助：查看这份说明。\n\n"
            "- 最近播客：查看已有完整文字稿、可继续问答的节目。\n"
            "- 文字稿 <编号/节目/嘉宾>：下载完整 Markdown 文字稿。\n"
            "- 问 <编号> <问题>：只基于该期完整文字稿回答。\n\n"
            "你也可以直接发送 YouTube 或已支持的播客官网链接。我会先取得并核验完整文字稿，"
            "再按投研会议纪要整理并附上全文；没有完整文字稿时，不会根据标题或简介猜测。"
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
                ("无法确认当前对话类型，请在与情报官的私聊中发送，或在群聊中 @情报官。",)
            )
        target_type, target_id, label = target
        command = clean_text(text)
        if command == "订阅":
            changed = self.store.add_subscription(target_type, target_id)
            if changed:
                return PluginResponse(
                    (f"订阅成功。情报官会按设定时间向{label}发送每日播客情报。",)
                )
            return PluginResponse((f"{label}已经订阅每日播客情报。",))

        changed = self.store.remove_subscription(target_type, target_id)
        if changed:
            return PluginResponse((f"退订成功。情报官将不再向{label}发送每日播客情报。",))
        return PluginResponse((f"{label}当前没有订阅每日播客情报。",))


class CommandRouter:
    def __init__(self, plugins: list[BotPlugin]):
        self.plugins = plugins

    def select(self, text: str) -> BotPlugin:
        for plugin in self.plugins:
            if plugin.matches(text):
                return plugin
        raise RuntimeError("A fallback plugin must always be registered")
