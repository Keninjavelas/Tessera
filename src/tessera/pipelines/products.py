"""Phase I Pipeline: Massive Bulk Acquisition of AI Products (>= 1,000 unique records)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from tessera.config import Settings
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.llm.orchestrator import LLMExtractionError, LLMOrchestrator
from tessera.logging_config import get_logger
from tessera.resolution.resolver import EntityResolver
from tessera.schemas.entities import PricingModel, ProductContent, ProductRecord, SourceInfo
from tessera.storage.state_store import StateStore

logger = get_logger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_HF_SPACES_URL = "https://huggingface.co/api/spaces"
_HF_MODELS_URL = "https://huggingface.co/api/models"
_YC_AI_URL = "https://yc-oss.github.io/api/tags/artificial-intelligence.json"

# Verified AI organizations on Hugging Face (eliminates random personal user accounts)
_VERIFIED_HF_ORGS: dict[str, str] = {
    "huggingface": "Hugging Face",
    "huggingfacetb": "Hugging Face",
    "huggingfaceh4": "Hugging Face",
    "huggingfacem4": "Hugging Face",
    "huggingfacefw": "Hugging Face",
    "huggingface-projects": "Hugging Face",
    "black-forest-labs": "Black Forest Labs",
    "stabilityai": "Stability AI",
    "meta-llama": "Meta AI",
    "facebook": "Meta AI",
    "google": "Google",
    "google-bert": "Google",
    "google-t5": "Google",
    "openai": "OpenAI",
    "microsoft": "Microsoft",
    "nvidia": "NVIDIA",
    "mistralai": "Mistral AI",
    "deepseek-ai": "DeepSeek",
    "qwen": "Alibaba Qwen",
    "allenai": "Allen Institute for AI",
    "baai": "BAAI",
    "apple": "Apple",
    "cohereforai": "Cohere",
    "coherelabs": "Cohere",
    "databricks": "Databricks",
    "ibm-granite": "IBM",
    "salesforce": "Salesforce",
    "bytedance-seed": "ByteDance",
    "bytedance": "ByteDance",
    "tencent": "Tencent",
    "thudm": "Zhipu AI",
    "zai-org": "Zhipu AI",
    "moonshotai": "Moonshot AI",
    "minimaxai": "MiniMax",
    "eleutherai": "EleutherAI",
    "tiiuae": "Technology Innovation Institute",
    "nousresearch": "Nous Research",
    "liquidai": "Liquid AI",
    "sentence-transformers": "Sentence Transformers",
    "ultralytics": "Ultralytics",
    "runwayml": "Runway",
    "ai21labs": "AI21 Labs",
    "bigcode": "BigCode",
    "lightricks": "Lightricks",
    "gradio": "Hugging Face",
    "spaces": "Hugging Face",
    "hexgrad": "Hexgrad",
    "kyutai": "Kyutai",
    "fishaudio": "Fish Audio",
    "wan-ai": "Alibaba Wan",
    "stepfun-ai": "StepFun",
    "internlm": "Shanghai AI Lab",
    "openbmb": "OpenBMB",
    "deepmind": "Google DeepMind",
    "xai-org": "xAI",
    "togethercomputer": "Together AI",
}

_OPENROUTER_PROVIDER_MAP: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "meta-llama": "Meta AI",
    "mistralai": "Mistral AI",
    "deepseek": "DeepSeek",
    "cohere": "Cohere",
    "x-ai": "xAI",
    "perplexity": "Perplexity",
    "ai21": "AI21 Labs",
    "databricks": "Databricks",
    "nvidia": "NVIDIA",
    "microsoft": "Microsoft",
    "amazon": "Amazon Web Services",
    "qwen": "Alibaba Qwen",
    "allenai": "Allen Institute for AI",
    "inflection": "Inflection AI",
    "liquid": "Liquid AI",
    "thinkingmachines": "Thinking Machines Lab",
    "writer": "Writer",
    "minimax": "MiniMax",
    "z-ai": "Zhipu AI",
    "moonshotai": "Moonshot AI",
    "nousresearch": "Nous Research",
    "together": "Together AI",
}


class _PricingClassification(BaseModel):
    """Structured output model for Phase III LLM fallback pricing classification."""

    pricingModel: PricingModel = Field(
        ..., description="Must be strictly one of FREE, FREEMIUM, PAID, ENTERPRISE"
    )


def _classify_openrouter_pricing(raw: dict[str, Any]) -> PricingModel:
    model_id = str(raw.get("id") or "").lower()
    pricing = raw.get("pricing") or {}
    try:
        prompt_price = float(pricing.get("prompt", "0") or 0)
        comp_price = float(pricing.get("completion", "0") or 0)
    except (TypeError, ValueError):
        prompt_price, comp_price = 0.0, 0.0

    if ":free" in model_id or (prompt_price == 0.0 and comp_price == 0.0):
        return PricingModel.FREE
    desc = str(raw.get("description") or "").lower()
    if "enterprise" in desc and prompt_price >= 0.00001:
        return PricingModel.ENTERPRISE
    return PricingModel.PAID


def _classify_hf_space_pricing(raw: dict[str, Any]) -> PricingModel:
    raw_card = raw.get("cardData")
    card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
    desc = " ".join(
        [
            str(card.get("short_description") or ""),
            str(card.get("title") or ""),
            " ".join(str(t) for t in (raw.get("tags") or [])),
        ]
    ).lower()
    if any(k in desc for k in ("api key", "pro tier", "freemium", "credits", "upgrade", "paid")):
        return PricingModel.FREEMIUM
    return PricingModel.FREE


def _classify_yc_pricing_deterministic(raw: dict[str, Any]) -> PricingModel | None:
    """Evidence-backed pricing mapper; returns None unless explicit pricing cues appear in YC copy."""
    tags = {str(t).lower() for t in (raw.get("tags") or [])}
    text = f"{raw.get('one_liner') or ''} {raw.get('long_description') or ''}".lower()

    if "open source" in tags or "open-source" in text or "open source" in text:
        return (
            PricingModel.FREEMIUM
            if any(k in text for k in ("cloud", "enterprise", "managed", "hosted", "paid"))
            else PricingModel.FREE
        )
    if any(k in text for k in ("freemium", "free tier", "free plan", "free trial", "free version")):
        return PricingModel.FREEMIUM
    if any(k in text for k in ("enterprise", "fortune 500", "fortune 1000", "enterprise-grade", "custom pricing")):
        return PricingModel.ENTERPRISE
    if any(k in text for k in ("subscription", "per seat", "per month", "pricing starts", "paid plan")):
        return PricingModel.PAID
    return None


async def run_products_pipeline(
    *,
    settings: Settings,
    store: StateStore,
    fetcher: HttpFetcher,
    resolver: EntityResolver,
    llm: LLMOrchestrator | None = None,
    target_count: int | None = None,
    max_llm_enrichments: int = 6,
) -> int:
    """Collect >= 1,000 unique AI products across OpenRouter, Hugging Face Spaces, and YC AI."""
    goal = target_count or settings.phase1_target_count
    existing_keys = await store.get_dedup_keys("PRODUCT")
    if len(existing_keys) >= goal:
        logger.info("products_checkpoint_already_met", count=len(existing_keys), target=goal)
        return len(existing_keys)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=300)
    num_workers = 8
    seen_keys: set[str] = set(existing_keys)
    lock = asyncio.Lock()
    buffered_rows: list[tuple[str, str, str, dict[str, Any]]] = []
    inserted_total = len(existing_keys)
    llm_used = 0
    llm_sem = asyncio.Semaphore(1)

    async def producer() -> None:
        queued = 0

        # Source 1: OpenRouter AI Model Catalog (all ~478 models with exact live API per-token pricing)
        status_or, or_data, _ = await fetcher.fetch_json(_OPENROUTER_MODELS_URL)
        if status_or == 200 and isinstance(or_data, dict):
            models = or_data.get("data") or []
            logger.info("products_source_loaded", source="OpenRouter AI Catalog", rows=len(models))
            for m in models:
                if isinstance(m, dict) and m.get("id"):
                    await queue.put({"_kind": "openrouter", "payload": m})
                    queued += 1

        # Source 2: YC AI Products (ONLY records with explicit pricing evidence or LLM classification)
        status_yc, yc_data, _ = await fetcher.fetch_json(_YC_AI_URL)
        if status_yc == 200 and isinstance(yc_data, list):
            active_yc = [c for c in yc_data if isinstance(c, dict) and c.get("status") == "Active"]
            logger.info("products_source_loaded", source="YC AI Products", rows=len(active_yc))
            for comp in active_yc:
                await queue.put({"_kind": "yc_product", "payload": comp})
                queued += 1

        # Source 3: Official Org-Backed Hugging Face Spaces & Open-Weights AI Models (zero personal handles)
        for sort_by in ("likes", "trendingScore"):
            status_hf, hf_data, _ = await fetcher.fetch_json(
                _HF_SPACES_URL,
                params={"limit": 500, "sort": sort_by, "direction": -1, "full": "true"},
            )
            if status_hf == 200 and isinstance(hf_data, list):
                org_spaces = [
                    sp
                    for sp in hf_data
                    if isinstance(sp, dict)
                    and str(sp.get("author") or str(sp.get("id") or "").split("/")[0]).lower()
                    in _VERIFIED_HF_ORGS
                ]
                logger.info(
                    "products_source_loaded",
                    source=f"Hugging Face Org Spaces ({sort_by})",
                    rows=len(org_spaces),
                )
                for sp in org_spaces:
                    await queue.put({"_kind": "hf_space", "payload": sp})
                    queued += 1

        # Supplement with top official org-backed open-weights models on Hugging Face
        org_sem = asyncio.Semaphore(6)
        target_orgs = [
            "meta-llama",
            "google",
            "openai",
            "mistralai",
            "deepseek-ai",
            "microsoft",
            "nvidia",
            "Qwen",
            "stabilityai",
            "black-forest-labs",
            "allenai",
            "BAAI",
            "apple",
            "ibm-granite",
            "Salesforce",
            "databricks",
            "facebook",
            "CohereLabs",
            "EleutherAI",
            "NousResearch",
            "LiquidAI",
            "ByteDance-Seed",
            "moonshotai",
            "MiniMaxAI",
            "zai-org",
            "ai21labs",
            "tiiuae",
            "sentence-transformers",
        ]

        async def fetch_org_models(org_slug: str) -> list[dict[str, Any]]:
            async with org_sem:
                try:
                    st, data, _ = await fetcher.fetch_json(
                        _HF_MODELS_URL,
                        params={"author": org_slug, "sort": "downloads", "direction": -1, "limit": 25},
                        timeout=15.0,
                    )
                    if st == 200 and isinstance(data, list):
                        return [m for m in data if isinstance(m, dict) and m.get("id")]
                except Exception:
                    return []
                return []

        org_model_batches = await asyncio.gather(*(fetch_org_models(o) for o in target_orgs))
        hf_models_loaded = 0
        for batch in org_model_batches:
            for model_item in batch:
                await queue.put({"_kind": "hf_model", "payload": model_item})
                hf_models_loaded += 1
                queued += 1
        logger.info("products_source_loaded", source="Hugging Face Org Models", rows=hf_models_loaded)

        for _ in range(num_workers):
            await queue.put(None)
        logger.info("products_producer_done", queued=queued)

    async def worker() -> None:
        nonlocal inserted_total, llm_used
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                kind = item["_kind"]
                raw = item["payload"]

                source_name: str
                source_url: str
                product_name: str
                raw_startup: str
                domain_hint: str | None = None
                pricing: PricingModel | None = None

                if kind == "openrouter":
                    model_id = str(raw["id"]).strip()
                    prefix = model_id.split("/")[0].lower() if "/" in model_id else "unknown"
                    raw_startup = _OPENROUTER_PROVIDER_MAP.get(prefix, prefix.replace("-", " "))
                    raw_title = str(raw.get("name") or model_id).strip()
                    product_name = raw_title.split(":", 1)[-1].strip() if ":" in raw_title else raw_title
                    source_name = "OpenRouter AI Model Directory"
                    source_url = f"https://openrouter.ai/{model_id}"
                    pricing = _classify_openrouter_pricing(raw)

                elif kind == "hf_space":
                    space_id = str(raw["id"]).strip()
                    if "/" not in space_id:
                        continue
                    author, slug = space_id.split("/", 1)
                    author_key = str(raw.get("author") or author).strip().lower()
                    if author_key not in _VERIFIED_HF_ORGS:
                        continue
                    card = raw.get("cardData") if isinstance(raw.get("cardData"), dict) else {}
                    title = str(card.get("title") or slug.replace("-", " ").replace("_", " ")).strip()
                    product_name = title[:250] if title else slug
                    # Preserve original HuggingFace org handle for authentic entity resolution logging
                    raw_startup = (
                        str(raw.get("author") or author).strip()
                        if author_key.startswith("huggingface")
                        else _VERIFIED_HF_ORGS[author_key]
                    )
                    source_name = "Hugging Face Spaces"
                    source_url = f"https://huggingface.co/spaces/{space_id}"
                    pricing = _classify_hf_space_pricing(raw)

                elif kind == "hf_model":
                    model_id = str(raw["id"]).strip()
                    if "/" not in model_id:
                        continue
                    author, slug = model_id.split("/", 1)
                    author_key = author.lower()
                    raw_startup = _VERIFIED_HF_ORGS.get(author_key, author)
                    product_name = slug[:250]
                    source_name = "Hugging Face Models"
                    source_url = f"https://huggingface.co/{model_id}"
                    pricing = PricingModel.FREE

                else:  # yc_product
                    comp_name = str(raw.get("name") or "").strip()
                    source_url = str(raw.get("url") or "").strip()
                    if not comp_name or not source_url.startswith("http"):
                        continue
                    product_name = comp_name
                    raw_startup = comp_name
                    domain_hint = str(raw.get("website") or "").strip() or None
                    source_name = "Y Combinator AI Products"
                    pricing = _classify_yc_pricing_deterministic(raw)

                    # Hybrid LLM fallback ONLY for records with substantive description text; never blind-guess
                    if pricing is None:
                        long_desc = str(raw.get("long_description") or "").strip()
                        use_llm = False
                        if len(long_desc) >= 120:
                            async with lock:
                                if (
                                    llm is not None
                                    and llm.has_configured_provider
                                    and llm_used < max_llm_enrichments
                                ):
                                    llm_used += 1
                                    use_llm = True
                        if use_llm and llm is not None:
                            prose = (
                                f"Product: {comp_name}\n"
                                f"Industry: {raw.get('industry')} / {raw.get('subindustry')}\n"
                                f"Tags: {', '.join(raw.get('tags') or [])}\n"
                                f"Summary: {raw.get('one_liner') or ''}\n"
                                f"Description: {long_desc}"
                            )
                            try:
                                async with llm_sem:
                                    cls_out, _prov = await llm.extract_structured(
                                        raw_content=prose,
                                        instruction=(
                                            "Classify this AI product's pricingModel into strictly one of: "
                                            "FREE, FREEMIUM, PAID, or ENTERPRISE based strictly on the description."
                                        ),
                                        response_model=_PricingClassification,
                                        focus_keywords=("pricing", "free", "freemium", "enterprise", "open source"),
                                        source_url=source_url,
                                    )
                                pricing = cls_out.pricingModel
                            except LLMExtractionError:
                                continue
                        else:
                            # Skip records lacking verifiable pricing evidence rather than guessing
                            continue

                resolved = resolver.resolve(
                    raw_startup,
                    domain=domain_hint,
                    record_type="PRODUCT",
                    source_url=source_url,
                )
                dedup_key = f"{resolved.canonical_name.lower()}::{product_name.lower()}"

                async with lock:
                    if dedup_key in seen_keys or inserted_total >= goal:
                        continue
                    seen_keys.add(dedup_key)

                    record = ProductRecord(
                        source=SourceInfo(name=source_name, url=source_url),  # type: ignore[arg-type]
                        content=ProductContent(
                            productName=product_name,
                            startupName=resolved.canonical_name,
                            pricingModel=pricing,
                        ),
                    )
                    buffered_rows.append(
                        (
                            "PRODUCT",
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
                            "products", {"collected": inserted_total, "target": goal}
                        )
            except Exception as exc:
                logger.warning("product_worker_item_error", error=str(exc)[:180])
            finally:
                queue.task_done()

    await asyncio.gather(producer(), *(worker() for _ in range(num_workers)))

    if buffered_rows:
        await store.upsert_records_batch(buffered_rows)
        buffered_rows.clear()
    await store.save_entity_mappings(resolver.pop_logs())
    final_count = await store.count_records("PRODUCT")
    await store.save_checkpoint("products", {"collected": final_count, "completed": True})
    logger.info("products_pipeline_complete", total_unique=final_count, llm_enrichments=llm_used)
    return final_count
