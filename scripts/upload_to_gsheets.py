"""Optional Google Sheets uploader pushing all 6 Tessera CSV tabs via gspread."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from tessera.config import get_settings

TAB_TO_CSV: list[tuple[str, str]] = [
    ("Startups", "startups.csv"),
    ("Products", "products.csv"),
    ("Research Papers", "research_papers.csv"),
    ("Jobs", "jobs.csv"),
    ("News", "news.csv"),
    ("Entity Mapping Log", "entity_mapping_log.csv"),
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def upload_to_google_sheets(
    *,
    credentials_path: Path,
    spreadsheet_id: str | None,
    spreadsheet_title: str,
    csv_dir: Path,
) -> str:
    """Authenticate with Google Service Account and push all 6 tabs to Google Sheets."""
    creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
    client = gspread.authorize(creds)

    if spreadsheet_id:
        sh = client.open_by_key(spreadsheet_id)
    else:
        sh = client.create(spreadsheet_title)
        sh.share(None, perm_type="anyone", role="reader")

    existing = {ws.title: ws for ws in sh.worksheets()}

    for tab_name, csv_name in TAB_TO_CSV:
        csv_path = csv_dir / csv_name
        if not csv_path.exists():
            print(f"[WARN] Skipping missing CSV: {csv_path}")
            continue
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if not rows:
            continue

        num_rows = max(len(rows), 2)
        num_cols = max(len(rows[0]), 1)
        if tab_name in existing:
            ws = existing[tab_name]
            ws.clear()
            ws.resize(rows=num_rows, cols=num_cols)
        else:
            ws = sh.add_worksheet(title=tab_name, rows=num_rows, cols=num_cols)

        ws.update(values=rows, range_name="A1")
        ws.freeze(rows=1)
        print(f"[OK] Uploaded {len(rows) - 1} rows -> '{tab_name}'")

    # Remove default empty Sheet1 if unused
    if "Sheet1" in existing and len(sh.worksheets()) > 1:
        sh.del_worksheet(existing["Sheet1"])

    return str(sh.url)


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Upload Tessera 6-tab exports to Google Sheets.")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(settings.google_sheets_credentials_path)
        if settings.google_sheets_credentials_path
        else None,
        help="Path to Google Service Account JSON key file.",
    )
    parser.add_argument(
        "--spreadsheet-id",
        type=str,
        default=settings.google_sheets_spreadsheet_id,
        help="Existing Google Spreadsheet ID to update (creates a new sheet if omitted).",
    )
    parser.add_argument("--title", type=str, default="Tessera AI Intelligence Graph Dataset")
    parser.add_argument("--csv-dir", type=Path, default=Path("exports/csv"))
    args = parser.parse_args()

    if not args.credentials or not args.credentials.exists():
        print(
            "ERROR: Provide --credentials /path/to/service_account.json "
            "or set GOOGLE_SHEETS_CREDENTIALS_PATH in .env.\n"
            "Alternatively, import exports/tessera_output.xlsx directly via File -> Import in Google Sheets.",
            file=sys.stderr,
        )
        sys.exit(1)

    url = upload_to_google_sheets(
        credentials_path=args.credentials,
        spreadsheet_id=args.spreadsheet_id,
        spreadsheet_title=args.title,
        csv_dir=args.csv_dir,
    )
    print(f"Google Sheet ready: {url}")


if __name__ == "__main__":
    main()
