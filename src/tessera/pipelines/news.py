"""Phase II Pipeline: High-Fidelity AI News Ingestion across 5 Distinct Sources (Strict < 24h)."""

from __future__ import annotations

import asyncio
import html as html_lib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from tessera.config import Settings
from tessera.fetchers.browser_fetcher import SmartFetcher
from tessera.llm.orchestrator import LLMExtractionError, LLMOrchestrator
from tessera.llm.preprocessor import extract_clean_text, prepare_llm_context
from tessera.logging_config import get_logger
from tessera.resolution.resolver import EntityResolver
from tessera.schemas.entities import NewsContent, NewsRecord, SourceInfo
from tessera.storage.state_store import StateStore, canonicalize_url
from tessera.utils.dates import extract_date_from_html, is_within_hours, normalize_to_utc

logger = get_logger(__name__)


@dataclass(frozen=True)
class NewsSourceConfig:
    """Configuration for a distinct AI news RSS/Atom source."""

    name: str
    feed_url: str
    max_articles: int = 6


# 5 primary distinct high-signal AI news outlets + 2 backups
PRIMARY_NEWS_SOURCES: tuple[NewsSourceConfig, ...] = (
    NewsSourceConfig(
        name="TechCrunch AI",
        feed_url="https://techcrunch.com/category/artificial-intelligence/feed/",
        max_articles=4,
    ),
    NewsSourceConfig(
        name="The Verge AI",
        feed_url="https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        max_articles=4,
    ),
    NewsSourceConfig(
        name="SiliconANGLE AI",
        feed_url="https://siliconangle.com/category/ai/feed/",
        max_articles=4,
    ),
    NewsSourceConfig(
        name="The Decoder",
        feed_url="https://the-decoder.com/feed/",
        max_articles=4,
    ),
    NewsSourceConfig(
        name="Wired AI",
        feed_url="https://www.wired.com/feed/tag/ai/latest/rss",
        max_articles=4,
    ),
)

BACKUP_NEWS_SOURCES: tuple[NewsSourceConfig, ...] = (
    NewsSourceConfig(
        name="MIT Technology Review",
        feed_url="https://www.technologyreview.com/feed/",
        max_articles=4,
    ),
    NewsSourceConfig(
        name="MarkTechPost",
        feed_url="https://www.marktechpost.com/feed/",
        max_articles=4,
    ),
)


class _NewsLLMExtraction(BaseModel):
    """Structured LLM extraction output grounded strictly in the fetched article body."""

    summary: str = Field(
        ...,
        min_length=20,
        max_length=1200,
        description="A concise 2-sentence factual summary grounded strictly in the article text.",
    )
    mentioned_entities: list[str] = Field(
        default_factory=list,
        description="List of AI startups, tech companies, or AI products explicitly mentioned in the article.",
    )


