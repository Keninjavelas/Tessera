"""Unit tests for Phase III Multi-Tier LLM Orchestrator: 413 chunking, 429 backoff/jitter, Tier 1->2->3 failover, and schema repair."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from tessera.llm.orchestrator import LLMExtractionError, LLMOrchestrator
from tessera.llm.preprocessor import estimate_tokens, prepare_llm_context, strip_html_boilerplate
from tessera.llm.providers import (
    BaseLLMProvider,
    LLMPayloadTooLargeError,
    LLMRateLimitError,
)
from tessera.llm.rate_limiter import compute_full_jitter_backoff


class _SampleOutput(BaseModel):
    summary: str = Field(..., min_length=5)
    entities: list[str]


class _MockLLMProvider(BaseLLMProvider):
    """Deterministic fake provider scripted with responses or exceptions."""

    def __init__(
        self,
        name: str,
        tier: int,
        outcomes: list[str | Exception],
        api_key: str | None = "test-key",
    ) -> None:
        self.name = name
        self.tier = tier
        super().__init__(api_key=api_key, model=f"{name}-mock", rpm=600.0, tpm=1_000_000.0)
        self._outcomes = list(outcomes)
        self.calls: int = 0
        self.prompt_lengths: list[int] = []

    async def complete_json(
        self,
        client: httpx.AsyncClient,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
    ) -> str:
        self.calls += 1
        self.prompt_lengths.append(len(user_prompt))
        if not self._outcomes:
            raise RuntimeError("No scripted outcomes left")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_anti_413_html_stripping_and_semantic_budget() -> None:
    noisy_html = (
        "<html><head><title>AI Launch Pricing</title>"
        "<script>var huge = '" + ("x" * 20000) + "';</script>"
        "<style>.banner { color: red; }</style></head>"
        "<body><nav>Home About Careers</nav>"
        "<div class='cookie-banner'>Accept cookies</div>"
        "<main><h1>Frontier Model Launch</h1>"
        + "".join(f"<p>Filler paragraph number {i} discussing general background context.</p>" for i in range(40))
        + "<p>Enterprise pricing starts at $500 per month with a free tier for developers.</p>"
        + "</main><footer>Copyright 2026</footer></body></html>"
    )
    cleaned_html, meta = strip_html_boilerplate(noisy_html)
    assert "<script>" not in cleaned_html
    assert "cookie-banner" not in cleaned_html
    assert meta["title"] == "AI Launch Pricing"

    compressed = prepare_llm_context(
        noisy_html, max_tokens=220, focus_keywords=("pricing", "enterprise", "free")
    )
    assert estimate_tokens(compressed) <= 260
    assert "Enterprise pricing starts at $500" in compressed
    assert "Frontier Model Launch" in compressed


def test_full_jitter_backoff_and_retry_after() -> None:
    for attempt in range(4):
        val = compute_full_jitter_backoff(attempt, base_seconds=1.0, cap_seconds=10.0)
        assert 0.0 <= val <= min(10.0, 1.0 * (2**attempt))

    ra = compute_full_jitter_backoff(0, base_seconds=1.0, cap_seconds=10.0, retry_after=4.0)
    assert 4.0 <= ra <= 4.3


@pytest.mark.asyncio
async def test_tier1_429_exhaustion_fails_over_to_tier2_and_tier3() -> None:
    t1 = _MockLLMProvider(
        "gemini",
        1,
        [
            LLMRateLimitError("429", provider="gemini", retry_after=0.01),
            LLMRateLimitError("429", provider="gemini", retry_after=0.01),
        ],
    )
    t2 = _MockLLMProvider(
        "groq",
        2,
        [
            LLMRateLimitError("429", provider="groq", retry_after=0.01),
            LLMRateLimitError("429", provider="groq", retry_after=0.01),
        ],
    )
    t3 = _MockLLMProvider(
        "deepseek",
        3,
        [json.dumps({"summary": "DeepSeek succeeded after failovers.", "entities": ["OpenAI"]})],
    )

    orch = LLMOrchestrator(
        [t1, t2, t3],
        max_retries_per_tier=2,
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.02,
    )
    try:
        out, provider = await orch.extract_structured(
            raw_content="OpenAI announced a new model.",
            instruction="Extract summary and entities.",
            response_model=_SampleOutput,
            source_url="https://example.com/news/1",
        )
        assert provider == "deepseek"
        assert out.entities == ["OpenAI"]
        assert t1.circuit_breaker.is_open is True
        assert t2.circuit_breaker.is_open is True
        assert orch.stats.retries_429 == 4
        assert orch.stats.tier_failovers == 2
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_reactive_413_halves_budget_and_succeeds() -> None:
    long_doc = "\n\n".join(
        [f"Paragraph {i}: AI startups raised capital and shipped products." for i in range(200)]
    )
    t1 = _MockLLMProvider(
        "gemini",
        1,
        [
            LLMPayloadTooLargeError("413 too large", provider="gemini", status_code=413),
            json.dumps({"summary": "Chunked successfully.", "entities": ["Anthropic"]}),
        ],
    )
    orch = LLMOrchestrator(
        [t1], max_input_tokens=4000, max_retries_per_tier=3, base_backoff_seconds=0.01
    )
    try:
        out, provider = await orch.extract_structured(
            raw_content=long_doc,
            instruction="Summarize.",
            response_model=_SampleOutput,
        )
        assert provider == "gemini"
        assert out.summary == "Chunked successfully."
        assert orch.stats.retries_413_chunked == 1
        assert t1.calls == 2
        assert t1.prompt_lengths[1] < t1.prompt_lengths[0]
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_schema_repair_retry_fixes_malformed_json() -> None:
    t1 = _MockLLMProvider(
        "groq",
        2,
        [
            '{"summary": "Ok", "entities": "not-a-list"}',  # fails Pydantic validation (min_length & list)
            json.dumps({"summary": "Repaired valid summary.", "entities": ["Mistral AI"]}),
        ],
    )
    orch = LLMOrchestrator([t1], max_retries_per_tier=2, base_backoff_seconds=0.01)
    try:
        out, _ = await orch.extract_structured(
            raw_content="Mistral AI released a model.",
            instruction="Summarize.",
            response_model=_SampleOutput,
        )
        assert out.entities == ["Mistral AI"]
        assert orch.stats.schema_repairs == 1
        assert t1.calls == 2
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_unconfigured_tiers_skipped_and_dead_letter_on_exhaustion() -> None:
    missing = _MockLLMProvider("gemini", 1, [], api_key=None)
    failing = _MockLLMProvider(
        "groq",
        2,
        [LLMRateLimitError("429", provider="groq", retry_after=0.01)],
    )
    orch = LLMOrchestrator([missing, failing], max_retries_per_tier=1, base_backoff_seconds=0.01)
    try:
        assert len(orch.active_providers) == 1
        with pytest.raises(LLMExtractionError):
            await orch.extract_structured(
                raw_content="Test text",
                instruction="Summarize",
                response_model=_SampleOutput,
                source_url="https://example.com/fail",
            )
        assert len(orch.stats.dead_letters) == 1
        assert orch.stats.dead_letters[0]["source_url"] == "https://example.com/fail"
    finally:
        await orch.close()
