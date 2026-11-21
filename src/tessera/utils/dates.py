"""Custom UTC ISO-8601 date normalizer supporting relative dates, timezones, and HTML fallbacks."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateutil_parser

# Common timezone abbreviations mapped to UTC offsets in seconds
_TZINFOS: dict[str, timezone] = {
    "UTC": UTC,
    "GMT": UTC,
    "Z": UTC,
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "MST": timezone(timedelta(hours=-7)),
    "MDT": timezone(timedelta(hours=-6)),
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "BST": timezone(timedelta(hours=1)),
    "CET": timezone(timedelta(hours=1)),
    "CEST": timezone(timedelta(hours=2)),
    "IST": timezone(timedelta(hours=5, minutes=30)),
    "JST": timezone(timedelta(hours=9)),
    "AEST": timezone(timedelta(hours=10)),
}

_RELATIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:just\s+now|now|moments?\s+ago|a\s+few\s+seconds?\s+ago)$", re.I), "now"),
    (re.compile(r"^today$", re.I), "today"),
    (re.compile(r"^yesterday$", re.I), "yesterday"),
    (
        re.compile(
            r"^(?:about\s+|~)?(\d+|a|an)\s*(s|sec|secs|second|seconds)\s*(?:ago)?$",
            re.I,
        ),
        "seconds",
    ),
    (
        re.compile(
            r"^(?:about\s+|~)?(\d+|a|an)\s*(m|min|mins|minute|minutes)\s*(?:ago)?$",
            re.I,
        ),
        "minutes",
    ),
    (
        re.compile(
            r"^(?:about\s+|~)?(\d+|a|an)\s*(h|hr|hrs|hour|hours)\s*(?:ago)?$",
            re.I,
        ),
        "hours",
    ),
    (
        re.compile(
            r"^(?:about\s+|~)?(\d+|a|an)\s*(d|day|days)\s*(?:ago)?$",
            re.I,
        ),
        "days",
    ),
    (
        re.compile(
            r"^(?:about\s+|~)?(\d+|a|an)\s*(w|wk|wks|week|weeks)\s*(?:ago)?$",
            re.I,
        ),
        "weeks",
    ),
    (
        re.compile(
            r"^(?:about\s+|~)?(\d+|a|an)\s*(mo|mos|month|months)\s*(?:ago)?$",
            re.I,
        ),
        "months",
    ),
]

_URL_DATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)"),
    re.compile(r"[/_-](\d{4})-(\d{2})-(\d{2})(?:[/_.-]|$)"),
]

_META_DATE_SELECTORS: list[tuple[str, str]] = [
    ("property", "article:published_time"),
    ("property", "og:published_time"),
    ("name", "article:published_time"),
    ("name", "pubdate"),
    ("name", "publish-date"),
    ("name", "publish_date"),
    ("name", "date"),
    ("name", "DC.date.issued"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
    ("itemprop", "datePublished"),
    ("property", "article:modified_time"),
]


def _parse_relative(text: str, ref: datetime) -> datetime | None:
    """Parse relative English date expressions like '2 hours ago', '45m ago', 'Yesterday'."""
    cleaned = re.sub(r"^(?:posted|published|updated)\s+", "", text.strip(), flags=re.I).strip()
    for pattern, unit in _RELATIVE_PATTERNS:
        m = pattern.match(cleaned)
        if not m:
            continue
        if unit == "now":
            return ref
        if unit == "today":
            return ref.replace(hour=0, minute=0, second=0, microsecond=0)
        if unit == "yesterday":
            return ref - timedelta(days=1)
        qty_raw = m.group(1).lower()
        qty = 1 if qty_raw in ("a", "an") else int(qty_raw)
        if unit == "seconds":
            return ref - timedelta(seconds=qty)
        if unit == "minutes":
            return ref - timedelta(minutes=qty)
        if unit == "hours":
            return ref - timedelta(hours=qty)
        if unit == "days":
            return ref - timedelta(days=qty)
        if unit == "weeks":
            return ref - timedelta(weeks=qty)
        if unit == "months":
            return ref - timedelta(days=qty * 30)
    return None


def normalize_to_utc(
    value: datetime | int | float | str | None,
    *,
    reference_now: datetime | None = None,
) -> datetime | None:
    """Parse any date representation (ISO, RFC-2822, epoch, relative, human) to UTC datetime."""
    if value is None:
        return None

    ref = reference_now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    else:
        ref = ref.astimezone(UTC)

    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).replace(microsecond=0)

    if isinstance(value, (int, float)):
        ts = float(value)
        # Heuristic: milliseconds epoch > 1e12
        if ts > 1e12:
            ts /= 1000.0
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=UTC).replace(microsecond=0)

    raw = value.strip()
    if not raw:
        return None

    # Numeric epoch string
    if re.fullmatch(r"\d{10,13}", raw):
        return normalize_to_utc(int(raw), reference_now=ref)

    # 1. Relative expressions ("2 hours ago", "45m ago", "Just now", "Yesterday")
    rel = _parse_relative(raw, ref)
    if rel is not None:
        return rel.astimezone(UTC).replace(microsecond=0)

    # 2. Fast ISO-8601 path
    iso_candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).replace(microsecond=0)
    except ValueError:
        pass

    # 3. RFC-2822 (RSS pubDate — strictly 24-hour clock, never AM/PM)
    if not re.search(r"\b(?:am|pm)\b", raw, re.I):
        try:
            dt_rfc = parsedate_to_datetime(raw)
            if dt_rfc is not None:
                if dt_rfc.tzinfo is None:
                    dt_rfc = dt_rfc.replace(tzinfo=UTC)
                return dt_rfc.astimezone(UTC).replace(microsecond=0)
        except (TypeError, ValueError):
            pass

    # 4. General human-readable dates via dateutil
    try:
        cleaned = re.sub(r"^(?:posted|published|updated)\s*(?:on|:)?\s*", "", raw, flags=re.I)
        dt_parsed = dateutil_parser.parse(cleaned, tzinfos=_TZINFOS, fuzzy=True)
        if dt_parsed.tzinfo is None:
            dt_parsed = dt_parsed.replace(tzinfo=UTC)
        return dt_parsed.astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError, TypeError):
        return None


def _find_jsonld_date(node: Any) -> str | None:
    """Recursively search JSON-LD objects/arrays for standard publication date fields."""
    if isinstance(node, dict):
        for key in ("datePublished", "datePosted", "dateCreated", "uploadDate"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for child in node.values():
            found = _find_jsonld_date(child)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_jsonld_date(item)
            if found:
                return found
    return None


def extract_date_from_html(
    html: str,
    url: str | None = None,
    *,
    reference_now: datetime | None = None,
) -> tuple[datetime | None, str]:
    """Extract publication timestamp from HTML metadata, JSON-LD, <time>, or URL patterns.

    Returns a tuple of `(utc_datetime_or_none, extraction_method)`.
    """
    soup = BeautifulSoup(html, "lxml")

    # 1. Meta tags
    for attr_name, attr_val in _META_DATE_SELECTORS:
        tag = soup.find("meta", attrs={attr_name: re.compile(f"^{re.escape(attr_val)}$", re.I)})
        if isinstance(tag, Tag):
            content = tag.get("content")
            if isinstance(content, str) and content.strip():
                dt = normalize_to_utc(content, reference_now=reference_now)
                if dt is not None:
                    return dt, "META_TAG"

    # 2. JSON-LD blocks
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw_json = script.string or script.get_text()
        if not raw_json or not raw_json.strip():
            continue
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        date_str = _find_jsonld_date(parsed)
        if date_str:
            dt = normalize_to_utc(date_str, reference_now=reference_now)
            if dt is not None:
                return dt, "JSON_LD"

    # 3. <time datetime="..."> tags
    for time_tag in soup.find_all("time"):
        dt_attr = time_tag.get("datetime")
        candidate = dt_attr if isinstance(dt_attr, str) and dt_attr.strip() else time_tag.get_text(" ", strip=True)
        if candidate:
            dt = normalize_to_utc(candidate, reference_now=reference_now)
            if dt is not None:
                return dt, "TIME_TAG"

    # 4. URL date pattern fallback (/YYYY/MM/DD/)
    if url:
        for pattern in _URL_DATE_PATTERNS:
            m = pattern.search(url)
            if m:
                try:
                    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if 2015 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31:
                        return datetime(year, month, day, 12, 0, 0, tzinfo=UTC), "URL_PATTERN"
                except ValueError:
                    continue

    return None, "NONE"


def is_within_hours(
    dt: datetime,
    hours: int = 24,
    *,
    reference_now: datetime | None = None,
    future_skew_minutes: int = 15,
    safety_margin_minutes: int = 0,
) -> bool:
    """Return True iff `dt` is strictly within the trailing `hours` window (allowing minor clock skew)."""
    ref = reference_now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    else:
        ref = ref.astimezone(UTC)

    dt_utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    lower = ref - timedelta(hours=hours) + timedelta(minutes=safety_margin_minutes)
    upper = ref + timedelta(minutes=future_skew_minutes)
    return lower <= dt_utc <= upper
