"""New-format deep-read writer, rendering helpers and stable cache identity."""
import hashlib
import json
import logging
import re

from .library import inline_elements

logger = logging.getLogger(__name__)


def _identity_chapters(episode):
    """Legacy chapter metadata hash input, retained only for deployed cache identity.

    This is never passed to the model or used to render chapter outlines.
    """
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
        from .deepread import generate
        try:
            return generate(self.client, self.model, record)
        except Exception:
            logger.warning('New-style deepread failed for episode %s; no old-style fallback, job will retry',
                           record.episode.id)
            raise


def text_node(text, kind='text'):
    number = {'text': 2, 'heading1': 3, 'heading2': 4, 'heading3': 5,
              'heading4': 6, 'bullet': 12, 'quote': 15}[kind]
    return {'block_type': number, kind: {'elements': inline_elements(text)}}


def render_episode(record, notes, digest=''):
    from .deepread import render
    if "chapters" not in notes:
        raise ValueError("New-style deepread required; refusing old-style notes")
    return render(record, notes, digest)


def note_identity(record):
    from .deepread import VERSION
    # Preserve the deployed new-format identity, including publisher chapter metadata.
    return hashlib.sha256((VERSION + record.record_revision_sha256 + json.dumps(_identity_chapters(record.episode), sort_keys=True)).encode()).hexdigest()
