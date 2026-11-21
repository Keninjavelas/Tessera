"""Unit & integration tests for UTC date normalization, HTML metadata fallbacks, 24h cutoff, and heuristic freshness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera.storage.state_store import StateStore, canonicalize_url
from tessera.utils.dates import extract_date_from_html, is_within_hours, normalize_to_utc

REF_NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-21T10:30:00Z", datetime(2026, 11, 21, 10, 30, 0, tzinfo=UTC)),
        ("2026-11-21T05:30:00-05:00", datetime(2026, 11, 21, 10, 30, 0, tzinfo=UTC)),
        ("Fri, 20 Nov 2026 18:00:00 GMT", datetime(2026, 11, 20, 18, 0, 0, tzinfo=UTC)),
        ("2 hours ago", datetime(2026, 11, 21, 10, 0, 0, tzinfo=UTC)),
        ("45m ago", datetime(2026, 11, 21, 11, 15, 0, tzinfo=UTC)),
        ("Just now", REF_NOW),
        ("Yesterday", datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)),
        ("Posted 3 hrs ago", datetime(2026, 11, 21, 9, 0, 0, tzinfo=UTC)),
        ("Nov 20, 2026 3:00 PM EST", datetime(2026, 11, 20, 20, 0, 0, tzinfo=UTC)),
        (int(REF_NOW.timestamp()), REF_NOW),
    ],
)
def test_normalize_to_utc_formats(raw: object, expected: datetime) -> None:
    parsed = normalize_to_utc(raw, reference_now=REF_NOW)  # type: ignore[arg-type]
    assert parsed == expected


def test_html_date_extraction_precedence() -> None:
    html_meta = """
    <html><head>
      <meta property="article:published_time" content="2026-11-21T09:15:00Z" />
    </head><body><p>Hello</p></body></html>
    """
    dt, method = extract_date_from_html(html_meta, reference_now=REF_NOW)
    assert method == "META_TAG"
    assert dt == datetime(2026, 11, 21, 9, 15, 0, tzinfo=UTC)

    html_jsonld = """
    <html><head>
      <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "NewsArticle", "datePublished": "2026-11-21T08:00:00+00:00"}
      </script>
    </head><body></body></html>
    """
    dt2, method2 = extract_date_from_html(html_jsonld, reference_now=REF_NOW)
    assert method2 == "JSON_LD"
    assert dt2 == datetime(2026, 11, 21, 8, 0, 0, tzinfo=UTC)

    html_time = '<html><body><time datetime="2026-11-21T07:30:00Z">4 hours ago</time></body></html>'
    dt3, method3 = extract_date_from_html(html_time, reference_now=REF_NOW)
    assert method3 == "TIME_TAG"
    assert dt3 == datetime(2026, 11, 21, 7, 30, 0, tzinfo=UTC)

    html_none = "<html><body><p>No explicit timestamp on page</p></body></html>"
    dt4, method4 = extract_date_from_html(
        html_none, url="https://blog.example.com/2026/11/21/launch-post", reference_now=REF_NOW
    )
    assert method4 == "URL_PATTERN"
    assert dt4 == datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def test_strict_24h_cutoff() -> None:
    assert is_within_hours(REF_NOW - timedelta(hours=23, minutes=59), hours=24, reference_now=REF_NOW)
    assert not is_within_hours(REF_NOW - timedelta(hours=24, minutes=1), hours=24, reference_now=REF_NOW)
    assert not is_within_hours(REF_NOW + timedelta(hours=2), hours=24, reference_now=REF_NOW)


def test_canonicalize_url_strips_utm_and_fragments() -> None:
    u1 = "https://Example.com/post/123/?utm_source=twitter&id=42&fbclid=abc#section"
    u2 = "https://example.com/post/123?id=42"
    assert canonicalize_url(u1) == canonicalize_url(u2)


@pytest.mark.asyncio
async def test_heuristic_freshness_baseline_and_diff(tmp_path: Path) -> None:
    db = tmp_path / "freshness.db"
    async with StateStore(db) as store:
        t0 = REF_NOW - timedelta(hours=3)
        # First run: establishes baseline -> existing undated URLs are NOT classified as fresh
        fresh0, _, method0 = await store.evaluate_heuristic_freshness(
            "DatelessAIBlog",
            "https://dateless.ai/old-post?utm_source=rss",
            "Old article text",
            reference_now=t0,
        )
        assert fresh0 is False
        assert method0 == "HEURISTIC_BASELINE"
        await store.mark_baseline_initialized("DatelessAIBlog", reference_now=t0)

        # Second run 2 hours later: old post stays stale, new unseen URL is classified fresh (HEURISTIC_DIFF)
        t1 = REF_NOW - timedelta(hours=1)
        fresh_old, _, method_old = await store.evaluate_heuristic_freshness(
            "DatelessAIBlog",
            "https://dateless.ai/old-post",
            "Old article text",
            reference_now=t1,
        )
        assert fresh_old is False
        assert method_old == "HEURISTIC_SEEN_STALE"

        fresh_new, first_seen, method_new = await store.evaluate_heuristic_freshness(
            "DatelessAIBlog",
            "https://dateless.ai/brand-new-post",
            "Brand new AI release",
            reference_now=t1,
        )
        assert fresh_new is True
        assert method_new == "HEURISTIC_DIFF"
        assert first_seen == t1

        # Third run 26 hours after t1: previously new post expires out of 24h window
        t2 = t1 + timedelta(hours=26)
        fresh_expired, _, method_expired = await store.evaluate_heuristic_freshness(
            "DatelessAIBlog",
            "https://dateless.ai/brand-new-post",
            "Brand new AI release",
            reference_now=t2,
        )
        assert fresh_expired is False
        assert method_expired == "HEURISTIC_SEEN_STALE"
