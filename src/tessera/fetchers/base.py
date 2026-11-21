"""BaseFetcher interface and FetchResult data structure."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class FetchResult:
    """Normalized response returned by any Tessera fetcher implementation."""

    url: str
    final_url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    fetched_via: Literal["http", "browser"] = "http"
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Return True if HTTP status code indicates success (2xx)."""
        return 200 <= self.status_code < 300


class BaseFetcher(ABC):
    """Abstract asynchronous web fetcher interface."""

    @abstractmethod
    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        """Fetch a remote URL asynchronously and return a normalized FetchResult."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying connections or browser resources."""
