"""Opt-in, source-covered Chinese interview study notes and chronological QA."""

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .glossary import GLOSSARY_INSTRUCTIONS, preserve_glossary_values
from .shownotes import Correction, NotePoint, text_node, transcript_evidence
from .titles import normalize_deepread_title

LOGGER = logging.getLogger(__name__)

INSTRUCTIONS = (Path(__file__).parent / "prompts" / "deepread_new_style.md").read_text(
    encoding="utf-8"
) + GLOSSARY_INSTRUCTIONS
# Pinned to the pre-#58 prompt hash so deployed docs keep their identity;
# prompt edits apply only to episodes without an existing document.
# Bump explicitly only when existing documents must be regenerated.
VERSION = "deepread-new-style-v3-3169390d8c8e"
INTRO = "依据完整文字稿整理核心内容与中文对谈；保留原始顺序、第一人称、案例与限定条件，删除广告和无实质信息的重复。"
# Output validation rejects workflow provenance, including variants used in drafts.
WORKFLOW_LABEL = re.compile(
    r"王[颢頗颇]蓁|(?i:wang\s+method)|"
    r"(?<![A-Za-z0-9_])A-B(?![A-Za-z0-9_]|\s*(?:testing|测试))|[\[【]A[-/]B[\]】]|"
    r"(?<![A-Za-z0-9_])A[-/]B(?=\s*(?:工作流|流程|方法|标签))"
)
BATCH_BLOCKS = 5
BATCH_CHARACTERS = 3000
MAX_OUTPUT_TOKENS = 12000
CHUNK_INSTRUCTIONS = (
    "\n输入是按原稿顺序切分的一批文字稿；仅整理本批全部内容，不补写其他批次。"
    "core 仅概括本批；所有 evidence_ids 必须来自本批 transcript。"
    "known_speakers 仅用于保持姓名和角色一致，不引用先前批次的内容。"
)


class Speaker(BaseModel):
    name: str = Field(min_length=1)
    role: Literal["host", "guest", "other"]
    evidence_ids: list[str] = Field(min_length=1)


class DialogueTurn(BaseModel):
    speaker: str
    paragraphs: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class Chapter(BaseModel):
    title: str = Field(min_length=1)
    turns: list[DialogueTurn] = Field(min_length=1)


class ProperNoun(BaseModel):
    original: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class Exclusion(BaseModel):
    evidence_ids: list[str] = Field(min_length=1)
    reason: Literal["advertisement", "montage", "filler", "outro", "duplicate"]


class DeepReadPoint(NotePoint):
    evidence_ids: list[str] = Field(min_length=1)


class EvidenceModel(BaseModel):
    @model_validator(mode='before')
    @classmethod
    def normalize_ids(cls, value):
        return normalize_evidence_ids(value)


class DeepRead(EvidenceModel):
    speakers: list[Speaker] = Field(min_length=1)
    core: list[DeepReadPoint] = Field(min_length=1)
    chapters: list[Chapter] = Field(min_length=1)
    proper_nouns: list[ProperNoun]
    exclusions: list[Exclusion]
    corrections: list[Correction]


class ChapterBoundary(BaseModel):
    title: str = Field(min_length=1)
    start_turn: int = Field(ge=0)


class Consolidation(EvidenceModel):
    core: list[DeepReadPoint] = Field(min_length=4, max_length=18)
    chapters: list[ChapterBoundary] = Field(min_length=2, max_length=10)


def consolidation_minimums(evidence):
    """Scale requirements from short (6k chars) to long (30k chars) sources."""
    characters = sum(len(body["text"]) for body in evidence.values())
    progress = min(24_000, max(0, characters - 6_000))
    return 4 + progress * 6 // 24_000, 2 + progress * 3 // 24_000


