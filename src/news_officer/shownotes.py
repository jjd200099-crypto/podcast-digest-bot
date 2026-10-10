"""Transcript-grounded daily shownotes, adapted from podcast-notes-feishu.

No browser dependency, full-transcript republication, invented timestamps, or
deletion of the durable transcript archive. The whole transcript reaches the
model; source IDs and short verbatim quotes are checked before publication.
"""

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .library import inline_elements
from .qa import chunk_transcript

WORKFLOW = (Path(__file__).parent / 'prompts' / 'podcast_shownotes.md').read_text(encoding='utf-8')
VERSION = 'podcast-notes-feishu-v3-' + hashlib.sha256(WORKFLOW.encode()).hexdigest()[:12]
INTRO = '本文基于已核验完整文字稿编译。观点、数字与预测归属节目嘉宾；不附整期实录。'


class NotePoint(BaseModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1, max_length=4)


class TableRow(BaseModel):
    cells: list[str] = Field(min_length=2, max_length=5)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)


class ComparisonTable(BaseModel):
    columns: list[str] = Field(min_length=2, max_length=5)
    rows: list[TableRow] = Field(min_length=2, max_length=8)


class NotePart(BaseModel):
    title: str
    points: list[NotePoint] = Field(min_length=2, max_length=6)
    tables: list[ComparisonTable] = Field(default_factory=list, max_length=2)


class ShortQuote(BaseModel):
    text: str
    speaker: str
    evidence_id: str
    part_index: int = Field(default=0, ge=0, le=11)


class Correction(BaseModel):
    original: str
    corrected: str
    reason: str
    evidence_id: str


class EpisodeNotes(BaseModel):
    participants: str
    participant_evidence_ids: list[str]
    core: list[NotePoint] = Field(min_length=6, max_length=9)
    parts: list[NotePart] = Field(min_length=1, max_length=12)
    quotes: list[ShortQuote] = Field(max_length=12)
    corrections: list[Correction] = Field(max_length=8)


# Shared by scheduled and on-demand document generation.
INSTRUCTIONS = WORKFLOW


def transcript_evidence(text):
    result, previous = {}, ''
    # Preserve every source character while anchoring timestamped utterances
    # individually, instead of assigning several minutes to a chunk's first cue.
    bodies = re.split(r'(?m)(?=^\[\d{1,3}:\d{2}(?::\d{2})?\])', text)
    chunks = [(f'C{i:04d}', body) for i, body in enumerate(filter(None, bodies), 1)] if len(bodies) > 1 else chunk_transcript(text)
    for key, body in chunks:
        times = re.findall(r'\[(\d{1,3}:\d{2}(?::\d{2})?)\]', body)
        result[key] = {'text': body, 'time': times[0] if times else previous}
        if times:
            previous = times[-1]
    return result


def validate_notes(value, evidence):
    notes = EpisodeNotes.model_validate(value)
    if notes.participants != '原稿未明确标注' and not notes.participant_evidence_ids:
        raise ValueError('Participant identity needs transcript evidence')
    if any(key not in evidence for key in notes.participant_evidence_ids):
        raise ValueError('Unknown participant evidence')
    for point in [*notes.core, *(p for part in notes.parts for p in part.points)]:
        if not point.text.strip() or len(point.text) > 450:
            raise ValueError('Empty or overlong shownotes point')
        if any(key not in evidence for key in point.evidence_ids):
            raise ValueError('Unknown shownotes evidence')
        if re.search(r'https?://', point.text):
            raise ValueError('Sources are rendered by the application')
        if re.search(r'编者(?:分析|理解|收束)|完整对谈实录', point.text):
            raise ValueError('Use source-led notes, not editorial commentary or full transcript')
    for part in notes.parts:
        for table in part.tables:
            for row in table.rows:
                if len(row.cells) != len(table.columns) or any(len(cell) > 250 for cell in row.cells):
                    raise ValueError('Invalid comparison table row')
                if any(key not in evidence for key in row.evidence_ids):
                    raise ValueError('Unknown table evidence')
    words = 0
    for quote in notes.quotes:
        if (quote.evidence_id not in evidence or not quote.text.strip()
                or quote.text not in evidence[quote.evidence_id]['text']):
            raise ValueError('Quote not found in cited transcript')
        if quote.part_index >= len(notes.parts):
            raise ValueError('Quote part does not exist')
        words += len(re.findall(r"\w+(?:['’-]\w+)*", quote.text))
    if words > 25:
        raise ValueError('Quote budget exceeded')
    for correction in notes.corrections:
        if (correction.evidence_id not in evidence or not correction.original.strip()
                or correction.original not in evidence[correction.evidence_id]['text']):
            raise ValueError('ASR original not found')
    return notes.model_dump()


def chapter_outline(episode):
    """Only use publisher-supplied timestamp cues, never model-invented chapters."""
    description = str(episode.metadata.get('description') or '')
    cues = list(re.finditer(r'(?:^|\n|\()(\d{1,2}:\d{2}(?::\d{2})?)\)?\s+', description))
    chapters = []
    for i, cue in enumerate(cues):
        title = description[cue.end():cues[i + 1].start() if i + 1 < len(cues) else len(description)].split('\n')[0].strip()[:180]
        fields = [int(v) for v in cue[1].split(':')]
        seconds = sum(v * 60 ** n for n, v in enumerate(reversed(fields)))
        if not title or any(v >= 60 for v in fields[1:]) or (episode.duration_seconds and seconds >= episode.duration_seconds):
            return []
        if chapters and seconds <= chapters[-1]['seconds']:
            return []
        chapters.append({'time': cue[1], 'seconds': seconds, 'title': title})
    return chapters if len(chapters) >= 2 and chapters[0]['seconds'] <= 60 else []


