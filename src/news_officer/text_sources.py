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
                   'subscribe to continue', 'paid subscribers', 'unlock this article',
                   'subscribe to stratechery plus for full access')


def text_sources_enabled() -> bool:
    return os.environ.get('NEWS_OFFICER_TEXT_SOURCES', '').lower() in {'1', 'true', 'yes', 'on'}


def load_text_sources(path: Path) -> list[dict]:
    if not text_sources_enabled():
        return []
    return [s for s in json.loads(path.read_text()).get('sources', [])
            if s.get('type') == 'text' and s.get('enabled', True)]


def extract_article(page: str) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(page, 'html.parser')
    title = soup.select_one('meta[property="og:title"]')
    heading = (soup.select_one('.post-title') or soup.select_one('article h1')
               or soup.select_one('main h1') or soup.find('h1'))
    title_text = str(title.get('content', '')) if title else ''
    if not title_text:
        title = heading or soup.find('title')
        title_text = title.get_text('', strip=False).strip() if title else ''
    title_text = ' '.join(title_text.split())
    site = soup.select_one('meta[property="og:site_name"]')
    site_name = str(site.get('content', '')) if site else ''
    heading_text = ' '.join(heading.get_text().split()) if heading else ''
    for separator in (' | ', ' - ', ' — ', ' – '):
        if title_text == heading_text:
            break
        parts = title_text.split(separator)
        if len(parts) > 1:
            if site_name and parts[0].strip() == site_name:
                title_text = separator.join(parts[1:])
            elif heading and parts[-1].strip() == heading_text:
                title_text = parts[-1].strip()
            elif site_name and parts[-1].strip() == site_name:
                title_text = separator.join(parts[:-1])
            elif heading:
                if heading_text in parts:
                    title_text = heading_text

            elif not site_name and not soup.select_one('meta[property="og:title"]'):
                title_text = separator.join(parts[:-1])
    title_text = ' '.join(title_text.split())
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
    # Explicit body selectors outrank layout containers and recommendation cards.
    main = None
    for selector in ('[itemprop="articleBody"]', '.entry-content', '.post-content',
                     '.post-body', '.w-richtext', '[class*="post_post__"]',
                     'article', 'main'):
        candidates = soup.select(selector)
        if selector == '.w-richtext':
            articles = soup.select('article')
            if len(articles) == 1 and len(articles[0].get_text()) > 1000:
                candidates = articles
        if candidates:
            main = max(candidates, key=lambda node: len(node.get_text()))
            break
    if main is None:
        return title_text, '', published
    for node in main.select('script, style, nav, footer, header, aside, form, button, '
                            '.paywall-footer, .subscription-widget, .subscribe-widget, .post-footer'):
        node.decompose()
    # Preserve adjacent inline nodes ("B<span>etter</span>") and separate blocks.
    for node in main.select('p, div, section, h1, h2, h3, h4, li, blockquote, pre, br'):
        node.insert_before('\n')
        node.insert_after('\n')
    text = '\n'.join(' '.join(line.split()) for line in main.get_text().splitlines()
                     if line.strip())
    return title_text, text, published


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


def _discover_articles(source: dict) -> list[tuple[Episode, Transcript]]:
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
            resolved = urljoin(source['rss_url'], link.strip())
            parts = urlsplit(resolved)
            # Legacy feeds may advertise HTTP permalinks on their own HTTPS host.
            if parts.scheme == 'http' and parts.hostname == urlsplit(source['rss_url']).hostname:
                resolved = urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))
            url = _safe_https_url(resolved)
            if url:
                entries.append((url, title, _parse_date(date or '')))
    else:
        entries = [(url, '', None) for url in article_links(_fetch(source['index_url']), source)]
    limit = max(1, int(source.get('max_articles', 10)))
    entries.sort(key=lambda entry: entry[2].timestamp() if entry[2] else 0, reverse=True)
    results = []
    seen = set()
    fetch_error = None
    for url, feed_title, feed_date in entries:
        if url in seen:
            continue
        seen.add(url)
        try:
            page_html = _fetch(url)
            title, text, date = extract_article(page_html)
            if not text or not (feed_title or title):
                continue
            page = BeautifulSoup(page_html, 'html.parser')
            paywall = page.select_one('.paywall, .paywall-content, .paywall-overlay, '
                                      '[data-paywall], .locked-content')
            if source.get('free_only') and (len(text) < 1000 or paywall or any(
                    line.casefold().startswith(marker)
                    for line in text.splitlines() for marker in PAYWALL_MARKERS)):
                continue
            identity = 'text:' + hashlib.sha256(url.encode()).hexdigest()[:32]
            episode = Episode(identity, ' '.join((feed_title or title).split()), url, source['name'],
                              published_at=feed_date or date,
                              metadata={'source_name': source['name'], 'source_type': 'text', 'source_priority': source.get('priority', 'B')})
            results.append((episode, Transcript(text, 'publisher article', url, True)))
            if len(results) >= limit:
                break
        except Exception as error:  # noqa: BLE001 - isolate individual articles
            fetch_error = error
            logger.warning('Article fetch failed for %s: %s', url, error)
    if not results and fetch_error is not None:
        raise fetch_error
    return results


def discover_articles(source: dict, *, raise_on_error: bool = False) -> list[tuple[Episode, Transcript]]:
    """Isolate feed/index failures so other publishers can still be discovered.

    ``max_articles`` caps successful articles per source (default 10).
    ``raise_on_error`` lets health checks distinguish outages from empty scans.
    """
    try:
        return _discover_articles(source)
    except Exception as error:
        if raise_on_error:
            raise
        logger.warning('Article discovery failed for %s: %s', source.get('name'), error)
        return []
