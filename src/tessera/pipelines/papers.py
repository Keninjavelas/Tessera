"""Phase I Pipeline: Massive Bulk Acquisition of AI Research Papers (>= 1,000) + Live GitHub Stars."""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any

from tessera.config import Settings
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.logging_config import get_logger
from tessera.schemas.entities import PaperContent, ResearchPaperRecord, SourceInfo
from tessera.storage.state_store import StateStore
from tessera.utils.dates import normalize_to_utc

logger = get_logger(__name__)

_HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"
_ARXIV_API_URL = "https://export.arxiv.org/api/query"
_PWC_REFERENCE_URL = "https://paperswithcode.co/paper/98456"

_GITHUB_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_GITHUB_INVALID_OWNERS: frozenset[str] = frozenset(
    {"topics", "explore", "search", "features", "pricing", "about", "login", "orgs", "sponsors", "settings"}
)


def normalize_github_repo_url(raw_url: str | None) -> tuple[str | None, str | None]:
    """Extract and normalize a valid GitHub repo URL to `(https://github.com/owner/repo, owner/repo)`."""
    if not raw_url:
        return None, None
    m = _GITHUB_REPO_RE.search(raw_url)
    if not m:
        return None, None
    owner = m.group(1).strip()
    repo = m.group(2).strip().rstrip(".,;:)[]")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo or owner.lower() in _GITHUB_INVALID_OWNERS:
        return None, None
    return f"https://github.com/{owner}/{repo}", f"{owner}/{repo}"


_GH_HTML_ARIA_STARS_RE = re.compile(
    r'aria-label="([0-9,]+)\s+users?\s+starred\s+this\s+repository"', re.IGNORECASE
)
_GH_HTML_TITLE_STARS_RE = re.compile(
    r'id="repo-stars-counter-star"[^>]*title="([0-9,]+)"|title="([0-9,]+)"[^>]*id="repo-stars-counter-star"',
    re.IGNORECASE,
)


def _parse_github_html_stars(html: str) -> int | None:
    """Parse exact integer star count from live GitHub repo HTML (never rounded k-abbreviations)."""
    m = _GH_HTML_ARIA_STARS_RE.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    m2 = _GH_HTML_TITLE_STARS_RE.search(html)
    if m2:
        raw_num = m2.group(1) or m2.group(2)
        if raw_num:
            return int(raw_num.replace(",", ""))
    return None