def _strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _parse_feed_entries(xml_text: str) -> list[dict[str, str]]:
    """Parse RSS 2.0 and Atom feeds into normalized candidate dicts."""
    entries: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries

    # RSS 2.0 <item>
    for item in root.findall(".//item"):
        title = _strip_tags(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub_date = (
            item.findtext("pubDate")
            or item.findtext("{http://purl.org/dc/elements/1.1/}date")
            or ""
        ).strip()
        desc = _strip_tags(
            item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            or item.findtext("description")
            or ""
        )
        if title and link.startswith("http"):
            entries.append({"title": title, "url": link, "date_raw": pub_date, "snippet": desc})

    if entries:
        return entries

    # Atom <entry>
    for entry in root.iter():
        if entry.tag.split("}")[-1].lower() != "entry":
            continue
        title = ""
        link = ""
        pub_date = ""
        snippet = ""
        for child in entry:
            tag = child.tag.split("}")[-1].lower()
            if tag == "title" and child.text:
                title = _strip_tags(child.text)
            elif tag == "link":
                href = child.attrib.get("href", "").strip()
                rel = child.attrib.get("rel", "alternate")
                if href.startswith("http") and (rel == "alternate" or not link):
                    link = href
            elif tag in ("published", "updated") and child.text and not pub_date:
                pub_date = child.text.strip()
            elif tag in ("content", "summary") and child.text and len(child.text) > len(snippet):
                snippet = _strip_tags(child.text)
        if title and link.startswith("http"):
            entries.append({"title": title, "url": link, "date_raw": pub_date, "snippet": snippet})

    return entries


async def _process_news_source(
    source_cfg: NewsSourceConfig,
    *,
    fetcher: SmartFetcher,
    llm: LLMOrchestrator,
    resolver: EntityResolver,
    store: StateStore,
    freshness_hours: int,
    reference_now: datetime,
    llm_sem: asyncio.Semaphore,
) -> list[tuple[str, str, str, dict[str, object]]]:
    """Fetch one RSS/Atom feed, filter strictly <24h, fetch article body, extract via LLM."""
    try:
        feed_res = await fetcher.http.fetch(source_cfg.feed_url, timeout=20.0)
        if not feed_res.ok:
            logger.warning("news_feed_http_error", source=source_cfg.name, status=feed_res.status_code)
            return []
    except Exception as exc:
        logger.warning("news_feed_fetch_failed", source=source_cfg.name, error=str(exc)[:150])
        return []

    raw_entries = _parse_feed_entries(feed_res.text)
    fresh_candidates: list[tuple[dict[str, str], datetime]] = []

    for entry in raw_entries:
        dt = normalize_to_utc(entry["date_raw"], reference_now=reference_now)
        if dt is not None:
            if is_within_hours(
                dt,
                hours=freshness_hours,
                reference_now=reference_now,
                safety_margin_minutes=15,
            ):
                fresh_candidates.append((entry, dt))
        else:
            # No RSS date -> will attempt HTML metadata extraction + heuristic freshness fallback
            fresh_candidates.append((entry, datetime.min.replace(tzinfo=UTC)))

    # Keep only verified fresh entries first, capped per source
    verified_first = [c for c in fresh_candidates if c[1].year > 1970]
    dateless = [c for c in fresh_candidates if c[1].year == 1970]
    selected = (verified_first + dateless)[: source_cfg.max_articles]

    rows: list[tuple[str, str, str, dict[str, object]]] = []

    for entry, parsed_dt in selected:
        url = entry["url"]
        title = entry["title"]
        body_text = ""

        # Fetch full article page asynchronously (SmartFetcher escalates to Playwright on 403/JS shell)
        try:
            page_res = await fetcher.fetch(url, timeout=20.0)
            if page_res.ok and page_res.text:
                clean_body, _, _ = extract_clean_text(page_res.text)
                body_text = clean_body
                if parsed_dt.year == 1970:
                    html_dt, _method = extract_date_from_html(
                        page_res.text, url=url, reference_now=reference_now
                    )
                    if html_dt is not None:
                        parsed_dt = html_dt
        except Exception as exc:
            logger.debug("news_article_fetch_fallback", url=url, error=str(exc)[:120])

        # Enforce strict 24h freshness rule
        if parsed_dt.year == 1970:
            is_fresh, heur_dt, _method = await store.evaluate_heuristic_freshness(
                source_cfg.name,
                url,
                body_text or entry["snippet"] or title,
                freshness_hours=freshness_hours,
                reference_now=reference_now,
            )
            if not is_fresh:
                continue
            parsed_dt = heur_dt
        elif not is_within_hours(parsed_dt, hours=freshness_hours, reference_now=reference_now):
            continue

        # Fall back to RSS body snippet only if full page fetch was blocked/empty
        article_for_llm = body_text if len(body_text) >= 160 else entry["snippet"]
        if len(article_for_llm) < 40:
            article_for_llm = f"{title}. {entry['snippet']}".strip()

        # Compress article cleanly to ~800 tokens for high-density TPM-efficient LLM extraction
        compact_article = prepare_llm_context(
            article_for_llm,
            max_tokens=800,
            focus_keywords=("ai", "model", "startup", "launch", "funding", "release", "announced"),
        )

        # Phase III Multi-Tier LLM extraction for grounded summary + entity extraction
        try:
            async with llm_sem:
                extracted, _provider = await llm.extract_structured(
                    raw_content=f"HEADLINE: {title}\n\nARTICLE:\n{compact_article}",
                    instruction=(
                        "Write a concise 2-sentence factual summary strictly grounded in the article text, "
                        "and extract a list of AI companies, startups, or AI products explicitly mentioned."
                    ),
                    response_model=_NewsLLMExtraction,
                    focus_keywords=("ai", "model", "startup", "launch", "funding", "release", "announced"),
                    source_url=url,
                )
        except LLMExtractionError as exc:
            logger.error("news_llm_extraction_failed_dead_letter", url=url, error=str(exc)[:180])
            continue

        # Phase IV deterministic canonicalization of all mentioned entities
        canonical_entities: list[str] = []
        for raw_ent in extracted.mentioned_entities:
            res = resolver.resolve(
                raw_ent,
                record_type="NEWS",
                source_url=url,
            )
            if res.canonical_name not in canonical_entities:
                canonical_entities.append(res.canonical_name)

        record = NewsRecord(
            source=SourceInfo(name=source_cfg.name, url=url),  # type: ignore[arg-type]
            content=NewsContent(
                title=title,
                published_date=parsed_dt,
                summary=extracted.summary,
                mentioned_entities=canonical_entities,
            ),
        )
        dedup_key = canonicalize_url(url)
        rows.append(("NEWS", dedup_key, str(record.source.url), record.model_dump(mode="json")))

    if dateless and not await store.has_source_baseline(source_cfg.name):
        await store.mark_baseline_initialized(source_cfg.name, reference_now=reference_now)

    logger.info("news_source_processed", source=source_cfg.name, fresh_exported=len(rows))
    return rows


async def run_news_pipeline(
    *,
    settings: Settings,
    store: StateStore,
    fetcher: SmartFetcher,
    llm: LLMOrchestrator,
    resolver: EntityResolver,
) -> int:
    """Ingest fresh (<24h) AI news across 5 distinct sources with full-text + LLM summarization."""
    reference_now = datetime.now(UTC).replace(microsecond=0)
    # Refresh NEWS table so exported dataset strictly reflects current 24h window
    await store.clear_records("NEWS")

    llm_sem = asyncio.Semaphore(1)
    source_results = await asyncio.gather(
        *(
            _process_news_source(
                src,
                fetcher=fetcher,
                llm=llm,
                resolver=resolver,
                store=store,
                freshness_hours=settings.freshness_hours,
                reference_now=reference_now,
                llm_sem=llm_sem,
            )
            for src in PRIMARY_NEWS_SOURCES
        )
    )

    all_rows: list[tuple[str, str, str, dict[str, object]]] = []
    active_sources = 0
    for rows in source_results:
        if rows:
            active_sources += 1
            all_rows.extend(rows)

    # Activate backup feeds automatically if any primary outlet had zero <24h posts
    if active_sources < 5:
        for backup in BACKUP_NEWS_SOURCES:
            if active_sources >= 5:
                break
            b_rows = await _process_news_source(
                backup,
                fetcher=fetcher,
                llm=llm,
                resolver=resolver,
                store=store,
                freshness_hours=settings.freshness_hours,
                reference_now=reference_now,
                llm_sem=llm_sem,
            )
            if b_rows:
                active_sources += 1
                all_rows.extend(b_rows)

    if all_rows:
        await store.upsert_records_batch(all_rows)
    await store.save_entity_mappings(resolver.pop_logs())
    final_count = await store.count_records("NEWS")
    await store.save_checkpoint(
        "news", {"collected": final_count, "active_sources": active_sources, "completed": True}
    )
    logger.info("news_pipeline_complete", total_fresh=final_count, distinct_sources=active_sources)
    return final_count
