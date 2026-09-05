from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup, Tag

from .models import Episode, Transcript

COLOSSUS_HOSTS = {
    "colossus.com",
    "www.colossus.com",
    "joincolossus.com",
    "www.joincolossus.com",
}
EPISODE_PATH_RE = re.compile(r"^/episode/[^/]+/?$")
EPISODE_SITEMAP_PATH_RE = re.compile(
    r"^/podcast_episode-sitemap\d*\.xml$", re.IGNORECASE
)
URL_RE = re.compile(r"https?://[^\s<>\"]+")
TIMESTAMP_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:[\N{RIGHT SINGLE QUOTATION MARK}'-][A-Za-z0-9]+)*"
)
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCKED_SELECTORS = (
    ".content-gate-obscure",
    "section.content-gate",
    "[data-testid='paywall']",
    "[class*='paywall']",
)
BLOCKED_MARKERS = (
    "access the full transcript",
    "log in or register to view episode transcripts",
    "login or register to view episode transcripts",
    "sign in to read the rest",
    "subscribe to continue reading",
    "transcript coming soon",
    "transcript to come",
    "[insert transcript",
    "[transcript pending",
    "transcript unavailable",
)


def _safe_official_url(
    raw_url: str,
    *,
    episode_only: bool = False,
    sitemap_only: bool = False,
) -> str | None:
    """Canonicalize only HTTPS URLs on Colossus-owned web origins."""

    cleaned = html.unescape(raw_url).strip().rstrip(".,，。;；:：)]}）】>'\"")
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme.lower() != "https"
        or host not in COLOSSUS_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
    ):
        return None
    path = parts.path or "/"
    if episode_only and EPISODE_PATH_RE.fullmatch(path) is None:
        return None
    if sitemap_only and not (
        path.casefold() == "/sitemap_index.xml"
        or EPISODE_SITEMAP_PATH_RE.fullmatch(path)
    ):
        return None
    # The legacy www host is currently blocked by Cloudflare, while the apex
    # host performs the documented migration redirect to colossus.com.
    if host == "www.joincolossus.com":
        host = "joincolossus.com"
    return urlunsplit(("https", host, path, "", ""))


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", unquote(value)).casefold()
    words = re.findall(r"[a-z0-9]+", normalized)
    noise = {
        "episode",
        "ep",
        "invest",
        "like",
        "the",
        "best",
        "with",
        "patrick",
        "oshaughnessy",
        "o",
        "shaughnessy",
        "colossus",
        "podcast",
    }
    return " ".join(word for word in words if word not in noise and not word.isdigit())


