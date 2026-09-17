"""Explicit public-podcast corpus; never a silent fallback for private Drive."""

from .library import LibraryDocument


class PodcastArchive:
    mode = "podcast_archive"
    folder = ""

    def __init__(self, store):
        self.store = store

    def initialize(self):
        pass

    def snapshot(self):
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT * FROM episode_transcripts ORDER BY stored_at DESC LIMIT 201"
            ).fetchall()
        warnings = []
        if len(rows) > 200:
            warnings.append(
                "本次检索覆盖最近入库的 200 份已核验播客全文，不代表全部历史"
            )
        records = [self.store._stored_transcript(row) for row in rows[:200]]
        return [
            LibraryDocument(
                r.reference,
                f"{r.episode.title} | {r.episode.show}",
                r.transcript.source_url,
                r.transcript.text,
            )
            for r in records
            if r.transcript.verified_complete
        ], warnings
