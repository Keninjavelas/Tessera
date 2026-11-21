"""100% deterministic multi-stage Entity Resolution engine with audit logging."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rapidfuzz import fuzz, process

from tessera.schemas.entities import EntityMappingLog, MatchMethod

_DEFAULT_SEED_PATH = Path(__file__).with_name("seed_entities.json")

# Legal entity suffixes stripped only from the end of strings
_LEGAL_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:"
    r"inc\.?|incorporated|llc|l\.l\.c\.?|ltd\.?|limited|corp\.?|corporation|"
    r"pbc|p\.b\.c\.?|public\s+benefit\s+corporation|gmbh|co\.?|company|"
    r"plc|s\.a\.?|sas|b\.v\.?|pte\.?\s+ltd\.?|lp|l\.p\.?|llp|ag|s\.r\.l\.?|oy|ab"
    r"))+\s*$",
    re.IGNORECASE,
)

# Parenthetical annotations like "(YC W23)" or "(formerly Foo)"
_PAREN_NOISE_RE = re.compile(r"\s*\([^)]*\)\s*")
_URL_IN_NAME_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TRADEMARK_RE = re.compile(r"[™®©]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# Common subdomains ignored when normalizing domains
_STRIP_SUBDOMAINS: frozenset[str] = frozenset(
    {"www", "jobs", "careers", "boards", "app", "api", "docs", "blog", "cloud", "platform", "get"}
)

# Aggregator/platform hosts that do not identify a company's own corporate domain
_GENERIC_PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        "ycombinator.com",
        "huggingface.co",
        "openrouter.ai",
        "github.com",
        "greenhouse.io",
        "ashbyhq.com",
        "lever.co",
        "jobicy.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
    }
)

# Acronyms kept uppercase during fallback normalization
_KNOWN_ACRONYMS: frozenset[str] = frozenset(
    {"AI", "ML", "LLM", "NLP", "GPU", "API", "SDK", "DB", "SQL", "IO", "HQ", "3D", "VR", "AR", "XR", "OS", "UI", "UX"}
)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Result of resolving a raw organization name."""

    raw_name: str
    canonical_name: str
    match_method: MatchMethod
    confidence: float


def normalize_org_text(raw: str) -> str:
    """Stage 1 normalization: Unicode NFKC, strip URLs/trademarks/parens/legal suffixes."""
    text = unicodedata.normalize("NFKC", raw).strip()
    text = _URL_IN_NAME_RE.sub(" ", text)
    text = _TRADEMARK_RE.sub("", text)
    text = _PAREN_NOISE_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip(" ,.-_|:/")

    # Iteratively strip trailing legal suffixes without eroding core words
    prev = ""
    while text and text != prev:
        prev = text
        candidate = _LEGAL_SUFFIX_RE.sub("", text).strip(" ,.-")
        if candidate:
            text = candidate
    return text


def _lookup_key(cleaned: str) -> str:
    """Space-preserved alphanumeric lowercase key."""
    return _WHITESPACE_RE.sub(" ", _NON_ALNUM_RE.sub(" ", cleaned.lower())).strip()


def _compact_key(cleaned: str) -> str:
    """Spaceless alphanumeric lowercase key (maps 'Open AI' -> 'openai')."""
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def extract_canonical_domain(url_or_domain: str | None) -> str | None:
    """Extract normalized root domain from a URL or raw hostname."""
    if not url_or_domain:
        return None
    raw = url_or_domain.strip().lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    host = (urlparse(raw).hostname or "").strip(".")
    if not host or "." not in host:
        return None
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in _STRIP_SUBDOMAINS:
        parts.pop(0)
    return ".".join(parts)


def _smart_display_name(cleaned: str) -> str:
    """Cleanly format an unseen organization name while preserving intentional casing."""
    if not cleaned:
        return "Unknown"
    # If already contains intentional mixed case (e.g., OpenRouter, DeepSeek, iRobot), keep it
    has_upper = any(c.isupper() for c in cleaned)
    has_lower = any(c.islower() for c in cleaned)
    if has_upper and has_lower:
        return _WHITESPACE_RE.sub(" ", cleaned).strip()

    # Otherwise title-case words while preserving known acronyms
    tokens = _WHITESPACE_RE.sub(" ", cleaned).strip().split(" ")
    formatted: list[str] = []
    for tok in tokens:
        up = tok.upper()
        if up in _KNOWN_ACRONYMS:
            formatted.append(up)
        else:
            formatted.append(tok.capitalize())
    return " ".join(formatted)