class ShownotesWriter:
    def __init__(self, client, model):
        self.client, self.model = client, model

    def generate(self, record):
        evidence = transcript_evidence(record.transcript.text)
        if not evidence or not record.transcript.verified_complete:
            raise ValueError('Complete transcript required')
        prompt = json.dumps({'title': record.episode.title, 'show': record.episode.show,
                             'official_chapters': chapter_outline(record.episode),
                             'transcript': evidence}, ensure_ascii=False)
        # Fail visibly instead of silently truncating a long interview.
        if len(prompt) > 900_000:
            raise ValueError('Transcript exceeds shownotes input budget; no truncation')
        repair = ''
        for _ in range(3):
            response = self.client.responses.parse(
                model=self.model, instructions=INSTRUCTIONS + repair, input=prompt,
                text_format=EpisodeNotes, store=False, max_output_tokens=11000,
            )
            try:
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError('Incomplete model output')
                return validate_notes(parsed.model_dump(), evidence)
            except ValueError as error:
                repair = '\n上次输出未通过校验：' + str(error)[:250]
        raise ValueError('Shownotes validation failed')


def text_node(text, kind='text'):
    number = {'text': 2, 'heading1': 3, 'heading2': 4, 'heading3': 5,
              'heading4': 6, 'bullet': 12, 'quote': 15}[kind]
    return {'block_type': number, kind: {'elements': inline_elements(text)}}


def table_node(rows):
    width = len(rows[0]) if rows else 0
    if not width or any(len(row) != width for row in rows):
        raise ValueError('Invalid table')
    return {'block_type': 31, 'table': {'property': {'row_size': len(rows), 'column_size': width,
                                                   'header_row': True,
                                                   'column_width': [732 // width] * width}},
            'children': [{'block_type': 32, 'table_cell': {}, 'children': [text_node(cell)]}
                         for row in rows for cell in row]}


def render_episode(record, notes, digest='', *, fulltext_attached=False):
    evidence = transcript_evidence(record.transcript.text)
    notes = validate_notes(notes, evidence)
    ep = record.episode
    published = ep.published_at.isoformat()[:10] if ep.published_at else '原源未标明'
    duration = f'{int(ep.duration_seconds) // 60} 分 {int(ep.duration_seconds) % 60:02d} 秒' if ep.duration_seconds else '未提供'
    metadata = [f'嘉宾/主持：{notes["participants"]}', f'节目：{ep.show}',
                f'发布时间：{published}｜时长：{duration}',
                f'[收听本期]({ep.url})',
                ('章节沿节目说明中的时间标记整理。' if chapter_outline(ep) else '未取得官方章节，按原对谈话题分章。')
                + '时间戳取自所引原文段落，无时间戳时明确标注。']
    metadata.append('观点、数字与预测归属节目嘉宾；' + (
        '精读之后附去除时间戳的完整中文文字稿。' if fulltext_attached else '本文为主题精读，不附整期实录。'))
    for line in digest.splitlines():
        if line.startswith(('推荐理由：', '推荐星级：')):
            metadata.append(line)
    nodes = [text_node(ep.title, 'heading1'),
             {'block_type': 19, 'callout': {'background_color': 5, 'emoji_id': 'microphone'},
              'children': [text_node(line) for line in metadata]}, text_node('核心论点', 'heading2')]
    markdown = ['# ' + ep.title, *['> ' + line for line in metadata], '\n## 核心论点']
    for point in notes['core']:
        timestamp = evidence[point['evidence_ids'][0]]['time'] or '原稿无时间戳'
        line = '[' + timestamp + '] ' + point['text']
        nodes.append(text_node(line, 'bullet'))
        markdown.append('- ' + line)
    quotes_by_part = {}
    for quote in notes['quotes']:
        index = quote['part_index']
        quotes_by_part.setdefault(index, []).append(quote)
    for i, part in enumerate(notes['parts'], 1):
        title = f'Part {i}｜{part["title"]}'
        nodes.append(text_node(title, 'heading2'))
        markdown.append('\n## ' + title)
        for quote in quotes_by_part.get(i - 1, []):
            line = f'"{quote["text"]}" —— {quote["speaker"]}（原话片段）'
            nodes.append(text_node(line, 'quote'))
            markdown.append('> ' + line)
        for point in part['points']:
            nodes.append(text_node(point['text'], 'bullet'))
            markdown.append('- ' + point['text'])
        for table in part['tables']:
            rows = [table['columns'], *[row['cells'] for row in table['rows']]]
            nodes.append(table_node(rows))
            markdown.extend(['| ' + ' | '.join(v.replace('|', '／') for v in row) + ' |'
                             for row in [rows[0], ['---'] * len(rows[0]), *rows[1:]]])
    if notes['corrections']:
        title = '附：关键 ASR 订正'
        rows = [['原文', '订正', '说明'], *[[x['original'], x['corrected'], x['reason']] for x in notes['corrections']]]
        nodes.extend([text_node(title, 'heading2'), table_node(rows)])
        markdown.extend(['\n## ' + title, '| 原文 | 订正 | 说明 |', '| --- | --- | --- |',
                         *['| ' + ' | '.join(v.replace('|', '／') for v in row) + ' |' for row in rows[1:]]])
    return nodes, '\n\n'.join(markdown)


def note_identity(record):
    return hashlib.sha256((VERSION + record.record_revision_sha256 + json.dumps(chapter_outline(record.episode), sort_keys=True)).encode()).hexdigest()
