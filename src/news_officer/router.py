from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .agent import AgentCatalogItem, AgentIntentError, AgentIntentResolver
from .models import IncomingMessage, StoredTranscript, TranscriptAttachment
from .podcast import PodcastService
from .qa import QAFormatError, TranscriptQAService, TranscriptTooLongError
from .store import Store
from .transcript_view import RENDERER_VERSION, render_readable_transcript

LOGGER = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>]+")
MENTION_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE)
REFERENCE_RE = re.compile(
    r"(?<![0-9a-f])(?:ep:|#)?([0-9a-f]{8})(?![0-9a-f])",
    re.IGNORECASE,
)
CHOICE_RE = re.compile(r"^选\s*(\d{1,2})$")
NATURAL_CHOICE_RE = re.compile(
    r"^(?:选)?(?:第)?([一二三四五六七八九十]|\d{1,2})(?:个|期)?(?:吧)?[。！!]*$"
)
CHINESE_CHOICE_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
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
GREETING_RE = re.compile(
    r"^(?:你好(?:呀|啊|哇)?|您好|嗨|哈[喽啰]|hello|hi|hey|"
    r"早上好|上午好|下午好|晚上好|在吗)[呀啊吗嘛呢～~!！。,.，?？]*$",
    re.IGNORECASE,
)
THANKS_RE = re.compile(
    r"^(?:谢谢(?:你)?|多谢|感谢|辛苦了|好的谢谢|ok thanks|thanks|thank you)"
    r"[呀啊啦～~!！。,.，]*$",
    re.IGNORECASE,
)
HELP_RE = re.compile(
    r"^(?:帮助|怎么用|如何使用|你能做什么|你会做什么|有什么功能|介绍一下(?:你自己)?)"
    r"[呀啊吗呢～~!！。,.，?？]*$"
)
SELECTOR_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'’-]{2,}|[\u3400-\u9fff]{3,}")
SELECTOR_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "with",
        "podcast",
        "episode",
        "interview",
        "show",
        "part",
        "agent",
        "agents",
        "model",
        "models",
        "market",
        "markets",
        "technology",
        "tech",
        "why",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "this",
        "that",
        "these",
        "those",
        "does",
        "did",
        "was",
        "were",
        "are",
        "has",
        "have",
        "can",
        "could",
        "should",
        "would",
        "播客",
        "节目",
        "访谈",
        "嘉宾",
        "观点",
    }
)
def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub(" ", text)).strip()


