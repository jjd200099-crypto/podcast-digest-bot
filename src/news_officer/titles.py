"""Defensive cleanup for deep-read document and link titles."""

import re

_NEW_PREFIX = re.compile(r"^\s*(?:(?:\[新\]|【新】|\[NEW\])\s*)+", re.IGNORECASE)


def normalize_deepread_title(title: str) -> str:
    """Strip leading new-item badges, preserving unbadged titles verbatim."""
    return _NEW_PREFIX.sub("", title)
