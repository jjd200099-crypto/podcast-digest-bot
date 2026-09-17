"""Explicit public-podcast corpus; never a silent fallback for private Drive."""

from .file_memory import PodcastFileMemory


class PodcastArchive:
    mode = "podcast_archive"
    folder = ""

    def __init__(self, store, root=None):
        self.store = store
        self.files = PodcastFileMemory(store, root or store.path.parent / "podcast-memory")
        self.archive_root = self.files.root

    def initialize(self):
        self.archive_pending()

    def archive_pending(self):
        return self.files.sync()

    def snapshot(self):
        return self.files.snapshot()
