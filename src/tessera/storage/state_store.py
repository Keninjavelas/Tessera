"""Async SQLite checkpointing, deduplication hashes, GitHub cache, and heuristic freshness ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiosqlite

from tessera.schemas.entities import EntityMappingLog, format_utc_iso, utc_now

_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "gh_src",
    }
)


def canonicalize_url(url: str) -> str:
    """Normalize URL by lowercasing scheme/host, stripping fragments and UTM tracking query params."""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    filtered_query.sort()
    return urlunparse((scheme, netloc, path, "", urlencode(filtered_query), ""))


def sha256_text(text: str) -> str:
    """Return hex SHA-256 hash of normalized UTF-8 text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class StateStore:
    """Async SQLite store providing crash-resumable checkpoints, record storage, and freshness ledger."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open SQLite connection with WAL mode and ensure all schema tables exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_type TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                source_url TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                PRIMARY KEY (record_type, dedup_key)
            );

            CREATE TABLE IF NOT EXISTS entity_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_name TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                match_method TEXT NOT NULL,
                confidence REAL NOT NULL,
                record_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                UNIQUE (raw_name, canonical_name, record_type, source_url)
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                pipeline_name TEXT PRIMARY KEY,
                cursor_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_baselines (
                source_name TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_signals (
                source_name TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source_name, canonical_url)
            );

            CREATE TABLE IF NOT EXISTS github_stars_cache (
                repo_key TEXT PRIMARY KEY,
                stars INTEGER NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_metrics (
                metric_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> StateStore:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("StateStore is not connected. Call `await store.connect()` first.")
        return self._conn

    # ------------------------------------------------------------------
    # Record persistence & deduplication
    # ------------------------------------------------------------------
    async def upsert_record(
        self,
        record_type: str,
        dedup_key: str,
        source_url: str,
        payload: dict[str, Any],
    ) -> bool:
        """Insert or update a validated record. Returns True if newly inserted."""
        cur = await self.conn.execute(
            "SELECT 1 FROM records WHERE record_type = ? AND dedup_key = ?",
            (record_type, dedup_key),
        )
        existed = (await cur.fetchone()) is not None
        now_iso = format_utc_iso(utc_now())
        await self.conn.execute(
            """
            INSERT INTO records (record_type, dedup_key, source_url, payload_json, collected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(record_type, dedup_key) DO UPDATE SET
                source_url = excluded.source_url,
                payload_json = excluded.payload_json,
                collected_at = excluded.collected_at
            """,
            (record_type, dedup_key, source_url, json.dumps(payload, ensure_ascii=False), now_iso),
        )
        await self.conn.commit()
        return not existed

    async def upsert_records_batch(
        self,
        rows: list[tuple[str, str, str, dict[str, Any]]],
    ) -> int:
        """Batch upsert records `(record_type, dedup_key, source_url, payload)` in one transaction."""
        if not rows:
            return 0
        now_iso = format_utc_iso(utc_now())
        params = [
            (rtype, dkey, url, json.dumps(payload, ensure_ascii=False), now_iso)
            for rtype, dkey, url, payload in rows
        ]
        await self.conn.executemany(
            """
            INSERT INTO records (record_type, dedup_key, source_url, payload_json, collected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(record_type, dedup_key) DO UPDATE SET
                source_url = excluded.source_url,
                payload_json = excluded.payload_json,
                collected_at = excluded.collected_at
            """,
            params,
        )
        await self.conn.commit()
        return len(params)

    async def clear_records(self, record_type: str) -> None:
        """Remove all stored records of a given type (e.g., before refreshing 24h Phase II signals)."""
        await self.conn.execute("DELETE FROM records WHERE record_type = ?", (record_type,))
        await self.conn.commit()

    async def count_records(self, record_type: str) -> int:
        """Count distinct stored records for `record_type`."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM records WHERE record_type = ?",
            (record_type,),
        )
        row = await cur.fetchone()
        return int(row["cnt"]) if row is not None else 0

    async def get_dedup_keys(self, record_type: str) -> set[str]:
        """Return all existing dedup keys for `record_type` (for crash-resumable skipping)."""
        cur = await self.conn.execute(
            "SELECT dedup_key FROM records WHERE record_type = ?",
            (record_type,),
        )
        rows = await cur.fetchall()
        return {str(r["dedup_key"]) for r in rows}

    async def load_records(self, record_type: str) -> list[dict[str, Any]]:
        """Load all JSON payloads for `record_type`."""
        cur = await self.conn.execute(
            "SELECT payload_json FROM records WHERE record_type = ? ORDER BY collected_at DESC",
            (record_type,),
        )
        rows = await cur.fetchall()
        return [json.loads(str(r["payload_json"])) for r in rows]

    # ------------------------------------------------------------------
    # Entity Mapping Log persistence
    # ------------------------------------------------------------------
    async def save_entity_mappings(self, logs: list[EntityMappingLog]) -> int:
        """Persist entity resolution mapping events deduplicated by `(raw, canonical, type, url)`."""
        if not logs:
            return 0
        params = [
            (
                log.raw_name,
                log.canonical_name,
                log.match_method.value,
                log.confidence,
                log.record_type,
                log.source_url,
            )
            for log in logs
        ]
        await self.conn.executemany(
            """
            INSERT OR IGNORE INTO entity_mappings
                (raw_name, canonical_name, match_method, confidence, record_type, source_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        await self.conn.commit()
        return len(params)

    async def load_entity_mappings(self) -> list[dict[str, Any]]:
        """Return all stored entity resolution mapping rows."""
        cur = await self.conn.execute(
            """
            SELECT raw_name, canonical_name, match_method, confidence, record_type, source_url
            FROM entity_mappings
            ORDER BY id ASC
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Pipeline Checkpointing
    # ------------------------------------------------------------------
    async def save_checkpoint(self, pipeline_name: str, cursor: dict[str, Any]) -> None:
        """Persist pipeline progress cursor for resumability."""
        await self.conn.execute(
            """
            INSERT INTO checkpoints (pipeline_name, cursor_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pipeline_name) DO UPDATE SET
                cursor_json = excluded.cursor_json,
                updated_at = excluded.updated_at
            """,
            (pipeline_name, json.dumps(cursor), format_utc_iso(utc_now())),
        )
        await self.conn.commit()

    async def load_checkpoint(self, pipeline_name: str) -> dict[str, Any] | None:
        """Load last saved checkpoint cursor for `pipeline_name`, if present."""
        cur = await self.conn.execute(
            "SELECT cursor_json FROM checkpoints WHERE pipeline_name = ?",
            (pipeline_name,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        loaded: dict[str, Any] = json.loads(str(row["cursor_json"]))
        return loaded

    # ------------------------------------------------------------------
    # Heuristic Freshness Engine (for dateless sources)
    # ------------------------------------------------------------------
    async def has_source_baseline(self, source_name: str) -> bool:
        """Return True if an initial baseline snapshot exists for `source_name`."""
        cur = await self.conn.execute(
            "SELECT 1 FROM source_baselines WHERE source_name = ?",
            (source_name,),
        )
        return (await cur.fetchone()) is not None

    async def mark_baseline_initialized(
        self,
        source_name: str,
        *,
        reference_now: datetime | None = None,
    ) -> None:
        """Mark `source_name` baseline snapshot as initialized."""
        now_iso = format_utc_iso(reference_now or utc_now())
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO source_baselines (source_name, initialized_at)
            VALUES (?, ?)
            """,
            (source_name, now_iso),
        )
        await self.conn.commit()

    async def evaluate_heuristic_freshness(
        self,
        source_name: str,
        url: str,
        content_fingerprint_text: str,
        *,
        freshness_hours: int = 24,
        reference_now: datetime | None = None,
    ) -> tuple[bool, datetime, str]:
        """Classify a dateless signal using persistent baseline + diff tracking.

        - If `source_name` has no baseline yet: records item as baseline (`is_baseline=1`)
          and returns `(False, now, "HEURISTIC_BASELINE")`. Caller should call
          `mark_baseline_initialized(source_name)` after recording the initial batch.
        - On subsequent runs: unseen URLs are inserted as fresh (`is_baseline=0`) with
          `freshness_method = "HEURISTIC_DIFF"`. Previously seen non-baseline items remain
          fresh iff `first_seen_at` is within `freshness_hours`.
        """
        now = (reference_now or utc_now()).astimezone(UTC).replace(microsecond=0)
        now_iso = format_utc_iso(now)
        canon_url = canonicalize_url(url)
        content_hash = sha256_text(content_fingerprint_text)
        baseline_exists = await self.has_source_baseline(source_name)

        cur = await self.conn.execute(
            """
            SELECT first_seen_at, is_baseline, content_hash
            FROM seen_signals
            WHERE source_name = ? AND canonical_url = ?
            """,
            (source_name, canon_url),
        )
        existing = await cur.fetchone()

        if not baseline_exists:
            if existing is None:
                await self.conn.execute(
                    """
                    INSERT INTO seen_signals
                        (source_name, canonical_url, content_hash, first_seen_at, last_seen_at, is_baseline)
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (source_name, canon_url, content_hash, now_iso, now_iso),
                )
                await self.conn.commit()
            return False, now, "HEURISTIC_BASELINE"

        if existing is None:
            # Newly appeared signal after baseline -> Fresh!
            await self.conn.execute(
                """
                INSERT INTO seen_signals
                    (source_name, canonical_url, content_hash, first_seen_at, last_seen_at, is_baseline)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (source_name, canon_url, content_hash, now_iso, now_iso),
            )
            await self.conn.commit()
            return True, now, "HEURISTIC_DIFF"

        first_seen_str = str(existing["first_seen_at"])
        first_seen_dt = datetime.fromisoformat(first_seen_str.replace("Z", "+00:00")).astimezone(UTC)
        is_baseline = int(existing["is_baseline"]) == 1

        await self.conn.execute(
            """
            UPDATE seen_signals
            SET last_seen_at = ?, content_hash = ?
            WHERE source_name = ? AND canonical_url = ?
            """,
            (now_iso, content_hash, source_name, canon_url),
        )
        await self.conn.commit()

        if not is_baseline and first_seen_dt >= (now - timedelta(hours=freshness_hours)):
            return True, first_seen_dt, "HEURISTIC_DIFF"
        return False, first_seen_dt, "HEURISTIC_SEEN_STALE"

    # ------------------------------------------------------------------
    # GitHub Stars Cache & Run Metrics
    # ------------------------------------------------------------------
    async def get_cached_github_stars(self, repo_key: str) -> int | None:
        """Look up cached GitHub stars for `owner/repo`."""
        cur = await self.conn.execute(
            "SELECT stars FROM github_stars_cache WHERE repo_key = ?",
            (repo_key.lower(),),
        )
        row = await cur.fetchone()
        return int(row["stars"]) if row is not None else None

    async def set_cached_github_stars(self, repo_key: str, stars: int) -> None:
        """Store fetched GitHub star count for `owner/repo`."""
        await self.conn.execute(
            """
            INSERT INTO github_stars_cache (repo_key, stars, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(repo_key) DO UPDATE SET
                stars = excluded.stars,
                fetched_at = excluded.fetched_at
            """,
            (repo_key.lower(), int(stars), format_utc_iso(utc_now())),
        )
        await self.conn.commit()

    async def save_metric(self, key: str, value: Any) -> None:
        """Persist pipeline run telemetry for reporting and auditing."""
        await self.conn.execute(
            """
            INSERT INTO run_metrics (metric_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(metric_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value), format_utc_iso(utc_now())),
        )
        await self.conn.commit()

    async def load_all_metrics(self) -> dict[str, Any]:
        """Load all saved run metrics."""
        cur = await self.conn.execute("SELECT metric_key, value_json FROM run_metrics")
        rows = await cur.fetchall()
        return {str(r["metric_key"]): json.loads(str(r["value_json"])) for r in rows}
