"""Unit tests for strict Pydantic v2 entity schemas, UTC timestamps, and null handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tessera.schemas.entities import (
    EntityMappingLog,
    JobContent,
    JobRecord,
    MatchMethod,
    NewsContent,
    NewsRecord,
    PaperContent,
    PricingModel,
    ProductContent,
    ProductRecord,
    ResearchPaperRecord,
    SourceInfo,
    StartupContent,
    StartupData,
    StartupRecord,
)


def test_startup_record_valid_and_null_employee_count() -> None:
    rec = StartupRecord(
        source=SourceInfo(name="YC", url="https://www.ycombinator.com/companies/openai"),  # type: ignore[arg-type]
        content=StartupContent(entityName="OpenAI", data=StartupData(employeeCount=None)),
    )
    dumped = rec.model_dump(mode="json")
    assert dumped["schemaVersion"] == "1.0"
    assert dumped["recordType"] == "STARTUP"
    assert dumped["content"]["data"]["employeeCount"] is None
    assert dumped["collectedAt"].endswith("Z")


def test_startup_rejects_negative_employees_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        StartupData(employeeCount=-5)

    with pytest.raises(ValidationError):
        StartupRecord.model_validate(
            {
                "schemaVersion": "1.0",
                "recordType": "STARTUP",
                "source": {"name": "YC", "url": "https://example.com"},
                "content": {"entityName": "Acme AI", "data": {"employeeCount": 12}},
                "unexpectedField": "boom",
            }
        )


def test_product_strict_pricing_enum() -> None:
    rec = ProductRecord(
        source=SourceInfo(name="OpenRouter", url="https://openrouter.ai/openai/gpt-4o"),  # type: ignore[arg-type]
        content=ProductContent(
            productName="GPT-4o",
            startupName="OpenAI",
            pricingModel=PricingModel.PAID,
        ),
    )
    assert rec.model_dump(mode="json")["content"]["pricingModel"] == "PAID"

    with pytest.raises(ValidationError):
        ProductContent(
            productName="GPT-4o",
            startupName="OpenAI",
            pricingModel="CUSTOM_TIER",  # type: ignore[arg-type]
        )


def test_paper_github_consistency_rules() -> None:
    # Valid null github_url and null github_stars
    rec = ResearchPaperRecord(
        source=SourceInfo(name="arXiv", url="https://arxiv.org/abs/2501.00001"),  # type: ignore[arg-type]
        content=PaperContent(
            title="Scaling Sparse Attention",
            authors=["A. Researcher", "  ", "B. Scientist"],
            paper_url="https://arxiv.org/abs/2501.00001",  # type: ignore[arg-type]
            github_url=None,
            github_stars=None,
            published_date=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
        ),
    )
    assert rec.content.authors == ["A. Researcher", "B. Scientist"]
    assert rec.model_dump(mode="json")["content"]["published_date"] == "2026-11-20T12:00:00Z"

    # Invalid: stars without github_url
    with pytest.raises(ValidationError):
        PaperContent(
            title="Test",
            authors=["Author"],
            paper_url="https://arxiv.org/abs/2501.00001",  # type: ignore[arg-type]
            github_url=None,
            github_stars=42,
            published_date=datetime.now(UTC),
        )

    # Invalid: non-github host in github_url
    with pytest.raises(ValidationError):
        PaperContent(
            title="Test",
            authors=["Author"],
            paper_url="https://arxiv.org/abs/2501.00001",  # type: ignore[arg-type]
            github_url="https://gitlab.com/foo/bar",  # type: ignore[arg-type]
            github_stars=10,
            published_date=datetime.now(UTC),
        )


def test_job_and_news_timezone_normalization_to_utc() -> None:
    est = timezone(timedelta(hours=-5))
    local_dt = datetime(2026, 11, 20, 18, 0, 0, tzinfo=est)

    job = JobRecord(
        source=SourceInfo(name="Greenhouse", url="https://boards.greenhouse.io/anthropic/jobs/1"),  # type: ignore[arg-type]
        content=JobContent(
            title="Research Engineer",
            company="Anthropic",
            date=local_dt,
            is_remote=False,
            role_family="Research",
        ),
    )
    assert job.model_dump(mode="json")["content"]["date"] == "2026-11-20T23:00:00Z"

    news = NewsRecord(
        source=SourceInfo(name="TechCrunch AI", url="https://techcrunch.com/2026/11/20/ai-launch/"),  # type: ignore[arg-type]
        content=NewsContent(
            title="Anthropic Releases Next-Gen Model",
            published_date="2026-11-20T18:00:00-05:00",  # type: ignore[arg-type]
            summary="Anthropic released a new frontier model today. The release improves coding benchmarks.",
            mentioned_entities=["Anthropic", "anthropic", "OpenAI"],
        ),
    )
    dumped_news = news.model_dump(mode="json")
    assert dumped_news["content"]["published_date"] == "2026-11-20T23:00:00Z"
    assert dumped_news["content"]["mentioned_entities"] == ["Anthropic", "OpenAI"]


def test_entity_mapping_log_bounds() -> None:
    log = EntityMappingLog(
        raw_name="OpenAI, Inc.",
        canonical_name="OpenAI",
        match_method=MatchMethod.EXACT,
        confidence=1.0,
        record_type="STARTUP",
        source_url="https://www.ycombinator.com/companies/openai",
    )
    assert log.match_method == MatchMethod.EXACT

    with pytest.raises(ValidationError):
        EntityMappingLog(
            raw_name="Foo",
            canonical_name="Bar",
            match_method=MatchMethod.FUZZY,
            confidence=1.5,
            record_type="STARTUP",
            source_url="https://example.com",
        )


def test_github_url_normalizer_and_html_star_parser() -> None:
    from tessera.pipelines.papers import _parse_github_html_stars, normalize_github_repo_url

    url, key = normalize_github_repo_url("Code at https://github.com/huggingface/transformers.git).")
    assert url == "https://github.com/huggingface/transformers"
    assert key == "huggingface/transformers"

    bad_url, bad_key = normalize_github_repo_url("https://github.com/topics/machine-learning")
    assert bad_url is None and bad_key is None

    # Exact integer aria-label parsing (never rounded 14.2k strings)
    html_aria = '<a aria-label="14,283 users starred this repository" href="/org/repo/stargazers">14.3k</a>'
    assert _parse_github_html_stars(html_aria) == 14283

    html_title = '<span id="repo-stars-counter-star" title="1,024" class="Counter">1k</span>'
    assert _parse_github_html_stars(html_title) == 1024

    assert _parse_github_html_stars("<html><body>no stars here</body></html>") is None