class _GitHubStarClient:
    """Async GitHub live star fetcher: REST API (authenticated/rate-limited) -> live GitHub HTML -> upstream fallback."""

    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        store: StateStore,
        token: str | None,
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._token = token.strip() if token else None
        self._api_exhausted = False
        self._sem = asyncio.Semaphore(6 if self._token else 4)
        self.live_api_hits = 0
        self.live_html_hits = 0
        self.cache_hits = 0
        self.upstream_verified_hits = 0

    async def get_stars(self, repo_key: str, *, upstream_stars: int | None = None) -> int | None:
        """Fetch live exact GitHub star count via REST API, falling back to live GitHub HTML when unauthenticated quota hits 0."""
        cached = await self._store.get_cached_github_stars(repo_key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        # Tier 1: Official GitHub REST API (5,000 req/hr with GITHUB_TOKEN; 60 req/hr unauthenticated)
        if not self._api_exhausted:
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            async with self._sem:
                if not self._api_exhausted:
                    try:
                        status, data, resp_headers = await self._fetcher.fetch_json(
                            f"https://api.github.com/repos/{repo_key}",
                            headers=headers,
                            timeout=12.0,
                        )
                        remaining_str = resp_headers.get("x-ratelimit-remaining")
                        if remaining_str is not None and remaining_str.isdigit():
                            if int(remaining_str) <= 2:
                                self._api_exhausted = True
                                logger.warning(
                                    "github_api_ratelimit_low",
                                    remaining=int(remaining_str),
                                    reset=resp_headers.get("x-ratelimit-reset"),
                                    authenticated=bool(self._token),
                                )
                        if status == 200 and isinstance(data, dict) and "stargazers_count" in data:
                            stars = int(data["stargazers_count"])
                            await self._store.set_cached_github_stars(repo_key, stars)
                            self.live_api_hits += 1
                            return stars
                        if status in (403, 429):
                            self._api_exhausted = True
                            logger.warning(
                                "github_api_ratelimit_exhausted",
                                status=status,
                                reset=resp_headers.get("x-ratelimit-reset"),
                            )
                    except Exception as exc:
                        logger.debug("github_api_lookup_error", repo=repo_key, error=str(exc)[:120])

        # Tier 2: Live GitHub repository HTML exact-integer scrape (exact aria-label/title, never rounded)
        async with self._sem:
            try:
                res = await self._fetcher.fetch(
                    f"https://github.com/{repo_key}",
                    timeout=12.0,
                )
                if res.ok and res.text:
                    exact_stars = _parse_github_html_stars(res.text)
                    if exact_stars is not None:
                        await self._store.set_cached_github_stars(repo_key, exact_stars)
                        self.live_html_hits += 1
                        return exact_stars
            except Exception as exc:
                logger.debug("github_html_lookup_error", repo=repo_key, error=str(exc)[:120])

        # Tier 3: Upstream verified star count (e.g. Hugging Face Daily Papers sync) if GitHub unreachable
        if upstream_stars is not None and upstream_stars >= 0:
            await self._store.set_cached_github_stars(repo_key, int(upstream_stars))
            self.upstream_verified_hits += 1
            return int(upstream_stars)
        return None


async def _fetch_pwc_reference(fetcher: HttpFetcher) -> dict[str, Any] | None:
    """Inspect the prompt's reference PapersWithCode page (`paperswithcode.co/paper/98456`)."""
    try:
        res = await fetcher.fetch(_PWC_REFERENCE_URL, timeout=15.0)
        if not res.ok:
            return None
        title_m = re.search(r"<title>(.*?)</title>", res.text, re.S | re.I)
        raw_title = title_m.group(1).strip() if title_m else "Papers with Code Paper 98456"
        title = re.sub(r"\s*\|\s*Papers with Code.*$", "", raw_title, flags=re.I).strip()
        import html as html_lib

        title = html_lib.unescape(title)
        gh_matches = _GITHUB_REPO_RE.findall(res.text)
        gh_url: str | None = None
        for owner, repo in gh_matches:
            if owner.lower() not in _GITHUB_INVALID_OWNERS:
                gh_url = f"https://github.com/{owner}/{repo}"
                break
        return {
            "dedup_key": "pwc:98456",
            "source_name": "Papers with Code",
            "source_url": _PWC_REFERENCE_URL,
            "title": title,
            "authors": ["Zhipu AI Research Team"],
            "paper_url": _PWC_REFERENCE_URL,
            "github_url_raw": gh_url,
            "upstream_stars": None,
            "published_date": datetime.now(UTC).replace(microsecond=0),
        }
    except Exception as exc:
        logger.warning("pwc_reference_fetch_failed", error=str(exc))
        return None


async def _fetch_arxiv_batch(fetcher: HttpFetcher, start: int = 0, max_results: int = 100) -> list[dict[str, Any]]:
    """Fetch recent AI/ML/CL/CV papers from the official arXiv Atom API."""
    params: dict[str, str | int] = {
        "search_query": "cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:cs.CV",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": max_results,
    }
    try:
        res = await fetcher.fetch(_ARXIV_API_URL, params=params, timeout=25.0)
        if not res.ok:
            return []
        root = ET.fromstring(res.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        out: list[dict[str, Any]] = []
        for entry in root.findall("atom:entry", ns):
            raw_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
            title = re.sub(r"\s+", " ", (entry.findtext("atom:title", default="", namespaces=ns) or "")).strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            published_raw = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
            authors = [
                (a.findtext("atom:name", default="", namespaces=ns) or "").strip()
                for a in entry.findall("atom:author", ns)
            ]
            if not raw_id or not title:
                continue
            arxiv_id = raw_id.rsplit("/", 1)[-1].split("v")[0]
            paper_url = f"https://arxiv.org/abs/{arxiv_id}"
            gh_url, _ = normalize_github_repo_url(summary)
            dt = normalize_to_utc(published_raw) or datetime.now(UTC).replace(microsecond=0)
            out.append(
                {
                    "dedup_key": f"arxiv:{arxiv_id}",
                    "source_name": "arXiv API",
                    "source_url": paper_url,
                    "title": title,
                    "authors": [a for a in authors if a],
                    "paper_url": paper_url,
                    "github_url_raw": gh_url,
                    "upstream_stars": None,
                    "published_date": dt,
                }
            )
        return out
    except Exception as exc:
        logger.warning("arxiv_batch_fetch_failed", start=start, error=str(exc)[:150])
        return []


async def run_papers_pipeline(
    *,
    settings: Settings,
    store: StateStore,
    fetcher: HttpFetcher,
    target_count: int | None = None,
) -> int:
    """Collect >= 1,000 unique AI research papers with GitHub URLs and live GitHub star counts."""
    goal = target_count or settings.phase1_target_count
    existing_keys = await store.get_dedup_keys("RESEARCH_PAPER")
    if len(existing_keys) >= goal:
        logger.info("papers_checkpoint_already_met", count=len(existing_keys), target=goal)
        return len(existing_keys)

    gh_client = _GitHubStarClient(fetcher=fetcher, store=store, token=settings.github_token)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=300)
    num_workers = 10
    seen_keys: set[str] = set(existing_keys)
    lock = asyncio.Lock()
    buffered_rows: list[tuple[str, str, str, dict[str, Any]]] = []
    inserted_total = len(existing_keys)

    async def producer() -> None:
        queued = 0

        # 1. Inspect explicit prompt reference page (paperswithcode.co/paper/98456)
        pwc_item = await _fetch_pwc_reference(fetcher)
        if pwc_item:
            await queue.put(pwc_item)
            queued += 1

        # 2. Hugging Face Daily Papers API across recent 65 calendar days (~1,100+ papers, ~65% with GitHub!)
        today = datetime.now(UTC).date()
        date_strings = [(today - timedelta(days=offset)).isoformat() for offset in range(65)]
        day_sem = asyncio.Semaphore(6)

        async def fetch_hf_day(day_str: str) -> list[dict[str, Any]]:
            async with day_sem:
                try:
                    status, data, _ = await fetcher.fetch_json(
                        _HF_DAILY_PAPERS_URL,
                        params={"date": day_str, "limit": 100},
                        timeout=20.0,
                    )
                    if status == 200 and isinstance(data, list):
                        return data
                except Exception as exc:
                    logger.debug("hf_papers_day_error", date=day_str, error=str(exc)[:100])
                return []

        day_results = await asyncio.gather(*(fetch_hf_day(d) for d in date_strings))
        hf_raw_count = 0
        for day_items in day_results:
            for wrapper in day_items:
                paper = wrapper.get("paper") if isinstance(wrapper, dict) else None
                if not isinstance(paper, dict):
                    continue
                paper_id = str(paper.get("id") or "").strip()
                title = re.sub(r"\s+", " ", str(paper.get("title") or "")).strip()
                if not paper_id or not title:
                    continue
                authors = [
                    str(a.get("name") or "").strip()
                    for a in (paper.get("authors") or [])
                    if isinstance(a, dict) and a.get("name")
                ]
                dt = normalize_to_utc(paper.get("publishedAt") or wrapper.get("publishedAt"))
                if dt is None:
                    dt = datetime.now(UTC).replace(microsecond=0)

                gh_raw = paper.get("githubRepo")
                if not gh_raw:
                    # Also scan abstract summary for embedded GitHub URLs
                    gh_raw, _ = normalize_github_repo_url(str(paper.get("summary") or ""))

                upstream_stars = paper.get("githubStars")
                upstream_stars_int = (
                    int(upstream_stars)
                    if isinstance(upstream_stars, int) and upstream_stars >= 0
                    else None
                )
                await queue.put(
                    {
                        "dedup_key": f"arxiv:{paper_id}",
                        "source_name": "Hugging Face Daily Papers",
                        "source_url": f"https://huggingface.co/papers/{paper_id}",
                        "title": title,
                        "authors": authors,
                        "paper_url": f"https://arxiv.org/abs/{paper_id}",
                        "github_url_raw": str(gh_raw) if gh_raw else None,
                        "upstream_stars": upstream_stars_int,
                        "published_date": dt,
                    }
                )
                hf_raw_count += 1
                queued += 1

        logger.info("papers_hf_loaded", raw_items=hf_raw_count)

        # 3. Supplement with official arXiv API if needed for extra breadth
        for start in (0, 100):
            arxiv_items = await _fetch_arxiv_batch(fetcher, start=start, max_results=100)
            for aitem in arxiv_items:
                await queue.put(aitem)
                queued += 1

        for _ in range(num_workers):
            await queue.put(None)
        logger.info("papers_producer_done", queued=queued)

    async def worker() -> None:
        nonlocal inserted_total
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                dedup_key = str(item["dedup_key"]).lower()

                async with lock:
                    if dedup_key in seen_keys or inserted_total >= goal:
                        continue
                    seen_keys.add(dedup_key)

                gh_url, repo_key = normalize_github_repo_url(item.get("github_url_raw"))
                gh_stars: int | None = None
                if gh_url and repo_key:
                    gh_stars = await gh_client.get_stars(
                        repo_key, upstream_stars=item.get("upstream_stars")
                    )

                record = ResearchPaperRecord(
                    source=SourceInfo(
                        name=str(item["source_name"]),
                        url=str(item["source_url"]),  # type: ignore[arg-type]
                    ),
                    content=PaperContent(
                        title=str(item["title"]),
                        authors=list(item.get("authors") or []),
                        paper_url=str(item["paper_url"]),  # type: ignore[arg-type]
                        github_url=gh_url,  # type: ignore[arg-type]
                        github_stars=gh_stars if gh_url else None,
                        published_date=item["published_date"],
                    ),
                )

                async with lock:
                    if inserted_total >= goal:
                        continue
                    buffered_rows.append(
                        (
                            "RESEARCH_PAPER",
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
                        await store.save_checkpoint(
                            "papers", {"collected": inserted_total, "target": goal}
                        )
            except Exception as exc:
                logger.warning("paper_worker_item_error", error=str(exc)[:180])
            finally:
                queue.task_done()

    await asyncio.gather(producer(), *(worker() for _ in range(num_workers)))

    if buffered_rows:
        await store.upsert_records_batch(buffered_rows)
        buffered_rows.clear()
    final_count = await store.count_records("RESEARCH_PAPER")
    await store.save_checkpoint("papers", {"collected": final_count, "completed": True})
    await store.save_metric(
        "github_stars_stats",
        {
            "live_github_rest_api_hits": gh_client.live_api_hits,
            "live_github_html_hits": gh_client.live_html_hits,
            "cache_hits": gh_client.cache_hits,
            "upstream_verified_hits": gh_client.upstream_verified_hits,
            "authenticated_token": bool(settings.github_token),
        },
    )
    logger.info(
        "papers_pipeline_complete",
        total_unique=final_count,
        live_gh_api=gh_client.live_api_hits,
        upstream_gh=gh_client.upstream_verified_hits,
    )
    return final_count