def _duration_seconds(episode: Episode) -> float | None:
    if episode.duration_seconds is not None:
        try:
            value = float(episode.duration_seconds)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    raw = str(episode.duration_string or "").strip()
    if not raw:
        return None
    try:
        parts = [float(part) for part in raw.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        minutes, seconds = parts
        value = minutes * 60 + seconds
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        value = hours * 3600 + minutes * 60 + seconds
    else:
        return None
    return value if value > 0 else None


def _parse_iso_duration(value: str) -> float | None:
    match = ISO_DURATION_RE.fullmatch(value.strip())
    if match is None:
        return None
    seconds = (
        int(match.group("days") or 0) * 86_400
        + int(match.group("hours") or 0) * 3_600
        + int(match.group("minutes") or 0) * 60
        + float(match.group("seconds") or 0)
    )
    return seconds if seconds > 0 else None


def _timestamp_seconds(value: str) -> int | None:
    match = TIMESTAMP_RE.search(value)
    if match is None:
        return None
    hours = int(match.group(1) or 0)
    return hours * 3_600 + int(match.group(2)) * 60 + int(match.group(3))


class ColossusOfficialTranscriptProvider:
    """Read strictly verified Invest Like the Best transcripts from Colossus."""

    name = "Invest Like the Best official transcript"
    sitemap_index_url = "https://colossus.com/sitemap_index.xml"
    minimum_words_per_minute = 70.0
    maximum_words_per_minute = 230.0

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._sitemap_cache: tuple[float, list[str]] | None = None

    def supports_url(self, url: str) -> bool:
        return _safe_official_url(url, episode_only=True) is not None

    def _get(self, url: str) -> requests.Response:
        current = _safe_official_url(url)
        if current is None:
            raise ValueError("Only HTTPS URLs on official Colossus domains are allowed")
        for _ in range(5):
            response = requests.get(
                current,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; NewsOfficerBot/0.2; "
                    "+transcript-verification)"
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                redirected = _safe_official_url(urljoin(current, location or ""))
                if location is None or redirected is None:
                    raise ValueError(
                        "Colossus page redirected outside the allowed official origins"
                    )
                current = redirected
                continue
            response.raise_for_status()
            final_url = getattr(response, "url", "")
            if final_url and _safe_official_url(str(final_url)) is None:
                raise ValueError("Response URL is outside the allowed official origins")
            if len(response.content) > 12_000_000:
                raise ValueError("Official transcript page is unexpectedly large")
            return response
        raise ValueError("Too many redirects while reading the Colossus page")

    @staticmethod
    def _title_score(episode_title: str, url: str) -> float:
        target = _normalized_title(episode_title)
        slug = _normalized_title(
            urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        )
        if not target or not slug:
            return 0.0
        target_words = set(target.split())
        slug_words = set(slug.split())
        overlap = len(target_words & slug_words) / max(
            1, min(len(target_words), len(slug_words))
        )
        return SequenceMatcher(None, target, slug).ratio() * 0.65 + overlap * 0.35

    def _sitemap_episode_urls(self) -> list[str]:
        now = time.monotonic()
        if self._sitemap_cache and now - self._sitemap_cache[0] < 600:
            return self._sitemap_cache[1]

        index = ElementTree.fromstring(self._get(self.sitemap_index_url).content)
        sitemap_urls: list[str] = []
        for element in index.iter():
            if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                continue
            safe_url = _safe_official_url(element.text, sitemap_only=True)
            if safe_url and EPISODE_SITEMAP_PATH_RE.fullmatch(urlsplit(safe_url).path):
                sitemap_urls.append(safe_url)
        if not sitemap_urls or len(sitemap_urls) > 10:
            raise ValueError("Official Colossus episode sitemaps are missing or invalid")

        episode_urls: list[str] = []
        seen: set[str] = set()
        for sitemap_url in sitemap_urls:
            root = ElementTree.fromstring(self._get(sitemap_url).content)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                    continue
                safe_url = _safe_official_url(element.text, episode_only=True)
                if safe_url and safe_url not in seen:
                    seen.add(safe_url)
                    episode_urls.append(safe_url)
                    if len(episode_urls) > 10_000:
                        raise ValueError("Official Colossus sitemap is unexpectedly large")
        self._sitemap_cache = (now, episode_urls)
        return episode_urls

    def _description_url(self, episode: Episode) -> str | None:
        description = str(episode.metadata.get("description") or "")
        candidates: list[tuple[float, str]] = []
        for raw_url in URL_RE.findall(description):
            safe_url = _safe_official_url(raw_url, episode_only=True)
            if safe_url:
                candidates.append((self._title_score(episode.title, safe_url), safe_url))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        best_score, best_url = candidates[0]
        if best_score < 0.45:
            return None
        if (
            len(candidates) > 1
            and best_score < 0.90
            and best_score - candidates[1][0] < 0.04
        ):
            return None
        return best_url

    def _official_url(self, episode: Episode) -> str | None:
        direct = _safe_official_url(episode.url, episode_only=True)
        if direct:
            return direct
        from_description = self._description_url(episode)
        if from_description:
            return from_description
        identity = f"{episode.show} {episode.title}".casefold()
        if "invest like the best" not in identity:
            return None
        matches = sorted(
            (
                (self._title_score(episode.title, url), url)
                for url in self._sitemap_episode_urls()
            ),
            reverse=True,
        )
        if not matches or matches[0][0] < 0.64:
            return None
        if (
            len(matches) > 1
            and matches[0][0] < 0.92
            and matches[0][0] - matches[1][0] < 0.04
        ):
            return None
        return matches[0][1]

    @staticmethod
    def _is_invest_like_the_best(soup: BeautifulSoup) -> bool:
        show = soup.select_one(".single-podcast-episode-header__podcast-name")
        return bool(
            show
            and " ".join(show.get_text(" ", strip=True).casefold().split())
            == "invest like the best"
        )

    @staticmethod
    def _page_duration(soup: BeautifulSoup) -> float | None:
        for selector in (
            "meta[property='og:audio:duration']",
            "meta[property='og:video:duration']",
        ):
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                try:
                    duration = float(str(tag["content"]))
                except ValueError:
                    continue
                if duration > 0:
                    return duration
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                raw = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            records = raw.get("@graph", []) if isinstance(raw, dict) else raw
            if not isinstance(records, list):
                records = [records]
            for record in records:
                if not isinstance(record, dict) or not record.get("duration"):
                    continue
                duration = _parse_iso_duration(str(record["duration"]))
                if duration:
                    return duration
        return None

    @staticmethod
    def _published_at(soup: BeautifulSoup) -> datetime | None:
        tag = soup.select_one("meta[property='article:published_time']")
        if tag and tag.get("content"):
            try:
                return datetime.fromisoformat(
                    str(tag["content"]).strip().replace("Z", "+00:00")
                )
            except ValueError:
                pass
        date_tag = soup.select_one(".single-podcast-episode-header__date")
        if date_tag:
            try:
                return datetime.strptime(
                    date_tag.get_text(" ", strip=True), "%m.%d.%Y"
                ).replace(tzinfo=UTC)
            except ValueError:
                pass
        return None

    @classmethod
    def _metadata_from_page(cls, url: str, soup: BeautifulSoup) -> Episode | None:
        if not cls._is_invest_like_the_best(soup):
            return None
        heading = soup.select_one(".single-podcast-episode-header__title")
        if heading is None:
            return None
        title = heading.get_text(" ", strip=True)
        if not title:
            return None
        description_tag = soup.select_one(
            ".single-podcast-episode-header__description"
        )
        host_tag = soup.select_one(".single-podcast-episode-header__host--name")
        number_tag = soup.select_one(
            ".single-podcast-episode-header__podcast-episode-number"
        )
        number_match = re.search(
            r"\bEpisode\s+(\d+)\b",
            number_tag.get_text(" ", strip=True) if number_tag else "",
            re.IGNORECASE,
        )
        slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        return Episode(
            id=f"colossus:{slug}",
            title=title,
            url=url,
            show="Invest Like the Best",
            duration_seconds=cls._page_duration(soup),
            published_at=cls._published_at(soup),
            metadata={
                "description": description_tag.get_text(" ", strip=True)
                if description_tag
                else "",
                "host": host_tag.get_text(" ", strip=True) if host_tag else "",
                "episode_number": int(number_match.group(1)) if number_match else None,
                "official_url": url,
            },
        )

    def episode_from_url(self, url: str) -> Episode | None:
        safe_url = _safe_official_url(url, episode_only=True)
        if safe_url is None:
            return None
        response = self._get(safe_url)
        resolved_url = _safe_official_url(str(response.url), episode_only=True)
        if resolved_url is None:
            raise ValueError("Colossus did not resolve to an official episode URL")
        soup = BeautifulSoup(response.content, "html.parser")
        return self._metadata_from_page(resolved_url, soup)

    @staticmethod
    def _chapter_is_populated(heading: Tag) -> bool:
        characters = 0
        for sibling in heading.find_next_siblings():
            if not isinstance(sibling, Tag):
                continue
            if sibling.name == "h2":
                break
            if sibling.name not in {"p", "blockquote", "ul", "ol"}:
                continue
            characters += len(sibling.get_text(" ", strip=True))
        return characters >= 80

    @classmethod
    def _extract_transcript(
        cls, soup: BeautifulSoup
    ) -> tuple[str, set[str], int] | None:
        article = soup.select_one("article.transcript")
        if article is None:
            return None
        banner = article.select_one(":scope > header .banner__title")
        if banner is None or banner.get_text(" ", strip=True).casefold() != "transcript":
            return None
        if any(soup.select_one(selector) is not None for selector in BLOCKED_SELECTORS):
            return None
        container = article.select_one(".transcript__content")
        if container is None:
            return None
        article_text = article.get_text(" ", strip=True).casefold()
        if any(marker in article_text for marker in BLOCKED_MARKERS):
            return None

        headings = container.find_all("h2", recursive=False)
        if len(headings) < 2 or any(
            not cls._chapter_is_populated(heading) for heading in headings
        ):
            return None
        found_ids: set[str] = set()
        for heading in headings:
            if heading.get("id"):
                found_ids.add(str(heading["id"]))
            anchor = heading.find(id=True)
            if anchor and anchor.get("id"):
                found_ids.add(str(anchor["id"]))
        expected_ids = {
            str(link["href"])[1:]
            for link in article.select(".contents__links a[href^='#']")
            if len(str(link.get("href") or "")) > 1
        }
        if expected_ids and not expected_ids.issubset(found_ids):
            return None

        speakers = {
            " ".join(tag.get_text(" ", strip=True).casefold().split())
            for tag in container.select("p .transcript__speaker")
            if tag.get_text(" ", strip=True)
        }
        host_paragraphs = container.select("p[data-transcript-host]")
        guest_paragraphs = container.select("p[data-transcript-guest]")
        labelled_turns = container.select(
            "p[data-transcript-speaker-changed] .transcript__speaker"
        )
        if (
            len(speakers) < 2
            or not host_paragraphs
            or not guest_paragraphs
            or len(labelled_turns) < 4
        ):
            return None

        blocks: list[str] = []
        for block in container.find_all(
            ["h2", "h3", "p", "blockquote", "li"], recursive=True
        ):
            if block.name == "li" and block.find_parent("li") is not None:
                continue
            text = block.get_text(" ", strip=True)
            if text:
                blocks.append(text)
        transcript = "\n".join(blocks).strip()
        if not transcript or any(
            marker in transcript.casefold() for marker in BLOCKED_MARKERS
        ):
            return None
        return transcript, speakers, len(headings)

    @staticmethod
    def _show_note_timestamps(soup: BeautifulSoup) -> list[int]:
        show_notes = soup.select_one(".show-notes-container")
        if show_notes is None:
            return []
        timestamps = {
            timestamp
            for text in show_notes.stripped_strings
            if (timestamp := _timestamp_seconds(str(text))) is not None
        }
        return sorted(timestamps)

    def fetch(self, episode: Episode) -> Transcript | None:
        official_url = self._official_url(episode)
        if official_url is None:
            return None
        response = self._get(official_url)
        resolved_url = _safe_official_url(str(response.url), episode_only=True)
        if resolved_url is None:
            raise ValueError("Colossus did not resolve to an official episode URL")
        soup = BeautifulSoup(response.content, "html.parser")
        if not self._is_invest_like_the_best(soup):
            return None
        extracted = self._extract_transcript(soup)
        if extracted is None:
            return None
        text, _speakers, _chapters = extracted

        duration = _duration_seconds(episode) or self._page_duration(soup)
        if duration is None:
            return None
        word_count = len(WORD_RE.findall(text))
        if word_count < 1_000:
            return None
        words_per_minute = word_count / (duration / 60.0)
        if not (
            self.minimum_words_per_minute
            <= words_per_minute
            <= self.maximum_words_per_minute
        ):
            return None

        timestamps = self._show_note_timestamps(soup)
        if len(timestamps) >= 2:
            if timestamps[0] > min(360, duration * 0.12):
                return None
            if duration - timestamps[-1] > max(1_200, duration * 0.25):
                return None

        return Transcript(
            text=text,
            source=self.name,
            source_url=resolved_url,
            verified_complete=True,
        )
