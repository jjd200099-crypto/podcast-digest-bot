from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup, Tag

from .models import Episode, Transcript

DAVID_SENRA_HOSTS = {"davidsenra.com", "www.davidsenra.com"}
EPISODE_PATH_RE = re.compile(r"^/episode/[A-Za-z0-9][A-Za-z0-9_-]*/?$")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:[\N{RIGHT SINGLE QUOTATION MARK}'-][A-Za-z0-9]+)*"
)
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
BRACKET_SPEAKER_RE = re.compile(r"^\s*\[([^\]\n]{1,80})\]\s*")
COLON_SPEAKER_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .\N{RIGHT SINGLE QUOTATION MARK}'-]{0,79}):(?:\s|$)"
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCKED_SELECTORS = (
    "[data-testid='paywall']",
    "[data-transcript-locked='true']",
    ".content-gate-obscure",
    "[class*='paywall']",
)
BLOCKED_MARKERS = (
    "access the full transcript",
    "subscribe to continue reading",
    "sign in to read the rest",
    "log in to read the rest",
    "transcript coming soon",
    "transcript to come",
    "transcript unavailable",
    "[insert transcript",
    "[transcript pending",
)


def _safe_official_url(
    raw_url: str,
    *,
    episode_only: bool = False,
    sitemap_only: bool = False,
) -> str | None:
    """Canonicalize an HTTPS URL only when it remains on David Senra's site."""

    cleaned = html.unescape(raw_url).strip().rstrip(".,，。;；:：)]}）】>'\"")
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme.casefold() != "https"
        or host not in DAVID_SENRA_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
    ):
        return None
    path = parts.path or "/"
    if episode_only and EPISODE_PATH_RE.fullmatch(path) is None:
        return None
    if sitemap_only and path.rstrip("/").casefold() != "/sitemap.xml":
        return None
    if path != "/":
        path = path.rstrip("/")
    netloc = host if port is None else f"{host}:443"
    return urlunsplit(("https", netloc, path, "", ""))


def _youtube_video_id(raw_url: str) -> str | None:
    cleaned = html.unescape(raw_url).strip().rstrip(".,，。;；)]}）】>'\"")
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return None
    if parts.scheme.casefold() != "https":
        return None
    host = (parts.hostname or "").casefold()
    candidate = ""
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parts.path.strip("/").split("/", 1)[0]
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }:
        if parts.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            match = re.fullmatch(
                r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{6,20})/?",
                parts.path,
            )
            if match:
                candidate = match.group(1)
    return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", unquote(value)).casefold()
    words = re.findall(r"[a-z0-9]+", normalized)
    noise = {
        "david",
        "senra",
        "founders",
        "podcast",
        "episode",
        "with",
        "interview",
    }
    return " ".join(word for word in words if word not in noise)


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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
        value = hours * 3_600 + minutes * 60 + seconds
    else:
        return None
    return value if value > 0 else None


def _json_ld_objects(value: object):
    if isinstance(value, list):
        for item in value:
            yield from _json_ld_objects(item)
        return
    if not isinstance(value, dict):
        return
    yield value
    graph = value.get("@graph")
    if isinstance(graph, (dict, list)):
        yield from _json_ld_objects(graph)


def _is_podcast_episode(record: dict[str, object]) -> bool:
    record_type = record.get("@type")
    if isinstance(record_type, str):
        return record_type.casefold() == "podcastepisode"
    if isinstance(record_type, list):
        return any(
            isinstance(item, str) and item.casefold() == "podcastepisode"
            for item in record_type
        )
    return False


def _podcast_records(soup: BeautifulSoup):
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for record in _json_ld_objects(data):
            if _is_podcast_episode(record):
                yield record


def _same_episode_url(first: str, second: str) -> bool:
    first_url = _safe_official_url(first, episode_only=True)
    second_url = _safe_official_url(second, episode_only=True)
    if first_url is None or second_url is None:
        return False
    return urlsplit(first_url).path == urlsplit(second_url).path


