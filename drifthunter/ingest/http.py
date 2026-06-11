"""Shared EDGAR-polite HTTP: rate cap, backoff, User-Agent, disk cache."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from drifthunter.config import EdgarConfig


class EdgarClient:
    """Construct once per run and reuse; each instance owns an httpx connection pool."""

    def __init__(self, cfg: EdgarConfig, cache_dir: Path):
        self._min_interval = 1.0 / cfg.max_requests_per_sec
        self._last_request = 0.0
        self._cache_dir = cache_dir
        self._client = httpx.Client(
            headers={"User-Agent": cfg.user_agent},
            timeout=30.0,
            follow_redirects=True,
        )

    def get_bytes(self, url: str, cache_key: str) -> bytes:
        """Fetch with throttle + retry; cache to disk keyed by cache_key.

        Cache is permanent: delete the cache file to force a re-fetch.
        """
        cached = self._cache_dir / cache_key
        if cached.exists():
            return cached.read_bytes()
        for attempt in range(5):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self._client.get(url)
            except httpx.TransportError:
                time.sleep(2 ** attempt)
                self._last_request = time.monotonic()
                continue
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                self._last_request = time.monotonic()
                continue
            resp.raise_for_status()
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(resp.content)
            return resp.content
        raise RuntimeError(f"EDGAR fetch failed after retries: {url}")
