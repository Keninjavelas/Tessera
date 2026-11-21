"""Anti-413 HTML boilerplate stripping and semantic token-budgeted chunking."""

from __future__ import annotations

import re

import trafilatura
from bs4 import BeautifulSoup, Comment, Tag

_STRIP_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "svg",
    "nav",
    "footer",
    "aside",
    "noscript",
    "iframe",
    "form",
    "button",
    "canvas",
    "video",
    "audio",
)

_AD_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(ad|ads|advert|banner|cookie|consent|newsletter|popup|modal|sidebar|promo|social-share|related-posts|comment)(?:$|[\s_-])",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def estimate_tokens(text: str) -> int:
    """Fast conservative pre-flight token estimator (avoids external tokenizer download)."""
    if not text:
        return 0
    char_est = len(text) // 4
    word_est = int(len(text.split()) * 1.32)
    return max(char_est, word_est, 1)


def strip_html_boilerplate(html: str) -> tuple[str, dict[str, str]]:
    """Strip scripts/styles/nav/footer/ads/comments and return `(cleaned_html, metadata)`."""
    soup = BeautifulSoup(html, "lxml")

    metadata: dict[str, str] = {}
    if soup.title and soup.title.string:
        metadata["title"] = soup.title.string.strip()

    for meta_attr, key_name in (
        (("name", "description"), "description"),
        (("property", "og:title"), "og_title"),
        (("property", "og:description"), "og_description"),
        (("property", "article:published_time"), "published_time"),
    ):
        tag = soup.find("meta", attrs={meta_attr[0]: meta_attr[1]})
        if isinstance(tag, Tag):
            content_val = tag.get("content")
            if isinstance(content_val, str) and content_val.strip():
                metadata[key_name] = content_val.strip()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Remove non-content structural tags
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    # Remove likely ad / cookie / sidebar containers
    for elem in list(soup.find_all(True)):
        attrs = getattr(elem, "attrs", None)
        if not attrs:
            continue
        cls_val = attrs.get("class")
        id_val = attrs.get("id")
        combined = " ".join(
            [
                " ".join(cls_val) if isinstance(cls_val, list) else str(cls_val or ""),
                str(id_val or ""),
            ]
        )
        if combined and _AD_CLASS_RE.search(combined):
            # Guard against accidentally stripping the main document body
            if elem.name not in ("html", "body", "main", "article"):
                elem.decompose()

    return str(soup), metadata


def extract_clean_text(html_or_text: str) -> tuple[str, dict[str, str], list[str]]:
    """Extract clean prose, page metadata, and H1/H2 headers from raw HTML or plain text."""
    stripped = html_or_text.strip()
    if not stripped:
        return "", {}, []

    looks_like_html = "<html" in stripped.lower() or "</p>" in stripped.lower() or "<div" in stripped.lower() or "<body" in stripped.lower()
    if not looks_like_html:
        clean = _MULTI_NEWLINE_RE.sub("\n\n", _WHITESPACE_RE.sub(" ", stripped))
        return clean, {}, []

    cleaned_html, metadata = strip_html_boilerplate(stripped)
    soup = BeautifulSoup(cleaned_html, "lxml")

    headers: list[str] = []
    for h in soup.find_all(["h1", "h2"]):
        h_text = _WHITESPACE_RE.sub(" ", h.get_text(" ", strip=True)).strip()
        if h_text and len(h_text) <= 220 and h_text not in headers:
            headers.append(h_text)

    # Prefer trafilatura for high-density main-article extraction
    extracted = trafilatura.extract(
        cleaned_html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )
    if not extracted or len(extracted.strip()) < 120:
        # BeautifulSoup structured paragraph fallback
        blocks: list[str] = []
        for elem in soup.find_all(["h1", "h2", "h3", "p", "li", "td"]):
            txt = _WHITESPACE_RE.sub(" ", elem.get_text(" ", strip=True)).strip()
            if len(txt) >= 20:
                blocks.append(txt)
        extracted = "\n\n".join(blocks) if blocks else soup.get_text("\n", strip=True)

    clean = _MULTI_NEWLINE_RE.sub("\n\n", extracted).strip()
    return clean, metadata, headers


def prepare_llm_context(
    html_or_text: str,
    *,
    max_tokens: int = 6000,
    focus_keywords: tuple[str, ...] = (),
) -> str:
    """Pre-flight anti-413 content compressor using semantic section prioritization.

    Retains title, metadata block, H1/H2 headings, opening paragraphs, and keyword-dense
    segments rather than blind character slicing whenever content exceeds `max_tokens`.
    """
    clean_text, metadata, headers = extract_clean_text(html_or_text)
    if not clean_text and not metadata:
        return ""

    prefix_parts: list[str] = []
    if metadata:
        meta_lines = [f"{k}: {v}" for k, v in metadata.items()]
        prefix_parts.append("[METADATA]\n" + "\n".join(meta_lines))
    if headers:
        prefix_parts.append("[HEADINGS]\n" + "\n".join(f"- {h}" for h in headers[:12]))

    prefix = "\n\n".join(prefix_parts)
    full_doc = f"{prefix}\n\n[CONTENT]\n{clean_text}" if prefix else clean_text

    if estimate_tokens(full_doc) <= max_tokens:
        return full_doc

    # Over budget -> apply semantic prioritization across paragraphs
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", clean_text) if p.strip()]
    if len(paragraphs) <= 1:
        # Split long single-block text by sentences
        paragraphs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean_text) if s.strip()]

    budget_remaining = max_tokens - estimate_tokens(prefix) - 40
    if budget_remaining <= 100:
        # Extreme budget constraint: trim prefix + keep opening snippet
        char_cap = max(200, max_tokens * 3)
        return full_doc[:char_cap]

    lower_kw = tuple(k.lower() for k in focus_keywords)
    scored: list[tuple[float, int, str]] = []
    for idx, para in enumerate(paragraphs):
        score = 0.0
        # Strong priority for opening context
        if idx < 3:
            score += 5.0 - idx
        elif idx < 6:
            score += 1.5
        # Keyword density boost
        if lower_kw:
            p_low = para.lower()
            hits = sum(1 for kw in lower_kw if kw in p_low)
            score += hits * 3.0
        # Slight boost for numeric/date/currency facts
        if re.search(r"(\$\d|\d+%|\b202\d\b|remote|hybrid|free|enterprise|pricing)", para, re.I):
            score += 1.0
        scored.append((score, idx, para))

    # Select highest-scoring paragraphs up to token budget, then restore reading order
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[int, str]] = []
    used_tokens = 0

    for _score, idx, para in scored:
        ptok = estimate_tokens(para)
        if used_tokens + ptok <= budget_remaining:
            selected.append((idx, para))
            used_tokens += ptok
        elif not selected and budget_remaining > 50:
            # Ensure at least one truncated paragraph fits even if a single paragraph is huge
            char_limit = max(120, budget_remaining * 3)
            selected.append((idx, para[:char_limit]))
            break

    selected.sort(key=lambda item: item[0])
    body = "\n\n".join(p for _, p in selected)
    return f"{prefix}\n\n[CONTENT]\n{body}" if prefix else body
