"""Unit tests for deterministic multi-stage Entity Resolution and mapping logs."""

from __future__ import annotations

import pytest

from tessera.resolution.resolver import EntityResolver, normalize_org_text
from tessera.schemas.entities import MatchMethod


def test_seed_dictionary_meets_minimum_size() -> None:
    resolver = EntityResolver()
    assert resolver.seed_count >= 60


@pytest.mark.parametrize(
    ("raw", "expected_clean"),
    [
        ("OpenAI, Inc.", "OpenAI"),
        ("Anthropic PBC", "Anthropic"),
        ("Mistral AI SAS", "Mistral AI"),
        ("Cohere Ltd.", "Cohere"),
        ("Stability AI Limited", "Stability AI"),
        ("Databricks® Corp.", "Databricks"),
        ("Scale AI (YC S16)", "Scale AI"),
    ],
)
def test_stage1_legal_suffix_normalization(raw: str, expected_clean: str) -> None:
    assert normalize_org_text(raw) == expected_clean


def test_stage2_exact_and_alias_matching() -> None:
    resolver = EntityResolver()

    r1 = resolver.resolve("Open AI", record_type="STARTUP", source_url="https://example.com/1")
    assert r1.canonical_name == "OpenAI"
    assert r1.match_method in (MatchMethod.EXACT, MatchMethod.ALIAS)
    assert r1.confidence == 1.0

    r2 = resolver.resolve("Anysphere, Inc.", record_type="JOB", source_url="https://example.com/2")
    assert r2.canonical_name == "Cursor"
    assert r2.match_method == MatchMethod.ALIAS
    assert r2.confidence == 1.0

    r3 = resolver.resolve("Anthropic PBC", record_type="JOB", source_url="https://example.com/3")
    assert r3.canonical_name == "Anthropic"
    assert r3.match_method == MatchMethod.EXACT


def test_stage3_domain_matching() -> None:
    resolver = EntityResolver()
    r = resolver.resolve(
        "Some Weird Legal Holding Name",
        domain="https://jobs.openai.com/careers/ml",
        record_type="JOB",
        source_url="https://jobs.openai.com/careers/ml",
    )
    assert r.canonical_name == "OpenAI"
    assert r.match_method == MatchMethod.DOMAIN
    assert r.confidence == 0.99


def test_stage4_controlled_fuzzy_and_short_acronym_protection() -> None:
    resolver = EntityResolver()

    # Typo in longer name >= 5 chars should match cleanly via FUZZY
    r_fuzzy = resolver.resolve(
        "Anthopic",  # missing 'r', 8 chars vs 9 chars
        record_type="NEWS",
        source_url="https://example.com/typo",
    )
    assert r_fuzzy.canonical_name == "Anthropic"
    assert r_fuzzy.match_method == MatchMethod.FUZZY
    assert r_fuzzy.confidence >= 0.90

    # Short <= 4 char acronyms MUST NOT trigger fuzzy false positives
    r_short = resolver.resolve("ZAI", record_type="STARTUP", source_url="https://example.com/zai")
    assert r_short.match_method == MatchMethod.NORMALIZED_NEW
    assert r_short.canonical_name == "Zai"

    # Exact short canonical entity 'xAI' still resolves via Stage 2 EXACT
    r_xai = resolver.resolve("xAI", record_type="JOB", source_url="https://x.ai/careers")
    assert r_xai.canonical_name == "xAI"
    assert r_xai.match_method == MatchMethod.EXACT

    # Domain contradiction guard blocks false-positive fuzzy merges across distinct startups
    for raw, dom in [
        ("Serra", "https://serra.io"),
        ("Pit.AI", "https://pit.ai"),
        ("Cognitio Labs", "https://cognitiolabs.com"),
        ("K-Scale Labs", "https://kscale.dev"),
    ]:
        res = resolver.resolve(raw, domain=dom, record_type="STARTUP", source_url=dom)
        assert res.match_method == MatchMethod.NORMALIZED_NEW, f"False positive on {raw} -> {res.canonical_name}"


def test_stage5_mapping_log_captures_every_event() -> None:
    resolver = EntityResolver()
    resolver.resolve("OpenAI, Inc.", record_type="STARTUP", source_url="https://a.com")
    resolver.resolve("BrandNew Quantum AI LLC", record_type="STARTUP", source_url="https://b.com")

    logs = resolver.pop_logs()
    assert len(logs) == 2
    assert logs[0].raw_name == "OpenAI, Inc."
    assert logs[0].canonical_name == "OpenAI"
    assert logs[1].raw_name == "BrandNew Quantum AI LLC"
    assert logs[1].canonical_name == "BrandNew Quantum AI"
    assert logs[1].match_method == MatchMethod.NORMALIZED_NEW
    # pop_logs drains buffer
    assert resolver.pop_logs() == []