class EntityResolver:
    """Deterministic 5-stage Entity Resolution engine (zero LLM calls).

    Stages:
        1. Normalization (NFKC, strip URLs/trademarks/legal suffixes)
        2. Exact & Alias lookup (confidence = 1.0)
        3. Domain matching (confidence = 0.99)
        4. Controlled fuzzy matching via rapidfuzz (threshold >= 90, disabled for len <= 4)
        5. Clean fallback normalization (NORMALIZED_NEW) + full audit logging
    """

    def __init__(
        self,
        seed_path: Path | str | None = None,
        *,
        fuzzy_threshold: float = 90.0,
        min_fuzzy_length: int = 7,
    ) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.min_fuzzy_length = min_fuzzy_length
        self._canonical_by_key: dict[str, str] = {}
        self._canonical_by_compact: dict[str, str] = {}
        self._alias_by_key: dict[str, str] = {}
        self._alias_by_compact: dict[str, str] = {}
        self._domain_to_canonical: dict[str, str] = {}
        self._canonical_domains: dict[str, set[str]] = {}
        self._fuzzy_choices: dict[str, str] = {}  # normalized candidate -> canonical
        self._logs: list[EntityMappingLog] = []
        self._seed_entities: list[dict[str, Any]] = []

        path = Path(seed_path) if seed_path is not None else _DEFAULT_SEED_PATH
        self._load_seeds(path)

    def _load_seeds(self, path: Path) -> None:
        raw_list: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        self._seed_entities = raw_list
        for entry in raw_list:
            canonical = str(entry["canonical_name"]).strip()
            norm_canon = normalize_org_text(canonical)
            k = _lookup_key(norm_canon)
            ck = _compact_key(norm_canon)
            if k:
                self._canonical_by_key[k] = canonical
                if len(ck) >= self.min_fuzzy_length:
                    self._fuzzy_choices[k] = canonical
            if ck:
                self._canonical_by_compact[ck] = canonical

            for alias in entry.get("aliases", []):
                norm_alias = normalize_org_text(str(alias))
                ak = _lookup_key(norm_alias)
                ack = _compact_key(norm_alias)
                if ak and ak not in self._canonical_by_key:
                    self._alias_by_key[ak] = canonical
                    # Only index longer, unambiguous aliases for fuzzy matching
                    if len(ack) >= max(self.min_fuzzy_length, 8):
                        self._fuzzy_choices[ak] = canonical
                if ack and ack not in self._canonical_by_compact:
                    self._alias_by_compact[ack] = canonical

            dom_set: set[str] = set()
            for dom in entry.get("domains", []):
                norm_dom = extract_canonical_domain(str(dom))
                if norm_dom:
                    self._domain_to_canonical[norm_dom] = canonical
                    dom_set.add(norm_dom)
            self._canonical_domains[canonical] = dom_set

    @property
    def seed_count(self) -> int:
        """Number of canonical AI entities loaded in the seed dictionary."""
        return len(self._seed_entities)

    @property
    def canonical_names(self) -> list[str]:
        """Return sorted list of all canonical seed entity names."""
        return sorted({str(e["canonical_name"]) for e in self._seed_entities})

    def resolve(
        self,
        raw_name: str,
        *,
        domain: str | None = None,
        record_type: str = "UNKNOWN",
        source_url: str = "https://unknown.local",
        log_event: bool = True,
    ) -> ResolutionResult:
        """Deterministically resolve a raw company name to its canonical form."""
        raw_clean = raw_name.strip() if raw_name else "Unknown"
        normalized = normalize_org_text(raw_clean)
        key = _lookup_key(normalized)
        compact = _compact_key(normalized)

        result: ResolutionResult | None = None

        # Stage 2a: Exact canonical match
        if key in self._canonical_by_key:
            result = ResolutionResult(
                raw_name=raw_clean,
                canonical_name=self._canonical_by_key[key],
                match_method=MatchMethod.EXACT,
                confidence=1.0,
            )
        elif compact and compact in self._canonical_by_compact:
            result = ResolutionResult(
                raw_name=raw_clean,
                canonical_name=self._canonical_by_compact[compact],
                match_method=MatchMethod.EXACT,
                confidence=1.0,
            )

        # Stage 2b: Alias dictionary match
        if result is None:
            if key in self._alias_by_key:
                result = ResolutionResult(
                    raw_name=raw_clean,
                    canonical_name=self._alias_by_key[key],
                    match_method=MatchMethod.ALIAS,
                    confidence=1.0,
                )
            elif compact and compact in self._alias_by_compact:
                result = ResolutionResult(
                    raw_name=raw_clean,
                    canonical_name=self._alias_by_compact[compact],
                    match_method=MatchMethod.ALIAS,
                    confidence=1.0,
                )

        # Stage 3: Domain matching
        norm_dom = extract_canonical_domain(domain) if domain else None
        if result is None and norm_dom and norm_dom in self._domain_to_canonical:
            result = ResolutionResult(
                raw_name=raw_clean,
                canonical_name=self._domain_to_canonical[norm_dom],
                match_method=MatchMethod.DOMAIN,
                confidence=0.99,
            )

        # Stage 4: Controlled Fuzzy Matching (disabled for short strings and blocked on domain contradiction)
        has_conflicting_corporate_domain = bool(
            norm_dom and norm_dom not in _GENERIC_PLATFORM_DOMAINS
        )
        if (
            result is None
            and not has_conflicting_corporate_domain
            and len(compact) >= self.min_fuzzy_length
            and self._fuzzy_choices
        ):
            choices = list(self._fuzzy_choices.keys())
            match = process.extractOne(key, choices, scorer=fuzz.token_sort_ratio)
            if match is not None:
                matched_key, score, _ = match
                len_ratio = min(len(key), len(matched_key)) / max(len(key), len(matched_key), 1)
                if score >= self.fuzzy_threshold and len_ratio >= 0.78:
                    result = ResolutionResult(
                        raw_name=raw_clean,
                        canonical_name=self._fuzzy_choices[matched_key],
                        match_method=MatchMethod.FUZZY,
                        confidence=round(float(score) / 100.0, 4),
                    )
            if result is None:
                # Secondary WRatio check with stricter threshold & length guard
                wmatch = process.extractOne(key, choices, scorer=fuzz.WRatio)
                if wmatch is not None:
                    matched_key, wscore, _ = wmatch
                    len_ratio = min(len(key), len(matched_key)) / max(len(key), len(matched_key), 1)
                    if wscore >= max(self.fuzzy_threshold + 2.0, 92.0) and len_ratio >= 0.80:
                        result = ResolutionResult(
                            raw_name=raw_clean,
                            canonical_name=self._fuzzy_choices[matched_key],
                            match_method=MatchMethod.FUZZY,
                            confidence=round(float(wscore) / 100.0, 4),
                        )

        # Stage 5: Fallback clean normalization
        if result is None:
            fallback_name = _smart_display_name(normalized or raw_clean)
            result = ResolutionResult(
                raw_name=raw_clean,
                canonical_name=fallback_name,
                match_method=MatchMethod.NORMALIZED_NEW,
                confidence=0.70,
            )

        if log_event:
            self._logs.append(
                EntityMappingLog(
                    raw_name=result.raw_name,
                    canonical_name=result.canonical_name,
                    match_method=result.match_method,
                    confidence=result.confidence,
                    record_type=record_type,
                    source_url=source_url,
                )
            )
        return result

    def pop_logs(self) -> list[EntityMappingLog]:
        """Drain and return accumulated mapping log entries."""
        logs = list(self._logs)
        self._logs.clear()
        return logs

    def peek_logs(self) -> list[EntityMappingLog]:
        """Return a copy of currently buffered mapping logs without clearing."""
        return list(self._logs)
