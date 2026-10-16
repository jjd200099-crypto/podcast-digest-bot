"""Canonical feed identity, independent of transport and tracking parameters."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
