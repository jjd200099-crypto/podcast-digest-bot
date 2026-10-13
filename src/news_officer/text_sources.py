"""Opt-in public articles, treated as full-text transcript records."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .models import Episode, Transcript
from .rss import MAX_FEED_BYTES, _local_name, _parse_date, _response, _safe_https_url

logger = logging.getLogger(__name__)
PAYWALL_MARKERS = ('stratechery plus', 'subscribe to read', 'this is a paid',
                   'subscribe to continue', 'paid subscribers', 'unlock this article')


def text_sources_enabled() -> bool:
    return os.environ.get('NEWS_OFFICER_TEXT_SOURCES', '').lower() in {'1', 'true', 'yes', 'on'}


def load_text_sources(path: Path) -> list[dict]:
    if not text_sources_enabled():
        return []
    return [s for s in json.loads(path.read_text()).get('sources', [])
            if s.get('type') == 'text' and s.get('enabled', True)]


def extract_article(page: str) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(page, 'html.parser')
    title = soup.find('h1') or soup.find('title')
    title_text = title.get_text(' ', strip=True) if title else ''
    date = soup.select_one('meta[property="article:published_time"], meta[name="date"], time[datetime]')
    published = _parse_date(str(date.get('content') or date.get('datetime') or '')) if date else None
    if published is None:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if isinstance(node, dict):
                    nodes.extend(node.get('@graph', []))
                    published = _parse_date(str(node.get('datePublished') or '')) or published
    main = soup.select_one('article, [itemprop="articleBody"], .entry-content, .post-content, .w-richtext, main')
    if main is None:
        return title_text, '', published
    for node in main.select('script, style, nav, footer, header, aside, form, button'):
        node.decompose()
    return title_text, main.get_text('\n', strip=True), published


def _fetch(url: str) -> str:
    return _response(url, timeout=20, max_bytes=MAX_FEED_BYTES).text


def article_links(page: str, source: dict) -> list[str]:
    base = source['index_url']
    pattern = re.compile(source['link_regex'])
    excluded = re.compile(source.get('exclude_regex', r'(?!)'))
    links = []
    for anchor in BeautifulSoup(page, 'html.parser').find_all('a', href=True):
        url = urljoin(base, anchor['href'])
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        if (parts.hostname != urlsplit(base).hostname or not _safe_https_url(url)
                or not (pattern.search(parts.path) or pattern.search(url))
                or excluded.search(parts.path)):
            continue
        if url not in links:
            links.append(url)
    return links


def discover_articles(source: dict) -> list[tuple[Episode, Transcript]]:
    """Fetch publisher pages, never summarize feed excerpts as full articles."""
    entries = []
    if source['mode'] == 'rss':
        root = ElementTree.fromstring(_fetch(source['rss_url']))
        for item in root.iter():
            if _local_name(item.tag) not in {'item', 'entry'}:
                continue
            fields = {_local_name(c.tag): c for c in item}
            title = ''.join(fields['title'].itertext()).strip() if 'title' in fields else ''
            link = next((c.get('href') or c.text or '' for c in item
                         if _local_name(c.tag) == 'link' and c.get('rel', 'alternate') == 'alternate'), '')
            date = next((fields[k].text for k in ('pubDate', 'published', 'updated') if k in fields), '')
            url = _safe_https_url(urljoin(source['rss_url'], link.strip()))
            if url:
                entries.append((url, title, _parse_date(date or '')))
    else:
        entries = [(url, '', None) for url in article_links(_fetch(source['index_url']), source)]
    results = []
    seen = set()
    for url, feed_title, feed_date in entries:
        if url in seen:
            continue
        seen.add(url)
        try:
            title, text, date = extract_article(_fetch(url))
            if not text or not (feed_title or title):
                continue
            if source.get('free_only') and (len(text) < 1000 or any(
                    marker in text.casefold() for marker in PAYWALL_MARKERS)):
                continue
            identity = 'text:' + hashlib.sha256(url.encode()).hexdigest()[:32]
            episode = Episode(identity, ' '.join((feed_title or title).split()), url, source['name'],
                              published_at=feed_date or date,
                              metadata={'source_type': 'text', 'source_priority': source.get('priority', 'B')})
            results.append((episode, Transcript(text, 'publisher article', url, True)))
        except Exception as error:  # noqa: BLE001 - isolate individual articles
            logger.warning('Article fetch failed for %s: %s', url, error)
    return results