def consolidate(client, model, merged, evidence):
    min_core, min_chapters = consolidation_minimums(evidence)
    turns = [turn for chapter in merged["chapters"] for turn in chapter["turns"]]
    prompt = json.dumps(
        {
            "core": merged["core"],
            "sections": [
                {"title": chapter["title"], "turn_count": len(chapter["turns"])}
                for chapter in merged["chapters"]
            ],
            "turns": [{"index": i, **turn} for i, turn in enumerate(turns)],
        },
        ensure_ascii=False,
    )
    instructions = (
        f"将各批核心内容与章节整合为整期统一内容：{min_core}–18条具体加粗标签的核心要点，"
        f"{min_chapters}–10章，合并重复主题并保留细节、例子和限定条件。core evidence_ids沿用输入。"
        "章节仅返回title和start_turn（从0开始的全局对谈索引），首章从0开始，"
        "严格递增，每章至少一轮，保持原始顺序。所有对谈由程序原样保留，不重写。"
        "不得出现内部工作流标签。"
    )
    for attempt in range(3):
        try:
            response = client.responses.parse(
                model=model,
                instructions=instructions,
                input=prompt,
                text_format=Consolidation,
                store=False,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            if response.output_parsed is None:
                raise ValueError("Incomplete consolidation output")
            value = Consolidation.model_validate(response.output_parsed.model_dump())
            if len(value.core) < min_core or len(value.chapters) < min_chapters:
                raise ValueError(
                    f"Consolidation requires at least {min_core} core points "
                    f"and {min_chapters} chapters for this transcript"
                )
            starts = [chapter.start_turn for chapter in value.chapters]
            if (
                starts[0] != 0
                or starts != sorted(set(starts))
                or starts[-1] >= len(turns)
            ):
                raise ValueError("Invalid consolidation chapter boundaries")
            result = {
                **merged,
                "core": [point.model_dump() for point in value.core],
                "chapters": [
                    {"title": chapter.title, "turns": turns[start:end]}
                    for chapter, start, end in zip(
                        value.chapters, starts, starts[1:] + [len(turns)]
                    )
                ],
            }
            return validate_deepread(result, evidence)
        except Exception as error:
            if attempt == 2:
                raise ValueError("Deepread consolidation failed") from error
            instructions += "\n上次输出未通过校验：" + str(error)[:200] + "。请修正。"


def source_evidence(text):
    """Merge adjacent timestamp cues from the same explicitly named speaker.

    Keep every source character. Per-cue IDs burden long-episode coverage with
    hundreds of half sentences; speaker turns are useful audit units. Bound
    long turns so a single turn cannot overwhelm a generation batch.
    Unlabelled or ambiguous blocks remain separate, never infer an identity.
    """
    result = {}
    previous = None
    for body in transcript_evidence(text).values():
        match = re.match(
            r"^\[\d{1,3}:\d{2}(?::\d{2})?\]\s*([^:\n：]{1,80})[:：]",
            body["text"],
        )
        speaker = match[1].strip() if match else None
        if speaker and speaker == previous:
            result[next(reversed(result))]["text"] += body["text"]
        else:
            result[f"C{len(result) + 1:04d}"] = dict(body)
        previous = speaker
    bounded = {}
    for body in result.values():
        for start in range(0, len(body["text"]), 2400):
            bounded[f"C{len(bounded) + 1:04d}"] = {
                **body,
                "text": body["text"][start : start + 2400],
            }
    return bounded


def check_output(text):
    if WORKFLOW_LABEL.search(text):
        raise ValueError("Internal workflow label in document")


def normalize_proper_noun(text):
    """Treat punctuation (including Unicode hyphens) as word separators."""
    return " ".join(
        "".join(
            " " if unicodedata.category(char).startswith("P") else char
            for char in text.casefold()
        ).split()
    )


def normalize_evidence_ids(value):
    """Recover quoted/comma-separated IDs without hiding unknown references."""
    if isinstance(value, list):
        return [normalize_evidence_ids(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {}
    for key, item in value.items():
        if key == 'evidence_ids' and isinstance(item, list):
            ids = []
            for element in item:
                tokens = (re.findall(r'(?<![A-Za-z0-9_])C\d{4}(?!\d)', element)
                          if isinstance(element, str) else [])
                for token in tokens or [element]:
                    if token not in ids:
                        ids.append(token)
            normalized[key] = ids
        else:
            normalized[key] = normalize_evidence_ids(item)
    return normalized


def validate_deepread(value, evidence):
    value = normalize_evidence_ids(value)
    source = "\n".join(body["text"] for body in evidence.values())
    notes = DeepRead.model_validate(preserve_glossary_values(value, source))
    check_output(json.dumps(notes.model_dump(), ensure_ascii=False))
    order = {key: index for index, key in enumerate(evidence)}

    def refs(keys):
        if any(key not in order for key in keys):
            raise ValueError(
                "Unknown deepread evidence: "
                + next(key for key in keys if key not in order)
            )

    names = [s.name for s in notes.speakers]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate speaker identity")
    for speaker in notes.speakers:
        refs(speaker.evidence_ids)
    for point in notes.core:
        refs(point.evidence_ids)
        if not point.text.strip() or not re.match(r"^\*\*.+?\*\*", point.text):
            raise ValueError("Core bullet needs a specific bold label")
    covered, previous, dialogue = set(), -1, []
    for chapter in notes.chapters:
        if not chapter.title.strip():
            raise ValueError("Empty chapter title")
        for turn in chapter.turns:
            refs(turn.evidence_ids)
            if turn.speaker not in names:
                raise ValueError("Unknown speaker label")
            first = min(order[key] for key in turn.evidence_ids)
            if first < previous:
                raise ValueError("QA chronology changed")
            previous = first
            covered.update(turn.evidence_ids)
            for paragraph in turn.paragraphs:
                if not paragraph.strip() or re.search(
                    r"\[\d{1,3}:\d{2}(?::\d{2})?\]", paragraph
                ):
                    raise ValueError("Empty QA paragraph or timestamp residue")
                dialogue.append(paragraph)
    for exclusion in notes.exclusions:
        refs(exclusion.evidence_ids)
        covered.update(exclusion.evidence_ids)
    if covered != set(evidence):
        missing = sorted(set(evidence) - covered)
        raise ValueError(
            f"Incomplete QA source coverage: {len(missing)} blocks missing: {missing[:20]}"
        )
    qa = normalize_proper_noun("\n".join([*names, *dialogue]))
    supported_nouns = []
    for noun in notes.proper_nouns:
        refs(noun.evidence_ids)
        original = normalize_proper_noun(noun.original)
        if not original or not any(
            original in normalize_proper_noun(evidence[key]["text"])
            for key in noun.evidence_ids
        ):
            LOGGER.warning(
                "Dropping unsupported proper noun %r from cited evidence %s",
                noun.original,
                noun.evidence_ids,
            )
            continue
        if (
            not re.search(r"[A-Za-z]", noun.canonical)
            or re.search(r"[\u3400-\u9fff]", noun.canonical)
        ):
            raise ValueError("English proper noun missing or translated in QA")
        if normalize_proper_noun(noun.canonical) not in qa:
            LOGGER.warning(
                "Dropping proper noun %r missing from QA", noun.canonical
            )
            continue
        supported_nouns.append(noun)
    notes.proper_nouns = supported_nouns
    for correction in notes.corrections:
        refs([correction.evidence_id])
        if (
            not correction.original.strip()
            or correction.original not in evidence[correction.evidence_id]["text"]
        ):
            raise ValueError("ASR original not found")
    return notes.model_dump()


def generate(client, model, record):
    evidence = source_evidence(record.transcript.text)
    if not evidence or not record.transcript.verified_complete:
        raise ValueError("Complete transcript required")
    ep = record.episode
    check_output(ep.title)
    if sum(len(body["text"]) for body in evidence.values()) > 900_000:
        raise ValueError("Transcript exceeds deepread input budget; no truncation")
    batches, batch = [], {}
    for key, body in evidence.items():
        if batch and (
            len(batch) >= BATCH_BLOCKS
            or sum(len(v["text"]) for v in batch.values()) + len(body["text"])
            > BATCH_CHARACTERS
        ):
            batches.append(batch)
            batch = {}
        batch[key] = body
    batches.append(batch)
    merged = {key: [] for key in DeepRead.model_fields}
    speakers = {}
    for batch in batches:
        prompt = json.dumps(
            {
                "title": ep.title,
                "show": ep.show,
                "description": ep.metadata.get("description", ""),
                "transcript": batch,
                "known_speakers": [
                    {"name": s["name"], "role": s["role"]} for s in speakers.values()
                ],
            },
            ensure_ascii=False,
        )
        repair = ""
        for attempt in range(3):
            try:
                response = client.responses.parse(
                    model=model,
                    instructions=INSTRUCTIONS + CHUNK_INSTRUCTIONS + repair,
                    input=prompt,
                    text_format=DeepRead,
                    store=False,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
                if response.output_parsed is None:
                    raise ValueError("Incomplete model output")
                value = validate_deepread(response.output_parsed.model_dump(), batch)
                break
            except Exception as error:
                if attempt == 2:
                    raise ValueError("Deepread chunk generation failed") from error
                if isinstance(error, ValueError):
                    repair = (
                        "\n上次输出未通过校验："
                        + str(error)[:200]
                        + "。仅返回本批源文的完整结果，核对覆盖与顺序，不缩写或省略。"
                    )
        for speaker in value["speakers"]:
            name = speaker["name"]
            if name in speakers:
                if speakers[name]["role"] != speaker["role"]:
                    raise ValueError("Inconsistent speaker identity across chunks")
                speakers[name]["evidence_ids"] = list(
                    dict.fromkeys(
                        speakers[name]["evidence_ids"] + speaker["evidence_ids"]
                    )
                )
            else:
                speakers[name] = speaker
        for key, items in merged.items():
            if key != "speakers":
                items.extend(value[key])
    merged["speakers"] = list(speakers.values())
    if len(batches) > 1:
        return consolidate(client, model, merged, evidence)
    return validate_deepread(merged, evidence)


def render(record, value, digest=""):
    notes = validate_deepread(value, source_evidence(record.transcript.text))
    ep = record.episode
    check_output(ep.title + ep.show + digest)
    published = ep.published_at.isoformat()[:10] if ep.published_at else "原源未标明"
    duration = (
        f"{int(ep.duration_seconds) // 60} 分 {int(ep.duration_seconds) % 60:02d} 秒"
        if ep.duration_seconds
        else "未提供"
    )
    nodes, markdown = [], []

    def add(text, kind="text", node=None):
        nodes.extend([node or text_node(text, kind), text_node(" ")])
        prefix = {"heading1": "# ", "heading2": "## ", "bullet": "- "}.get(kind, "")
        markdown.append(prefix + text)

    add(normalize_deepread_title(ep.title), "heading1")
    add(f"节目：{ep.show}｜发布时间：{published}｜时长：{duration}")
    add(f"[原始访谈]({ep.url})")
    for role, label in [("guest", "嘉宾"), ("host", "主持人"), ("other", "其他说话人")]:
        names = [s["name"] for s in notes["speakers"] if s["role"] == role]
        if names:
            add(label + "：" + "、".join(names))
    add(
        "整理说明："
        + INTRO
        + "修正可确认的 ASR 错误与断句；观点、数字及预测归属说话人，未作外部事实核验。专有名词保留 English，不保留时间戳。"
    )
    for line in digest.splitlines():
        if line.startswith(("推荐理由：", "推荐星级：")):
            add(line)
    add("核心内容", "heading1")
    for point in notes["core"]:
        add(point["text"], "bullet")
    add("QA 整理", "heading1")
    roles = {s["name"]: s["role"] for s in notes["speakers"]}
    for i, chapter in enumerate(notes["chapters"], 1):
        add(f"{i:02d}. {chapter['title']}", "heading2")
        for turn in chapter["turns"]:
            for j, paragraph in enumerate(turn["paragraphs"]):
                if j:
                    add(paragraph)
                    continue
                label = turn["speaker"] + "："
                style = {"bold": True, "background_color": 3}
                if roles[turn["speaker"]] == "guest":
                    style["text_color"] = 3
                node = text_node(paragraph)
                node["text"]["elements"].insert(
                    0, {"text_run": {"content": label, "text_element_style": style}}
                )
                add(label + paragraph, node=node)
    return nodes, "\n\n".join(markdown)
