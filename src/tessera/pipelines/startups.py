"""Phase I Pipeline: Massive Bulk Acquisition of AI Startups (>= 1,000 unique records)."""

from __future__ import annotations

import asyncio
from typing import Any

from tessera.config import Settings
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.logging_config import get_logger
from tessera.resolution.resolver import EntityResolver
from tessera.schemas.entities import SourceInfo, StartupContent, StartupData, StartupRecord
from tessera.storage.state_store import StateStore

logger = get_logger(__name__)

_YC_AI_URL = "https://yc-oss.github.io/api/tags/artificial-intelligence.json"
_YC_ALL_URL = "https://yc-oss.github.io/api/companies/all.json"

_AI_KEYWORDS: frozenset[str] = frozenset(
    {
        "artificial intelligence",
        "machine learning",
        "generative ai",
        "deep learning",
        "nlp",
        "natural language processing",
        "computer vision",
        "llm",
        "aiops",
        "ai",
        "robotics",
        "autonomous",
        "speech recognition",
        "voice ai",
        "data science",
    }
)


def _is_ai_yc_company(raw: dict[str, Any]) -> bool:
    """Return True if a YC company record is clearly in the AI/ML ecosystem."""
    tags = [str(t).lower() for t in raw.get("tags") or []]
    industries = [str(i).lower() for i in raw.get("industries") or []]
    subindustry = str(raw.get("subindustry") or "").lower()
    if any(t in _AI_KEYWORDS for t in tags):
        return True
    if any("artificial intelligence" in i or "machine learning" in i for i in industries):
        return True
    if "artificial intelligence" in subindustry or "machine learning" in subindustry:
        return True
    one_liner = str(raw.get("one_liner") or "").lower()
    return any(
        phrase in one_liner
        for phrase in (" ai ", " ai-", "llm", "machine learning", "generative ai", "neural network")
    )


def _parse_employee_count(raw: dict[str, Any]) -> int | None:
    """Extract verified integer employee count if available; strictly None if missing/zero."""
    val = raw.get("team_size")
    if isinstance(val, int) and val > 0:
        return val
    if isinstance(val, str) and val.strip().isdigit():
        parsed = int(val.strip())
        return parsed if parsed > 0 else None
    return None


async def run_startups_pipeline(
    *,
    settings: Settings,
    store: StateStore,
    fetcher: HttpFetcher,
    resolver: EntityResolver,
    target_count: int | None = None,
) -> int:
    """Collect >= 1,000 unique AI startups via decoupled Producer -> Queue -> Worker topology."""
    goal = target_count or settings.phase1_target_count
    existing_keys = await store.get_dedup_keys("STARTUP")
    if len(existing_keys) >= goal:
        logger.info("startups_checkpoint_already_met", count=len(existing_keys), target=goal)
        return len(existing_keys)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=250)
    num_workers = 8
    seen_canonical: set[str] = set(existing_keys)
    batch_lock = asyncio.Lock()
    buffered_rows: list[tuple[str, str, str, dict[str, Any]]] = []
    inserted_total = len(existing_keys)

    async def producer() -> None:
        yielded = 0
        # 1. Primary feed: YC AI tagged startups (1,050+ verified AI companies)
        status_ai, yc_ai_data, _ = await fetcher.fetch_json(_YC_AI_URL)
        if status_ai == 200 and isinstance(yc_ai_data, list):
            logger.info("startups_source_loaded", source="YC AI Directory", rows=len(yc_ai_data))
            for item in yc_ai_data:
                if isinstance(item, dict):
                    item["_source_name"] = "Y Combinator AI Directory"
                    await queue.put(item)
                    yielded += 1

        # 2. Expansion feed: YC All Companies filtered to AI/ML/LLM/CV/Robotics tags
        status_all, yc_all_data, _ = await fetcher.fetch_json(_YC_ALL_URL)
        if status_all == 200 and isinstance(yc_all_data, list):
            ai_extra = [c for c in yc_all_data if isinstance(c, dict) and _is_ai_yc_company(c)]
            logger.info("startups_source_loaded", source="YC All AI-Filtered", rows=len(ai_extra))
            for item in ai_extra:
                item["_source_name"] = "Y Combinator Directory"
                await queue.put(item)
                yielded += 1

        for _ in range(num_workers):
            await queue.put(None)
        logger.info("startups_producer_done", queued=yielded)

    async def worker() -> None:
        nonlocal inserted_total
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                raw_name = str(item.get("name") or "").strip()
                source_url = str(item.get("url") or "").strip()
                if not raw_name or not source_url.startswith("http"):
                    continue

                website = str(item.get("website") or "").strip() or None
                resolved = resolver.resolve(
                    raw_name,
                    domain=website,
                    record_type="STARTUP",
                    source_url=source_url,
                )
                dedup_key = resolved.canonical_name.lower()

                async with batch_lock:
                    if dedup_key in seen_canonical or inserted_total >= goal:
                        continue
                    seen_canonical.add(dedup_key)

                    record = StartupRecord(
                        source=SourceInfo(
                            name=str(item.get("_source_name") or "Y Combinator AI Directory"),
                            url=source_url,  # type: ignore[arg-type]
                        ),
                        content=StartupContent(
                            entityName=resolved.canonical_name,
                            data=StartupData(employeeCount=_parse_employee_count(item)),
                        ),
                    )
                    buffered_rows.append(
                        (
                            "STARTUP",
                            dedup_key,
                            str(record.source.url),
                            record.model_dump(mode="json"),
                        )
                    )
                    inserted_total += 1
                    if len(buffered_rows) >= 100:
                        to_flush = list(buffered_rows)
                        buffered_rows.clear()
                        await store.upsert_records_batch(to_flush)
                        await store.save_entity_mappings(resolver.pop_logs())
                        await store.save_checkpoint(
                            "startups", {"collected": inserted_total, "target": goal}
                        )
            except Exception as exc:
                logger.warning("startup_worker_item_error", error=str(exc)[:180])
            finally:
                queue.task_done()

    await asyncio.gather(producer(), *(worker() for _ in range(num_workers)))

    if buffered_rows:
        await store.upsert_records_batch(buffered_rows)
        buffered_rows.clear()
    await store.save_entity_mappings(resolver.pop_logs())
    final_count = await store.count_records("STARTUP")
    await store.save_checkpoint("startups", {"collected": final_count, "completed": True})
    logger.info("startups_pipeline_complete", total_unique=final_count)
    return final_count
