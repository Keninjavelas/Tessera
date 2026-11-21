"""Async Playwright headless browser adapter with stealth hardening and resource blocking."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from tessera.fetchers.base import BaseFetcher, FetchResult
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.logging_config import get_logger

logger = get_logger(__name__)

_BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset({"image", "media", "font", "stylesheet"})

_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


class PlaywrightFetcher(BaseFetcher):
    """Async Chromium fetcher with webdriver stealth flags and 70%+ bandwidth resource blocking."""

    def __init__(
        self,
        *,
        max_pages: int = 3,
        timeout_seconds: float = 30.0,
        block_resources: bool = True,
        headless: bool = True,
    ) -> None:
        self._sem = asyncio.Semaphore(max_pages)
        self._timeout_ms = int(timeout_seconds * 1000)
        self._block_resources = block_resources
        self._headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._init_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        async with self._init_lock:
            if self._browser is not None:
                return self._browser
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            logger.info("playwright_browser_launched", headless=self._headless)
            return self._browser

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        """Render URL in headless Chromium with resource blocking and stealth headers."""
        target_url = url
        if params:
            from urllib.parse import urlencode

            sep = "&" if "?" in target_url else "?"
            target_url = f"{target_url}{sep}{urlencode(params)}"

        browser = await self._ensure_browser()
        timeout_ms = int(timeout * 1000) if timeout is not None else self._timeout_ms

        async with self._sem:
            start = time.perf_counter()
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                extra_http_headers=headers or {},
            )
            await context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = await context.new_page()

            if self._block_resources:
                async def _route_handler(route: Any) -> None:
                    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _route_handler)

            try:
                response = await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                # Brief settle window for client-side hydration
                await page.wait_for_timeout(800)
                html = await page.content()
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                status = response.status if response is not None else 200
                resp_headers = dict(response.headers) if response is not None else {}

                logger.info(
                    "playwright_fetch_complete",
                    url=target_url,
                    status=status,
                    bytes=len(html),
                    elapsed_ms=round(elapsed_ms, 1),
                )
                return FetchResult(
                    url=target_url,
                    final_url=page.url,
                    status_code=status,
                    text=html,
                    headers={k.lower(): v for k, v in resp_headers.items()},
                    fetched_via="browser",
                    elapsed_ms=round(elapsed_ms, 2),
                )
            finally:
                await context.close()

    async def close(self) -> None:
        """Close Chromium browser and Playwright driver."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class SmartFetcher(BaseFetcher):
    """Fast HTTP/2 fetcher with automatic Playwright fallback on JS shells or 403 challenges."""

    def __init__(
        self,
        http_fetcher: HttpFetcher,
        browser_fetcher: PlaywrightFetcher | None = None,
    ) -> None:
        self.http = http_fetcher
        self.browser = browser_fetcher
        self.browser_fallbacks: int = 0

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        """Attempt fast HTTP/2 fetch first; escalate to Playwright only when needed."""
        result = await self.http.fetch(url, headers=headers, params=params, timeout=timeout)
        if self.browser is not None and HttpFetcher.needs_browser_fallback(result):
            logger.info(
                "smart_fetcher_escalating_to_browser",
                url=url,
                http_status=result.status_code,
            )
            try:
                browser_res = await self.browser.fetch(
                    url, headers=headers, params=params, timeout=timeout
                )
                self.browser_fallbacks += 1
                return browser_res
            except Exception as exc:
                logger.warning("browser_fallback_failed", url=url, error=str(exc))
        return result

    async def close(self) -> None:
        await self.http.close()
        if self.browser is not None:
            await self.browser.close()