def _selector_compact(text: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", text.casefold())


def _selector_aliases(text: str) -> set[str]:
    """Return conservative title/show aliases for deterministic routing."""

    compact = _selector_compact(text)
    aliases = (
        {compact}
        if len(compact) >= 3 and compact not in SELECTOR_STOPWORDS
        else set()
    )
    english_tokens: list[str] = []
    for token in SELECTOR_TOKEN_RE.findall(text):
        normalized = _selector_compact(token)
        if normalized in SELECTOR_STOPWORDS or len(normalized) < 3:
            continue
        if not re.fullmatch(r"[\u3400-\u9fff]+", normalized):
            english_tokens.append(normalized)
    # A unique name token such as "Naval" is a useful deterministic selector.
    # Catalog-wide frequency checks below prevent common words shared by
    # several episode titles from becoming a silent episode switch.
    aliases.update(english_tokens)
    for width in range(2, min(4, len(english_tokens)) + 1):
        aliases.update(
            "".join(english_tokens[index : index + width])
            for index in range(len(english_tokens) - width + 1)
        )
    return aliases


def first_url(text: str) -> str | None:
    match = URL_RE.search(clean_text(text))
    return match.group(0).rstrip(".,，。)]）") if match else None


def _choice_index(value: str, count: int) -> int | None:
    legacy = CHOICE_RE.fullmatch(value)
    if legacy:
        return int(legacy.group(1)) - 1
    match = NATURAL_CHOICE_RE.fullmatch(value)
    if match:
        raw = match.group(1)
        number = CHINESE_CHOICE_NUMBERS.get(raw, int(raw) if raw.isdigit() else 0)
        return number - 1
    if value in {"最新那期", "最新一期", "最新的", "第一个"}:
        return 0
    if value in {"最早那期", "最早一期", "最后一个"} and count:
        return count - 1
    return None


POSITIONAL_REQUEST_RE = re.compile(
    r"^(?:我说的是|就|选|把|请把)?\s*(?:第)?"
    r"(?P<number>[一二三四五六七八九十]|\d{1,2})"
    r"(?:个|期|集)(?:里|中|的)?\s*(?P<remainder>.*)$"
)
RECENCY_REQUEST_RE = re.compile(
    r"^(?:我说的是|就|选|把|请把)?\s*"
    r"(?P<order>最新|最早)(?:的|那)?(?:一)?期"
    r"(?:里|中|的)?\s*(?P<remainder>.*)$"
)
HISTORY_SWITCH_RE = re.compile(
    r"(?:回到|切回|回头看|之前那期|之前那个(?:播客|节目)?|前面那期|"
    r"前一期|上一期|上一个(?:播客|节目)?|最开始那期|最早问的那期|"
    r"刚才另(?:一|那)期)"
)
EXPLICIT_EPISODE_SWITCH_RE = re.compile(
    r"(?:换到|切到|改问|另问)[^，。？！?,]{1,120}"
    r"(?:那(?:一)?期|那个(?:节目|播客|访谈))"
)


def _positional_request(value: str) -> tuple[str, int | None, str] | None:
    """Return (kind, index, remainder) for a shown-list reference."""

    match = POSITIONAL_REQUEST_RE.fullmatch(value)
    if match:
        raw = match.group("number")
        number = CHINESE_CHOICE_NUMBERS.get(raw, int(raw) if raw.isdigit() else 0)
        return "index", number - 1, match.group("remainder").strip(" ，,:：")
    match = RECENCY_REQUEST_RE.fullmatch(value)
    if match:
        return match.group("order"), None, match.group("remainder").strip(
            " ，,:："
        )
    return None


def _candidate_sort_key(record: StoredTranscript) -> tuple[int, float, float, str]:
    """Known publication dates first and newest first, with stable ties."""

    published = record.episode.published_at
    published_timestamp = published.timestamp() if published is not None else 0.0
    return (
        0 if published is not None else 1,
        -published_timestamp,
        -record.stored_at.timestamp(),
        record.episode.id,
    )


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
    attachments: tuple[TranscriptAttachment, ...] = ()


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
            context_episode_id=result.episode.id,
        )


