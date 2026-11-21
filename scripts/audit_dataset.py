"""Automated Tessera Dataset Audit: Volume, Uniqueness, 24h Freshness, Live URL Check & PDF Gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from pypdf import PdfReader
from rich.console import Console
from rich.table import Table

from tessera.config import get_settings
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.storage.state_store import StateStore
from tessera.utils.dates import is_within_hours, normalize_to_utc

console = Console()

EXPECTED_TABS = [
    "Startups",
    "Products",
    "Research Papers",
    "Jobs",
    "News",
    "Entity Mapping Log",
]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


async def _verify_urls(urls: list[tuple[str, str]]) -> tuple[int, int, list[tuple[str, str, int]]]:
    fetcher = HttpFetcher(max_concurrency=10, per_domain_concurrency=3, timeout_seconds=15.0)
    try:
        results = await asyncio.gather(*(fetcher.head_ok(u) for _, u in urls))
    finally:
        await fetcher.close()

    ok_count = 0
    failures: list[tuple[str, str, int]] = []
    for (tab, url), (ok, code) in zip(urls, results, strict=True):
        if ok:
            ok_count += 1
        else:
            failures.append((tab, url, code))
    return ok_count, len(urls), failures


async def run_audit(export_dir: Path, pdf_path: Path, sample_size: int) -> int:
    settings = get_settings()
    jsonl_dir = export_dir / "jsonl"
    xlsx_path = export_dir / "tessera_output.xlsx"
    now = datetime.now(UTC)
    gates_passed = True

    startups = _load_jsonl(jsonl_dir / "startups.jsonl")
    products = _load_jsonl(jsonl_dir / "products.jsonl")
    papers = _load_jsonl(jsonl_dir / "research_papers.jsonl")
    jobs = _load_jsonl(jsonl_dir / "jobs.jsonl")
    news = _load_jsonl(jsonl_dir / "news.jsonl")
    mappings = _load_jsonl(jsonl_dir / "entity_mapping_log.jsonl")

    # 1. Volume checks
    vol_checks = [
        ("Startups >= 1,000", len(startups) >= 1000, f"{len(startups):,} rows"),
        ("Products >= 1,000", len(products) >= 1000, f"{len(products):,} rows"),
        ("Research Papers >= 1,000", len(papers) >= 1000, f"{len(papers):,} rows"),
        ("Jobs > 0 across >=5 boards", len(jobs) > 0 and len({r["source"]["name"] for r in jobs}) >= 5,
         f"{len(jobs)} rows across {len({r['source']['name'] for r in jobs})} boards"),
        ("News > 0 across >=5 sources", len(news) > 0 and len({r["source"]["name"] for r in news}) >= 5,
         f"{len(news)} rows across {len({r['source']['name'] for r in news})} sources"),
        ("Entity Mapping Log populated", len(mappings) > 0, f"{len(mappings):,} mappings"),
    ]

    # 2. Uniqueness checks (0 duplicates per vertical)
    startup_keys = [r["content"]["entityName"].lower() for r in startups]
    product_keys = [
        f"{r['content']['startupName'].lower()}::{r['content']['productName'].lower()}" for r in products
    ]
    paper_keys = [r["content"]["paper_url"].lower() for r in papers]
    job_keys = [r["source"]["url"].lower() for r in jobs]
    news_keys = [r["source"]["url"].lower() for r in news]

    dup_counts = {
        "Startups": len(startup_keys) - len(set(startup_keys)),
        "Products": len(product_keys) - len(set(product_keys)),
        "Research Papers": len(paper_keys) - len(set(paper_keys)),
        "Jobs": len(job_keys) - len(set(job_keys)),
        "News": len(news_keys) - len(set(news_keys)),
    }
    total_dups = sum(dup_counts.values())

    # 3. Strict 24h freshness verification for Jobs & News
    stale_jobs = [
        r for r in jobs
        if not (
            (dt := normalize_to_utc(r["content"]["date"])) is not None
            and is_within_hours(dt, hours=24, reference_now=now)
        )
    ]
    stale_news = [
        r for r in news
        if not (
            (dt := normalize_to_utc(r["content"]["published_date"])) is not None
            and is_within_hours(dt, hours=24, reference_now=now)
        )
    ]

    # 4. GitHub URL & star coverage on Research Papers
    papers_with_gh = [r for r in papers if r["content"].get("github_url")]
    papers_with_stars = [
        r for r in papers_with_gh if isinstance(r["content"].get("github_stars"), int)
    ]
    gh_pct = (len(papers_with_gh) / max(len(papers), 1)) * 100.0
    stars_pct = (len(papers_with_stars) / max(len(papers_with_gh), 1)) * 100.0

    # 5. Workbook 6-tab verification
    xlsx_ok = False
    xlsx_detail = "Missing"
    if xlsx_path.exists():
        wb = load_workbook(xlsx_path, read_only=True)
        tabs = wb.sheetnames
        wb.close()
        xlsx_ok = tabs == EXPECTED_TABS
        xlsx_detail = f"Tabs={tabs}"

    # 6. Architecture PDF page count <= 3
    pdf_ok = False
    pdf_detail = "Missing"
    if pdf_path.exists():
        reader = PdfReader(str(pdf_path))
        pages = len(reader.pages)
        pdf_ok = 1 <= pages <= 3
        pdf_detail = f"{pages} pages (max 3)"

    # 7. Random live HTTP URL verification across all 5 data verticals
    url_pool: list[tuple[str, str]] = []
    for tab, recs in [
        ("Startups", startups),
        ("Products", products),
        ("Research Papers", papers),
        ("Jobs", jobs),
        ("News", news),
    ]:
        if recs:
            per_tab = max(1, sample_size // 5)
            picks = random.sample(recs, min(per_tab, len(recs)))
            for p in picks:
                url_pool.append((tab, str(p["source"]["url"])))

    ok_urls, total_urls, url_failures = await _verify_urls(url_pool)
    url_rate = (ok_urls / max(total_urls, 1)) * 100.0
    url_ok = total_urls > 0 and url_rate >= 90.0

    # Load telemetry metrics from SQLite
    metrics: dict[str, Any] = {}
    if settings.db_path.exists():
        async with StateStore(settings.db_path) as store:
            metrics = await store.load_all_metrics()

    # Build rich audit table
    table = Table(title=f"Tessera Automated Audit Report ({now.strftime('%Y-%m-%dT%H:%M:%SZ')})")
    table.add_column("Verification Gate", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Details", style="white")

    all_gates: list[tuple[str, bool, str]] = [
        *vol_checks,
        ("Zero Duplicates Per Vertical", total_dups == 0, str(dup_counts)),
        ("100% Jobs Fresh (<24h)", len(stale_jobs) == 0 and len(jobs) > 0, f"{len(jobs) - len(stale_jobs)}/{len(jobs)} within 24h"),
        ("100% News Fresh (<24h)", len(stale_news) == 0 and len(news) > 0, f"{len(news) - len(stale_news)}/{len(news)} within 24h"),
        (
            "Paper GitHub & Live Stars Coverage",
            len(papers_with_stars) >= 200,
            f"{len(papers_with_gh)}/{len(papers)} ({gh_pct:.1f}%) with GitHub URL; {len(papers_with_stars)} ({stars_pct:.1f}%) with integer stars",
        ),
        ("6-Tab Excel Workbook Valid", xlsx_ok, xlsx_detail),
        ("Live URL Reachability Sample", url_ok, f"{ok_urls}/{total_urls} ({url_rate:.1f}%) reachable"),
        ("architecture.pdf <= 3 Pages", pdf_ok, pdf_detail),
    ]

    for label, passed, detail in all_gates:
        if not passed:
            gates_passed = False
        status_str = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        table.add_row(label, status_str, detail)

    console.print(table)

    if url_failures:
        console.print("[yellow]Sample URLs returning non-2xx/3xx:[/yellow]")
        for tab, u, code in url_failures[:10]:
            console.print(f"  - [{tab}] HTTP {code}: {u}")

    if metrics:
        console.print("\n[bold cyan]Pipeline Telemetry & Provider Stats:[/bold cyan]")
        console.print_json(json.dumps(metrics, indent=2))

    # Pricing & Role distributions
    if products:
        pdist: dict[str, int] = {}
        for r in products:
            pm = r["content"]["pricingModel"]
            pdist[pm] = pdist.get(pm, 0) + 1
        console.print(f"[bold]Product Pricing Distribution:[/bold] {pdist}")

    if mappings:
        mdist: dict[str, int] = {}
        for m in mappings:
            mm = m["match_method"]
            mdist[mm] = mdist.get(mm, 0) + 1
        console.print(f"[bold]Entity Resolution Methods:[/bold]   {mdist}")

    if gates_passed:
        console.print("\n[bold green]ALL TESSERA VERIFICATION GATES PASSED.[/bold green]")
        return 0
    console.print("\n[bold red]ONE OR MORE AUDIT GATES FAILED.[/bold red]")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Tessera exported datasets and architecture PDF.")
    parser.add_argument("--export-dir", type=Path, default=Path("exports"))
    parser.add_argument("--pdf", type=Path, default=Path("architecture.pdf"))
    parser.add_argument("--sample-urls", type=int, default=30)
    args = parser.parse_args()
    code = asyncio.run(run_audit(args.export_dir, args.pdf, args.sample_urls))
    sys.exit(code)


if __name__ == "__main__":
    main()
