from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .models import StoredTranscript


RENDERER_VERSION = "readable-v1"
PARAGRAPH_TARGET_CHARS = 850
AD_MAX_BLOCKS = 18
AD_MAX_CHARS = 12_000

FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
TIMESTAMP_LINE_RE = re.compile(
    r"^\s*(?:\[|\()?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?(?:\]|\))?\s*$"
)
CUE_TIMING_RE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+"
    r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}(?:\s+.*)?$"
)
BRACKET_LABEL_RE = re.compile(r"^\[([^\]\n]{1,80})\]\s*(.*)$", re.DOTALL)
COLON_LABEL_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9 .,'’\-]{0,79}|[\u3400-\u9fff]{2,20})"
    r"[:：]\s*(.*)$",
    re.DOTALL,
)
INLINE_BRACKET_RE = re.compile(r"\[([^\]\n]{1,80})\]\s*")
SENTENCE_RE = re.compile(r".+?(?:[.!?。！？]+(?:[\"'’”)]*)|$)(?:\s+|$)", re.DOTALL)
DOMAIN_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9.-]*\.(?:com|ai|io|co|fm)"
    r"(?:/[a-z0-9_./?=&%+~-]*)?",
    re.IGNORECASE,
)
NUMBERED_POINT_RE = re.compile(r"^\s*1\.\s+\S")
TOPIC_HEADING_RE = re.compile(
    r"^(?:#{2,6}\s+\S.*|【[^】\r\n]{1,60}】|\*\*[^*\r\n]{1,60}\*\*|-{3,})$"
)

