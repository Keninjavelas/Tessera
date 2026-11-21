"""Phase II Pipeline: High-Fidelity AI Job Board Ingestion across >=5 Distinct Boards (Strict <24h)."""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from tessera.config import Settings
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.llm.orchestrator import LLMExtractionError, LLMOrchestrator
from tessera.logging_config import get_logger
from tessera.resolution.resolver import EntityResolver
from tessera.schemas.entities import JobContent, JobRecord, SourceInfo
from tessera.storage.state_store import StateStore, canonicalize_url
from tessera.utils.dates import is_within_hours, normalize_to_utc

logger = get_logger(__name__)

_ROLE_FAMILIES: tuple[str, ...] = (
    "Engineering",
    "Research",
    "Product",
    "Data & ML",
    "Operations",
    "Go-To-Market",
)


@dataclass(frozen=True)
class JobBoardConfig:
    """Configuration for a live AI job board endpoint."""

    name: str
    platform: Literal["greenhouse", "ashby", "jobicy"]
    endpoint: str
    default_company: str | None = None
    default_domain: str | None = None
    max_jobs: int = 15


JOB_BOARDS: tuple[JobBoardConfig, ...] = (
    JobBoardConfig(
        name="Greenhouse — Anthropic",
        platform="greenhouse",
        endpoint="https://boards-api.greenhouse.io/v1/boards/anthropic/jobs",
        default_company="Anthropic PBC",
        default_domain="anthropic.com",
        max_jobs=15,
    ),
    JobBoardConfig(
        name="Ashby — OpenAI",
        platform="ashby",
        endpoint="https://api.ashbyhq.com/posting-api/job-board/openai",
        default_company="OpenAI, Inc.",
        default_domain="openai.com",
        max_jobs=15,
    ),
    JobBoardConfig(
        name="Greenhouse — Databricks",
        platform="greenhouse",
        endpoint="https://boards-api.greenhouse.io/v1/boards/databricks/jobs",
        default_company="Databricks Inc.",
        default_domain="databricks.com",
        max_jobs=15,
    ),
    JobBoardConfig(
        name="Ashby — Harvey",
        platform="ashby",
        endpoint="https://api.ashbyhq.com/posting-api/job-board/harvey",
        default_company="Harvey AI",
        default_domain="harvey.ai",
        max_jobs=15,
    ),
    JobBoardConfig(
        name="Greenhouse — Scale AI",
        platform="greenhouse",
        endpoint="https://boards-api.greenhouse.io/v1/boards/scaleai/jobs",
        default_company="Scale AI, Inc.",
        default_domain="scale.com",
        max_jobs=15,
    ),
    JobBoardConfig(
        name="Ashby — Perplexity",
        platform="ashby",
        endpoint="https://api.ashbyhq.com/posting-api/job-board/perplexity",
        default_company="Perplexity.ai",
        default_domain="perplexity.ai",
        max_jobs=15,
    ),
    JobBoardConfig(
        name="Jobicy AI Board",
        platform="jobicy",
        endpoint="https://jobicy.com/api/v2/remote-jobs?count=50&tag=ai",
        default_company=None,
        default_domain=None,
        max_jobs=15,
    ),
)


class _JobClassificationLLM(BaseModel):
    """Structured LLM classification for ambiguous job listings."""

    is_remote: bool
    role_family: Literal["Engineering", "Research", "Product", "Data & ML", "Operations", "Go-To-Market"] = Field(
        ..., description="Normalized role family"
    )


