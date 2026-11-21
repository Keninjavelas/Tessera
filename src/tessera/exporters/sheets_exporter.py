"""Export Tessera datasets to 6-tab Excel (.xlsx), 6 CSVs, and validated JSONL archives."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from tessera.logging_config import get_logger
from tessera.schemas.entities import (
    EntityMappingLog,
    JobRecord,
    NewsRecord,
    ProductRecord,
    ResearchPaperRecord,
    StartupRecord,
)
from tessera.storage.state_store import StateStore

logger = get_logger(__name__)

# Exact 6 required workbook tab names
TAB_STARTUPS = "Startups"
TAB_PRODUCTS = "Products"
TAB_PAPERS = "Research Papers"
TAB_JOBS = "Jobs"
TAB_NEWS = "News"
TAB_MAPPING = "Entity Mapping Log"


@dataclass
class ExportSummary:
    """Paths and row counts produced by `export_all_datasets`."""

    xlsx_path: Path
    csv_dir: Path
    jsonl_dir: Path
    counts: dict[str, int]


def _flatten_startup(rec: dict[str, Any]) -> dict[str, Any]:
    validated = StartupRecord.model_validate(rec).model_dump(mode="json")
    return {
        "schemaVersion": validated["schemaVersion"],
        "recordType": validated["recordType"],
        "source.name": validated["source"]["name"],
        "source.url": validated["source"]["url"],
        "content.entityName": validated["content"]["entityName"],
        "content.data.employeeCount": validated["content"]["data"]["employeeCount"],
        "collectedAt": validated["collectedAt"],
    }


def _flatten_product(rec: dict[str, Any]) -> dict[str, Any]:
    validated = ProductRecord.model_validate(rec).model_dump(mode="json")
    return {
        "schemaVersion": validated["schemaVersion"],
        "recordType": validated["recordType"],
        "source.name": validated["source"]["name"],
        "source.url": validated["source"]["url"],
        "content.productName": validated["content"]["productName"],
        "content.startupName": validated["content"]["startupName"],
        "content.pricingModel": validated["content"]["pricingModel"],
        "collectedAt": validated["collectedAt"],
    }


def _flatten_paper(rec: dict[str, Any]) -> dict[str, Any]:
    validated = ResearchPaperRecord.model_validate(rec).model_dump(mode="json")
    c = validated["content"]
    return {
        "schemaVersion": validated["schemaVersion"],
        "recordType": validated["recordType"],
        "source.name": validated["source"]["name"],
        "source.url": validated["source"]["url"],
        "content.title": c["title"],
        "content.authors": "; ".join(c["authors"]),
        "content.paper_url": c["paper_url"],
        "content.github_url": c["github_url"],
        "content.github_stars": c["github_stars"],
        "content.published_date": c["published_date"],
        "collectedAt": validated["collectedAt"],
    }


def _flatten_job(rec: dict[str, Any]) -> dict[str, Any]:
    validated = JobRecord.model_validate(rec).model_dump(mode="json")
    c = validated["content"]
    return {
        "schemaVersion": validated["schemaVersion"],
        "recordType": validated["recordType"],
        "source.name": validated["source"]["name"],
        "source.url": validated["source"]["url"],
        "content.title": c["title"],
        "content.company": c["company"],
        "content.date": c["date"],
        "content.is_remote": c["is_remote"],
        "content.role_family": c["role_family"],
        "collectedAt": validated["collectedAt"],
    }


def _flatten_news(rec: dict[str, Any]) -> dict[str, Any]:
    validated = NewsRecord.model_validate(rec).model_dump(mode="json")
    c = validated["content"]
    return {
        "schemaVersion": validated["schemaVersion"],
        "recordType": validated["recordType"],
        "source.name": validated["source"]["name"],
        "source.url": validated["source"]["url"],
        "content.title": c["title"],
        "content.published_date": c["published_date"],
        "content.summary": c["summary"],
        "content.mentioned_entities": "; ".join(c["mentioned_entities"]),
        "collectedAt": validated["collectedAt"],
    }


def _flatten_mapping(rec: dict[str, Any]) -> dict[str, Any]:
    validated = EntityMappingLog.model_validate(rec).model_dump(mode="json")
    return {
        "raw_name": validated["raw_name"],
        "canonical_name": validated["canonical_name"],
        "match_method": validated["match_method"],
        "confidence": validated["confidence"],
        "record_type": validated["record_type"],
        "source_url": validated["source_url"],
    }


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _sanitize_excel_cell(val: Any) -> Any:
    """Strip illegal XML control characters that break openpyxl serialization."""
    if isinstance(val, str):
        return "".join(ch for ch in val if ch in ("\t", "\n", "\r") or ord(ch) >= 32)
    return val


def _populate_sheet(wb: Workbook, title: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    ws = wb.create_sheet(title=title)
    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    body_font = Font(name="Calibri", size=10, color="1E293B")
    alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    ws.append(columns)
    for col_idx in range(1, len(columns) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")

    text_format_cols = {
        idx
        for idx, name in enumerate(columns, start=1)
        if name in {"schemaVersion", "collectedAt", "content.published_date", "content.date"}
    }

    for row_idx, row in enumerate(rows, start=2):
        values = [_sanitize_excel_cell(row.get(col)) for col in columns]
        ws.append(values)
        is_alt = row_idx % 2 == 0
        for col_idx in range(1, len(columns) + 1):
            c = ws.cell(row=row_idx, column=col_idx)
            c.font = body_font
            if col_idx in text_format_cols:
                c.number_format = "@"
            if is_alt:
                c.fill = alt_fill

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(1, len(rows) + 1)}"

    # Set readable column widths based on header + sample rows
    for col_idx, col_name in enumerate(columns, start=1):
        sample_lens = [len(col_name)] + [
            len(str(r.get(col_name) or "")) for r in rows[:50]
        ]
        width = min(max(sample_lens) + 4, 52)
        ws.column_dimensions[get_column_letter(col_idx)].width = max(width, 14)


async def export_all_datasets(store: StateStore, export_dir: Path) -> ExportSummary:
    """Validate and export all 6 datasets to `.xlsx`, CSVs, and nested JSONL archives."""
    csv_dir = export_dir / "csv"
    jsonl_dir = export_dir / "jsonl"
    csv_dir.mkdir(parents=True, exist_ok=True)
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = export_dir / "tessera_output.xlsx"

    startups_raw = await store.load_records("STARTUP")
    products_raw = await store.load_records("PRODUCT")
    papers_raw = await store.load_records("RESEARCH_PAPER")
    jobs_raw = await store.load_records("JOB")
    news_raw = await store.load_records("NEWS")
    mappings_raw = await store.load_entity_mappings()

    startups_flat = [_flatten_startup(r) for r in startups_raw]
    products_flat = [_flatten_product(r) for r in products_raw]
    papers_flat = [_flatten_paper(r) for r in papers_raw]
    jobs_flat = [_flatten_job(r) for r in jobs_raw]
    news_flat = [_flatten_news(r) for r in news_raw]
    mappings_flat = [_flatten_mapping(r) for r in mappings_raw]

    specs: list[tuple[str, str, list[str], list[dict[str, Any]], list[dict[str, Any]]]] = [
        (
            TAB_STARTUPS,
            "startups",
            [
                "schemaVersion",
                "recordType",
                "source.name",
                "source.url",
                "content.entityName",
                "content.data.employeeCount",
                "collectedAt",
            ],
            startups_flat,
            startups_raw,
        ),
        (
            TAB_PRODUCTS,
            "products",
            [
                "schemaVersion",
                "recordType",
                "source.name",
                "source.url",
                "content.productName",
                "content.startupName",
                "content.pricingModel",
                "collectedAt",
            ],
            products_flat,
            products_raw,
        ),
        (
            TAB_PAPERS,
            "research_papers",
            [
                "schemaVersion",
                "recordType",
                "source.name",
                "source.url",
                "content.title",
                "content.authors",
                "content.paper_url",
                "content.github_url",
                "content.github_stars",
                "content.published_date",
                "collectedAt",
            ],
            papers_flat,
            papers_raw,
        ),
        (
            TAB_JOBS,
            "jobs",
            [
                "schemaVersion",
                "recordType",
                "source.name",
                "source.url",
                "content.title",
                "content.company",
                "content.date",
                "content.is_remote",
                "content.role_family",
                "collectedAt",
            ],
            jobs_flat,
            jobs_raw,
        ),
        (
            TAB_NEWS,
            "news",
            [
                "schemaVersion",
                "recordType",
                "source.name",
                "source.url",
                "content.title",
                "content.published_date",
                "content.summary",
                "content.mentioned_entities",
                "collectedAt",
            ],
            news_flat,
            news_raw,
        ),
        (
            TAB_MAPPING,
            "entity_mapping_log",
            [
                "raw_name",
                "canonical_name",
                "match_method",
                "confidence",
                "record_type",
                "source_url",
            ],
            mappings_flat,
            mappings_raw,
        ),
    ]

    wb = Workbook()
    default_sheet = wb.active
    if default_sheet is not None:
        wb.remove(default_sheet)

    counts: dict[str, int] = {}
    for tab_name, file_stem, cols, flat_rows, nested_rows in specs:
        _populate_sheet(wb, tab_name, cols, flat_rows)
        _write_csv(csv_dir / f"{file_stem}.csv", cols, flat_rows)
        _write_jsonl(jsonl_dir / f"{file_stem}.jsonl", nested_rows)
        counts[tab_name] = len(flat_rows)

    wb.save(xlsx_path)
    logger.info("exports_written", xlsx=str(xlsx_path), counts=counts)
    return ExportSummary(xlsx_path=xlsx_path, csv_dir=csv_dir, jsonl_dir=jsonl_dir, counts=counts)
