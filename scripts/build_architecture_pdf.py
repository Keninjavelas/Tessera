"""Programmatically compile Tessera's polished <=3-page executive `architecture.pdf` via ReportLab."""

from __future__ import annotations

import sys
from pathlib import Path

from pypdf import PdfReader
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#0F172A")
SLATE = colors.HexColor("#334155")
MUTED = colors.HexColor("#64748B")
ACCENT = colors.HexColor("#2563EB")
TEAL = colors.HexColor("#0D9488")
AMBER = colors.HexColor("#D97706")
LIGHT_BG = colors.HexColor("#F1F5F9")
BORDER = colors.HexColor("#CBD5E1")
WHITE = colors.white


def _build_architecture_diagram() -> Drawing:
    """Create a crisp vector system architecture diagram for Page 1."""
    d = Drawing(516, 152)

    # Background frame
    d.add(Rect(0, 0, 516, 152, rx=6, ry=6, fillColor=LIGHT_BG, strokeColor=BORDER, strokeWidth=0.8))

    def box(x: float, y: float, w: float, h: float, title: str, sub: str, fill: colors.Color) -> None:
        d.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=fill, strokeColor=NAVY, strokeWidth=0.7))
        d.add(String(x + w / 2, y + h - 14, title, textAnchor="middle", fontName="Helvetica-Bold", fontSize=7.5, fillColor=WHITE))
        d.add(String(x + w / 2, y + 6, sub, textAnchor="middle", fontName="Helvetica", fontSize=6.3, fillColor=WHITE))

    def arrow(x1: float, y1: float, x2: float, y2: float, col: colors.Color = SLATE) -> None:
        d.add(Line(x1, y1, x2, y2, strokeColor=col, strokeWidth=1.0))
        # Arrowhead pointing right or down
        if abs(x2 - x1) >= abs(y2 - y1):
            sgn = 1 if x2 > x1 else -1
            d.add(Polygon([x2, y2, x2 - 4 * sgn, y2 + 2.5, x2 - 4 * sgn, y2 - 2.5], fillColor=col, strokeColor=col))
        else:
            sgn = 1 if y2 > y1 else -1
            d.add(Polygon([x2, y2, x2 - 2.5, y2 - 4 * sgn, x2 + 2.5, y2 - 4 * sgn], fillColor=col, strokeColor=col))

    # Row 1: Ingestion Flow
    box(10, 104, 86, 36, "URL Frontier", "YC / HF / arXiv / RSS / ATS", NAVY)
    arrow(96, 122, 114, 122)
    box(114, 104, 88, 36, "Distributed Queue", "Kafka / SQS / Redis Streams", ACCENT)
    arrow(202, 122, 220, 122)
    box(220, 104, 98, 36, "K8s Fetch Workers", "HTTP/2 + Playwright Stealth", ACCENT)
    arrow(318, 122, 336, 122)
    box(336, 104, 82, 36, "LLM Gateway", "Gemini->Groq->DeepSeek", TEAL)
    arrow(418, 122, 434, 122)
    box(434, 104, 72, 36, "Entity Resolver", "Deterministic 5-Stage", TEAL)

    # Row 2: Control Plane & Polyglot Persistence
    box(10, 22, 96, 36, "Dead-Letter Queue", "Exhausted 429 / Parse Fails", AMBER)
    box(114, 22, 96, 36, "Redis Control Plane", "Bloom Dedup + Lua RPM/TPM", SLATE)
    box(220, 22, 96, 36, "S3 + Parquet Bronze", "Immutable HTML/JSON Replay", SLATE)
    box(326, 22, 86, 36, "PostgreSQL + Vector", "ACID Records + pgvector", NAVY)
    box(420, 22, 86, 36, "Neo4j Graph", "Multi-Hop AI Ecosystem", NAVY)

    # Clean orthogonal connectors (zero arrow crossings)
    arrow(58, 104, 58, 58, AMBER)      # Frontier / Retries -> DLQ
    arrow(158, 104, 158, 58, SLATE)    # Queue -> Redis Control Plane
    arrow(269, 104, 269, 58, SLATE)    # Workers -> Bronze S3
    # LLM Gateway -> Redis Control Plane (orthogonal elbow in gutter y=80)
    d.add(Line(368, 104, 368, 84, strokeColor=AMBER, strokeWidth=0.9))
    d.add(Line(368, 84, 182, 84, strokeColor=AMBER, strokeWidth=0.9))
    arrow(182, 84, 182, 58, AMBER)
    # Resolver -> PostgreSQL & Neo4j (clean T-split at y=74)
    arrow(463, 104, 463, 58, SLATE)
    d.add(Line(463, 74, 369, 74, strokeColor=SLATE, strokeWidth=0.9))
    arrow(369, 74, 369, 58, SLATE)

    d.add(String(258, 6, "Figure 1: Tessera Horizontal Scale Architecture (Single-Node SQLite/Queue -> Distributed K8s/Kafka/Polyglot Mesh)", textAnchor="middle", fontName="Helvetica-Oblique", fontSize=6.8, fillColor=MUTED))
    return d


