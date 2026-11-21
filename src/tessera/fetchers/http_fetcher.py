"""Async httpx client with HTTP/2, per-domain semaphores, header rotation, and jittered retries."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import httpx

from tessera.fetchers.base import BaseFetcher, FetchResult
from tessera.logging_config import get_logger

logger = get_logger(__name__)

_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
)

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment...",
    "cf-browser-verification",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "__cf_chl_opt",
    "datadome",
)


class HttpFetcher(BaseFetcher):
    """High-throughput async HTTP/2 fetcher with bounded domain concurrency and polite retries."""

    def __init__(
        self,
        *,
        max_concurrency: int = 20,
        per_domain_concurrency: int = 4,
        timeout_seconds: float = 25.0,
        max_retries: int = 3,
        base_backoff_seconds: float = 0.75,
        max_backoff_seconds: float = 20.0,
        min_domain_delay_seconds: float = 0.05,
    ) -> None:
        self._global_sem = asyncio.Semaphore(max_concurrency)
        self._per_domain_concurrency = per_domain_concurrency
        self._domain_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_domain_concurrency)
        )
        self._domain_last_req: dict[str, float] = {}
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._min_domain_delay = min_domain_delay_seconds

        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(
                max_connections=max_concurrency * 2,
                max_keepalive_connections=max_concurrency,
            ),
        )

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        }
        if extra:
            headers.update(extra)
        return headers

    def _compute_sleep(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                val = float(retry_after.strip())
                if 0 < val <= 60.0:
                    return val + random.uniform(0.05, 0.35)
            except ValueError:
                pass
        cap = min(self._max_backoff, self._base_backoff * (2**attempt))
        return random.uniform(0.1, cap)

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        """Fetch URL with global & per-domain semaphores and full-jitter exponential backoff."""
        domain = (urlparse(url).netloc or "unknown").lower()
        domain_sem = self._domain_sems[domain]
        req_headers = self._build_headers(headers)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            async with self._global_sem, domain_sem:
                # Polite per-domain spacing
                now = time.monotonic()
                elapsed_since_last = now - self._domain_last_req.get(domain, 0.0)
                if elapsed_since_last < self._min_domain_delay:
                    await asyncio.sleep(self._min_domain_delay - elapsed_since_last)
                self._domain_last_req[domain] = time.monotonic()

                start = time.perf_counter()
                try:
                    resp = await self._client.get(
                        url,
                        headers=req_headers,
                        params=params,
                        timeout=timeout or self._timeout,
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000.0
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}

                    if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                        sleep_s = self._compute_sleep(attempt, resp_headers.get("retry-after"))
                        logger.warning(
                            "http_retryable_status",
                            url=url,
                            status=resp.status_code,
                            attempt=attempt + 1,
                            sleep_seconds=round(sleep_s, 2),
                        )
                        await asyncio.sleep(sleep_s)
                        continue

                    return FetchResult(
                        url=url,
                        final_url=str(resp.url),
                        status_code=resp.status_code,
                        text=resp.text,
                        headers=resp_headers,
                        fetched_via="http",
                        elapsed_ms=round(elapsed_ms, 2),
                    )
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    last_error = exc
                    if attempt < self._max_retries:
                        sleep_s = self._compute_sleep(attempt, None)
                        logger.warning(
                            "http_transport_retry",
                            url=url,
                            error=str(exc),
                            attempt=attempt + 1,
                            sleep_seconds=round(sleep_s, 2),
                        )
                        await asyncio.sleep(sleep_s)
                        continue

        raise httpx.RequestError(
            f"HttpFetcher exhausted {self._max_retries + 1} attempts for {url}: {last_error}"
        )

    async def fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        """Fetch and decode JSON response along with status code and headers."""
        merged_headers = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        result = await self.fetch(url, headers=merged_headers, params=params, timeout=timeout)
        if not result.ok:
            return result.status_code, None, result.headers
        return result.status_code, json.loads(result.text), result.headers

    async def head_ok(self, url: str, timeout: float = 12.0) -> tuple[bool, int]:
        """Verify live URL reachability using HEAD with GET fallback."""
        domain = (urlparse(url).netloc or "unknown").lower()
        async with self._global_sem, self._domain_sems[domain]:
            hdrs = self._build_headers()
            try:
                resp = await self._client.head(url, headers=hdrs, timeout=timeout)
                if 200 <= resp.status_code < 400:
                    return True, resp.status_code
                # Some servers reject HEAD with 403/405; verify with lightweight GET
                if resp.status_code in (403, 405, 400, 501):
                    resp_get = await self._client.get(
                        url, headers={**hdrs, "Range": "bytes=0-2048"}, timeout=timeout
                    )
                    return 200 <= resp_get.status_code < 400, resp_get.status_code
                return False, resp.status_code
            except httpx.HTTPError:
                return False, 0

    @staticmethod
    def needs_browser_fallback(result: FetchResult) -> bool:
        """Detect Cloudflare/bot blocks or unrendered SPA shells requiring headless Chromium."""
        if result.status_code in (403, 503):
            return True
        lower = result.text[:6000].lower()
        if any(marker in lower for marker in _CHALLENGE_MARKERS):
            return True
        # Detect empty SPA shell (<div id="root"></div> or __next with almost no content)
        if (
            len(result.text) < 2500
            and ("<div id=\"root\">" in lower or "<div id=\"app\">" in lower)
            and "<p>" not in lower
        ):
            return True
        return False

    async def close(self) -> None:
        """Close the underlying httpx AsyncClient."""
        await self._client.aclose()