REMOVABLE_STAGE_DIRECTIONS = {
    "music",
    "applause",
    "laughter",
    "laughs",
    "音乐",
    "掌声",
    "笑声",
}
EVIDENCE_MARKERS = {
    "inaudible",
    "crosstalk",
    "听不清",
}
FILLER_ONLY = {
    "um",
    "uh",
    "erm",
    "呃",
}
AD_START_RE = re.compile(
    r"(?:"
    r"presenting (?:sponsor|partner)|"
    r"thank (?:our|the) (?:brand new )?(?:presenting )?"
    r"(?:sponsor|partner|friends)(?: at)?|"
    r"thank you to (?:our|the) (?:sponsors|partners|friends)|"
    r"this (?:episode|podcast|show) is (?:brought to you|sponsored) by|"
    r"(?:a )?(?:quick|brief) word from (?:our|the) sponsor|"
    r"support for this (?:episode|podcast|show) comes from|"
    r"brought to you by our|paid partnership with|"
    r"感谢本期(?:赞助商|合作伙伴)|本期(?:节目)?由.+赞助"
    r")",
    re.IGNORECASE,
)
AD_CTA_RE = re.compile(
    r"(?:go(?:ing)? to|head to|visit|sign up|start (?:your )?free trial|use (?:the )?(?:code|promo)|"
    r"learn more at|link in (?:the )?show notes|tell (?:them|’em|'em) that|"
    r"访问|优惠码|免费试用|点击节目简介)",
    re.IGNORECASE,
)
AD_RETURN_RE = re.compile(
    r"\b(?:now |so,? )?(?:let(?:'s| us) )?(?:go|get|come)?\s*back to\b|"
    r"\bbefore we go back to\b|回到(?:节目|对谈|正题)",
    re.IGNORECASE,
)
POST_CTA_TRANSITION_RE = re.compile(
    r"\b(?:okay|all right),\s*(?:[A-Z][A-Za-z'’.-]{0,30},\s*)?"
    r"(?:so\s+)?(?=(?:how|what|why|where|when|back|let(?:'s| us)))",
    re.IGNORECASE,
)
OUTRO_RE = re.compile(
    r"^\s*(?:"
    r"i hope you enjoyed (?:this|the) episode"
    r"(?:\s+(?:and\s+)?please (?:remember to )?subscribe)?|"
    r"thanks for listening"
    r"(?:\s+(?:and\s+)?please (?:remember to )?subscribe)?|"
    r"please (?:remember to )?subscribe"
    r"(?:\s+to\s+(?:this|the|our)\s+(?:show|podcast|channel))?"
    r"(?:\s*,?\s*(?:rate(?:\s+and\s+review)?|review))?|"
    r"please (?:remember to )?rate\s+and\s+review"
    r"(?:\s+(?:this|the|our)\s+(?:show|podcast|episode))?|"
    r"please (?:remember to )?(?:rate|review)\s+(?:this|the|our)\s+"
    r"(?:show|podcast|episode)|"
    r"join (?:the|our) email list|check out our companion pdf|"
    r"a huge thank you to our partners|"
    r"感谢收听(?:\s*[，,]\s*(?:欢迎订阅|点赞并订阅))?|"
    r"欢迎订阅|点赞并订阅"
    r")(?=\s*(?:[.!?。！？]|$))",
    re.IGNORECASE,
)
AD_CONFLICT_RE = re.compile(
    r"(?:audit|audited|bias|biased|disclos|conflict|contract|investigat|"
    r"did not influence|funded the study|reported revenue|审计|偏见|利益冲突|"
    r"赞助披露|合同|未影响)",
    re.IGNORECASE,
)
DISCLAIMER_RE = re.compile(
    r"\b(?:this (?:show|podcast) is not (?:financial|investment) advice|"
    r"for informational and entertainment purposes only)\b",
    re.IGNORECASE,
)
HOUSEKEEPING_RE = re.compile(
    r"(?:companion pdf|join (?:the|our) email list|join the slack|"
    r"behind-the-scenes photos|vote on future episode topics|"
    r"come discuss it with|节目资料包|加入邮件列表|加入社群)",
    re.IGNORECASE,
)
FOOTER_PROMO_RE = re.compile(
    r"(?:a huge thank you to our partners|感谢本季(?:所有)?合作伙伴)",
    re.IGNORECASE,
)
AD_EVIDENCE_RE = re.compile(
    r"(?:\d|[$€£¥%]|[?？]|"
    r"\b(?:but|could|guest|however|may|might|not|only|said|told)\b|"
    r"\b(?:accord|audit|bias|claim|conflict|contract|declin|estimate|"
    r"investigat|margin|report|revenue|risk|study)\w*\b|"
    r"收入|营收|利润|毛利|增长|下降|审计|报告|声称|合同|调查|偏见|冲突|"
    r"风险|但是|但|尚未|未经|可能|嘉宾|数据显示)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TranscriptBlock:
    kind: str
    text: str
    speaker: str = ""


@dataclass(frozen=True)
class CleanedTranscript:
    blocks: tuple[TranscriptBlock, ...]
    removed_stage_directions: int = 0
    removed_filler_turns: int = 0
    removed_ad_blocks: int = 0
    removed_caption_duplicates: int = 0

    @property
    def removed_count(self) -> int:
        return (
            self.removed_stage_directions
            + self.removed_filler_turns
            + self.removed_ad_blocks
            + self.removed_caption_duplicates
        )


def _plain(value: object) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _safe_link(label: str, url: str) -> str:
    clean_label = _plain(label).replace("[", "［").replace("]", "］")
    parts = urlsplit(url)
    if parts.scheme in {"http", "https"} and parts.netloc:
        return f"[{clean_label}]({url})"
    return clean_label


def _looks_like_speaker(label: str) -> bool:
    clean = re.sub(r"\s+", " ", label).strip()
    normalized = clean.casefold().strip(" .:：-—_[]()")
    if not normalized or normalized in (
        REMOVABLE_STAGE_DIRECTIONS | EVIDENCE_MARKERS
    ):
        return False
    if normalized in {"kind", "language", "note", "region", "style"}:
        return False
    if TIMESTAMP_LINE_RE.fullmatch(clean):
        return False
    if len(clean.split()) > 8:
        return False
    return re.fullmatch(r"[A-Za-z0-9\u3400-\u9fff .,'’\-]{1,80}", clean) is not None


def _direction_label(text: str) -> str:
    clean = text.strip()
    if len(clean) > 80:
        return ""
    if not (
        (clean.startswith("[") and clean.endswith("]"))
        or (clean.startswith("(") and clean.endswith(")"))
    ):
        return ""
    inner = re.sub(r"\s+", " ", clean[1:-1]).strip().casefold()
    inner = re.sub(r"\s+\d{1,2}:\d{2}(?::\d{2})?$", "", inner)
    return inner if inner in (REMOVABLE_STAGE_DIRECTIONS | EVIDENCE_MARKERS) else ""


def _filler_only(text: str) -> bool:
    if re.search(r"\d|[$€£¥%]", text):
        return False
    normalized = re.sub(r"[^A-Za-z\u3400-\u9fff'-]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized in FILLER_ONLY


def _split_inline_speakers(text: str) -> list[str]:
    matches = [
        match
        for match in INLINE_BRACKET_RE.finditer(text)
        if _looks_like_speaker(match.group(1))
    ]
    # Inline splitting is enabled only after the line itself establishes the
    # bracketed-speaker format. Otherwise ordinary prose such as "use [AI]"
    # could be mistaken for a new speaker turn.
    if not matches or matches[0].start() != 0:
        return [text]
    positions = [match.start() for match in matches]
    positions.append(len(text))
    return [
        text[start:end].strip()
        for start, end in pairwise(positions)
        if text[start:end].strip()
    ]


def _source_lines(text: str) -> list[str]:
    normalized = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\xa0", " ")
    )
    raw_lines = normalized.splitlines()
    webvtt = normalized.lstrip().casefold().startswith("webvtt")
    structured_captions = webvtt or any(
        CUE_TIMING_RE.fullmatch(line.strip()) for line in raw_lines
    )
    if not structured_captions:
        return [
            split
            for line in raw_lines
            if (clean := line.strip())
            for split in _split_inline_speakers(clean)
        ]

    output: list[str] = []
    index = 0
    in_cue = False
    at_block_start = True
    skip_control_block = False
    seen_cue = False
    while index < len(raw_lines):
        line = raw_lines[index].strip()
        if not line:
            in_cue = False
            at_block_start = True
            skip_control_block = False
            index += 1
            continue

        # NOTE/STYLE/REGION are control blocks only between WebVTT cues. The
        # same words are ordinary evidence when they occur inside cue payload.
        if skip_control_block:
            index += 1
            continue

        if in_cue:
            output.extend(_split_inline_speakers(line))
            at_block_start = False
            index += 1
            continue

        if (
            webvtt
            and at_block_start
            and not seen_cue
            and (
                line.casefold() == "webvtt"
                or line.startswith(("Kind:", "Language:"))
            )
        ):
            index += 1
            continue

        if webvtt and at_block_start and re.match(
            r"^(?:NOTE(?:\s|$)|STYLE$|REGION$)", line
        ):
            skip_control_block = True
            index += 1
            continue

        if (
            at_block_start
            and index + 1 < len(raw_lines)
            and CUE_TIMING_RE.fullmatch(raw_lines[index + 1].strip())
        ):
            # A WebVTT cue identifier or SRT sequence number.
            index += 1
            continue

        if CUE_TIMING_RE.fullmatch(line):
            in_cue = True
            seen_cue = True
            at_block_start = False
            index += 1
            continue

        # Malformed caption exports sometimes contain ordinary text outside a
        # cue. Preserve it instead of guessing that a clock-looking line is a
        # timestamp; a value such as 12:34 may itself be evidence.
        output.extend(_split_inline_speakers(line))
        at_block_start = False
        index += 1
    return output


def _heading(text: str) -> bool:
    clean = text.strip()
    latin_letters = re.findall(r"[A-Za-z]", clean)
    return bool(
        clean
        and len(clean) <= 100
        and not any(mark in clean for mark in ("?", "!", "？", "！"))
        and (
            bool(latin_letters)
            and clean.upper() == clean
            and clean.lower() != clean
            or re.fullmatch(
                r"(?:PART|CHAPTER)\s+\d+(?:\s*[:|—-].*)?",
                clean,
                re.IGNORECASE,
            )
        )
    )


def _parse_blocks(text: str) -> list[TranscriptBlock]:
    blocks: list[TranscriptBlock] = []
    current_speaker = ""
    for line in _source_lines(text):
        direction = _direction_label(line)
        if direction in REMOVABLE_STAGE_DIRECTIONS:
            blocks.append(TranscriptBlock("stage", line))
            continue
        if direction in EVIDENCE_MARKERS:
            blocks.append(TranscriptBlock("marker", line))
            current_speaker = ""
            continue
        bracket = BRACKET_LABEL_RE.fullmatch(line)
        if bracket and _looks_like_speaker(bracket.group(1)):
            current_speaker = _plain(bracket.group(1))
            body = bracket.group(2).strip()
            if body:
                blocks.append(
                    TranscriptBlock("speech", body, current_speaker)
                )
            continue
        colon = COLON_LABEL_RE.fullmatch(line)
        if colon and _looks_like_speaker(colon.group(1)):
            current_speaker = _plain(colon.group(1))
            body = colon.group(2).strip()
            if body:
                blocks.append(
                    TranscriptBlock("speech", body, current_speaker)
                )
            continue
        if _heading(line):
            blocks.append(TranscriptBlock("heading", line))
            current_speaker = ""
        elif current_speaker:
            blocks.append(TranscriptBlock("speech", line, current_speaker))
        else:
            blocks.append(TranscriptBlock("text", line))
    return blocks


def _normalized_duplicate(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _remove_caption_duplicates(
    blocks: list[TranscriptBlock], *, caption_mode: bool
) -> tuple[list[TranscriptBlock], int]:
    if not caption_mode:
        return blocks, 0
    output: list[TranscriptBlock] = []
    removed = 0
    for block in blocks:
        if (
            output
            and block.kind == "text"
            and output[-1].kind == block.kind
            and output[-1].speaker == block.speaker
            and _normalized_duplicate(output[-1].text)
            == _normalized_duplicate(block.text)
        ):
            removed += 1
            continue
        output.append(block)
    return output, removed


AD_BRAND_STOPWORDS = {
    "and",
    "at",
    "by",
    "for",
    "from",
    "our",
    "partner",
    "sponsor",
    "the",
    "this",
    "with",
}


def _ad_brand_tokens(text: str, start_match: re.Match[str]) -> set[str]:
    remainder = text[start_match.end() :]
    sentence = next(iter(SENTENCE_RE.finditer(remainder)), None)
    candidate = sentence.group(0) if sentence is not None else remainder
    # Only proper-name-looking tokens may extend an ad across blocks. Taking
    # every word from the sponsor sentence made generic words such as "which"
    # or "build" delete unrelated interview answers many turns later.
    tokens = {
        token.casefold()
        for token in re.findall(r"\b[A-Z][A-Za-z0-9'-]{1,39}\b", candidate)
        if token.casefold() not in AD_BRAND_STOPWORDS
    }
    chinese = re.search(r"由\s*([^\s，。！？]{2,30}?)\s*赞助", start_match.group(0))
    if chinese:
        tokens.add(chinese.group(1).casefold())
    return tokens


def _ad_continuation(text: str, brand_tokens: set[str]) -> bool:
    if AD_START_RE.search(text) or AD_CTA_RE.search(text) or DOMAIN_RE.search(text):
        return True
    folded = text.casefold()
    return any(
        re.search(rf"\b{re.escape(token)}\b", folded)
        for token in brand_tokens
    )


def _sentence_values(text: str) -> list[str]:
    values = [match.group(0).strip() for match in SENTENCE_RE.finditer(text)]
    return values or ([text.strip()] if text.strip() else [])


def _evidence_from_ad_copy(text: str, *, drop_start: bool = False) -> str:
    """Keep only evidence-bearing sentences inside a confirmed sponsor read."""

    kept: list[str] = []
    for sentence in _sentence_values(text):
        start = AD_START_RE.search(sentence) if drop_start else None
        if start is not None:
            prefix = sentence[: start.start()].strip()
            suffix = sentence[start.end() :].strip(" ,;:—-")
            if prefix:
                kept.append(prefix)
            if suffix and AD_EVIDENCE_RE.search(suffix):
                kept.append(suffix)
            drop_start = False
            continue
        if AD_EVIDENCE_RE.search(sentence) or AD_CONFLICT_RE.search(sentence):
            kept.append(sentence)
    return " ".join(kept).strip()


def _cta_matches_brand(text: str, brand_tokens: set[str]) -> bool:
    domains = [match.group(0).casefold() for match in DOMAIN_RE.finditer(text)]
    if not domains:
        return False
    if any(token in domain for token in brand_tokens for domain in domains):
        return True
    # Some reads do not expose a clean brand token in the opening sentence.
    # A code/referral instruction or explicit return to the interview is still
    # a sufficiently strong boundary; a generic research link is not.
    return bool(
        re.search(
            r"(?:use (?:the )?(?:code|promo)|tell (?:them|’em|'em) that|"
            r"sent you|回到(?:节目|对谈|正题)|优惠码)",
            text,
            re.IGNORECASE,
        )
        or AD_RETURN_RE.search(text)
        or POST_CTA_TRANSITION_RE.search(text)
    )


def _post_cta_position(text: str) -> int | None:
    matches = list(DOMAIN_RE.finditer(text))
    if not matches:
        return None
    start = matches[-1].end()
    transitions = [
        match
        for pattern in (POST_CTA_TRANSITION_RE, AD_RETURN_RE)
        if (match := pattern.search(text, start)) is not None
    ]
    return min(match.start() for match in transitions) if transitions else None


def _sentence_end_after(text: str, position: int) -> int:
    for match in SENTENCE_RE.finditer(text):
        if match.end() >= position:
            return match.end()
    return len(text)


def _cta_domain_parts(text: str) -> tuple[str, str] | None:
    cta = AD_CTA_RE.search(text)
    domains = list(DOMAIN_RE.finditer(text))
    if cta is None or not domains:
        return None
    remove_start = min(cta.start(), domains[0].start())
    transition = _post_cta_position(text)
    marker_end = max(cta.end(), domains[-1].end())
    remove_end = (
        transition
        if transition is not None and transition >= marker_end
        else _sentence_end_after(text, marker_end)
    )
    return text[:remove_start].strip(), text[remove_end:].strip()


def _remove_ads(blocks: list[TranscriptBlock]) -> tuple[list[TranscriptBlock], int]:
    output: list[TranscriptBlock] = []
    removed = 0
    index = 0
    while index < len(blocks):
        block = blocks[index]
        start_match = AD_START_RE.search(block.text)
        starts_ad = bool(start_match and not AD_CONFLICT_RE.search(block.text))
        if not starts_ad:
            output.append(block)
            index += 1
            continue

        scan = index
        char_count = 0
        end_found = False
        ad_speaker = block.speaker.strip().casefold()
        brand_tokens = _ad_brand_tokens(block.text, start_match)
        end_parts: tuple[str, str] | None = None
        while scan < min(len(blocks), index + AD_MAX_BLOCKS):
            candidate = blocks[scan]
            candidate_speaker = candidate.speaker.strip().casefold()
            if (
                scan > index
                and ad_speaker
                and candidate_speaker
                and candidate_speaker != ad_speaker
            ):
                # A speaker change is an evidence boundary, not an ad-copy
                # continuation. Without a CTA before that boundary we cannot
                # prove where the sponsor read ends, so retain the whole span.
                break
            char_count += len(candidate.text)
            if char_count > AD_MAX_CHARS:
                break
            if scan > index and candidate.kind in {
                "boundary",
                "heading",
                "marker",
                "stage",
            }:
                break
            candidate_text = (
                candidate.text[start_match.start() :]
                if scan == index and start_match is not None
                else candidate.text
            )
            parts = _cta_domain_parts(candidate_text)
            if parts is not None and _cta_matches_brand(
                candidate_text, brand_tokens
            ):
                end_parts = parts
                end_found = True
                break
            disclaimer = DISCLAIMER_RE.search(candidate_text)
            if disclaimer is not None:
                end_parts = (
                    candidate_text[: disclaimer.start()].strip(),
                    candidate_text[disclaimer.start() :].strip(),
                )
                end_found = True
                break
            scan += 1

        if not end_found or end_parts is None:
            output.append(block)
            index += 1
            continue
        leading_prefix = (
            block.text[: start_match.start()].strip()
            if start_match is not None
            else ""
        )
        if leading_prefix:
            output.append(
                TranscriptBlock(block.kind, leading_prefix, block.speaker)
            )
        output.append(TranscriptBlock("boundary", ""))
        for segment_index in range(index, scan + 1):
            candidate = blocks[segment_index]
            if segment_index == index:
                segment_text = candidate.text[start_match.start() :]
                if segment_index == scan:
                    segment_text = end_parts[0]
                kept = _evidence_from_ad_copy(segment_text, drop_start=True)
            elif segment_index == scan:
                kept = _evidence_from_ad_copy(end_parts[0])
            else:
                kept = _evidence_from_ad_copy(candidate.text)
            if kept:
                output.append(
                    TranscriptBlock(candidate.kind, kept, candidate.speaker)
                )
            if segment_index == scan and end_parts[1]:
                output.append(
                    TranscriptBlock(
                        candidate.kind, end_parts[1], candidate.speaker
                    )
                )
            if kept or segment_index == scan:
                output.append(TranscriptBlock("boundary", ""))
        removed += scan - index + 1
        index = scan + 1
    return [
        block
        for block in output
        if block.kind == "boundary" or block.text.strip()
    ], removed


def _remove_housekeeping_noise(
    blocks: list[TranscriptBlock],
) -> tuple[list[TranscriptBlock], int]:
    """Remove clearly bounded show logistics near the opening."""

    output: list[TranscriptBlock] = []
    removed = 0
    opening_limit = min(40, len(blocks))
    closing_start = max(0, len(blocks) - 40)
    for index, block in enumerate(blocks):
        domains = list(DOMAIN_RE.finditer(block.text))
        if (
            (index < opening_limit or index >= closing_start)
            and len(domains) >= 2
            and HOUSEKEEPING_RE.search(block.text)
            and not AD_EVIDENCE_RE.search(block.text)
        ):
            output.append(TranscriptBlock("boundary", ""))
            removed += 1
            continue
        output.append(block)
    return output, removed


def _remove_outro_phrases(text: str) -> str:
    kept: list[str] = []
    cursor = 0
    for sentence in SENTENCE_RE.finditer(text):
        if sentence.start() > cursor:
            kept.append(text[cursor : sentence.start()])
        value = sentence.group(0)
        match = OUTRO_RE.match(value)
        if match is None:
            kept.append(value)
        else:
            suffix = value[match.end() :]
            suffix = re.sub(
                r"^[\s,;:—-]*(?:and\s+)?",
                "",
                suffix,
                flags=re.IGNORECASE,
            ).strip()
            if not re.search(r"[A-Za-z0-9㐀-鿿]", suffix):
                suffix = ""
            if suffix:
                kept.append(suffix)
        cursor = sentence.end()
    if cursor < len(text):
        kept.append(text[cursor:])
    return " ".join(part.strip() for part in kept if part.strip()).strip()


def _remove_outro_noise(
    blocks: list[TranscriptBlock],
) -> tuple[list[TranscriptBlock], int]:
    removed = 0
    cutoff = max(0, len(blocks) - 12)
    output: list[TranscriptBlock] = []
    for index, block in enumerate(blocks):
        footer = FOOTER_PROMO_RE.search(block.text) if index >= cutoff else None
        if footer is not None and len(DOMAIN_RE.findall(block.text)) >= 2:
            prefix = block.text[: footer.start()].strip()
            if prefix.casefold().strip(" .,!?:;—-") not in {
                "yes",
                "yep",
                "right",
                "okay",
                "ok",
            } and prefix:
                output.append(TranscriptBlock(block.kind, prefix, block.speaker))
            output.append(TranscriptBlock("boundary", ""))
            removed += 1
            continue
        cleaned_text = (
            _remove_outro_phrases(block.text)
            if index >= cutoff
            else block.text
        )
        if cleaned_text != block.text:
            removed += 1
            if cleaned_text:
                output.append(
                    TranscriptBlock(block.kind, cleaned_text, block.speaker)
                )
            output.append(TranscriptBlock("boundary", ""))
            continue
        output.append(block)
    return output, removed


def _merge_blocks(blocks: list[TranscriptBlock]) -> list[TranscriptBlock]:
    output: list[TranscriptBlock] = []
    merge_allowed = False
    for block in blocks:
        if block.kind == "boundary":
            merge_allowed = False
            continue
        clean = re.sub(r"\s+", " ", block.text).strip()
        if not clean:
            continue
        normalized = TranscriptBlock(block.kind, clean, block.speaker)
        if (
            merge_allowed
            and output
            and normalized.kind == "speech"
            and output[-1].kind == "speech"
            and normalized.speaker == output[-1].speaker
        ):
            previous = output[-1]
            output[-1] = TranscriptBlock(
                "speech", f"{previous.text} {normalized.text}", previous.speaker
            )
            continue
        output.append(normalized)
        merge_allowed = True
    return output


def clean_transcript(text: str, *, source: str = "") -> CleanedTranscript:
    """Create a conservative presentation layer without touching the evidence text."""

    blocks = _parse_blocks(text)
    stage_count = sum(block.kind == "stage" for block in blocks)
    without_stage: list[TranscriptBlock] = []
    for block in blocks:
        if block.kind == "stage":
            without_stage.append(TranscriptBlock("boundary", ""))
        else:
            without_stage.append(block)

    kept: list[TranscriptBlock] = []
    filler_count = 0
    for block in without_stage:
        if block.kind == "speech" and _filler_only(block.text):
            filler_count += 1
            kept.append(TranscriptBlock("boundary", ""))
            continue
        kept.append(block)
    blocks, duplicate_count = _remove_caption_duplicates(
        kept, caption_mode="caption" in source.casefold()
    )
    blocks, ad_count = _remove_ads(blocks)
    blocks, housekeeping_count = _remove_housekeeping_noise(blocks)
    ad_count += housekeeping_count
    blocks, edge_noise_count = _remove_outro_noise(blocks)
    ad_count += edge_noise_count
    blocks = _merge_blocks(blocks)
    if not blocks:
        # Presentation cleanup must never turn a verified source into an empty
        # attachment. Falling back affects only this view, not the source archive.
        blocks = _merge_blocks(
            [block for block in _parse_blocks(text) if block.kind != "stage"]
        )
    return CleanedTranscript(
        tuple(blocks),
        removed_stage_directions=stage_count,
        removed_filler_turns=filler_count,
        removed_ad_blocks=ad_count,
        removed_caption_duplicates=duplicate_count,
    )


def _paragraphs(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= PARAGRAPH_TARGET_CHARS:
        return [clean] if clean else []
    sentences = [match.group(0).strip() for match in SENTENCE_RE.finditer(clean)]
    if len(sentences) <= 1:
        return [
            clean[index : index + PARAGRAPH_TARGET_CHARS]
            for index in range(0, len(clean), PARAGRAPH_TARGET_CHARS)
        ]
    paragraphs: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > PARAGRAPH_TARGET_CHARS:
            paragraphs.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        paragraphs.append(current)
    return paragraphs


def _render_blocks(blocks: tuple[TranscriptBlock, ...]) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            lines.extend((f"### {block.text.title()}", ""))
            continue
        if block.kind == "marker":
            lines.extend((f"*{block.text}*", ""))
            continue
        paragraphs = _paragraphs(block.text)
        if block.speaker:
            lines.extend((f"**{block.speaker}**", ""))
        for paragraph in paragraphs:
            lines.extend((paragraph, ""))
    return lines


def _digest_body(markdown: str) -> str:
    lines = markdown.strip().splitlines()
    first_point = next(
        (index for index, line in enumerate(lines) if NUMBERED_POINT_RE.match(line)),
        None,
    )
    if first_point is None:
        return ""
    start = first_point
    cursor = first_point - 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    if cursor >= 0 and TOPIC_HEADING_RE.fullmatch(lines[cursor].strip()):
        start = cursor
    return "\n".join(lines[start:]).strip()


def _duration(record: StoredTranscript) -> str:
    if record.episode.duration_string:
        return _plain(record.episode.duration_string)
    if record.episode.duration_seconds:
        return f"{round(record.episode.duration_seconds / 60)} 分钟"
    return "未提供"


def _published_date(record: StoredTranscript) -> str:
    value = record.episode.published_at
    return value.date().isoformat() if isinstance(value, datetime) else "未提供"


def _attachment_filename(record: StoredTranscript) -> str:
    title = FILENAME_UNSAFE_RE.sub("_", _plain(record.episode.title))
    title = re.sub(r"\s+", " ", title).strip(" ._") or "podcast"
    title = title[:72].rstrip(" ._") or "podcast"
    published = _published_date(record)
    prefix = f"{published}_" if published != "未提供" else ""
    return f"{prefix}{title}_{record.reference}_精编文字稿.md"


def render_readable_transcript(
    record: StoredTranscript, *, digest_markdown: str = ""
) -> tuple[str, bytes]:
    transcript = record.transcript
    if not transcript.verified_complete:
        raise ValueError("An unverified transcript cannot be attached")
    if not transcript.text.strip():
        raise ValueError("A complete transcript cannot be empty")

    cleaned = clean_transcript(transcript.text, source=transcript.source)
    if not cleaned.blocks:
        raise ValueError("A complete transcript cannot render an empty view")
    episode = record.episode
    lines = [
        f"# {_plain(episode.title)}｜精编可读版文字稿",
        "",
        f"**节目**：{_plain(episode.show) or '未提供'}  ",
        f"**发布日期**：{_published_date(record)}｜**时长**：{_duration(record)}  ",
        (
            f"**来源**：{_safe_link('原节目', episode.url)}｜"
            f"{_safe_link(transcript.source, transcript.source_url)}"
        ),
        "",
        "> 阅读说明：本文只删除高置信度的广告口播、开场/收尾推广、纯语气词、舞台提示和机械重复；",
        "> 所有实质性问答、数字、例子、异议和限定条件按原顺序保留。原始核验全文未被改写，仍由情报官用于问答与核验。",
        "",
    ]
    digest_body = _digest_body(digest_markdown)
    if digest_body:
        lines.extend(("## 核心论点", "", digest_body, ""))
    lines.extend(("## 精编对谈实录", ""))
    lines.extend(_render_blocks(cleaned.blocks))
    speaker_labeled = any(block.speaker for block in cleaned.blocks)
    lines.extend(
        (
            "## 来源与核验",
            "",
            "- 来源覆盖：已取得并核验完整文字稿；不代表节目中的事实已经外部核验",
            f"- 说话人标签：{'来源已保留' if speaker_labeled else '来源未可靠保留，未推断归因'}",
            f"- 本次清理：移除 {cleaned.removed_count} 个高置信度非实质内容块",
            f"- 检索编号：{record.reference}",
            f"- 原稿指纹：{record.content_sha256[:12]}",
            f"- 阅读版规则：{RENDERER_VERSION}",
            "",
        )
    )
    return _attachment_filename(record), "\n".join(lines).encode("utf-8")
