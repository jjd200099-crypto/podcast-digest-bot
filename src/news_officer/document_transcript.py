"""Lossless, opt-in full-text appendix from the already verified archive.

No model rewrite, new provider quota or implicit translation. Publication is
enabled only after the operator confirms rights for the configured audience.
"""

import hashlib
import json

from .shownotes import text_node

VERSION = 'authorized-archive-appendix-v1'


def fulltext_identity(record):
    transcript = record.transcript
    if not transcript.verified_complete or not transcript.text.strip():
        raise ValueError('全文附录必须使用已核验的完整文字稿')
    content = [VERSION, record.episode.id, transcript.text, transcript.language,
               transcript.source, transcript.source_url]
    return 'fulltext:' + hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()


def render_fulltext(record):
    fulltext_identity(record)  # Fail closed, including for imported/test records.
    transcript = record.transcript
    language = transcript.language or '未标明语言'
    title = f'完整文字稿（归档原文 · {language}）'
    intro = '以下为已核验的完整归档稿，保留原有发言顺序和时间标记，未另行翻译或删节。'
    nodes = [text_node(title, 'heading2'), text_node(intro)]
    # Literal text runs: do not interpret Markdown links, mention syntax or
    # instructions within a transcript. Preserve every character, including
    # whitespace, while keeping text runs below Feishu's UTF-16 size limit.
    text = transcript.text
    while text:
        end = min(1400, len(text))
        if end < len(text):
            boundary = text.rfind('\n', 0, end)
            if boundary >= end // 2:
                end = boundary + 1
        paragraph, text = text[:end], text[end:]
        elements = [{'text_run': {'content': paragraph[i:i + 700]}}
                    for i in range(0, len(paragraph), 700)]
        nodes.append({'block_type': 2, 'text': {'elements': elements}})
    return nodes
