"""Shared request pacing and durable 429 cooldowns; never store credentials."""

import hashlib
import math
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime


class PodwiseAPIError(RuntimeError):
    """Credential-free diagnostic; never include request/response bodies."""


class PodwiseRateLimited(PodwiseAPIError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__('Podwise rate limited; deferred until cooldown expires')


class RequestGuard:
    def __init__(self, key, *, store=None, interval=1.0, clock=time.time, sleep=time.sleep):
        self.key, self.store, self.interval = key, store, interval
        self.clock, self.sleep = clock, sleep
        self.lock = threading.RLock()
        self.next_request = self.retry_at = 0.0
        self.failures = 0

    def _refresh(self):
        if self.store is not None:
            state = self.store.provider_cooldown(self.key)
            if state:
                self.retry_at = max(self.retry_at, state['retry_at'])
                self.failures = max(self.failures, state['failures'])

    @contextmanager
    def request(self):
        # One in-flight request per credential, shared by discovery and answers.
        with self.lock:
            self._refresh()
            if self.clock() < self.retry_at:
                raise PodwiseRateLimited(self.retry_at)
            wait = max(0, self.next_request - self.clock())
            if wait:
                self.sleep(wait)
            try:
                yield
            finally:
                self.next_request = self.clock() + self.interval

    def limited(self, retry_after=None):
        delay = 300 * 2 ** min(self.failures, 4)
        if isinstance(retry_after, str):
            try:
                requested = float(retry_after)
            except ValueError:
                try:
                    requested = parsedate_to_datetime(retry_after).timestamp() - self.clock()
                except (ValueError, TypeError, OverflowError):
                    requested = 0
            if math.isfinite(requested):
                delay = max(delay, min(86400, requested))
        self.failures += 1
        self.retry_at = self.clock() + delay
        if self.store is not None:
            self.store.save_provider_cooldown(self.key, self.retry_at, self.failures)
        raise PodwiseRateLimited(self.retry_at)

    def succeeded(self):
        if self.failures:
            self.retry_at = self.failures = 0
            if self.store is not None:
                self.store.save_provider_cooldown(self.key, 0, 0)


_guards = {}
_registry_lock = threading.Lock()


def podwise_guard(token, store=None):
    key = 'podwise:' + hashlib.sha256(token.encode()).hexdigest()
    with _registry_lock:
        guard = _guards.setdefault(key, RequestGuard(key))
        if store is not None:
            guard.store = store
        return guard