class TranscriptInteractionPlugin:
    name = "transcript_qa"

    def __init__(
        self,
        store: Store,
        qa: TranscriptQAService,
        intent_resolver: AgentIntentResolver | None = None,
    ):
        self.store = store
        self.qa = qa
        self.intent_resolver = intent_resolver

    def matches(self, text: str) -> bool:
        value = clean_text(text)
        if self.intent_resolver is not None:
            return bool(value)
        return bool(
            value == "最近播客"
            or value.startswith(("文字稿", "问 "))
            or CHOICE_RE.fullmatch(value)
            or any(cue in value for cue in QUESTION_CUES)
        )

    def acknowledgement(self, text: str) -> str | None:
        value = clean_text(text)
        if (
            GREETING_RE.fullmatch(value)
            or THANKS_RE.fullmatch(value)
            or HELP_RE.fullmatch(value)
            or value == "最近播客"
            or value.startswith("文字稿")
            or CHOICE_RE.fullmatch(value)
            or NATURAL_CHOICE_RE.fullmatch(value)
            or value in {"最新那期", "最新一期", "最新的", "最早那期", "最早一期"}
        ):
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
        lines.append(
            "\n你可以直接问，例如“第二期里嘉宾为什么看好 Agent？”；"
            "也可以说“把第一期的文字稿发我”。"
        )
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
        reference_matches = list(REFERENCE_RE.finditer(value))
        if reference_matches:
            records: list[StoredTranscript] = []
            for match in reference_matches:
                prefix = value[max(0, match.start() - 12) : match.start()]
                if re.search(
                    r"(?:不是|而不是|不要|不要回答|不要选|别选|排除)\s*(?:ep:|#)?$",
                    prefix,
                    re.IGNORECASE,
                ):
                    continue
                record = self.store.get_verified_transcript(match.group(1).lower())
                if record is not None and all(
                    existing.episode.id != record.episode.id for existing in records
                ):
                    records.append(record)
            return records, True

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
        records = sorted(records, key=_candidate_sort_key)
        if len(records) == 1:
            return records[0]
        self.store.save_conversation_context(
            key,
            pending_episode_ids=tuple(record.episode.id for record in records),
            pending_question=question,
            pending_action=action,
        )
        lines = [
            (
                "我找到多期可能相关的节目。你可以说“第二个”“最新那期”，"
                "也可以回复标题里的几个字："
            )
        ]
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
        history_user_text: str = "",
    ) -> PluginResponse:
        self.store.save_conversation_context(key, episode_id=record.episode.id)
        if action == "transcript":
            revision = self.store.get_transcript_digest_revision(
                record.episode.id
            )
            digest_markdown = (
                revision[2]
                if revision is not None
                and revision[0] == record.content_sha256
                and revision[1] == record.record_revision_sha256
                else ""
            )
            filename, content = render_readable_transcript(
                record, digest_markdown=digest_markdown
            )
            attachment = TranscriptAttachment.from_rendered(
                record,
                digest_markdown=digest_markdown,
                renderer_version=RENDERER_VERSION,
                filename=filename,
                content=content,
            )
            return PluginResponse(
                (
                    (
                        f"已附上精编可读版文字稿：{record.episode.title}\n"
                        f"原始核验全文仍由情报官保存并用于问答。\n"
                        f"文字稿来源：{record.transcript.source_url}"
                    ),
                ),
                attachment_episode_ids=(record.episode.id,),
                context_episode_id=record.episode.id,
                attachments=(attachment,),
            )
        try:
            answer = self.qa.answer(record, question)
        except TranscriptTooLongError:
            answer = "这期文字稿过长，当前无法在不截断全文的前提下可靠回答。"
        except QAFormatError:
            answer = "这次回答没有通过文字稿引用校验，因此没有发送可能失真的内容。请换一种问法再试。"
        except ValueError:
            answer = "这个问题为空或过长（最多 500 字），请简化并明确问题后再试。"
        self.store.append_conversation_turn(
            key,
            user_text=history_user_text or question,
            assistant_text=answer,
            episode_id=record.episode.id,
        )
        return PluginResponse(
            (answer,), context_episode_id=record.episode.id
        )

    @staticmethod
    def _greeting_message() -> str:
        return (
            "你好，我在。你可以直接问某期播客、某位嘉宾的观点，"
            "也可以把节目链接发给我；我会先核验完整文字稿再回答。"
        )

    @staticmethod
    def _help_message() -> str:
        return (
            "我是情报官，一个基于完整播客文字稿工作的研究 Agent。"
            "你不需要记指令，可以直接说“Acquired 讲英伟达那期，黄仁勋怎么看推理需求？”、"
            "“他为什么这么判断？”或“把这期全文发我”。"
            "也可以让我订阅或退订每日播客情报；没有完整文字稿时，我会明确说不能可靠回答。"
        )

    def _agent_catalog(
        self,
        message: IncomingMessage,
        context: dict,
    ) -> tuple[tuple[AgentCatalogItem, ...], dict[str, StoredTranscript]]:
        reply_episode_id = self.store.episode_for_remote_message(
            message.parent_message_id
        ) or ""
        current_episode_id = str(context.get("episode_id") or "")
        pending_episode_ids = tuple(context.get("pending_episode_ids") or ())
        recent_episode_ids = tuple(context.get("recent_episode_ids") or ())
        history_episode_ids = tuple(
            str(turn.get("episode_id") or "")
            for turn in tuple(context.get("history") or ())
            if isinstance(turn, dict)
        )

        records: list[StoredTranscript] = []
        seen_ids: set[str] = set()
        for episode_id in (
            reply_episode_id,
            current_episode_id,
            *pending_episode_ids,
            *recent_episode_ids,
            *history_episode_ids,
        ):
            if not episode_id or episode_id in seen_ids:
                continue
            record = self.store.get_verified_transcript(episode_id)
            if record is not None:
                records.append(record)
                seen_ids.add(episode_id)
        for record in self.store.list_recent_transcripts(40):
            if record.episode.id in seen_ids:
                continue
            records.append(record)
            seen_ids.add(record.episode.id)

        by_reference = {record.reference: record for record in records}
        catalog = tuple(
            AgentCatalogItem(
                reference=record.reference,
                title=record.episode.title,
                show=record.episode.show,
                published_date=(
                    record.episode.published_at.date().isoformat()
                    if record.episode.published_at
                    else "日期未知"
                ),
                is_current=record.episode.id == current_episode_id,
                is_reply=record.episode.id == reply_episode_id,
                is_pending=record.episode.id in pending_episode_ids,
            )
            for record in records
        )
        return catalog, by_reference

    @staticmethod
    def _negated_catalog_references(
        value: str,
        catalog: tuple[AgentCatalogItem, ...],
    ) -> tuple[str, ...]:
        compact_value = _selector_compact(value)
        markers = (
            "不是",
            "而不是",
            "不要",
            "不要回答",
            "不要选",
            "别选",
            "排除",
        )
        excluded: list[str] = []
        for item in catalog:
            aliases = _selector_aliases(item.title) | _selector_aliases(item.show)
            title = _selector_compact(item.title)
            show = _selector_compact(item.show)
            if len(title) >= 3:
                aliases.add(title)
            if len(show) >= 3:
                aliases.add(show)
            if any(
                marker + alias in compact_value
                for marker in markers
                for alias in aliases
                if len(alias) >= 3
            ):
                excluded.append(item.reference)
        return tuple(excluded)

    @staticmethod
    def _strong_catalog_references(
        value: str,
        catalog: tuple[AgentCatalogItem, ...],
    ) -> tuple[str, ...]:
        """Return full title/show matches, excluding negated selectors."""

        compact_value = _selector_compact(value)
        excluded = set(
            TranscriptInteractionPlugin._negated_catalog_references(value, catalog)
        )
        return tuple(
            item.reference
            for item in catalog
            if item.reference not in excluded
            and any(
                len(selector) >= 3 and selector in compact_value
                for selector in (
                    _selector_compact(item.title),
                    _selector_compact(item.show),
                )
            )
        )

    @staticmethod
    def _explicit_catalog_references(
        value: str,
        catalog: tuple[AgentCatalogItem, ...],
    ) -> tuple[str, ...]:
        """Resolve only selectors that can be verified without model judgment.

        The intent model may choose among valid references, but a valid ID can
        still name the wrong episode.  Full title/show matches and distinctive
        multi-token names provide a deterministic constraint before Q&A.
        """

        compact_value = _selector_compact(value)
        title_aliases_by_reference = {
            item.reference: _selector_aliases(item.title) for item in catalog
        }
        show_aliases_by_reference = {
            item.reference: _selector_aliases(item.show) for item in catalog
        }
        alias_frequency: dict[str, int] = {}
        for aliases in (
            *title_aliases_by_reference.values(),
            *show_aliases_by_reference.values(),
        ):
            for alias in aliases:
                alias_frequency[alias] = alias_frequency.get(alias, 0) + 1

        excluded = set(
            TranscriptInteractionPlugin._negated_catalog_references(value, catalog)
        )
        metadata_term = _selector_compact(
            TranscriptInteractionPlugin._search_term(
                value, transcript_command=False
            )
        )
        title_matches: list[str] = []
        show_matches: list[str] = []
        for item in catalog:
            if item.reference in excluded:
                continue
            title = _selector_compact(item.title)
            show = _selector_compact(item.show)
            title_aliases = {
                alias
                for alias in title_aliases_by_reference[item.reference]
                if (
                    len(alias) >= 3
                    and alias in compact_value
                    and alias_frequency.get(alias) == 1
                )
            }
            show_aliases = {
                alias
                for alias in show_aliases_by_reference[item.reference]
                if (
                    len(alias) >= 3
                    and alias in compact_value
                    and alias_frequency.get(alias) == 1
                )
            }
            if len(title) >= 3 and title in compact_value:
                title_aliases.add(title)
            if len(show) >= 3 and show in compact_value:
                show_aliases.add(show)
            if len(metadata_term) >= 2 and metadata_term in title:
                title_aliases.add(metadata_term)
            if len(metadata_term) >= 2 and metadata_term in show:
                show_aliases.add(metadata_term)
            if title_aliases:
                title_matches.append(item.reference)
            if show_aliases:
                show_matches.append(item.reference)
        if title_matches and show_matches:
            intersection = set(title_matches).intersection(show_matches)
            if intersection:
                return tuple(
                    item.reference
                    for item in catalog
                    if item.reference in intersection
                )
            combined = set(title_matches).union(show_matches)
            return tuple(
                item.reference for item in catalog if item.reference in combined
            )
        return tuple(title_matches or show_matches)

    def _handle_agent(
        self,
        value: str,
        message: IncomingMessage,
        *,
        key: str,
        context: dict,
    ) -> PluginResponse:
        if self.intent_resolver is None:  # pragma: no cover - guarded by caller
            raise RuntimeError("Natural-language routing requires an intent resolver")
        catalog, by_reference = self._agent_catalog(message, context)
        try:
            intent = self.intent_resolver.resolve(
                value,
                catalog,
                history=tuple(context.get("history") or ()),
            )
        except AgentIntentError as error:
            LOGGER.warning("Agent intent did not pass validation: %s", error)
            return PluginResponse(
                (
                    (
                        "我没能可靠判断你想问哪期节目。告诉我节目名或嘉宾，"
                        "也可以直接把链接发来。"
                    ),
                )
            )

        if intent.intent == "greet":
            return PluginResponse((self._greeting_message(),))
        if intent.intent == "help":
            return PluginResponse((self._help_message(),))
        if intent.intent == "recent":
            return PluginResponse((self._recent_message(key),))
        if intent.intent == "clarify":
            return PluginResponse(
                (
                    intent.clarification
                    or "你指的是哪期节目？告诉我节目名、嘉宾，或直接发链接即可。",
                )
            )

        explicit_references = self._explicit_catalog_references(value, catalog)
        strong_references = self._strong_catalog_references(value, catalog)
        excluded_references = self._negated_catalog_references(value, catalog)
        reply_references = tuple(
            item.reference for item in catalog if item.is_reply
        )
        current_references = tuple(
            item.reference for item in catalog if item.is_current
        )
        context_references = set(reply_references or current_references)
        intent_references = set(intent.episode_references)
        history_episode_ids = {
            str(turn.get("episode_id") or "")
            for turn in tuple(context.get("history") or ())
            if isinstance(turn, dict)
        }
        history_references = {
            reference
            for reference, record in by_reference.items()
            if record.episode.id in history_episode_ids
        }
        lookup_is_explicit = bool(
            intent.lookup_query
            and any(
                alias in _selector_compact(value)
                for alias in _selector_aliases(intent.lookup_query)
            )
        )
        records: list[StoredTranscript]
        if HISTORY_SWITCH_RE.search(value):
            selected_history = tuple(
                intent_references
                & (history_references - set(current_references))
            )
            if not selected_history:
                return PluginResponse(
                    ("我不确定你说的“之前那期”是哪一期，请回复节目标题。",)
                )
            records = [by_reference[reference] for reference in selected_history]
        elif explicit_references:
            # A model/context conflict with a literal metadata match is
            # ambiguous: the same words may be a topic phrase rather than an
            # episode switch. Never silently answer the other episode.
            if (
                len(explicit_references) == 1
                and context_references
                and not context_references.intersection(explicit_references)
                and (
                    not EXPLICIT_EPISODE_SWITCH_RE.search(value)
                    or (
                        intent_references & context_references
                        and not intent_references.intersection(
                            explicit_references
                        )
                    )
                )
            ):
                explicit_record = by_reference[explicit_references[0]]
                context_record = by_reference[next(iter(context_references))]
                return PluginResponse(
                    (
                        (
                            "我不确定你是在继续问“"
                            f"{context_record.episode.title}”，还是切换到“"
                            f"{explicit_record.episode.title}”。如果要切换，请回复“"
                            f"换到 {explicit_record.episode.title} 那期”。"
                        ),
                    )
                )
            if (
                len(explicit_references) == 1
                and not context_references
                and explicit_references[0] not in strong_references
                and explicit_references[0] not in intent_references
            ):
                return PluginResponse(
                    ("我识别到一个可能的节目关键词，但还不能可靠确定目标。请回复完整标题。",)
                )
            # Programmatically verified selectors override a model-selected
            # unrelated catalog ID. Multiple matches stay ambiguous.
            records = [by_reference[reference] for reference in explicit_references]
        elif excluded_references:
            return PluginResponse(
                (
                    "我识别到你排除了一期节目，但还不能可靠确定目标。请回复目标标题。",
                )
            )
        elif intent.lookup_query and lookup_is_explicit:
            records = self.store.search_verified_transcripts(
                intent.lookup_query, limit=5
            )
        elif reply_references:
            # With no explicit new selector, a reply is the strongest binding.
            records = [by_reference[reply_references[0]]]
        elif current_references:
            records = [by_reference[current_references[0]]]
        else:
            # A real catalog reference can still name the wrong episode. Do
            # not trust a model-selected ID unless the user text or a stored
            # conversation binding independently constrains it.
            records = []
        if not records:
            query = intent.lookup_query or "你提到的节目"
            return PluginResponse(
                (
                    (
                        f"我还没有找到与“{query}”匹配的已核验完整文字稿。"
                        "你可以补充节目名或嘉宾，或者把节目链接发给我。"
                    ),
                )
            )

        action = "transcript" if intent.intent == "transcript" else "qa"
        question = intent.question or value
        selected = self._select_or_defer(
            records,
            key=key,
            question=question,
            action=action,
        )
        if isinstance(selected, PluginResponse):
            return selected
        if selected is None:  # pragma: no cover - records is non-empty
            raise RuntimeError("Agent selected no transcript")
        return self._perform(
            selected,
            question=question,
            action=action,
            key=key,
            history_user_text=value,
        )

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        value = clean_text(text)
        key = conversation_key(message)
        if value == "最近播客":
            return PluginResponse((self._recent_message(key),))

        context = self.store.get_conversation_context(key) or {}
        if GREETING_RE.fullmatch(value):
            return PluginResponse((self._greeting_message(),))
        if THANKS_RE.fullmatch(value):
            return PluginResponse(("不客气。你可以继续追问刚才那期节目。",))
        if HELP_RE.fullmatch(value):
            return PluginResponse((self._help_message(),))

        pending_ids = tuple(context.get("pending_episode_ids") or ())
        recent_ids = tuple(context.get("recent_episode_ids") or ())
        positional = _positional_request(value)
        if positional is None:
            exact_index = _choice_index(value, len(pending_ids or recent_ids))
            if exact_index is not None:
                positional = ("index", exact_index, "")
        if positional is not None:
            kind, index, remainder = positional
            pool_ids = pending_ids or recent_ids
            if not pool_ids and kind in {"最新", "最早"}:
                pool_ids = tuple(
                    record.episode.id
                    for record in self.store.list_recent_transcripts(10)
                )
            if not pool_ids:
                return PluginResponse(
                    ("我还没有给你展示可编号的节目列表。请先说“最近播客”或补充节目标题。",)
                )
            records = [
                self.store.get_verified_transcript(episode_id)
                for episode_id in pool_ids
            ]
            if any(record is None for record in records):
                return PluginResponse(
                    ("刚才列表中的节目已发生变化，请重新说“最近播客”后再选择。",)
                )
            available_records = [record for record in records if record is not None]
            if kind == "index":
                if index is None or not 0 <= index < len(available_records):
                    return PluginResponse(
                        ("没有对应的待选节目，请重新发送问题或输入“最近播客”。",)
                    )
                record = available_records[index]
            else:
                ordered = sorted(available_records, key=_candidate_sort_key)
                if kind == "最新":
                    record = ordered[0]
                else:
                    published = [
                        item for item in ordered if item.episode.published_at is not None
                    ]
                    record = (published or ordered)[-1]
            if record is None:
                return PluginResponse(("这期节目的完整文字稿已不可用，请重新选择。",))
            if remainder:
                action = (
                    "transcript"
                    if "文字稿" in remainder or "全文" in remainder
                    else "qa"
                )
                question = "" if action == "transcript" else remainder
            elif pending_ids:
                action = str(context.get("pending_action") or "qa")
                question = str(context.get("pending_question") or "")
            else:
                self.store.save_conversation_context(
                    key, episode_id=record.episode.id
                )
                return PluginResponse(
                    (
                        (
                            f"已选中：{record.episode.title}。你可以直接继续问它的观点，"
                            "或说“把这期文字稿发我”。"
                        ),
                    ),
                    context_episode_id=record.episode.id,
                )
            return self._perform(
                record,
                question=question,
                action=action,
                key=key,
                history_user_text=value,
            )

        transcript_command = value.startswith("文字稿")
        hard_number = re.match(r"^(?:文字稿|问)\s*\d{1,2}(?:\s|$)", value)
        hard_selector = bool(
            transcript_command or hard_number or REFERENCE_RE.search(value)
        )
        if self.intent_resolver is not None and not hard_selector:
            return self._handle_agent(
                value,
                message,
                key=key,
                context=context,
            )

        action = "transcript" if transcript_command else "qa"
        question = value

        # A selector written in the current message is stronger than reply or
        # stale conversation context. This prevents "reply to A, ask about B"
        # from silently answering the wrong episode.
        explicit_records, selector_attempted = self._resolve_records(
            value,
            transcript_command=transcript_command,
            recent_episode_ids=tuple(context.get("recent_episode_ids") or ()),
        )
        record: StoredTranscript | None = None
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
                ("没有找到对应的已归档完整文字稿。你可以直接补充节目名、嘉宾或链接。",)
            )

        if record is None:
            episode_id = self.store.episode_for_remote_message(
                message.parent_message_id
            )
            record = self.store.get_verified_transcript(episode_id or "")

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
            record,
            question=question,
            action=action,
            key=key,
            history_user_text=value,
        )


