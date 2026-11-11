"""Canonical feed identity, independent of transport and tracking parameters."""

import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Episode

_TRACKING = {'fbclid', 'gclid', 'mc_cid', 'mc_eid', 'igshid', 'ref', 'ref_src',
             'si', '_hsenc', '_hsmi'}


def normalize_rss_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname:
            return ''
        host = parts.hostname.lower()
        if ':' in host:
            host = f'[{host}]'
        if parts.port is not None and parts.port not in {80, 443}:
            host += f':{parts.port}'
        query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.casefold().startswith('utm_') and k.casefold() not in _TRACKING)
        return urlunsplit(('https', host, parts.path.rstrip('/'), urlencode(query), ''))
    except ValueError:
        return ''


def matches_include_keywords(episode: Episode, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    title = str(episode.metadata.get("youtube_title") or episode.title)
    lead = re.split(r"[.?!]\s+", str(episode.metadata.get("description") or ""), maxsplit=1)[0]
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", text.casefold())
        is not None
        for keyword in keywords
        for text in (title, lead)
    )