def _classify_role_family_deterministic(title: str, department: str = "") -> str | None:
    """Fast deterministic role family mapper; returns None when title/dept is ambiguous."""
    combined = f"{title} {department}".lower()
    if any(k in combined for k in ("research scientist", "research engineer", "researcher", "fellow", "alignment", "interpretability", "post-training", "pre-training")):
        return "Research"
    if any(k in combined for k in ("machine learning", "ml ", " ml", "data scientist", "data engineer", "analytics", "ai engineer", "applied ai", "llm")):
        return "Data & ML"
    if any(k in combined for k in ("software", "engineer", "developer", "infrastructure", "systems", "security", "sre", "devops", "platform", "frontend", "backend", "fullstack", "architect")):
        return "Engineering"
    if any(k in combined for k in ("product manager", "product design", "designer", "ux", "product ")):
        return "Product"
    if any(k in combined for k in ("account executive", "sales", "solutions architect", "marketing", "customer success", "partnerships", "business development", "gtm", "revenue", "commercial")):
        return "Go-To-Market"
    if any(k in combined for k in ("recruiter", "recruiting", "people", "legal", "counsel", "finance", "accounting", "operations", "chief of staff", "workplace", "policy", "communications", "hr ")):
        return "Operations"
    return None


def _detect_remote_deterministic(location_text: str, explicit_flag: bool | None = None) -> bool | None:
    if isinstance(explicit_flag, bool):
        return explicit_flag
    low = location_text.lower()
    if any(k in low for k in ("remote", "anywhere", "distributed", "work from home")):
        return True
    if any(k in low for k in ("san francisco", "new york", "london", "seattle", "tokyo", "paris", "dublin", "toronto", "in-office", "onsite", "on-site", "mountain view")):
        return False
    return None


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