def _record_for_page(url: str, soup: BeautifulSoup) -> dict[str, object] | None:
    for record in _podcast_records(soup):
        record_url = record.get("url")
        if isinstance(record_url, str) and _same_episode_url(url, record_url):
            return record
    return None


def _associated_youtube_ids(record: dict[str, object]) -> set[str]:
    ids: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in {"contentUrl", "embedUrl", "url"} and isinstance(item, str):
                video_id = _youtube_video_id(item)
                if video_id:
                    ids.add(video_id)
            elif isinstance(item, (dict, list)):
                visit(item)

    visit(record.get("associatedMedia"))
    return ids


class DavidSenraOfficialTranscriptProvider:
    """Read complete, public transcripts from David Senra's official website."""

    name = "David Senra official transcript"
    sitemap_url = "https://www.davidsenra.com/sitemap.xml"
    minimum_words_per_minute = 70.0
    maximum_words_per_minute = 260.0

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._sitemap_cache: tuple[float, list[str]] | None = None
        self._youtube_url_cache: dict[str, str] = {}

    def supports_url(self, url: str) -> bool:
        return _safe_official_url(url, episode_only=True) is not None

    def _get(self, url: str) -> requests.Response:
        current = _safe_official_url(url)
        if current is None:
            raise ValueError("Only HTTPS URLs on davidsenra.com are allowed")
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
                        "David Senra page redirected outside the official origin"
                    )
                current = redirected
                continue
            response.raise_for_status()
            final_url = getattr(response, "url", "")
            if final_url and _safe_official_url(str(final_url)) is None:
                raise ValueError("Response URL is outside the official origin")
            if len(response.content) > 12_000_000:
                raise ValueError("Official transcript page is unexpectedly large")
            return response
        raise ValueError("Too many redirects while reading the David Senra page")

    @staticmethod
    def _resolved_episode_url(requested_url: str, response: requests.Response) -> str:
        final_url = str(getattr(response, "url", "") or requested_url)
        resolved = _safe_official_url(final_url, episode_only=True)
        if resolved is None:
            raise ValueError("David Senra did not resolve to an official episode URL")
        return resolved

    def _page(self, url: str) -> tuple[str, BeautifulSoup]:
        response = self._get(url)
        resolved = self._resolved_episode_url(url, response)
        return resolved, BeautifulSoup(response.content, "html.parser")

    def _sitemap_episode_urls(self) -> list[str]:
        now = time.monotonic()
        if self._sitemap_cache and now - self._sitemap_cache[0] < 600:
            return self._sitemap_cache[1]
        response = self._get(self.sitemap_url)
        root = ElementTree.fromstring(response.content)
        urls: list[str] = []
        seen: set[str] = set()
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                continue
            safe_url = _safe_official_url(element.text, episode_only=True)
            if safe_url and safe_url not in seen:
                seen.add(safe_url)
                urls.append(safe_url)
                if len(urls) > 2_000:
                    raise ValueError(
                        "Official David Senra sitemap is unexpectedly large"
                    )
        if not urls:
            raise ValueError("Official David Senra sitemap has no episode URLs")
        self._sitemap_cache = (now, urls)
        return urls

    @staticmethod
    def _title_score(title: str, url: str) -> float:
        target = _normalized_title(title)
        slug = _normalized_title(
            urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        )
        if not target or not slug:
            return 0.0
        target_words = set(target.split())
        slug_words = set(slug.split())
        overlap = len(target_words & slug_words) / max(1, len(slug_words))
        return SequenceMatcher(None, target, slug).ratio() * 0.6 + overlap * 0.4

    @staticmethod
    def _episode_youtube_id(episode: Episode) -> str | None:
        video_id = _youtube_video_id(episode.url)
        if video_id:
            return video_id
        for key in ("youtube_url", "webpage_url"):
            video_id = _youtube_video_id(str(episode.metadata.get(key) or ""))
            if video_id:
                return video_id
        return None

    @staticmethod
    def _is_david_senra_episode(episode: Episode) -> bool:
        identity = " ".join(
            (
                episode.show,
                str(episode.metadata.get("author_name") or ""),
                str(episode.metadata.get("author_url") or ""),
            )
        ).casefold()
        # Fan and aggregation channels also use "Founders" in their name.
        # Automatic sitemap matching is reserved for David's verified identity;
        # other callers must supply a URL on his official site.
        return "david senra" in identity

    @staticmethod
    def _description_candidates(episode: Episode) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        official_url = _safe_official_url(
            str(episode.metadata.get("official_url") or ""), episode_only=True
        )
        if official_url:
            seen.add(official_url)
            candidates.append(official_url)
        description = str(episode.metadata.get("description") or "")
        for raw_url in URL_RE.findall(description):
            safe_url = _safe_official_url(raw_url, episode_only=True)
            if safe_url and safe_url not in seen:
                seen.add(safe_url)
                candidates.append(safe_url)
        return candidates

    @staticmethod
    def _page_matches_video(
        url: str, soup: BeautifulSoup, expected_video_id: str
    ) -> bool:
        record = _record_for_page(url, soup)
        return bool(record and expected_video_id in _associated_youtube_ids(record))

    def _official_page(self, episode: Episode) -> tuple[str, BeautifulSoup] | None:
        direct = _safe_official_url(episode.url, episode_only=True)
        expected_video_id = self._episode_youtube_id(episode)
        candidates = ([direct] if direct else []) + self._description_candidates(
            episode
        )
        tried: set[str] = set()
        for candidate in candidates:
            if candidate is None or candidate in tried:
                continue
            tried.add(candidate)
            resolved, soup = self._page(candidate)
            record = _record_for_page(resolved, soup)
            if record is None:
                continue
            if (
                expected_video_id is None
                or expected_video_id in _associated_youtube_ids(record)
            ):
                if expected_video_id:
                    self._youtube_url_cache[expected_video_id] = resolved
                return resolved, soup

        if expected_video_id is None or not self._is_david_senra_episode(episode):
            return None

        cached_url = self._youtube_url_cache.get(expected_video_id)
        if cached_url and cached_url not in tried:
            tried.add(cached_url)
            resolved, soup = self._page(cached_url)
            if self._page_matches_video(resolved, soup, expected_video_id):
                return resolved, soup
            self._youtube_url_cache.pop(expected_video_id, None)

        sitemap_urls = sorted(
            self._sitemap_episode_urls(),
            key=lambda url: (self._title_score(episode.title, url), url),
            reverse=True,
        )
        # Bound network work for one user request. A weak title match is safer to
        # skip than to crawl the whole site or accidentally accept another episode.
        for candidate in sitemap_urls[:12]:
            if candidate in tried:
                continue
            resolved, soup = self._page(candidate)
            record = _record_for_page(resolved, soup)
            if record is None:
                continue
            for video_id in _associated_youtube_ids(record):
                self._youtube_url_cache[video_id] = resolved
            if expected_video_id in _associated_youtube_ids(record):
                return resolved, soup
        return None

    @staticmethod
    def _metadata_from_page(url: str, soup: BeautifulSoup) -> Episode | None:
        record = _record_for_page(url, soup)
        if record is None:
            return None
        title = str(record.get("name") or record.get("headline") or "").strip()
        if not title:
            return None
        published_at = None
        if record.get("datePublished"):
            published_at = _parse_datetime(str(record["datePublished"]))
        duration = None
        if record.get("duration"):
            duration = _parse_iso_duration(str(record["duration"]))
        description = str(record.get("description") or "").strip()
        video_ids = sorted(_associated_youtube_ids(record))

        actor = record.get("actor")
        actors = actor if isinstance(actor, list) else [actor]
        guests = [
            str(item.get("name") or "").strip()
            for item in actors
            if isinstance(item, dict) and item.get("name")
        ]
        slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        return Episode(
            id=f"david-senra:{slug}",
            title=title,
            url=url,
            show="David Senra",
            duration_seconds=duration,
            published_at=published_at,
            metadata={
                "description": description,
                "official_url": url,
                "youtube_url": (
                    f"https://www.youtube.com/watch?v={video_ids[0]}"
                    if video_ids
                    else ""
                ),
                "guest": ", ".join(guests),
            },
        )

    def episode_from_url(self, url: str) -> Episode | None:
        safe_url = _safe_official_url(url, episode_only=True)
        if safe_url is None:
            return None
        resolved, soup = self._page(safe_url)
        return self._metadata_from_page(resolved, soup)

    @staticmethod
    def _speaker_label(text: str) -> str | None:
        match = BRACKET_SPEAKER_RE.match(text) or COLON_SPEAKER_RE.match(text)
        if match is None:
            return None
        name = " ".join(match.group(1).casefold().split())
        if (
            not name
            or len(name.split()) > 10
            or re.search(r"[.!?]", name)
            or not re.search(r"[a-z]", name)
        ):
            return None
        return name

    @classmethod
    def _extract_transcript(
        cls, soup: BeautifulSoup
    ) -> tuple[str, set[str], int, list[int]] | None:
        containers = [
            tag
            for tag in soup.select("[transcript-wrapper='true'].pc-detail_transcript")
            if isinstance(tag, Tag)
        ]
        if len(containers) != 1:
            return None
        container = containers[0]
        section = container.find_parent(
            "div", class_=lambda value: value and "pc-detail_content" in value
        )
        if not isinstance(section, Tag):
            return None
        heading = next(
            (
                tag
                for tag in section.find_all(["h1", "h2", "h3"], recursive=True)
                if " ".join(tag.get_text(" ", strip=True).casefold().split())
                in {"episode transcript", "transcript"}
            ),
            None,
        )
        if heading is None or any(
            section.select_one(selector) is not None for selector in BLOCKED_SELECTORS
        ):
            return None

        rich_text = container.select_one(":scope > .w-richtext") or container
        blocks: list[str] = []
        speakers: set[str] = set()
        labelled_positions: list[int] = []
        for block in rich_text.find_all(["p", "blockquote", "li"], recursive=True):
            if block.name == "li" and block.find_parent("li") is not None:
                continue
            text = block.get_text(" ", strip=True)
            if not text:
                continue
            position = len(blocks)
            blocks.append(text)
            speaker = cls._speaker_label(text)
            if speaker:
                speakers.add(speaker)
                labelled_positions.append(position)
        transcript = "\n".join(blocks).strip()
        if not transcript or any(
            marker in transcript.casefold() for marker in BLOCKED_MARKERS
        ):
            return None
        return transcript, speakers, len(blocks), labelled_positions

    def fetch(self, episode: Episode) -> Transcript | None:
        page = self._official_page(episode)
        if page is None:
            return None
        official_url, soup = page
        if self._metadata_from_page(official_url, soup) is None:
            return None
        extracted = self._extract_transcript(soup)
        if extracted is None:
            return None
        text, speakers, block_count, labelled_positions = extracted
        if (
            block_count < 20
            or len(speakers) < 2
            or "david senra" not in speakers
            or len(labelled_positions) < max(8, block_count // 20)
            or labelled_positions[0] > max(2, round(block_count * 0.05))
            or labelled_positions[-1] < round(block_count * 0.80)
        ):
            return None

        word_count = len(WORD_RE.findall(text))
        duration = _duration_seconds(episode)
        if duration is None:
            page_episode = self._metadata_from_page(official_url, soup)
            duration = _duration_seconds(page_episode) if page_episode else None
        if duration is None:
            if word_count < 3_000 or len(text) < 15_000:
                return None
        else:
            if word_count < 1_000:
                return None
            words_per_minute = word_count / (duration / 60.0)
            if not (
                self.minimum_words_per_minute
                <= words_per_minute
                <= self.maximum_words_per_minute
            ):
                return None

        return Transcript(
            text=text,
            source=self.name,
            source_url=official_url,
            verified_complete=True,
        )


# "Founders" is the name many listeners use for David Senra's work. Keep an
# explicit alias so the provider is discoverable under either product name.
FoundersOfficialTranscriptProvider = DavidSenraOfficialTranscriptProvider
