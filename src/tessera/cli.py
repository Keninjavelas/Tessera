"""Unified Tessera CLI (`run-all`, `run-phase1`, `run-phase2`, `export`, `audit`, `demo-browser`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from tessera.config import get_settings
from tessera.exporters.sheets_exporter import export_all_datasets
from tessera.fetchers.browser_fetcher import PlaywrightFetcher, SmartFetcher
from tessera.fetchers.http_fetcher import HttpFetcher
from tessera.llm.orchestrator import LLMOrchestrator
from tessera.logging_config import configure_logging
from tessera.pipelines.jobs import run_jobs_pipeline
from tessera.pipelines.news import run_news_pipeline
from tessera.pipelines.papers import run_papers_pipeline
from tessera.pipelines.products import run_products_pipeline
from tessera.pipelines.startups import run_startups_pipeline
from tessera.resolution.resolver import EntityResolver
from tessera.storage.state_store import StateStore

app = typer.Typer(
    name="tessera",
    help="Tessera — Production AI Intelligence Ingestion, Multi-Tier LLM Extraction & Entity Resolution Pipeline.",
    add_completion=False,
)
console = Console()


async def _exec_phase1(target: int | None) -> dict[str, int]:
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings.log_level, json_logs=True)

    http = HttpFetcher(
        max_concurrency=settings.http_max_concurrency,
        per_domain_concurrency=settings.per_domain_concurrency,
        timeout_seconds=settings.http_timeout_seconds,
    )
    llm = LLMOrchestrator.from_settings(settings)
    resolver = EntityResolver()

    try:
        async with StateStore(settings.db_path) as store:
            startups_n = await run_startups_pipeline(
                settings=settings, store=store, fetcher=http, resolver=resolver, target_count=target
            )
            products_n = await run_products_pipeline(
                settings=settings,
                store=store,
                fetcher=http,
                resolver=resolver,
                llm=llm,
                target_count=target,
            )
            papers_n = await run_papers_pipeline(
                settings=settings, store=store, fetcher=http, target_count=target
            )
            await store.save_metric("llm_stats_phase1", llm.stats.to_dict())
            return {"Startups": startups_n, "Products": products_n, "Research Papers": papers_n}
    finally:
        await http.close()
        await llm.close()


async def _exec_phase2(use_browser_fallback: bool) -> dict[str, int]:
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings.log_level, json_logs=True)

    http = HttpFetcher(
        max_concurrency=settings.http_max_concurrency,
        per_domain_concurrency=settings.per_domain_concurrency,
        timeout_seconds=settings.http_timeout_seconds,
    )
    browser = PlaywrightFetcher(max_pages=2, block_resources=True) if use_browser_fallback else None
    smart = SmartFetcher(http_fetcher=http, browser_fetcher=browser)
    llm = LLMOrchestrator.from_settings(settings)
    resolver = EntityResolver()

    try:
        async with StateStore(settings.db_path) as store:
            news_n = await run_news_pipeline(
                settings=settings, store=store, fetcher=smart, llm=llm, resolver=resolver
            )
            jobs_n = await run_jobs_pipeline(
                settings=settings, store=store, fetcher=http, llm=llm, resolver=resolver
            )
            await store.save_metric("llm_stats_phase2", llm.stats.to_dict())
            await store.save_metric("browser_fallbacks", smart.browser_fallbacks)
            return {"News (<24h)": news_n, "Jobs (<24h)": jobs_n}
    finally:
        await smart.close()
        await llm.close()


async def _exec_export() -> None:
    settings = get_settings()
    settings.ensure_directories()
    async with StateStore(settings.db_path) as store:
        summary = await export_all_datasets(store, settings.export_dir)
    table = Table(title="Tessera Export Summary")
    table.add_column("Tab / Dataset", style="cyan")
    table.add_column("Rows", justify="right", style="green")
    for tab, cnt in summary.counts.items():
        table.add_row(tab, str(cnt))
    console.print(table)
    console.print(f"[bold green]Excel workbook:[/bold green] {summary.xlsx_path.resolve()}")
    console.print(f"[bold green]CSV directory:[/bold green]  {summary.csv_dir.resolve()}")
    console.print(f"[bold green]JSONL directory:[/bold green] {summary.jsonl_dir.resolve()}")


@app.command("run-phase1")
def run_phase1(
    target: int = typer.Option(1060, help="Target unique record count per Phase I vertical."),
) -> None:
    """Run Phase I massive bulk acquisition (Startups, Products, Research Papers + GitHub Stars)."""
    counts = asyncio.run(_exec_phase1(target))
    console.print("[bold green]Phase I Complete:[/bold green]", counts)


@app.command("run-phase2")
def run_phase2(
    browser_fallback: bool = typer.Option(
        True, help="Enable Playwright headless browser fallback for JS/403 pages."
    ),
) -> None:
    """Run Phase II high-fidelity <24h signal ingestion (5 AI News Sources + 5+ AI Job Boards)."""
    counts = asyncio.run(_exec_phase2(browser_fallback))
    console.print("[bold green]Phase II Complete:[/bold green]", counts)


@app.command("export")
def export_cmd() -> None:
    """Export SQLite state store to 6-tab Excel (.xlsx), 6 CSVs, and validated JSONL archives."""
    asyncio.run(_exec_export())


@app.command("run-all")
def run_all(
    target: int = typer.Option(1060, help="Target unique record count per Phase I vertical."),
    browser_fallback: bool = typer.Option(True, help="Enable Playwright fallback."),
) -> None:
    """Execute Phase I, Phase II, and full dataset export end-to-end."""
    p1 = asyncio.run(_exec_phase1(target))
    p2 = asyncio.run(_exec_phase2(browser_fallback))
    asyncio.run(_exec_export())
    console.print("[bold green]Tessera Full Run Complete:[/bold green]", {**p1, **p2})


@app.command("demo-browser")
def demo_browser(
    url: str = typer.Option(
        "https://paperswithcode.co/paper/98456",
        help="Target JS-heavy or protected URL to benchmark with PlaywrightFetcher.",
    ),
) -> None:
    """Live demonstration of PlaywrightFetcher with stealth flags and resource blocking."""

    async def _run() -> None:
        bf = PlaywrightFetcher(max_pages=1, block_resources=True)
        try:
            res = await bf.fetch(url)
            console.print(
                f"[bold cyan]PlaywrightFetcher Live Demo[/bold cyan]\n"
                f"  URL:       {res.final_url}\n"
                f"  Status:    {res.status_code}\n"
                f"  Bytes:     {len(res.text):,}\n"
                f"  Latency:   {res.elapsed_ms:.1f} ms\n"
                f"  FetchedBy: {res.fetched_via}"
            )
        finally:
            await bf.close()

    asyncio.run(_run())


@app.command("audit")
def audit_cmd(
    sample_urls: int = typer.Option(30, help="Number of random URLs to verify live over HTTP."),
    pdf_path: Path = typer.Option(Path("architecture.pdf"), help="Path to architecture.pdf."),
) -> None:
    """Run automated fidelity, freshness, uniqueness, URL reachability, and PDF page audit."""
    import subprocess
    import sys

    script = Path("scripts/audit_dataset.py")
    if not script.exists():
        console.print(f"[bold red]Audit script not found:[/bold red] {script}")
        raise typer.Exit(code=1)
    proc = subprocess.run(
        [sys.executable, str(script), "--sample-urls", str(sample_urls), "--pdf", str(pdf_path)],
        check=False,
    )
    raise typer.Exit(code=proc.returncode)


if __name__ == "__main__":
    app()
