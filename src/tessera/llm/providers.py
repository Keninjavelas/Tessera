"""Modular async LLM provider clients for Gemini Flash (Tier 1), Groq (Tier 2), and DeepSeek (Tier 3)."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from tessera.llm.rate_limiter import AsyncTokenBucket, CircuitBreaker


class LLMProviderError(Exception):
    """Base exception for LLM provider failures."""

    def __init__(self, message: str, *, provider: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class LLMRateLimitError(LLMProviderError):
    """Raised on HTTP 429 Too Many Requests."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retry_after: float | None = None,
        status_code: int = 429,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code)
        self.retry_after = retry_after


class LLMPayloadTooLargeError(LLMProviderError):
    """Raised on HTTP 413 Payload Too Large or context length exceeded."""


class LLMTransientError(LLMProviderError):
    """Raised on transient 5xx / timeout / connection failures."""


_BODY_RETRY_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|s)\b", re.IGNORECASE)


def _parse_retry_after(headers: httpx.Headers, body: str = "") -> float | None:
    raw = headers.get("retry-after")
    if raw:
        try:
            return float(raw.strip())
        except ValueError:
            pass
    m = _BODY_RETRY_RE.search(body)
    if m:
        val = float(m.group(1))
        return val / 1000.0 if m.group(2).lower() == "ms" else val
    return None


def _raise_for_status(provider: str, resp: httpx.Response) -> None:
    code = resp.status_code
    if 200 <= code < 300:
        return
    body = resp.text[:400]
    if code == 429:
        raise LLMRateLimitError(
            f"{provider} rate limited (429): {body}",
            provider=provider,
            retry_after=_parse_retry_after(resp.headers, body),
            status_code=429,
        )
    if code == 413 or (
        code == 400
        and any(
            marker in body.lower()
            for marker in ("context_length_exceeded", "too many tokens", "maximum context", "payload too large")
        )
    ):
        raise LLMPayloadTooLargeError(
            f"{provider} payload/context too large ({code}): {body}",
            provider=provider,
            status_code=code,
        )
    if code in (500, 502, 503, 504):
        raise LLMTransientError(
            f"{provider} transient server error ({code}): {body}",
            provider=provider,
            status_code=code,
        )
    raise LLMProviderError(
        f"{provider} request failed ({code}): {body}",
        provider=provider,
        status_code=code,
    )


class BaseLLMProvider(ABC):
    """Abstract async LLM provider with dedicated token bucket and circuit breaker."""

    name: str
    tier: int
    model: str

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        rpm: float,
        tpm: float,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.api_key = api_key.strip() if api_key else None
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.rate_limiter = AsyncTokenBucket(requests_per_minute=rpm, tokens_per_minute=tpm)
        self.circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=45.0)

    @property
    def is_configured(self) -> bool:
        """Return True if an API key is configured for this provider."""
        return bool(self.api_key)

    @abstractmethod
    async def complete_json(
        self,
        client: httpx.AsyncClient,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
    ) -> str:
        """Invoke the LLM in strict JSON output mode and return the raw JSON string."""


class GeminiProvider(BaseLLMProvider):
    """Tier 1: Google Gemini Flash REST client."""

    name = "gemini"
    tier = 1

    def __init__(self, api_key: str | None, model: str = "gemini-2.5-flash") -> None:
        super().__init__(api_key=api_key, model=model, rpm=15.0, tpm=200_000.0)

    async def complete_json(
        self,
        client: httpx.AsyncClient,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
    ) -> str:
        if not self.api_key:
            raise LLMProviderError("GEMINI_API_KEY not configured", provider=self.name)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                f"{user_prompt}\n\n"
                                f"Return ONLY valid JSON conforming to this schema:\n"
                                f"{json.dumps(json_schema)}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }
        try:
            resp = await client.post(
                url,
                params={"key": self.api_key},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise LLMTransientError(f"gemini network error: {exc}", provider=self.name) from exc

        _raise_for_status(self.name, resp)
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise LLMProviderError("gemini returned no candidates", provider=self.name)
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(p.get("text", "")) for p in parts).strip()
        if not text:
            raise LLMProviderError("gemini returned empty text", provider=self.name)
        return text


class _OpenAICompatibleProvider(BaseLLMProvider):
    """Shared base for OpenAI-compatible chat completion APIs (Groq, DeepSeek)."""

    base_url: str

    async def complete_json(
        self,
        client: httpx.AsyncClient,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
    ) -> str:
        if not self.api_key:
            raise LLMProviderError(f"{self.name} API key not configured", provider=self.name)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        f"You MUST reply with a single valid JSON object matching this JSON Schema:\n"
                        f"{json.dumps(json_schema)}"
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            resp = await client.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise LLMTransientError(
                f"{self.name} network error: {exc}", provider=self.name
            ) from exc

        _raise_for_status(self.name, resp)
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMProviderError(f"{self.name} returned no choices", provider=self.name)
        content = str(choices[0].get("message", {}).get("content", "")).strip()
        if not content:
            raise LLMProviderError(f"{self.name} returned empty content", provider=self.name)
        return content


class GroqProvider(_OpenAICompatibleProvider):
    """Tier 2: Groq ultra-low-latency inference client (paced safely within 8K free-tier TPM)."""

    name = "groq"
    tier = 2
    base_url = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str | None, model: str = "openai/gpt-oss-20b") -> None:
        super().__init__(api_key=api_key, model=model, rpm=18.0, tpm=5_200.0)


class DeepSeekProvider(_OpenAICompatibleProvider):
    """Tier 3: DeepSeek chat client."""

    name = "deepseek"
    tier = 3
    base_url = "https://api.deepseek.com/chat/completions"

    def __init__(self, api_key: str | None, model: str = "deepseek-chat") -> None:
        super().__init__(api_key=api_key, model=model, rpm=30.0, tpm=120_000.0)