def build_pdf(output_path: Path) -> int:
    """Build architecture.pdf and verify strictly <= 3 pages."""
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title="Tessera — Production AI Intelligence Graph Architecture",
        author="Tessera Systems Engineering",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16.5,
        leading=20,
        textColor=NAVY,
        alignment=0,
        spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "SubtitleCustom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=MUTED,
        spaceAfter=8,
    )
    h1_style = ParagraphStyle(
        "H1Custom",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=14,
        textColor=ACCENT,
        spaceBefore=8,
        spaceAfter=3,
    )
    h2_style = ParagraphStyle(
        "H2Custom",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        textColor=NAVY,
        spaceBefore=5,
        spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "BodyCustom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.6,
        leading=11.8,
        textColor=SLATE,
        spaceAfter=4,
    )
    bullet_style = ParagraphStyle(
        "BulletCustom",
        parent=body_style,
        leftIndent=12,
        firstLineIndent=-8,
        spaceAfter=2.5,
    )

    def _footer(canvas: object, doc_obj: object) -> None:
        c = canvas
        c.saveState()  # type: ignore[attr-defined]
        c.setStrokeColor(BORDER)  # type: ignore[attr-defined]
        c.setLineWidth(0.5)  # type: ignore[attr-defined]
        c.line(0.65 * inch, 0.42 * inch, LETTER[0] - 0.65 * inch, 0.42 * inch)  # type: ignore[attr-defined]
        c.setFont("Helvetica", 7.5)  # type: ignore[attr-defined]
        c.setFillColor(MUTED)  # type: ignore[attr-defined]
        c.drawString(0.65 * inch, 0.28 * inch, "Tessera — Production AI Intelligence Ingestion Pipeline | Technical Architecture")  # type: ignore[attr-defined]
        c.drawRightString(LETTER[0] - 0.65 * inch, 0.28 * inch, f"Page {doc_obj.page}")  # type: ignore[attr-defined]
        c.restoreState()  # type: ignore[attr-defined]

    story: list[object] = []

    # Header
    story.append(Paragraph("Tessera: Production AI Intelligence Graph Architecture", title_style))
    story.append(
        Paragraph(
            "Asynchronous Ingestion, Multi-Tier LLM Extraction, Anti-Bot Resilience & Deterministic Entity Resolution (500K+ Scale Blueprint)",
            subtitle_style,
        )
    )
    story.append(_build_architecture_diagram())
    story.append(Spacer(1, 6))

    # SECTION 1
    story.append(Paragraph("1. Scale Strategy: 500,000+ Records Without Code Changes", h1_style))
    story.append(
        Paragraph(
            "Tessera is architected around strict interface boundaries—<b>BaseFetcher</b>, <b>Queue</b> (Producer -> Buffer -> Worker), "
            "<b>LLMOrchestrator</b>, <b>EntityResolver</b>, and <b>StateStore</b>—so migrating from the local 3,000+ record SQLite/asyncio prototype "
            "to a 500,000+ record distributed cluster requires swapping infrastructure adapters only, with zero changes to extraction or schema logic.",
            body_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Decoupled Producer-Queue-Worker Topology:</b> Lightweight URL Frontier schedulers push canonical crawl tasks into partitioned "
            "<b>Kafka / SQS / Redis Streams</b> topics. Stateless Kubernetes worker pods consume batches partitioned by target domain "
            "(<i>hash(domain) % N</i>), preserving per-domain politeness caps without cross-pod coordination overhead.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Throughput & Capacity Math (500,000 Records / 6-Hour SLA):</b> Sustained rate is <b>23.2 records/sec</b> (~50 req/sec peak). "
            "One async Python worker pod running <b>httpx HTTP/2</b> (<i>max_concurrency=30</i>, median RTT 350 ms) sustains ~85 req/sec. "
            "Just <b>4–6 fetch pods</b> (0.5 vCPU, 512 MB RAM) handle raw network I/O, while failed tasks route cleanly to a Dead-Letter Queue (DLQ).",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Enterprise Anti-Bot & Browser Tiering:</b> 85–90% of targets resolve via HTTP/2. When <b>SmartFetcher</b> detects HTTP 403, "
            "Cloudflare/Datadome challenges, or empty SPA shells, it escalates to an isolated <b>Playwright Chromium pool</b> that aborts images, "
            "fonts, and media (&gt;70% bandwidth reduction) with stealth init scripts. Enterprise scale adds TLS fingerprint impersonation "
            "(<i>curl_cffi</i> JA3/JA4), residential sticky proxy pools, and direct XHR/GraphQL response interception.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Hybrid Deterministic + LLM Efficiency:</b> Structured API fields (YC firmographics, arXiv authors, GitHub stars, ATS timestamps) "
            "bypass the LLM completely. Reserving LLMs strictly for unstructured prose (~12% of records) cuts inference spend by <b>~88%</b> "
            "(~60K LLM calls vs 500K, costing ~$10–$15 per full backfill on Gemini Flash / Groq <i>openai/gpt-oss-20b</i>).",
            bullet_style,
        )
    )

    # SECTION 2
    story.append(Paragraph("2. Handling HTTP 413s & 429s at Massive Concurrency", h1_style))
    story.append(
        Paragraph(
            "In-process rate limiters break under horizontal scaling because independent pods exhaust shared LLM quotas simultaneously. "
            "Production Tessera centralizes all model traffic through an asynchronous <b>LLM Gateway</b>:",
            body_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Redis Distributed Token Buckets (Anti-429):</b> An atomic Redis Lua script enforces sliding-window RPM and TPM limits per "
            "<i>(provider, api_key)</i>. Workers lease tokens before calling upstream APIs, preventing thundering-herd 429 spikes.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Full-Jitter Backoff, Circuit Breakers & 3-Tier Failover:</b> On HTTP 429/503, the gateway honors <i>Retry-After</i> headers; "
            "otherwise computes AWS full jitter <i>sleep = uniform(0, min(cap, base * 2^attempt))</i> to desynchronize retries. After 3 consecutive "
            "failures, a Redis-shared circuit breaker trips (45s cooldown) and fails over immediately: <b>Tier 1 Gemini Flash -> Tier 2 Groq (<i>openai/gpt-oss-20b</i>) -> Tier 3 DeepSeek</b>. "
            "Missing keys auto-skip with telemetry; total exhaustion routes to DLQ (zero fabricated rows).",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Pre-Flight Anti-413 Boilerplate Stripping & Semantic Chunking:</b> Before tokenization, <i>preprocessor.py</i> strips "
            "&lt;script&gt;, &lt;style&gt;, &lt;svg&gt;, &lt;nav&gt;, &lt;footer&gt;, ads, and comments (75–92% byte reduction) via BeautifulSoup/Trafilatura. "
            "If estimated tokens exceed budget (6,000 tokens), semantic prioritization retains metadata, H1/H2 headers, opening context, and "
            "keyword-dense paragraphs (pricing/remote/date cues). On a reactive 413, Tessera halves the budget and retries automatically.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Schema Repair & Batch API Routing:</b> Strict JSON mode + Pydantic v2 validation executes an automatic 1-shot repair retry "
            "feeding exact validation errors back to the model. Non-urgent Phase I enrichment routes to asynchronous Batch APIs (50% lower cost, "
            "isolated quota), preserving live RPM for strict &lt;24h Phase II news and job feeds.",
            bullet_style,
        )
    )

    # SECTION 3 (start cleanly at top of Page 2 so heading is never orphaned)
    story.append(PageBreak())
    story.append(Paragraph("3. Freshness Tracking & Distributed Deduplication", h1_style))
    story.append(
        Paragraph(
            "Ensuring <b>0% duplicates</b> and <b>strict 24-hour freshness</b> across concurrent crawlers relies on four deterministic controls:",
            body_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Canonical URL Normalization:</b> Lowercase scheme/host, strip URL fragments, sort query keys, and strip tracking parameters "
            "(<i>utm_*</i>, <i>fbclid</i>, <i>gclid</i>, <i>ref</i>, <i>gh_src</i>) prior to hashing.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Two-Tier Deduplication + SimHash:</b> Tier 1 uses a <b>Redis Bloom Filter</b> (<i>BF.EXISTS</i>, ~1.2 MB per 1M URLs at 0.1% FP) "
            "for sub-millisecond negative checks. Tier 2 checks PostgreSQL SHA-256 <i>(record_type, canonical_url)</i> indexes plus a 64-bit "
            "<b>SimHash</b> content digest to detect syndicated news stories republished under different URLs.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Distributed Lease Locks:</b> Workers acquire a short TTL lock (<i>SET lock:{url_hash} worker_id NX PX 60000</i>) before "
            "fetching/extracting, guaranteeing two pods never double-scrape or double-bill LLM tokens for the same breaking article.",
            bullet_style,
        )
    )
    story.append(
        Paragraph(
            "&bull; <b>Multi-Fallback Date Engine & Heuristic Diff Ledger:</b> Dates normalize to UTC ISO-8601 across RFC-2822, relative strings "
            "(<i>'2 hours ago'</i>, <i>'Yesterday'</i>), HTML <i>&lt;meta&gt;</i>, JSON-LD <i>datePublished</i>, <i>&lt;time&gt;</i>, and URL patterns. "
            "Dateless sources use a persistent <i>seen_signals</i> baseline ledger: initial runs establish baseline snapshots; newly appearing unseen "
            "URLs on subsequent runs receive <i>freshness_method = 'HEURISTIC_DIFF'</i> with a strict 24h TTL. Final writes use idempotent "
            "<i>ON CONFLICT DO UPDATE</i> upserts.",
            bullet_style,
        )
    )

    # SECTION 4
    story.append(Paragraph("4. Polyglot Storage & Entity Resolution Strategy", h1_style))
    story.append(
        Paragraph(
            "A single datastore cannot simultaneously optimize ACID auditability, multi-hop graph traversals, vector similarity, and raw HTML replay. "
            "Tessera persists resolved records (canonicalized deterministically in 5 stages: NFKC/suffix strip -> Exact/Alias -> Domain -> "
            "Controlled Rapidfuzz &ge;90 for len&ge;7 with domain-contradiction guard -> Logged Fallback) across four purpose-built storage layers:",
            body_style,
        )
    )

    storage_data = [
        ["Storage Tier", "Engine", "Workload & Architectural Justification"],
        [
            "Bronze Raw\nArchive",
            "S3 / GCS +\nApache Parquet",
            "Immutable raw HTML/JSON payloads partitioned by dt=YYYY-MM-DD/source. Enables schema evolution, prompt iteration, and bug-fix replay across 500K+ records without re-scraping live sites.",
        ],
        [
            "System of\nRecord (OLTP)",
            "PostgreSQL 16\n(ACID + Timescale)",
            "Canonical Startup/Product/Paper/Job/News tables, append-only entity_mapping_log audit trail, and daily time-series metric snapshots (GitHub star velocity, hiring surge deltas).",
        ],
        [
            "Intelligence\nGraph",
            "Neo4j (or PG\nApache AGE)",
            "Index-free adjacency for sub-50ms multi-hop traversals: (Founder)-[:FOUNDED]->(Startup)-[:BUILT]->(Product), (Startup)-[:PUBLISHED]->(Paper)-[:IMPLEMENTED_IN]->(Repo).",
        ],
        [
            "Semantic\nVector Layer",
            "pgvector /\nQdrant",
            "Dense embeddings of abstracts, product specs, and news summaries enabling hybrid semantic search, automated paper-to-product linking, and vector-assisted entity disambiguation.",
        ],
    ]

    t_style = ParagraphStyle("TblCell", parent=body_style, fontSize=8.0, leading=10.5, spaceAfter=0)
    t_hdr = ParagraphStyle("TblHdr", parent=t_style, fontName="Helvetica-Bold", textColor=WHITE)
    wrapped_rows = [
        [Paragraph(c, t_hdr) for c in storage_data[0]],
        *[[Paragraph(c, t_style) for c in row] for row in storage_data[1:]],
    ]

    tbl = Table(wrapped_rows, colWidths=[1.05 * inch, 1.15 * inch, 4.95 * inch], repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_BG]),
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(Spacer(1, 2))
    story.append(tbl)
    story.append(Spacer(1, 6))

    # Operational Summary Callout
    story.append(Paragraph("5. Production Observability & Verification Gates", h2_style))
    story.append(
        Paragraph(
            "Every Tessera run emits structured JSON logs (<i>structlog</i>) capturing per-domain latency, Playwright escalation counts, "
            "LLM tier failover ratios, 413 chunking triggers, and GitHub API rate-limit headroom. Automated post-run CI gates "
            "(<i>scripts/audit_dataset.py</i>) block downstream graph publication if vertical counts drop below SLA, duplicate keys exceed 0, "
            "Phase II freshness violates 24 hours, or live HTTP URL reachability falls below threshold.",
            body_style,
        )
    )

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)

    reader = PdfReader(str(output_path))
    pages = len(reader.pages)
    if pages > 3:
        raise RuntimeError(f"architecture.pdf exceeded strict 3-page limit: {pages} pages!")
    return pages


def main() -> None:
    out = Path("architecture.pdf")
    pages = build_pdf(out)
    print(f"[OK] Built {out.resolve()} ({pages} pages, limit <= 3)")


if __name__ == "__main__":
    main()
    sys.exit(0)