class HelpPlugin:
    name = "help"

    def matches(self, text: str) -> bool:
        return True

    def acknowledgement(self, text: str) -> str | None:
        return None

    def handle(self, text: str, message: IncomingMessage) -> PluginResponse:
        value = clean_text(text)
        if GREETING_RE.fullmatch(value):
            return PluginResponse((TranscriptInteractionPlugin._greeting_message(),))
        if THANKS_RE.fullmatch(value):
            return PluginResponse(("不客气。你可以直接告诉我想研究哪期播客。",))
        return PluginResponse((TranscriptInteractionPlugin._help_message(),))


class SubscriptionPlugin:
    name = "subscriptions"
    SUBSCRIBE_RE = re.compile(
        r"^(?:请|麻烦)?(?:帮我|给我)?(?:"
        r"订阅(?:一下)?(?:(?:每日|每天)的?)?(?:播客)?(?:日报|情报|更新)?|"
        r"(?:开启|开始)(?:一下)?(?:每日|每天)的?(?:播客)?(?:日报|情报|更新)"
        r")(?:吧)?[。！!]*$"
    )
    UNSUBSCRIBE_RE = re.compile(
        r"^(?:请|麻烦)?(?:帮我|给我)?(?:"
        r"退订|取消订阅|停止(?:给我)?推送|不要再发(?:了)?|别再发(?:了)?"
        r")(?:每日|每天)?(?:的)?(?:播客)?(?:日报|情报|更新)?(?:吧)?[。！!]*$"
    )

    def __init__(self, store: Store):
        self.store = store

    @classmethod
    def _command(cls, text: str) -> str:
        value = clean_text(text)
        if cls.SUBSCRIBE_RE.fullmatch(value):
            return "订阅"
        if cls.UNSUBSCRIBE_RE.fullmatch(value):
            return "退订"
        return ""

    def matches(self, text: str) -> bool:
        return bool(self._command(text))

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
        command = self._command(text)
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
