"""Strict Pydantic v2 models for all 5 Tessera entity types plus Entity Mapping Log."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    field_validator,
    model_validator,
)


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def format_utc_iso(dt: datetime) -> str:
    """Format any datetime strictly as UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_datetime(value: datetime | str) -> datetime:
    """Validate and normalize datetime input to timezone-aware UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    else:
        raise TypeError("Expected datetime or ISO-8601 string")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


UtcTimestamp = Annotated[
    datetime,
    PlainSerializer(format_utc_iso, return_type=str, when_used="json"),
]


class PricingModel(StrEnum):
    """Controlled vocabulary for Product pricing models."""

    FREE = "FREE"
    FREEMIUM = "FREEMIUM"
    PAID = "PAID"
    ENTERPRISE = "ENTERPRISE"


class MatchMethod(StrEnum):
    """Deterministic entity resolution match stages."""

    EXACT = "EXACT"
    ALIAS = "ALIAS"
    DOMAIN = "DOMAIN"
    FUZZY = "FUZZY"
    NORMALIZED_NEW = "NORMALIZED_NEW"


class StrictBaseModel(BaseModel):
    """Base model forbidding unexpected fields and trimming string whitespace."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SourceInfo(StrictBaseModel):
    """Traceable origin metadata for every collected record."""

    name: str = Field(..., min_length=1, max_length=200)
    url: HttpUrl


class _BaseRecord(StrictBaseModel):
    """Common envelope shared across all five Tessera entity records."""

    schemaVersion: Literal["1.0"] = "1.0"
    source: SourceInfo
    collectedAt: UtcTimestamp = Field(default_factory=utc_now)

    @field_validator("collectedAt", mode="before")
    @classmethod
    def _validate_collected_at(cls, v: datetime | str) -> datetime:
        return _parse_utc_datetime(v)


# ---------------------------------------------------------------------------
# 1. Startup (recordType = "STARTUP")
# ---------------------------------------------------------------------------
class StartupData(StrictBaseModel):
    """Structured firmographic attributes for a startup."""

    employeeCount: int | None = Field(default=None, ge=0)


class StartupContent(StrictBaseModel):
    """Content payload for a Startup record."""

    entityName: str = Field(..., min_length=1, max_length=300)
    data: StartupData = Field(default_factory=StartupData)


class StartupRecord(_BaseRecord):
    """Canonical Startup record."""

    recordType: Literal["STARTUP"] = "STARTUP"
    content: StartupContent


# ---------------------------------------------------------------------------
# 2. Product (recordType = "PRODUCT")
# ---------------------------------------------------------------------------
class ProductContent(StrictBaseModel):
    """Content payload for a Product record."""

    productName: str = Field(..., min_length=1, max_length=300)
    startupName: str = Field(..., min_length=1, max_length=300)
    pricingModel: PricingModel


class ProductRecord(_BaseRecord):
    """Canonical Product record."""

    recordType: Literal["PRODUCT"] = "PRODUCT"
    content: ProductContent


# ---------------------------------------------------------------------------
# 3. Research Paper (recordType = "RESEARCH_PAPER")
# ---------------------------------------------------------------------------
class PaperContent(StrictBaseModel):
    """Content payload for a Research Paper record."""

    title: str = Field(..., min_length=1, max_length=1000)
    authors: list[str] = Field(default_factory=list)
    paper_url: HttpUrl
    github_url: HttpUrl | None = None
    github_stars: int | None = Field(default=None, ge=0)
    published_date: UtcTimestamp

    @field_validator("published_date", mode="before")
    @classmethod
    def _validate_published_date(cls, v: datetime | str) -> datetime:
        return _parse_utc_datetime(v)

    @field_validator("authors")
    @classmethod
    def _clean_authors(cls, v: list[str]) -> list[str]:
        return [a.strip() for a in v if a and a.strip()]

    @model_validator(mode="after")
    def _validate_github_consistency(self) -> PaperContent:
        if self.github_url is None and self.github_stars is not None:
            raise ValueError("github_stars must be null when github_url is null")
        if self.github_url is not None:
            host = (self.github_url.host or "").lower()
            if host not in ("github.com", "www.github.com"):
                raise ValueError(f"github_url must point to github.com, got host={host}")
        return self


class ResearchPaperRecord(_BaseRecord):
    """Canonical Research Paper record."""

    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    content: PaperContent


# ---------------------------------------------------------------------------
# 4. Job (recordType = "JOB")
# ---------------------------------------------------------------------------
class JobContent(StrictBaseModel):
    """Content payload for a Job record."""

    title: str = Field(..., min_length=1, max_length=400)
    company: str = Field(..., min_length=1, max_length=300)
    date: UtcTimestamp
    is_remote: bool
    role_family: str = Field(..., min_length=1, max_length=100)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, v: datetime | str) -> datetime:
        return _parse_utc_datetime(v)


class JobRecord(_BaseRecord):
    """Canonical Job record."""

    recordType: Literal["JOB"] = "JOB"
    content: JobContent


# ---------------------------------------------------------------------------
# 5. News (recordType = "NEWS")
# ---------------------------------------------------------------------------
class NewsContent(StrictBaseModel):
    """Content payload for a News record."""

    title: str = Field(..., min_length=1, max_length=500)
    published_date: UtcTimestamp
    summary: str = Field(..., min_length=10, max_length=2000)
    mentioned_entities: list[str] = Field(default_factory=list)

    @field_validator("published_date", mode="before")
    @classmethod
    def _validate_published_date(cls, v: datetime | str) -> datetime:
        return _parse_utc_datetime(v)

    @field_validator("mentioned_entities")
    @classmethod
    def _dedup_entities(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in v:
            cleaned = item.strip()
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                out.append(cleaned)
        return out


class NewsRecord(_BaseRecord):
    """Canonical News record."""

    recordType: Literal["NEWS"] = "NEWS"
    content: NewsContent


# ---------------------------------------------------------------------------
# 6. Entity Mapping Log
# ---------------------------------------------------------------------------
class EntityMappingLog(StrictBaseModel):
    """Audit record produced by the Phase IV deterministic entity resolver."""

    raw_name: str = Field(..., min_length=1, max_length=400)
    canonical_name: str = Field(..., min_length=1, max_length=400)
    match_method: MatchMethod
    confidence: float = Field(..., ge=0.0, le=1.0)
    record_type: str = Field(..., min_length=1, max_length=50)
    source_url: str = Field(..., min_length=1, max_length=2000)
