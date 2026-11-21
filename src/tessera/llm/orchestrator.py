"""Multi-tier LLM extraction orchestrator with 413 chunking, 429 backoff/failover, and JSON repair."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from tessera.config import Settings
from tessera.llm.preprocessor import estimate_tokens, prepare_llm_context
from tessera.llm.providers import (
    BaseLLMProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    LLMPayloadTooLargeError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTransientError,
)
from tessera.llm.rate_limiter import compute_full_jitter_backoff
from tessera.logging_config import get_logger

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMExtractionError(RuntimeError):
    """Raised when all configured LLM tiers fail or are exhausted."""


@dataclass
class OrchestratorStats:
    """Telemetry counters for multi-tier LLM execution and failover audits."""

    total_requests: int = 0
    total_successes: int = 0
    provider_successes: dict[str, int] = field(default_factory=dict)
    provider_failures: dict[str, int] = field(default_factory=dict)
    retries_429: int = 0
    retries_413_chunked: int = 0
    schema_repairs: int = 0
    tier_failovers: int = 0
    dead_letters: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "provider_successes": dict(self.provider_successes),
            "provider_failures": dict(self.provider_failures),
            "retries_429": self.retries_429,
            "retries_413_chunked": self.retries_413_chunked,
            "schema_repairs": self.schema_repairs,
            "tier_failovers": self.tier_failovers,
            "dead_letter_count": len(self.dead_letters),
        }


def _strip_json_fences(raw: str) -> str:
    """Strip accidental markdown code fences surrounding JSON output."""
    cleaned = _CODE_FENCE_RE.sub("", raw.strip()).strip()
    # Extract outermost JSON object if extra prose leaked before/after braces
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last != -1 and last > first:
        return cleaned[first : last + 1]
    return cleaned


class LLMOrchestrator:
    """Resilient multi-tier LLM orchestrator (Tier 1 Gemini -> Tier 2 Groq -> Tier 3 DeepSeek).

    Features:
        - Pre-flight anti-413 HTML boilerplate stripping & semantic token budgeting
        - Reactive 413 budget halving retry
        - Per-provider async token bucket (RPM/TPM)
        - 429/503 Retry-After + full-jitter exponential backoff + circuit breaker
        - Automatic Tier 1 -> Tier 2 -> Tier 3 failover
        - Strict Pydantic v2 validation + automatic 1-shot schema repair retry
        - Dead-letter tracking (never fabricates data on total exhaustion)
    """

    def __init__(
        self,
        providers: list[BaseLLMProvider],
        *,
        max_input_tokens: int = 6000,
        max_retries_per_tier: int = 3,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._all_providers = providers
        self.active_providers: list[BaseLLMProvider] = []
        for p in providers:
            if p.is_configured:
                self.active_providers.append(p)
            else:
                logger.warning(
                    "llm_tier_skipped_missing_key",
                    provider=p.name,
                    tier=p.tier,
                    model=p.model,
                )
        self.max_input_tokens = max_input_tokens
        self.max_retries_per_tier = max_retries_per_tier
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(http2=True, timeout=60.0)
        self.stats = OrchestratorStats()

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMOrchestrator:
        """Instantiate default 3-tier fallback chain from application settings."""
        providers: list[BaseLLMProvider] = [
            GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model),
            GroqProvider(api_key=settings.groq_api_key, model=settings.groq_model),
            DeepSeekProvider(api_key=settings.deepseek_api_key, model=settings.deepseek_model),
        ]
        return cls(
            providers=providers,
            max_input_tokens=settings.llm_max_input_tokens,
            max_retries_per_tier=settings.llm_max_retries,
        )

    @property
    def has_configured_provider(self) -> bool:
        """Return True if at least one LLM tier is configured."""
        return len(self.active_providers) > 0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def extract_structured(
        self,
        *,
        raw_content: str,
        instruction: str,
        response_model: type[ModelT],
        focus_keywords: tuple[str, ...] = (),
        source_url: str = "",
    ) -> tuple[ModelT, str]:
        """Extract strictly validated Pydantic model from raw HTML/text across configured tiers.

        Returns `(validated_model, provider_name)`.
        Raises `LLMExtractionError` if all tiers fail (recording item in dead-letter queue).
        """
        self.stats.total_requests += 1
        if not self.active_providers:
            err = "No LLM providers configured (set GEMINI_API_KEY, GROQ_API_KEY, or DEEPSEEK_API_KEY)"
            self.stats.dead_letters.append({"source_url": source_url, "error": err})
            raise LLMExtractionError(err)

        json_schema = response_model.model_json_schema()
        system_prompt = (
            "You are a precision data extraction engine for an AI ecosystem intelligence graph. "
            "Extract ONLY facts explicitly grounded in the provided text. Never fabricate or guess "
            "unsupported facts. Output strictly valid JSON matching the schema."
        )

        budget = self.max_input_tokens
        prepared_text = prepare_llm_context(
            raw_content, max_tokens=budget, focus_keywords=focus_keywords
        )

        last_errors: list[str] = []
        attempted_any = False

        for tier_idx, provider in enumerate(self.active_providers):
            if provider.circuit_breaker.is_open:
                logger.warning(
                    "llm_tier_circuit_open_skip",
                    provider=provider.name,
                    cooldown_remaining=round(provider.circuit_breaker.remaining_cooldown(), 1),
                )
                continue

            if attempted_any:
                self.stats.tier_failovers += 1
                logger.info(
                    "llm_tier_failover",
                    to_provider=provider.name,
                    tier=provider.tier,
                    source_url=source_url,
                )
            attempted_any = True

            current_budget = budget
            current_context = prepared_text

            for attempt in range(self.max_retries_per_tier):
                user_prompt = f"{instruction}\n\n---\nSOURCE TEXT:\n{current_context}"
                est_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_prompt) + 350

                try:
                    await provider.rate_limiter.acquire(est_tokens)
                    raw_json = await provider.complete_json(
                        self._client,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        json_schema=json_schema,
                    )
                    validated = await self._validate_with_repair(
                        provider=provider,
                        raw_json=raw_json,
                        response_model=response_model,
                        json_schema=json_schema,
                        system_prompt=system_prompt,
                    )
                    provider.circuit_breaker.record_success()
                    self.stats.total_successes += 1
                    self.stats.provider_successes[provider.name] = (
                        self.stats.provider_successes.get(provider.name, 0) + 1
                    )
                    return validated, provider.name

                except LLMPayloadTooLargeError as exc:
                    self.stats.retries_413_chunked += 1
                    current_budget = max(500, current_budget // 2)
                    current_context = prepare_llm_context(
                        raw_content, max_tokens=current_budget, focus_keywords=focus_keywords
                    )
                    logger.warning(
                        "llm_413_budget_halved",
                        provider=provider.name,
                        new_budget_tokens=current_budget,
                        attempt=attempt + 1,
                        error=str(exc),
                    )
                    last_errors.append(f"{provider.name}:413:{exc}")
                    continue

                except (LLMRateLimitError, LLMTransientError) as exc:
                    self.stats.retries_429 += 1
                    retry_after = exc.retry_after if isinstance(exc, LLMRateLimitError) else None
                    is_last_attempt = attempt >= self.max_retries_per_tier - 1
                    if not is_last_attempt:
                        sleep_s = compute_full_jitter_backoff(
                            attempt,
                            base_seconds=self.base_backoff_seconds,
                            cap_seconds=self.max_backoff_seconds,
                            retry_after=retry_after,
                        )
                        logger.warning(
                            "llm_rate_limit_backoff",
                            provider=provider.name,
                            status=exc.status_code,
                            attempt=attempt + 1,
                            sleep_seconds=round(sleep_s, 2),
                        )
                        await asyncio.sleep(sleep_s)
                    else:
                        provider.circuit_breaker.record_failure(force_open=True)
                        self.stats.provider_failures[provider.name] = (
                            self.stats.provider_failures.get(provider.name, 0) + 1
                        )
                        logger.error(
                            "llm_tier_exhausted_circuit_tripped",
                            provider=provider.name,
                            tier_idx=tier_idx,
                            error=str(exc),
                        )
                        last_errors.append(f"{provider.name}:{exc}")
                        break

                except (LLMProviderError, ValidationError, json.JSONDecodeError) as exc:
                    provider.circuit_breaker.record_failure()
                    self.stats.provider_failures[provider.name] = (
                        self.stats.provider_failures.get(provider.name, 0) + 1
                    )
                    logger.warning(
                        "llm_provider_or_schema_error",
                        provider=provider.name,
                        attempt=attempt + 1,
                        error=str(exc)[:250],
                    )
                    last_errors.append(f"{provider.name}:{exc}")
                    break

        error_summary = "; ".join(last_errors) or "All configured tiers have open circuit breakers"
        self.stats.dead_letters.append({"source_url": source_url, "error": error_summary})
        raise LLMExtractionError(f"All LLM tiers exhausted: {error_summary}")

    async def _validate_with_repair(
        self,
        *,
        provider: BaseLLMProvider,
        raw_json: str,
        response_model: type[ModelT],
        json_schema: dict[str, Any],
        system_prompt: str,
    ) -> ModelT:
        """Validate JSON against Pydantic schema with automatic 1-shot LLM repair on failure."""
        cleaned = _strip_json_fences(raw_json)
        try:
            return response_model.model_validate_json(cleaned)
        except ValidationError as first_err:
            self.stats.schema_repairs += 1
            logger.warning(
                "llm_schema_validation_failed_repairing",
                provider=provider.name,
                error=str(first_err)[:250],
            )
            repair_prompt = (
                f"Your previous JSON failed strict schema validation.\n"
                f"Validation Error:\n{first_err}\n\n"
                f"Previous Output:\n{cleaned[:2000]}\n\n"
                f"Fix the JSON so it strictly satisfies the schema without inventing new facts."
            )
            await provider.rate_limiter.acquire(estimate_tokens(repair_prompt) + 300)
            repaired_raw = await provider.complete_json(
                self._client,
                system_prompt=system_prompt,
                user_prompt=repair_prompt,
                json_schema=json_schema,
            )
            return response_model.model_validate_json(_strip_json_fences(repaired_raw))
