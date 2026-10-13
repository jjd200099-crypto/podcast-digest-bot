"""Bounded, TLS-verified public proxy rotation for YouTube captions."""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlsplit

import requests
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

DEFINITIVE_ERRORS = (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable)
DEFAULT_LISTS = (
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
)
_WORKING: list[str] = []
_LOCK = threading.Lock()
T = TypeVar("T")


def _positive(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value >= 1 else default
    except ValueError:
        return default


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool
    lists: tuple[str, ...]
    workers: int
    timeout: float
    candidates: int
    budget: float

    @classmethod
    def from_env(cls) -> ProxyConfig:
        return cls(
            os.getenv("YOUTUBE_FREE_PROXY_ENABLED", "true").lower()
            not in {"false", "0", "no", "off"},
            tuple(
                x.strip()
                for x in os.getenv(
                    "YOUTUBE_FREE_PROXY_LISTS", ",".join(DEFAULT_LISTS)
                ).split(",")
                if x.strip()
            ),
            int(_positive("YOUTUBE_FREE_PROXY_WORKERS", 32)),
            _positive("YOUTUBE_FREE_PROXY_REQUEST_TIMEOUT", 8),
            int(_positive("YOUTUBE_FREE_PROXY_MAX_CANDIDATES", 300)),
            _positive("YOUTUBE_FREE_PROXY_TOTAL_BUDGET", 120),
        )


class TimeoutSession(requests.Session):
    def __init__(self, timeout: float, deadline: float | None = None):
        super().__init__()
        self.timeout = timeout
        self.deadline = deadline

    def request(self, method, url, **kwargs):
        remaining = (
            self.timeout if self.deadline is None else self.deadline - time.monotonic()
        )
        if remaining <= 0:
            raise requests.Timeout("Transcript budget exhausted")
        kwargs["timeout"] = min(self.timeout, remaining)
        kwargs["verify"] = True
        if self.proxies:
            kwargs["proxies"] = dict(self.proxies)
        return super().request(method, url, **kwargs)


def _load_candidates(config: ProxyConfig, deadline: float) -> list[str]:
    candidates: list[str] = []
    with TimeoutSession(config.timeout, deadline) as session:
        for url in config.lists:
            if time.monotonic() >= deadline:
                break
            try:
                response = session.get(url)
                response.raise_for_status()
                scheme = "socks5" if "socks5" in url.lower() else "http"
                for line in response.text.splitlines():
                    proxy = line.strip()
                    if "://" not in proxy:
                        proxy = f"{scheme}://{proxy}"
                    parts = urlsplit(proxy)
                    if (
                        parts.scheme in {"http", "https", "socks5", "socks5h"}
                        and parts.hostname
                        and parts.port
                    ):
                        candidates.append(proxy)
            except (requests.RequestException, ValueError):
                logging.getLogger(__name__).debug("Public proxy list unavailable")
    candidates = list(dict.fromkeys(candidates))
    random.shuffle(candidates)
    return candidates


def _race(
    candidates: list[str],
    fetch: Callable[[str, float, float], T | None],
    config: ProxyConfig,
    deadline: float,
) -> T | None:
    executor = ThreadPoolExecutor(max_workers=config.workers)
    pending = {}
    remaining = iter(candidates)

    def submit_next():
        if time.monotonic() < deadline:
            proxy = next(remaining, None)
            if proxy is not None:
                pending[executor.submit(fetch, proxy, config.timeout, deadline)] = proxy

    try:
        for _ in range(min(config.workers, len(candidates))):
            submit_next()
        while pending and time.monotonic() < deadline:
            done, _ = wait(
                pending,
                timeout=max(0, deadline - time.monotonic()),
                return_when=FIRST_COMPLETED,
            )
            if time.monotonic() >= deadline:
                return None
            for future in done:
                proxy = pending.pop(future)
                try:
                    result = future.result()
                except DEFINITIVE_ERRORS:
                    raise
                except Exception:  # noqa: BLE001 - failed public proxies are expected
                    result = None
                if result is not None:
                    with _LOCK:
                        if proxy in _WORKING:
                            _WORKING.remove(proxy)
                        _WORKING.insert(0, proxy)
                        del _WORKING[32:]
                    return result
                submit_next()
        return None
    finally:
        for future in pending:
            future.cancel()
        # Running requests finish under their own timeout; never wait for losers.
        executor.shutdown(wait=False, cancel_futures=True)


_SPENT: list[tuple[float, float]] = []


def fetch_with_free_proxies(fetch: Callable[[str, float, float], T | None]) -> T | None:
    config = ProxyConfig.from_env()
    if not config.enabled:
        return None
    started = time.monotonic()
    window = _positive("YOUTUBE_FREE_PROXY_WINDOW_BUDGET", 600)
    with _LOCK:
        _SPENT[:] = [(t, d) for t, d in _SPENT if started - t < 3600]
        remaining = window - sum(d for _, d in _SPENT)
    if remaining <= 0:
        # Bound total free-proxy time per hour so many slow videos cannot stall
        # the daily run past its watchdog; later retries pick them up.
        return None
    deadline = started + min(config.budget, remaining)
    try:
        return _fetch_with_free_proxies(fetch, config, deadline)
    finally:
        with _LOCK:
            _SPENT.append((started, time.monotonic() - started))


def _fetch_with_free_proxies(fetch: Callable[[str, float, float], T | None], config: ProxyConfig,
                             deadline: float) -> T | None:
    with _LOCK:
        remembered = _WORKING[: config.candidates]
    result = _race(remembered, fetch, config, deadline)
    if result is not None:
        return result
    candidates = [p for p in _load_candidates(config, deadline) if p not in remembered]
    return _race(
        candidates[: max(0, config.candidates - len(remembered))],
        fetch,
        config,
        deadline,
    )