async def _fetch_board_jobs(
    board: JobBoardConfig,
    *,
    fetcher: HttpFetcher,
    llm: LLMOrchestrator | None,
    resolver: EntityResolver,
    freshness_hours: int,
    reference_now: datetime,
    llm_sem: asyncio.Semaphore,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Fetch one live job board, enforce strict <24h filter, and canonicalize entities."""
    try:
        status, data, _ = await fetcher.fetch_json(board.endpoint, timeout=25.0)
        if status != 200 or not isinstance(data, dict):
            logger.warning("job_board_http_error", board=board.name, status=status)
            return []
    except Exception as exc:
        logger.warning("job_board_fetch_failed", board=board.name, error=str(exc)[:150])
        return []

    raw_jobs: list[dict[str, Any]] = data.get("jobs") or []
    rows: list[tuple[str, str, str, dict[str, Any]]] = []
    llm_used_on_board = 0

    for item in raw_jobs:
        if len(rows) >= board.max_jobs:
            break
        if not isinstance(item, dict):
            continue

        title: str = ""
        url: str = ""
        company_raw: str = board.default_company or ""
        date_raw: Any = None
        location_text: str = ""
        department_text: str = ""
        explicit_remote: bool | None = None
        snippet: str = ""

        if board.platform == "greenhouse":
            title = _clean_text(str(item.get("title") or ""))
            url = str(item.get("absolute_url") or "").strip()
            date_raw = item.get("updated_at")
            loc = item.get("location")
            location_text = str(loc.get("name") if isinstance(loc, dict) else loc or "")
            depts = item.get("departments") or []
            department_text = " ".join(str(d.get("name", "")) for d in depts if isinstance(d, dict))

        elif board.platform == "ashby":
            title = _clean_text(str(item.get("title") or ""))
            url = str(item.get("jobUrl") or item.get("applyUrl") or "").strip()
            date_raw = item.get("publishedAt")
            location_text = str(item.get("location") or "")
            department_text = f"{item.get('department') or ''} {item.get('team') or ''}".strip()
            if isinstance(item.get("isRemote"), bool):
                explicit_remote = bool(item["isRemote"])
            snippet = _clean_text(str(item.get("descriptionPlain") or ""))[:1200]

        elif board.platform == "jobicy":
            title = _clean_text(str(item.get("jobTitle") or ""))
            url = str(item.get("url") or "").strip()
            company_raw = _clean_text(str(item.get("companyName") or "Unknown"))
            date_raw = item.get("pubDate")
            location_text = f"Remote {item.get('jobGeo') or ''}".strip()
            industries = item.get("jobIndustry") or []
            department_text = " ".join(str(x) for x in industries) if isinstance(industries, list) else str(industries)
            explicit_remote = True
            snippet = _clean_text(str(item.get("jobExcerpt") or ""))[:1000]

        if not title or not url.startswith("http"):
            continue

        posted_dt = normalize_to_utc(date_raw, reference_now=reference_now)
        if posted_dt is None or not is_within_hours(
            posted_dt,
            hours=freshness_hours,
            reference_now=reference_now,
            safety_margin_minutes=15,
        ):
            continue

        is_remote = _detect_remote_deterministic(location_text, explicit_flag=explicit_remote)
        role_family = _classify_role_family_deterministic(title, department_text)

        # Phase III LLM fallback when role_family or remote flag is ambiguous
        if (role_family is None or is_remote is None) and llm is not None and llm.has_configured_provider and llm_used_on_board < 4:
            llm_used_on_board += 1
            try:
                async with llm_sem:
                    cls_out, _prov = await llm.extract_structured(
                        raw_content=(
                            f"Title: {title}\nCompany: {company_raw}\n"
                            f"Location: {location_text}\nDepartment: {department_text}\n"
                            f"Snippet: {snippet}"
                        ),
                        instruction=(
                            "Determine whether this job is remote (is_remote: boolean) and classify "
                            "role_family into strictly one of: Engineering, Research, Product, "
                            "Data & ML, Operations, Go-To-Market."
                        ),
                        response_model=_JobClassificationLLM,
                        focus_keywords=("remote", "hybrid", "onsite", "engineer", "research", "product", "sales"),
                        source_url=url,
                    )
                if is_remote is None:
                    is_remote = cls_out.is_remote
                if role_family is None:
                    role_family = cls_out.role_family
            except LLMExtractionError:
                pass

        if is_remote is None:
            is_remote = False
        if role_family is None:
            role_family = "Operations"

        resolved = resolver.resolve(
            company_raw,
            domain=board.default_domain,
            record_type="JOB",
            source_url=url,
        )

        record = JobRecord(
            source=SourceInfo(name=board.name, url=url),  # type: ignore[arg-type]
            content=JobContent(
                title=title,
                company=resolved.canonical_name,
                date=posted_dt,
                is_remote=is_remote,
                role_family=role_family,
            ),
        )
        dedup_key = canonicalize_url(url)
        rows.append(("JOB", dedup_key, str(record.source.url), record.model_dump(mode="json")))

    logger.info("job_board_processed", board=board.name, fresh_jobs=len(rows))
    return rows


async def run_jobs_pipeline(
    *,
    settings: Settings,
    store: StateStore,
    fetcher: HttpFetcher,
    llm: LLMOrchestrator | None,
    resolver: EntityResolver,
) -> int:
    """Ingest fresh (<24h) AI jobs across >= 5 distinct live AI job boards."""
    reference_now = datetime.now(UTC).replace(microsecond=0)
    await store.clear_records("JOB")

    llm_sem = asyncio.Semaphore(2)
    results = await asyncio.gather(
        *(
            _fetch_board_jobs(
                board,
                fetcher=fetcher,
                llm=llm,
                resolver=resolver,
                freshness_hours=settings.freshness_hours,
                reference_now=reference_now,
                llm_sem=llm_sem,
            )
            for board in JOB_BOARDS
        )
    )

    all_rows: list[tuple[str, str, str, dict[str, Any]]] = []
    active_boards = 0
    for rows in results:
        if rows:
            active_boards += 1
            all_rows.extend(rows)

    if all_rows:
        await store.upsert_records_batch(all_rows)
    await store.save_entity_mappings(resolver.pop_logs())
    final_count = await store.count_records("JOB")
    await store.save_checkpoint(
        "jobs", {"collected": final_count, "active_boards": active_boards, "completed": True}
    )
    logger.info("jobs_pipeline_complete", total_fresh=final_count, distinct_boards=active_boards)
    return final_count
