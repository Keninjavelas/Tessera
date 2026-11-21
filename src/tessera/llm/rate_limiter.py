"""Async token-bucket rate limiter, full-jitter exponential backoff, and circuit breaker."""

from __future__ import annotations

import asyncio
import random
import time


def compute_full_jitter_backoff(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 30.0,
    retry_after: float | None = None,
) -> float:
    """Compute sleep duration honoring Retry-After or AWS-style full jitter backoff.

    Full jitter: `sleep = random.uniform(0, min(cap, base * 2**attempt))`.
    """
    if retry_after is not None and retry_after > 0:
        return min(retry_after, cap_seconds) + random.uniform(0.05, 0.25)
    upper = min(cap_seconds, base_seconds * (2**attempt))
    return random.uniform(0.0, upper)


class AsyncTokenBucket:
    """Async sliding token bucket enforcing both RPM and TPM budgets per provider."""

    def __init__(
        self,
        *,
        requests_per_minute: float = 30.0,
        tokens_per_minute: float = 120_000.0,
    ) -> None:
        self._rpm = max(1.0, requests_per_minute)
        self._tpm = max(1000.0, tokens_per_minute)
        self._req_tokens: float = self._rpm
        self._tok_tokens: float = self._tpm
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        if elapsed <= 0:
            return
        self._req_tokens = min(self._rpm, self._req_tokens + elapsed * (self._rpm / 60.0))
        self._tok_tokens = min(self._tpm, self._tok_tokens + elapsed * (self._tpm / 60.0))
        self._last_refill = now

    async def acquire(self, estimated_tokens: int = 500) -> float:
        """Wait until both 1 request permit and `estimated_tokens` are available. Returns wait seconds."""
        needed_tok = float(min(max(1, estimated_tokens), int(self._tpm)))
        total_waited = 0.0

        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._req_tokens >= 1.0 and self._tok_tokens >= needed_tok:
                    self._req_tokens -= 1.0
                    self._tok_tokens -= needed_tok
                    return total_waited

                req_wait = (
                    (1.0 - self._req_tokens) / (self._rpm / 60.0)
                    if self._req_tokens < 1.0
                    else 0.0
                )
                tok_wait = (
                    (needed_tok - self._tok_tokens) / (self._tpm / 60.0)
                    if self._tok_tokens < needed_tok
                    else 0.0
                )
                sleep_for = max(req_wait, tok_wait, 0.02)

            await asyncio.sleep(sleep_for)
            total_waited += sleep_for


class CircuitBreaker:
    """Cooldown circuit breaker preventing repeated hammering of rate-limited LLM tiers."""

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_until: float = 0.0

    @property
    def is_open(self) -> bool:
        """Return True if the circuit is currently tripped and in cooldown."""
        if self._opened_until <= 0.0:
            return False
        if time.monotonic() >= self._opened_until:
            # Cooldown expired -> transition to half-open
            self._opened_until = 0.0
            self._consecutive_failures = 0
            return False
        return True

    def record_success(self) -> None:
        """Reset failure counters upon a healthy response."""
        self._consecutive_failures = 0
        self._opened_until = 0.0

    def record_failure(self, *, force_open: bool = False) -> bool:
        """Record a failure; returns True if the circuit transitioned to OPEN."""
        self._consecutive_failures += 1
        if force_open or self._consecutive_failures >= self._failure_threshold:
            self._opened_until = time.monotonic() + self._cooldown_seconds
            return True
        return False

    def remaining_cooldown(self) -> float:
        """Seconds remaining until half-open state."""
        if self._opened_until <= 0.0:
            return 0.0
        return max(0.0, self._opened_until - time.monotonic())
